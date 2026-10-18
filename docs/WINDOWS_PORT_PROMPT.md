# Miyamizu(音質=時間軸の正確さ優先プレイヤー) — 仕様プロンプト(Windows 向け)

以下を他の Claude(Claude Code 等)に渡すと、同じ思想のプレイヤーを Windows 向けに作らせられます。
Linux 版(ALSA/PipeWire)の構成を土台に、OS 依存部だけを Windows 用に置き換える前提です。

---

## 依頼

Windows 10/11 向けの音楽プレイヤー「Miyamizu」を Python 3.12 + PyQt6 で作ってください。
**設計理念: データ伝送のジッターを最小化し、音を時間的にできるだけ正しく出力する(JPLAY 的な専用コア思想)。**
DAC へ USB で送る構成(PC → USB → DDC → 光/同軸 → DAC)を想定し、**WASAPI 排他モード**でビットパーフェクト出力できることを最優先にします。

### 構成(Linux 版と同じ 3 層)
1. GUI プロセス(PyQt6)
2. エンジンプロセス: 制御スレッド + **リアルタイム出力スレッド**(出力はここだけが担当。GIL に邪魔されないよう重い処理を持たせない)
3. 準備プロセス(multiprocessing, spawn): デコード/リサンプル/量子化を行い、**共有メモリのリングバッファ(SPSC)** で出力スレッドへ渡す

GUI ⇄ エンジンは JSON 行(ローカル TCP 127.0.0.1 か名前付きパイプ)。GUI を閉じるとエンジンも終了。凍結版は同じ exe を `--engine` で再起動(`multiprocessing.freeze_support()` 必須)。

### 機能
- 再生: flac(+cue) 中心、wav/mp3/m4a/aac/ALAC。CUE+FLAC は CUE 1 枚を「フォルダ(アルバム)」として扱い、元の巨大ファイルは一覧に出さない
- ライブラリ: ディレクトリ管理(既定ディレクトリ)、SQLite、ツリー/曲リスト/アルバムアート背景/再生バーをドックで自由配置
- 取り込む拡張子の選択と「無視する拡張子」(無視が優先)
- リピート(1曲/フォルダ/全曲)、シャッフル(フォルダ/全曲)、YouTube 風プレイリスト(D&D/右クリック、M3U8 入出力)、再生キュー表示
- 次曲先読み(既定ON・ギャップレス)、RAM 再生(オプション。デコード済みを RAM に保持)
- リサンプル(オプション、soxr。品質/位相 linear・intermediate・minimum/帯域、モード: 系列内最大・×2・×4・固定レート)。**既定は OFF = 素通し**
- 出力: WASAPI 排他(既定の目標)と共有、出力デバイス選択、レート上限、ビットパーフェクト判定表示(処理の列挙とバッジ「✔ビットパーフェクト / 加工あり(排他出力) / 加工あり(共有出力)」)
- 時間軸: 出力スレッドを MMCSS(`AvSetMmThreadCharacteristicsW("Pro Audio")` + `AvSetMmThreadPriority`)と `SetThreadPriority(TIME_CRITICAL)` で優先化、専用 CPU コアへ `SetThreadAffinityMask`(SMT 兄弟も他処理から除外)、`timeBeginPeriod(1)`、周期ジッターのヒストグラムと xrun を計測して表示
- **アプリ音声の取り込み(排他出力専用)**: 他アプリの音を取り込み、プレイヤーの排他出力でのみ再生。取り込み中は RAM 再生/リサンプルを強制無効。クロック差はリング充填量を見て 1 フレーム単位で重複/脱落させて吸収
- アイコン: 実行ファイルの隣の `icon.png` を最優先で読み込む(無ければ同梱)
- PyInstaller(onedir, console=False)でビルドし、`build.bat` で venv 作成→不足パッケージのみ導入→ビルド。起動用 `start.bat` も用意(venv 作成/有効化/requirements 充足確認/起動)
- 設定は `%APPDATA%\Miyamizu\settings.json`、ライブラリ DB は `%LOCALAPPDATA%\Miyamizu`

### OS 依存部の置換え(Linux 版 → Windows)
| 役割 | Linux 版 | Windows 版 |
|---|---|---|
| 出力 | ALSA hw 直叩き(ctypes) / PipeWire | WASAPI 排他(イベント駆動、`IAudioClient3`/`IAudioClient`)。Python からは `sounddevice`(PortAudio の WasapiSettings(exclusive=True))か、ctypes/comtypes で WASAPI を直接叩く。**排他で実際に開けた形式(整数 24/32bit)を確認し、フォーマット変換を最小にする**。ASIO は任意の拡張 |
| 他アプリの排他奪取 | pactl でシンク suspend/プロファイル off | 不要(WASAPI 排他を取ると共有側は止まる。取れない時は「他アプリが排他中」と表示) |
| デバイス列挙 | /proc/asound | `IMMDeviceEnumerator`(`pycaw` か `sounddevice.query_devices`) |
| リアルタイム化 | SCHED_FIFO / rtkit | MMCSS + TIME_CRITICAL。電源プランは「高パフォーマンス」を推奨と案内 |
| アプリ音声取り込み | pactl で null シンクへ移動 → parec | **WASAPI プロセス別ループバック**(Windows 10 2004 以降、`ActivateAudioInterfaceAsync` + `VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK`、対象 PID を指定)。取り込んだアプリの元の出力は止められない点に注意(仮想ケーブル `VB-CABLE` 経由でアプリの出力先を切り替える方式も案内する) |
| パス | XDG | `%APPDATA%` / `%LOCALAPPDATA%` |
| IPC | UNIX ソケット | 127.0.0.1 の TCP(認証用ランダムトークン)または名前付きパイプ |

### 品質基準
- 出力スレッドは割り当て・ログ・GIL を要する処理をしない(事前確保したバッファの memcpy + API 呼び出しのみ)
- ビットパーフェクト条件(素通し・音量 0dB 固定・リサンプルなし・ビット深度を落とす時だけ TPDF ディザ)を満たす時だけバッジを出す
- テスト: ヌル出力/キャプチャ出力のテストバックエンドで、素通しの完全一致、シーク、ギャップレス、リサンプルのピーク周波数、取り込みの連続性を自動検証する
- 実機でしか確認できない点(WASAPI 排他の取得、ループバック)は README に確認手順を書く

### 配布時の注意
- PyQt6 は GPL v3/商用ライセンス、soxr(LGPL)、PyAV/FFmpeg(LGPL/GPL 構成に注意)、libsndfile(LGPL)。配布形態に合うライセンス表記を同梱すること
- コード署名が無いと SmartScreen の警告が出る

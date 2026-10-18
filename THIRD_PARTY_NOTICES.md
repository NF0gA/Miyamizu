# 第三者ソフトウェアのライセンス

Miyamizu 本体は GPL-3.0-or-later(`LICENSE`)です。以下のソフトウェアを利用・同梱しています。各ライセンスの全文は、それぞれの配布物(Python パッケージの `dist-info/licenses` など)に含まれます。

| 名称 | 用途 | ライセンス |
|---|---|---|
| PyQt6 (Riverbank Computing) / Qt 6 | GUI | GPL-3.0(商用ライセンスとのデュアル)/ Qt は LGPL-3.0・GPL-3.0 |
| NumPy | 数値処理 | BSD-3-Clause ほか |
| python-soundfile / libsndfile | WAV・FLAC などの読み込み | BSD-3-Clause / LGPL-2.1+ |
| python-soxr / libsoxr | リサンプル(soxr エンジン) | LGPL-2.1+ |
| mutagen | タグ読み取り | GPL-2.0-or-later |
| PyAV / FFmpeg | MP3・AAC・ALAC などのデコード | BSD-3-Clause / FFmpeg(PyAV の配布物は x264・x265 を含む GPL 構成) |
| charset-normalizer | CUE の文字コード判定 | MIT |
| r8brain-free-src 7.6 | リサンプル(r8brain エンジン) | MIT — `miyamizu/native/R8BRAIN_LICENSE.txt` |
| PyInstaller(ビルド時のみ) | 実行ファイルの作成 | GPL-2.0+(ブートローダは例外条項付き。生成物は制限されません) |

- Sample rate converter designed by Aleksey Vaneev of Voxengo.
- アイコン画像は ChatGPT(OpenAI)で生成したものを利用しています。
- 同梱物のソースコード: Miyamizu 本体と r8brain は本リポジトリに含まれます。その他は上記の各プロジェクトから入手できます。

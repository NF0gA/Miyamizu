"""設定(GUI/エンジン共通)。JSON で ~/.config/miyamizu/settings.json に保存する。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from .common import config_dir


# 出力プロファイルに含める項目(切り替えで一緒に変わる設定)
PROFILE_KEYS = ("backend", "alsa_device", "custom_device", "rate_cap", "s32_valid_bits", "period_ms", "periods",
                "resample_enabled", "resample_mode", "fixed_rate", "resample_quality", "resample_phase", "resample_band",
                "resample_engine", "r8_preset", "r8_trans_band", "r8_atten_db", "r8_phase", "r8_headroom_db",
                "ram_playback", "noise_shaping", "dither", "quiet_mode")


@dataclass
class Settings:
    # ---- ライブラリ ----
    library_dir: str = ""                 # デフォルトディレクトリ
    enabled_exts: list = field(default_factory=list)   # 取り込む拡張子(空=すべて対応形式)
    ignore_exts: list = field(default_factory=list)    # 無視する拡張子(例: .wv)。enabled より優先
    show_cue_source: bool = False         # CUE の元ファイル(巨大FLAC等)を一覧に出す
    cue_skip_pregap: bool = False         # True: 次トラックの INDEX 00 で切る(プリギャップ非再生)

    # ---- 出力 ----
    backend: str = "pipewire"             # pipewire | alsa_hw | custom
    alsa_device: str = ""                 # alsa_hw 時のデバイス (例: hw:CARD=X8,DEV=0)
    custom_device: str = ""               # custom 時の ALSA デバイス文字列
    rate_cap: int = 192000                # 出力レート上限 (D03J+ 経路は 192000)
    s32_valid_bits: int = 24              # S32_LE コンテナの有効ビット数
    debug_enabled: bool = False           # デバッグ機能(メニューの「デバッグ」)を表示する
    noise_shaping: str = "off"            # 量子化ノイズの整形: off | first | lipshitz | f_weighted | improved_e(ビット深度を落とす時だけ)
    dither: bool = True                   # ビット深度を落とす時だけ適用 (TPDF)
    period_ms: float = 20.0
    periods: int = 6

    # ---- リサンプリング (既定OFF) ----
    resample_enabled: bool = False
    resample_mode: str = "family_max"     # family_max | x2 | x4 | fixed
    fixed_rate: int = 192000
    resample_quality: str = "VHQ"         # QQ|LQ|MQ|HQ|VHQ|32 (32=最高精度)
    resample_phase: str = "linear"        # linear | intermediate | minimum   (libsoxr が必要。無ければ linear)
    resample_band: str = "standard"       # standard(通過域 約91%) | wide(約98%)   (libsoxr が必要)
    resample_engine: str = "soxr"         # soxr | r8brain (r8brain-free-src。無ければ soxr)
    r8_preset: str = "standard"           # standard(2%/207dB/線形) | minphase(2%/207dB/最小位相) | fast(10%/120dB) | custom
    r8_trans_band: float = 2.0            # custom: 遷移帯域 % (0.5〜45)
    r8_atten_db: float = 206.91           # custom: 阻止域減衰 dB (49〜218)
    r8_phase: str = "linear"              # custom: linear | minimum
    r8_headroom_db: float = 0.0           # リサンプル前に下げる量(dB, 0以下)。変換後のオーバーシュートによる歪みを避ける

    # ---- RAM 再生 (既定OFF) / 先読み (既定ON) ----
    ram_playback: bool = False
    ram_max_mb: int = 4096
    prefetch: bool = True
    prerender_ahead: int = 0              # 実験: 次曲より先の曲も事前に変換して RAM に置く曲数(0=次曲だけ)。RAM再生ON時のみ
    prerender_total_mb: int = 4096        # 事前変換した曲の合計上限
    capture_rate: int = 48000             # アプリ音声の取り込みレート(出力と同じ=リサンプルなし。デバイス対応レートに)
    capture_latency_ms: float = 40.0      # 取り込みバッファの目標(大きいほど安定、遅延は増える)
    capture_sync: str = "ratio"           # クロック差の吸収: "ratio"=連続補正(数ppmの微小リサンプル) / "slip"=1フレームの間引き・複製
    capture_rate_auto: bool = True        # 取り込みレートをアプリのストリームのレートに合わせる(DACが非対応なら上の固定値)
    capture_backend: str = "parec"        # 取り込み方法: "parec"(Pulse互換) / "pw-cat"(PipeWire直接・実験。届かなければ自動で parec へ)
    capture_latency_auto: bool = True     # 取り込みの遅延目標を、音切れしない範囲で自動調整する
    capture_latency_min_ms: float = 20.0
    capture_latency_max_ms: float = 200.0
    capture_period_ms: float = 10.0       # アプリ音声再生時の出力周期(通常再生より短くして遅延を減らす)
    capture_periods: int = 4              # 同: 周期数(出力側バッファ = 周期 × 周期数)
    stream_buffer_s: float = 1.0          # 出力リングバッファ長(RAM再生OFF時・アプリ音声入力時)

    # ---- 静音モード: 再生中の裏方の動き(状態通知・メトリクス・準備プロセスの待機)を減らし、再生スレッドへの干渉を抑える ----
    quiet_mode: bool = False
    quiet_status_s: float = 1.0           # 再生位置の通知間隔(通常 0.1 秒)
    quiet_metrics_s: float = 5.0          # メトリクスの通知間隔(通常 1 秒)
    quiet_idle_ms: float = 50.0           # 準備プロセスが暇な時の待機(通常 10ms)

    # ---- 時間軸の正確さ(JPLAY 的な専用コア) ----
    dedicated_cpu: int = -1               # -1: 使わない
    reserve_sibling: bool = True          # 専用コアの SMT 兄弟も他の処理から空ける
    rt_priority: int = 10                 # 0: 無効 (SCHED_FIFO、権限が無ければ rtkit を試す)
    confine_gui: bool = True              # GUI/生成プロセスを専用コアから外す

    # ---- デバイス運用 ----
    idle_close_s: float = 30.0            # 停止後、無音を流し続けてからデバイスを閉じるまで
    busy_retry_s: float = 8.0             # デバイス使用中のとき待つ秒数
    release_pipewire: bool = True         # alsa_hw: 開く前に PipeWire のシンクを suspend
    release_profile_off: bool = True      # suspend しても使用中なら、カードのプロファイルを off にして解放(終了/切替時に復元)
    claim_on_switch: bool = True          # 排他へ切替えた時、メイン機器を OS(共有)側から外して専有する(共有へ戻すと自動で戻す)
    default_on_shared: bool = True        # 共有へ切替えた/メイン機器を変えた時、メイン機器を PipeWire の既定出力にする
    lead_in_ms: float = 300.0             # デバイスを開き直した直後に流す無音(DDC/DAC のロック待ち。曲頭欠け防止)
    pw_force_rate: bool = False           # pipewire: セッション中 clock.force-rate をかける

    # ---- 出力プロファイル(名前 → 出力関連設定の一部) ----
    output_profiles: dict = field(default_factory=dict)

    # ---- 再生 ----
    volume: float = 1.0                   # ビットパーフェクト時は無視(0dB固定)
    repeat: str = "off"                   # off | one | folder | all
    shuffle: str = "off"                  # off | folder | all

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Settings":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def bitperfect_requested(self) -> bool:
        return self.backend == "alsa_hw"


def settings_path():
    return config_dir() / "settings.json"


def load_settings() -> Settings:
    p = settings_path()
    try:
        return Settings.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return Settings()


def save_settings(s: Settings) -> None:
    p = settings_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)

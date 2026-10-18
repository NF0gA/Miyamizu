"""設定ダイアログ。"""
from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox,
                             QTabWidget, QVBoxLayout, QWidget)

from ..common import AUDIO_EXTS, IS_WIN
from ..settings import Settings

RATES = [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000]


class SettingsDialog(QDialog):
    probe_requested = pyqtSignal(str)

    def __init__(self, settings: Settings, hello: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.resize(640, 560)
        self.s = replace(settings)
        self.hello = hello or {}
        tabs = QTabWidget()
        tabs.addTab(self._tab_output(), "出力")
        tabs.addTab(self._tab_resample(), "リサンプル")
        tabs.addTab(self._tab_playback(), "再生")
        tabs.addTab(self._tab_timing(), "時間軸(専用コア)")
        tabs.addTab(self._tab_library(), "ライブラリ")
        tabs.addTab(self._tab_other(), "その他")
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(bb)

    # ---- 出力 ----
    def _tab_output(self):
        w = QWidget()
        f = QFormLayout(w)
        self.backend = QComboBox()
        if IS_WIN:
            self.backend.addItem("WASAPI 共有(既定・他アプリと共存)", "pipewire")
            self.backend.addItem("ビットパーフェクト(WASAPI 排他)", "alsa_hw")
        else:
            self.backend.addItem("PipeWire 共有(既定・他アプリと共存)", "pipewire")
            self.backend.addItem("ビットパーフェクト(ALSA hw 直結・排他)", "alsa_hw")
            self.backend.addItem("カスタム ALSA デバイス名", "custom")
        self.backend.setCurrentIndex(max(0, self.backend.findData(self.s.backend)))
        f.addRow("出力方式", self.backend)
        self.device = QComboBox()
        for d in self.hello.get("devices", []):
            self.device.addItem(d["label"], d["name"])
        i = self.device.findData(self.s.alsa_device)
        self.device.setCurrentIndex(max(0, i))
        probe = QPushButton("このデバイスを調べる")
        probe.clicked.connect(lambda: self.probe_requested.emit(self.device.currentData() or ""))
        row = QHBoxLayout()
        row.addWidget(self.device, 1)
        row.addWidget(probe)
        f.addRow("メイン機器(共有・排他で共通)", row)
        self.claim = QCheckBox("排他に切替えたら、メイン機器を OS の共有側から外して専有する(共有へ戻すと自動で戻す)")
        self.claim.setChecked(self.s.claim_on_switch)
        f.addRow("", self.claim)
        self.defsink = QCheckBox("共有に切替えたら/メイン機器を変えたら、メイン機器を OS の既定の出力にする(システムのミキサー操作が不要)")
        self.defsink.setChecked(self.s.default_on_shared)
        f.addRow("", self.defsink)
        self.caps = QPlainTextEdit()
        self.caps.setReadOnly(True)
        self.caps.setMaximumHeight(120)
        self.caps.setPlaceholderText("「このデバイスを調べる」で対応レート/形式を表示(他が使用中だと失敗します)")
        f.addRow("", self.caps)
        self.custom = QLineEdit(self.s.custom_device)
        self.custom.setPlaceholderText("例: plughw:CARD=X8,DEV=0 / default")
        f.addRow("カスタムデバイス名", self.custom)
        if IS_WIN:
            self.custom.setVisible(False)
            f.labelForField(self.custom).setVisible(False)
        self.cap = QComboBox()
        for r in RATES:
            self.cap.addItem(f"{r / 1000:g} kHz", r)
        self.cap.setCurrentIndex(max(0, self.cap.findData(self.s.rate_cap)))
        self.cap.setToolTip("経路の上限。FX-D03J+ 経由なら 192kHz。FX-D03J(無印)なら 96kHz。")
        f.addRow("出力レート上限", self.cap)
        self.s32 = QComboBox()
        self.s32.addItem("24 bit(推奨)", 24)
        self.s32.addItem("32 bit", 32)
        self.s32.setCurrentIndex(max(0, self.s32.findData(self.s.s32_valid_bits)))
        f.addRow("S32_LE 時の有効ビット", self.s32)
        self.dither = QCheckBox("ビット深度を落とす場合だけ TPDF ディザを使う")
        self.dither.setChecked(self.s.dither)
        f.addRow("", self.dither)
        self.nshape = QComboBox()
        self.nshape.addItem("オフ(TPDFディザのみ)", "off")
        from ..nshape import FILTERS, available
        for k, (desc, _c) in FILTERS.items():
            self.nshape.addItem(desc, k)
        self.nshape.setCurrentIndex(max(0, self.nshape.findData(self.s.noise_shaping)))
        tip = ("ビット深度を落とす時(32bit/浮動小数→24/16bit、リサンプル後など)の量子化ノイズを、聞こえにくい高域へ寄せます。\n"
               "係数は 44.1/48kHz 向け。24bit 出力では差はほぼ聞こえず、16bit 出力で効果があります。ビットパーフェクトの素通しには影響しません。")
        if not available():
            tip += "\n※ このPCでは C ライブラリを用意できないため使えません(cc/gcc を入れると自動でビルドします)。"
            self.nshape.setEnabled(False)
        self.nshape.setToolTip(tip)
        f.addRow("ノイズシェーピング(実験)", self.nshape)
        self.period = QDoubleSpinBox()
        self.period.setRange(2.0, 200.0)
        self.period.setSuffix(" ms")
        self.period.setValue(self.s.period_ms)
        self.period.setToolTip("大きいほど起床回数が減る。小さいほど停止/シークの反応が速い。")
        f.addRow("ピリオド長", self.period)
        self.periods = QSpinBox()
        self.periods.setRange(2, 32)
        self.periods.setValue(self.s.periods)
        f.addRow("デバイスバッファ(ピリオド数)", self.periods)
        self.release_pw = QCheckBox("ビットパーフェクト開始前に PipeWire のシンクを一時停止して解放する")
        self.release_pw.setChecked(self.s.release_pipewire)
        f.addRow("", self.release_pw)
        self.pw_force = QCheckBox("PipeWire 共有時、再生中だけグラフのレートを曲に合わせる(clock.force-rate)")
        self.pw_force.setChecked(self.s.pw_force_rate)
        f.addRow("", self.pw_force)
        self.rel_off = QCheckBox("それでも使用中(busy)なら、カードのプロファイルを一時的に off にして解放する(終了時/共有へ戻す時に復元)")
        self.rel_off.setChecked(self.s.release_profile_off)
        f.addRow("", self.rel_off)
        if IS_WIN:      # PipeWire/ALSA 専用の項目は使わない
            for wdg in (self.release_pw, self.pw_force, self.rel_off):
                wdg.setVisible(False)
        self.lead = QDoubleSpinBox()
        self.lead.setRange(0, 3000)
        self.lead.setSuffix(" ms")
        self.lead.setValue(self.s.lead_in_ms)
        self.lead.setToolTip("デバイスを開き直した直後(曲間でレートが変わる時など)に先に流す無音。DDC/DAC のロックを待って曲頭が欠けるのを防ぐ。0 で無効")
        f.addRow("再オープン直後の無音", self.lead)
        self.idle = QDoubleSpinBox()
        self.idle.setRange(0, 3600)
        self.idle.setSuffix(" 秒")
        self.idle.setValue(self.s.idle_close_s)
        self.idle.setToolTip("停止後、この時間だけ無音を送り続けてデバイスを閉じる(DAC の再ロック音対策)。0 で即閉じる")
        f.addRow("停止後に無音を流す時間", self.idle)
        return w

    # ---- リサンプル ----
    def _tab_resample(self):
        w = QWidget()
        w.setStyleSheet("QWidget:disabled { color: #777; }")
        f = QFormLayout(w)
        self.rs_on = QCheckBox("リサンプリング/オーバーサンプリングを有効にする(既定OFF)")
        self.rs_on.setChecked(self.s.resample_enabled)
        f.addRow(self.rs_on)
        self.rs_mode = QComboBox()
        self.rs_mode.addItem("同一系列の最大(44.1k系→176.4k、48k系→192k など。上限内)", "family_max")
        self.rs_mode.addItem("2倍", "x2")
        self.rs_mode.addItem("4倍", "x4")
        self.rs_mode.addItem("固定レート(全曲を同じレートに揃える)", "fixed")
        self.rs_mode.setCurrentIndex(max(0, self.rs_mode.findData(self.s.resample_mode)))
        f.addRow("モード", self.rs_mode)
        self.rs_fixed = QComboBox()
        for r in RATES:
            self.rs_fixed.addItem(f"{r / 1000:g} kHz", r)
        self.rs_fixed.setCurrentIndex(max(0, self.rs_fixed.findData(self.s.fixed_rate)))
        f.addRow("固定レート", self.rs_fixed)
        self.have_soxr = bool(self.hello.get("libsoxr", True))
        self.have_r8 = bool(self.hello.get("r8brain", False))
        self.rs_engine = QComboBox()
        self.rs_engine.addItem("soxr(libsoxr)", "soxr")
        self.rs_engine.addItem("r8brain(r8brain-free-src / Voxengo)" + ("" if self.have_r8 else " — 利用不可"), "r8brain")
        self.rs_engine.setCurrentIndex(max(0, self.rs_engine.findData(self.s.resample_engine)))
        f.addRow("エンジン", self.rs_engine)
        self.r8_box = QWidget()
        rf = QFormLayout(self.r8_box)
        rf.setContentsMargins(0, 0, 0, 0)
        self.r8_preset = QComboBox()
        for k, t in [("standard", "標準(遷移帯域2% / 207dB / 線形位相)"), ("minphase", "最小位相(2% / 207dB / プリリンギング無し)"),
                     ("fast", "高速(10% / 120dB / 線形位相・軽量)"), ("custom", "詳細(手動)")]:
            self.r8_preset.addItem(t, k)
        self.r8_preset.setCurrentIndex(max(0, self.r8_preset.findData(self.s.r8_preset)))
        rf.addRow("プリセット", self.r8_preset)
        self.r8_tb = QDoubleSpinBox()
        self.r8_tb.setRange(0.5, 45.0)
        self.r8_tb.setDecimals(1)
        self.r8_tb.setSuffix(" %")
        self.r8_tb.setValue(self.s.r8_trans_band)
        rf.addRow("遷移帯域", self.r8_tb)
        self.r8_att = QDoubleSpinBox()
        self.r8_att.setRange(49.0, 218.0)
        self.r8_att.setDecimals(1)
        self.r8_att.setSuffix(" dB")
        self.r8_att.setValue(self.s.r8_atten_db)
        rf.addRow("阻止域減衰", self.r8_att)
        self.r8_ph = QComboBox()
        self.r8_ph.addItem("線形位相", "linear")
        self.r8_ph.addItem("最小位相", "minimum")
        self.r8_ph.setCurrentIndex(max(0, self.r8_ph.findData(self.s.r8_phase)))
        rf.addRow("位相", self.r8_ph)
        self.r8_head = QDoubleSpinBox()
        self.r8_head.setRange(-12.0, 0.0)
        self.r8_head.setDecimals(1)
        self.r8_head.setSingleStep(0.5)
        self.r8_head.setSuffix(" dB")
        self.r8_head.setValue(self.s.r8_headroom_db)
        self.r8_head.setToolTip("変換後のインターサンプルピークが0dBFSを超えると歪む(クリップ)ため、変換前に少し下げる。0=下げない")
        rf.addRow("ヘッドルーム", self.r8_head)
        f.addRow(self.r8_box)
        r8note = QLabel("r8brain は遷移帯域・阻止域減衰・位相を指定する高精度リサンプラです。事前変換(RAM再生)でも同じ設定を使います。\n"
                        "ライブラリが無い場合は自動で soxr に戻ります。リサンプルしない曲(同レート)は変換せずビットパーフェクトのままです。")
        r8note.setWordWrap(True)
        f.addRow(r8note)
        self.rs_q = QComboBox()
        for q, t in [("32", "32bit精度(最高・重い)"), ("VHQ", "VHQ(28bit・標準の最高品質)"), ("HQ", "HQ(20bit)"), ("MQ", "MQ"),
                     ("LQ", "LQ"), ("QQ", "QQ(最速)")]:
            self.rs_q.addItem(t, q)
        self.rs_q.setCurrentIndex(max(0, self.rs_q.findData(self.s.resample_quality)))
        f.addRow("soxr 品質", self.rs_q)
        self.rs_phase = QComboBox()
        self.rs_phase.addItem("線形位相(既定・波形が対称。プリリンギングあり)", "linear")
        self.rs_phase.addItem("中間位相(プリリンギングほぼ無し・位相ずれ小)", "intermediate")
        self.rs_phase.addItem("最小位相(プリリンギング無し・位相が周波数で変化)", "minimum")
        self.rs_phase.setCurrentIndex(max(0, self.rs_phase.findData(self.s.resample_phase)))
        f.addRow("フィルタの位相特性", self.rs_phase)
        self.rs_band = QComboBox()
        self.rs_band.addItem("標準(通過域 約91%: 44.1kHz 源で約20.5kHz まで平坦)", "standard")
        self.rs_band.addItem("広帯域(約98%: 約21.7kHz まで平坦・遷移域が急峻)", "wide")
        self.rs_band.setCurrentIndex(max(0, self.rs_band.findData(self.s.resample_band)))
        f.addRow("通過帯域", self.rs_band)
        if not self.have_soxr:
            for wdg in (self.rs_phase, self.rs_band):
                wdg.setEnabled(False)
            warn = QLabel("⚠ libsoxr が見つからないため、位相・帯域・32bit精度は選べません(python-soxr の標準設定になります)。\n"
                          + ("位相などを選びたい場合は、エンジンに r8brain を選んでください。" if IS_WIN else
                             "sudo zypper in libsoxr0 を実行してから再起動すると選べます。"))
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#c9a85a;")
            f.addRow(warn)
        def _en(wdg, on: bool):
            """入力欄とその行ラベルをまとめて有効/無効にする(無効は灰色で見分けられる)。"""
            wdg.setEnabled(on)
            lab = f.labelForField(wdg) if wdg.parentWidget() is w else None
            if lab is not None:
                lab.setEnabled(on)

        def _sync_engine(*_):
            on = self.rs_on.isChecked()
            r8 = self.rs_engine.currentData() == "r8brain" and self.have_r8
            cust = self.r8_preset.currentData() == "custom"
            if not self.have_r8:
                self.rs_engine.model().item(1).setEnabled(False)
                if self.rs_engine.currentData() == "r8brain":
                    self.rs_engine.setCurrentIndex(0)
            _en(self.rs_mode, on)
            _en(self.rs_fixed, on and self.rs_mode.currentData() == "fixed")
            _en(self.rs_engine, on)
            _en(self.rs_q, on and not r8)
            _en(self.rs_phase, on and not r8 and self.have_soxr)
            _en(self.rs_band, on and not r8 and self.have_soxr)
            self.r8_box.setVisible(r8)
            self.r8_box.setEnabled(on)
            for wdg in (self.r8_tb, self.r8_att, self.r8_ph):
                wdg.setEnabled(on and cust)
                lab = rf.labelForField(wdg)
                if lab is not None:
                    lab.setEnabled(on and cust)
        self.rs_engine.currentIndexChanged.connect(_sync_engine)
        self.r8_preset.currentIndexChanged.connect(_sync_engine)
        self.rs_on.toggled.connect(_sync_engine)
        self.rs_mode.currentIndexChanged.connect(_sync_engine)
        _sync_engine()
        note = QLabel("固定レートにすると曲間でレートが変わらず、DAC の再同期(無音/ポップ)とデバイス再オープンが起きません。\n"
                      "ソースが出力上限を超える場合は、設定に関係なく上限系列へダウンサンプルします。")
        note.setWordWrap(True)
        f.addRow(note)
        return w

    # ---- 再生 ----
    def _tab_playback(self):
        w = QWidget()
        f = QFormLayout(w)
        self.ram = QCheckBox("RAM 再生(曲を選ぶとデコード→(リサンプル)→RAM に全て置いてから再生・既定OFF)")
        self.ram.setChecked(self.s.ram_playback)
        f.addRow(self.ram)
        self.ram_mb = QSpinBox()
        self.ram_mb.setRange(64, 65536)
        self.ram_mb.setSuffix(" MB")
        self.ram_mb.setValue(self.s.ram_max_mb)
        f.addRow("1曲あたりの上限", self.ram_mb)
        self.prefetch = QCheckBox("次曲を先読みする(ギャップレス・既定ON)")
        self.prefetch.setChecked(self.s.prefetch)
        f.addRow(self.prefetch)
        self.pre_n = QSpinBox()
        self.pre_n.setRange(0, 20)
        self.pre_n.setSpecialValueText("0(次曲だけ)")
        self.pre_n.setValue(self.s.prerender_ahead)
        self.pre_n.setToolTip("実験的機能。RAM 再生ON時、次曲より先の曲も再生前に変換(デコード→リサンプル)して RAM に置いておく。\n"
                              "再生中は変換処理が走らず、RAM から読み出すだけになる。一覧の「RAM」列で、どの曲が載っているか分かる。")
        f.addRow("事前変換する先読み曲数(実験・RAM再生時)", self.pre_n)
        self.pre_mb = QSpinBox()
        self.pre_mb.setRange(256, 65536)
        self.pre_mb.setSuffix(" MB")
        self.pre_mb.setValue(self.s.prerender_total_mb)
        f.addRow("事前変換した曲の合計上限", self.pre_mb)
        self.buf = QDoubleSpinBox()
        self.buf.setRange(0.2, 30.0)
        self.buf.setSuffix(" 秒")
        self.buf.setValue(self.s.stream_buffer_s)
        self.buf.setToolTip("RAM 再生OFF時のリングバッファ長。音量変更の反映遅れもこの長さ程度になる。")
        f.addRow("出力リングバッファ長", self.buf)
        self.cap_lat = QDoubleSpinBox()
        self.cap_lat.setRange(10, 500)
        self.cap_lat.setSuffix(" ms")
        self.cap_lat.setValue(self.s.capture_latency_ms)
        self.cap_lat.setToolTip("アプリ音声を排他出力で再生する時の遅延(取り込みバッファの目標)。小さいほど遅延が減るが、音切れしやすくなる。\n"
                                "動画と同時に見るなら 20〜40 ms、音楽だけなら 100 ms 以上でも可。")
        f.addRow("アプリ音声の遅延(目標)", self.cap_lat)
        self.quiet = QCheckBox("静音モード: 再生中の状態通知(1秒)・メトリクス(5秒)・準備プロセスの待機を減らして、再生スレッドへの干渉を抑える")
        self.quiet.setChecked(self.s.quiet_mode)
        f.addRow(self.quiet)
        self.cap_auto = QCheckBox("遅延目標を音切れしない範囲で自動調整する(目標は上限の初期値)")
        self.cap_auto.setChecked(self.s.capture_latency_auto)
        f.addRow(self.cap_auto)
        self.cap_min = QDoubleSpinBox()
        self.cap_min.setRange(5, 500)
        self.cap_min.setSuffix(" ms")
        self.cap_min.setValue(self.s.capture_latency_min_ms)
        f.addRow("自動調整: 下限", self.cap_min)
        self.cap_max = QDoubleSpinBox()
        self.cap_max.setRange(10, 1000)
        self.cap_max.setSuffix(" ms")
        self.cap_max.setValue(self.s.capture_latency_max_ms)
        f.addRow("自動調整: 上限", self.cap_max)
        self.cap_sync = QComboBox()
        self.cap_sync.addItem("連続補正(推奨・数ppmの微小リサンプルで滑らかに吸収)", "ratio")
        self.cap_sync.addItem("1フレームの間引き・複製(元のサンプルを変えないが不連続が出る)", "slip")
        self.cap_sync.setCurrentIndex(max(0, self.cap_sync.findData(self.s.capture_sync)))
        f.addRow("アプリ音声のクロック差の吸収", self.cap_sync)
        self.cap_rate_auto = QCheckBox("取り込みレートをアプリのストリームのレートに合わせる(DAC が非対応なら設定の固定レート)")
        self.cap_rate_auto.setChecked(self.s.capture_rate_auto)
        f.addRow(self.cap_rate_auto)
        self.cap_be = QComboBox()
        self.cap_be.addItem("parec(Pulse互換・標準)", "parec")
        self.cap_be.addItem("pw-cat(PipeWire直接・実験。音が届かなければ自動で parec へ)", "pw-cat")
        self.cap_be.setCurrentIndex(max(0, self.cap_be.findData(self.s.capture_backend)))
        f.addRow("アプリ音声の取り込み方法", self.cap_be)
        if IS_WIN:      # アプリ音声の取り込みは Windows 版では未対応
            for wdg in (self.cap_lat, self.cap_auto, self.cap_min, self.cap_max, self.cap_sync, self.cap_rate_auto, self.cap_be):
                wdg.setVisible(False)
                lab = f.labelForField(wdg)
                if lab is not None:
                    lab.setVisible(False)
        self.pregap = QCheckBox("CUE: 次トラックの INDEX 00(プリギャップ)で切る = プリギャップを再生しない")
        self.pregap.setChecked(self.s.cue_skip_pregap)
        f.addRow(self.pregap)
        self.show_src = QCheckBox("CUE の元ファイル(アルバム丸ごと1ファイル)も一覧に表示する")
        self.show_src.setChecked(self.s.show_cue_source)
        f.addRow(self.show_src)
        return w

    # ---- 時間軸 ----
    def _tab_timing(self):
        w = QWidget()
        f = QFormLayout(w)
        intro = QLabel("出力スレッドを1つの CPU に固定し、それ以外(GUI・デコード・リサンプル)を別コアへ追い出します。\n"
                       "目的は、データ送信タイミングの揺れとクロック変動の低減です。効果は「メトリクス」パネルで確認できます。")
        intro.setWordWrap(True)
        f.addRow(intro)
        self.ded = QComboBox()
        self.ded.addItem("使わない", -1)
        for t in (self.hello.get("cpu", {}).get("topology") or []):
            sib = [x for x in t["siblings"] if x != t["cpu"]]
            self.ded.addItem(f"CPU {t['cpu']}(コア {t['core_id']}" + (f"、SMT兄弟 {sib}" if sib else "") + ")", t["cpu"])
        self.ded.setCurrentIndex(max(0, self.ded.findData(self.s.dedicated_cpu)))
        f.addRow("専用 CPU", self.ded)
        self.sib = QCheckBox("専用コアの SMT 兄弟スレッドも他の処理から空ける")
        self.sib.setChecked(self.s.reserve_sibling)
        f.addRow(self.sib)
        self.rt = QSpinBox()
        self.rt.setRange(0, 99)
        self.rt.setValue(self.s.rt_priority)
        self.rt.setToolTip("SCHED_FIFO の優先度。0 で無効。権限が無ければ rtkit を試します。")
        f.addRow("リアルタイム優先度", self.rt)
        self.conf = QCheckBox("GUI と準備プロセスを専用コアから外す")
        self.conf.setChecked(self.s.confine_gui)
        f.addRow(self.conf)
        cf = self.hello.get("cpu", {}).get("freq") or {}
        info = QLabel(f"cpufreq: driver={cf.get('driver')}  governor={cf.get('governor')}  EPP={cf.get('epp')}  boost={cf.get('boost')}\n"
                      "ガバナーや boost の変更には root 権限が必要なので、アプリからは変更しません(README 参照)。")
        info.setWordWrap(True)
        f.addRow(info)
        return w

    # ---- ライブラリ ----
    def _tab_other(self):
        w = QWidget()
        f = QFormLayout(w)
        self.debug = QCheckBox("デバッグ機能を有効にする(メニューに「デバッグ」を表示)")
        self.debug.setChecked(self.s.debug_enabled)
        f.addRow(self.debug)
        f.addRow(QLabel("デバッグ機能: ABX 聴き比べ(出力プロファイル A/B を同じ位置で切り替えて、聞き分けられるか統計を取る)。\n"
                        "通常の再生には必要ありません。設定の違いを聴感で確かめたい時に有効にしてください。"))
        return w

    def _tab_library(self):
        w = QWidget()
        f = QFormLayout(w)
        self.lib_dir = QLineEdit(self.s.library_dir)
        b = QPushButton("選択…")

        def pick():
            d = QFileDialog.getExistingDirectory(self, "デフォルトのディレクトリ", self.lib_dir.text() or "")
            if d:
                self.lib_dir.setText(d)

        b.clicked.connect(pick)
        row = QHBoxLayout()
        row.addWidget(self.lib_dir, 1)
        row.addWidget(b)
        f.addRow("デフォルトディレクトリ", row)
        cur = {e.lower() for e in (self.s.enabled_exts or sorted(AUDIO_EXTS))}
        grid = QWidget()
        gl = QHBoxLayout(grid)
        gl.setContentsMargins(0, 0, 0, 0)
        self.ext_boxes = {}
        for e in sorted(AUDIO_EXTS):
            cb = QCheckBox(e)
            cb.setChecked(e in cur)
            self.ext_boxes[e] = cb
            gl.addWidget(cb)
        gl.addStretch(1)
        f.addRow("取り込む形式", grid)
        self.ignore_exts = QLineEdit(" ".join(self.s.ignore_exts or []))
        self.ignore_exts.setPlaceholderText("例: .wv .ape(空白区切り。上の選択より優先)")
        f.addRow("無視する拡張子", self.ignore_exts)
        return w

    def show_caps(self, ev: dict):
        if not ev.get("ok"):
            self.caps.setPlainText(f"取得できません: {ev.get('error')}" + (" (使用中)" if ev.get("busy") else ""))
            return
        txt = (f"対応レート: {', '.join(f'{r / 1000:g}k' for r in ev['rates'])}\n"
               f"対応形式: {', '.join(ev['formats'])}\nチャンネル: {ev['ch_min']}–{ev['ch_max']}")
        if ev.get("stream_info"):
            txt += "\n\n" + ev["stream_info"].strip()
        self.caps.setPlainText(txt)

    def result_settings(self) -> Settings:
        s = self.s
        s.backend = self.backend.currentData()
        s.alsa_device = self.device.currentData() or ""
        s.custom_device = self.custom.text().strip()
        s.rate_cap = self.cap.currentData()
        s.s32_valid_bits = self.s32.currentData()
        s.dither = self.dither.isChecked()
        s.noise_shaping = self.nshape.currentData() or "off"
        s.period_ms = self.period.value()
        s.periods = self.periods.value()
        s.release_pipewire = self.release_pw.isChecked()
        s.pw_force_rate = self.pw_force.isChecked()
        s.idle_close_s = self.idle.value()
        s.release_profile_off = self.rel_off.isChecked()
        s.claim_on_switch = self.claim.isChecked()
        s.default_on_shared = self.defsink.isChecked()
        s.lead_in_ms = self.lead.value()
        s.resample_enabled = self.rs_on.isChecked()
        s.resample_mode = self.rs_mode.currentData()
        s.fixed_rate = self.rs_fixed.currentData()
        s.resample_quality = self.rs_q.currentData()
        s.resample_phase = self.rs_phase.currentData()
        s.resample_band = self.rs_band.currentData()
        s.resample_engine = self.rs_engine.currentData()
        s.r8_preset = self.r8_preset.currentData()
        s.r8_trans_band = self.r8_tb.value()
        s.r8_atten_db = self.r8_att.value()
        s.r8_phase = self.r8_ph.currentData()
        s.r8_headroom_db = self.r8_head.value()
        s.ram_playback = self.ram.isChecked()
        s.ram_max_mb = self.ram_mb.value()
        s.prefetch = self.prefetch.isChecked()
        s.prerender_ahead = self.pre_n.value()
        s.prerender_total_mb = self.pre_mb.value()
        s.stream_buffer_s = self.buf.value()
        s.capture_latency_ms = self.cap_lat.value()
        s.quiet_mode = self.quiet.isChecked()
        s.debug_enabled = self.debug.isChecked()
        s.capture_latency_auto = self.cap_auto.isChecked()
        s.capture_latency_min_ms = min(self.cap_min.value(), self.cap_max.value())
        s.capture_latency_max_ms = max(self.cap_min.value(), self.cap_max.value())
        s.capture_sync = self.cap_sync.currentData()
        s.capture_rate_auto = self.cap_rate_auto.isChecked()
        s.capture_backend = self.cap_be.currentData()
        s.cue_skip_pregap = self.pregap.isChecked()
        s.show_cue_source = self.show_src.isChecked()
        s.dedicated_cpu = self.ded.currentData()
        s.reserve_sibling = self.sib.isChecked()
        s.rt_priority = self.rt.value()
        s.confine_gui = self.conf.isChecked()
        s.library_dir = self.lib_dir.text().strip()
        on = [e for e, cb in self.ext_boxes.items() if cb.isChecked()]
        s.enabled_exts = [] if len(on) == len(self.ext_boxes) else on
        s.ignore_exts = [("." + t.lstrip(".")).lower() for t in self.ignore_exts.text().replace(",", " ").split() if t.strip(".")]
        return s

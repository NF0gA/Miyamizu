"""WASAPI 経路(wasapi.py + WasapiBackend + エンジンのデバイス判定)を、偽の sounddevice で Linux 上で検証する。"""
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- 偽 sounddevice ----
fake = types.ModuleType("sounddevice")
STREAMS = []


class PortAudioError(Exception):
    pass


class WasapiSettings:
    def __init__(self, exclusive=False, auto_convert=False, explicit_sample_format=False):
        self.exclusive, self.auto_convert, self.explicit = exclusive, auto_convert, explicit_sample_format


class RawOutputStream:
    def __init__(self, samplerate, blocksize, device, channels, dtype, latency, extra_settings):
        self.args = dict(samplerate=samplerate, device=device, channels=channels, dtype=dtype, exclusive=extra_settings.exclusive)
        self.latency = latency
        self.data = bytearray()
        self.started = self.closed = False
        STREAMS.append(self)

    def start(self): self.started = True
    def stop(self): self.started = False
    def abort(self): self.started = False
    def close(self): self.closed = True

    def write(self, buf):
        self.data += bytes(buf)
        time.sleep(len(buf) / (self.args["channels"] * {"int32": 4, "int24": 3, "int16": 2}[self.args["dtype"]]) / self.args["samplerate"] * 0.5)
        return False


def check_output_settings(device=None, channels=None, dtype=None, samplerate=None, extra_settings=None):
    if dtype not in ("int32", "int16") or samplerate not in (44100, 48000, 88200, 96000, 176400, 192000):
        raise PortAudioError("Invalid sample rate/format")


def query_hostapis(i=None):
    h = {"name": "Windows WASAPI", "devices": [3], "default_output_device": 3}
    return [h] if i is None else h


def query_devices(i=None):
    return {"name": "FakeDAC", "max_output_channels": 2, "default_samplerate": 48000, "hostapi": 0}


fake.PortAudioError, fake.WasapiSettings, fake.RawOutputStream = PortAudioError, WasapiSettings, RawOutputStream
fake.check_output_settings, fake.query_hostapis, fake.query_devices = check_output_settings, query_hostapis, query_devices
sys.modules["sounddevice"] = fake

from harness import *       # noqa: E402
import miyamizu.engine as E  # noqa: E402
from miyamizu import wasapi  # noqa: E402
from miyamizu.backends import WasapiBackend  # noqa: E402

D = "/tmp/claude-0/-home-claude/634049f8-76f1-5b8b-ba6e-ce947dd8eb75/scratchpad/media/"
os.makedirs(D, exist_ok=True)


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name, extra)
    return ok


def main():
    res = []
    devs = wasapi.list_hw_devices()
    res.append(check("デバイス列挙", [d.name for d in devs] == ["FakeDAC"]))
    caps = wasapi.probe_caps("FakeDAC")
    res.append(check("caps", caps.ok and caps.rates == [44100, 48000, 88200, 96000, 176400, 192000] and caps.formats == ["S32_LE", "S16_LE"] and caps.ch_max == 2, str((caps.rates, caps.formats))))
    res.append(check("probe_format", wasapi.probe_format("FakeDAC", 44100, 2) == ("S32_LE", "")))
    res.append(check("非対応レートは None", wasapi.probe_format("FakeDAC", 352800, 2)[0] is None))

    exp1 = make_noise_flac(D + "w1.flac", 1.2, 44100, 16, 2, seed=11)
    E.alsa = wasapi
    E.Engine.backend_factory = lambda self: WasapiBackend()
    H = Harness(None, "S32_LE", settings=dict(backend="alsa_hw", alsa_device="FakeDAC", release_pipewire=False,
                                              release_profile_off=False, claim_on_switch=False, default_on_shared=False,
                                              idle_close_s=0, lead_in_ms=0))
    try:
        m = H.mark()
        H.cmd("load", track={"id": 1, "path": D + "w1.flac"})
        H.wait("finished", timeout=20, since=m)
        time.sleep(0.3)
        st = STREAMS[-1]
        res.append(check("排他で開いた", st.args["exclusive"] and st.args["dtype"] == "int32" and st.args["samplerate"] == 44100, str(st.args)))
        x = left_i32_from_stream(bytes(st.data), "S32_LE", 2)
        s0 = find_start(x)
        got = x[s0:s0 + len(exp1)]
        res.append(check("ビットパーフェクト(WASAPI排他経路)", np.array_equal(got, exp1), f"len={len(got)}/{len(exp1)}"))
        sig = [e for e in H.events if e["ev"] == "track_changed"][0]["signal"]
        res.append(check("シグナルパス: 加工なし・bitperfect", sig["steps"] == [] and sig["bitperfect"], str(sig["steps"])))
    finally:
        H.close()
    print("ALL PASS" if all(res) else "SOME FAIL")


if __name__ == "__main__":
    main()

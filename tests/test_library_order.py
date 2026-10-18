import os, sys, shutil, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, soundfile as sf
from miyamizu.library import Library, extract_cover
from miyamizu.playorder import PlayOrder
from miyamizu.cue import parse_cue_text, decode_text

R = []
def check(n, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + n, extra); R.append(ok)

root = tempfile.mkdtemp(prefix="lib-")
def tone(path, sec, rate=44100):
    t = np.arange(int(sec * rate)) / rate
    sf.write(path, (0.3 * np.sin(2 * np.pi * 440 * t))[:, None].repeat(2, 1).astype(np.float32), rate, subtype="PCM_16", format="FLAC")

os.makedirs(root + "/ArtistA/Album1"); os.makedirs(root + "/ArtistA/Album2"); os.makedirs(root + "/CueAlbum")
for i in range(1, 4): tone(f"{root}/ArtistA/Album1/{i:02d}.flac", 1)
for i in range(1, 3): tone(f"{root}/ArtistA/Album2/{i:02d}.flac", 1)
tone(root + "/CueAlbum/image.flac", 9)
# Shift-JIS の CUE。FILE は .wav と書かれているが実体は .flac
cue = ('PERFORMER "歌手"\nTITLE "アルバム"\nFILE "image.wav" WAVE\n'
       '  TRACK 01 AUDIO\n    TITLE "一曲目"\n    INDEX 01 00:00:00\n'
       '  TRACK 02 AUDIO\n    TITLE "二曲目"\n    INDEX 00 00:02:50\n    INDEX 01 00:03:00\n'
       '  TRACK 03 AUDIO\n    TITLE "三曲目"\n    INDEX 01 00:06:00\n')
open(root + "/CueAlbum/image.cue", "wb").write(cue.encode("cp932"))

lib = Library(root + "/lib.sqlite")
n = lib.scan(root)
check("scan folders (root+3 dirs+Artist dir)", n == 5, f"changed={n}")
check("track count (cue source hidden)", lib.count() == 3 + 2 + 3, str(lib.count()))
cue_tracks = lib.tracks_in_dir(root + "/CueAlbum")
check("cue tracks parsed (cp932, .wav->.flac)", [t["title"] for t in cue_tracks] == ["一曲目", "二曲目", "三曲目"], str([t["title"] for t in cue_tracks]))
t2 = cue_tracks[1]
check("cue frames exact (3s, 6s @44.1k)", t2["start_frame"] == 3 * 44100 and t2["end_frame"] == 6 * 44100, f"{t2['start_frame']},{t2['end_frame']}")
check("pregap end (INDEX00) kept", cue_tracks[0]["end_nopregap"] == int(round(200 * 44100 / 75)), str(cue_tracks[0]["end_nopregap"]))
check("source row shown only on request", len(lib.tracks_in_dir(root + "/CueAlbum", show_cue_source=True)) == 4)
n2 = lib.scan(root)
check("incremental rescan = no change", n2 == 0, str(n2))
tone(f"{root}/ArtistA/Album2/03.flac", 1)
n3 = lib.scan(root)
check("new file detected (1 folder)", n3 == 1 and lib.count() == 9, f"{n3} {lib.count()}")
os.remove(f"{root}/ArtistA/Album2/03.flac"); lib.scan(root)
check("deleted file pruned", lib.count() == 8)
rec = lib.tracks_in_dir(root + "/ArtistA", recursive=True); nonrec = lib.tracks_in_dir(root + "/ArtistA", recursive=False)
check("recursive listing", len(rec) == 5 and len(nonrec) == 0)

# --- プレイリスト ---
pid = lib.create_playlist("お気に入り")
lib.add_to_playlist(pid, [cue_tracks[1], rec[0], rec[3]])
pl = lib.playlist_tracks(pid)
check("playlist items resolved (cue range preserved)", len(pl) == 3 and pl[0]["start_frame"] == t2["start_frame"] and not pl[0]["missing"])
lib.export_m3u8(pid, root + "/pl.m3u8")
pid2_n = lib.import_m3u8(root + "/pl.m3u8", "imported")
pl2 = lib.playlists()
check("m3u8 roundtrip", pid2_n == 3 and [p["n"] for p in pl2 if p["name"] == "imported"] == [3])
os.remove(rec[0]["path"])
pl = lib.playlist_tracks(pid)
check("missing track kept as unavailable", pl[1]["missing"] is True)

# --- 再生順序 ---
lib.scan(root)
class P:
    def __init__(s, l): s.l = l
    def folder_tracks(s, t): return [x for x in s.l.all_tracks() if x["folder"] == t["folder"]]
    def all_tracks(s): return s.l.all_tracks()
po = PlayOrder(P(lib), seed=1)
allt = lib.all_tracks()
a1 = [t for t in allt if t["folder"].endswith("Album1")]
po.set_context(a1, playlist=False)
po.set_modes("off", "off")
cur = a1[0]; po.on_started(cur)
check("sequential next", po.plan_next(cur)["id"] == a1[1]["id"])
check("end of list, repeat off -> stop", po.plan_next(a1[-1]) is None)
po.set_modes("folder", "off")
check("repeat folder wraps", po.plan_next(a1[-1])["id"] == a1[0]["id"])
po.set_modes("one", "off")
check("repeat one (auto)", po.plan_next(cur, auto=True)["id"] == cur["id"] and po.plan_next(cur, auto=False)["id"] == a1[1]["id"])
po.set_modes("all", "off")
last_all = allt[-1]
check("repeat all spans library & wraps", po.plan_next(last_all)["id"] == allt[0]["id"] and po.plan_next(a1[-1])["id"] == allt[allt.index(a1[-1]) + 1]["id"])
po.set_modes("off", "folder")
seen = set(); c = a1[0]; po.on_started(c); seen.add(c["id"])
for _ in range(len(a1) - 1):
    nx = po.plan_next(c); po.on_started(nx); seen.add(nx["id"]); c = nx
    assert nx["folder"] == a1[0]["folder"]
check("shuffle folder: visits every track once, stays in folder", seen == {t["id"] for t in a1})
check("shuffle folder, repeat off: ends after all", po.plan_next(c) is None)
po.set_modes("all", "all")
nx = po.plan_next(c); check("shuffle all + repeat all continues", nx is not None)
po.set_context(pl, playlist=True); po.set_modes("folder", "off")
pl_ok = [t for t in pl if not t["missing"]]
pl_ids = [t["id"] for t in pl_ok]
check("playlist context: 'folder' repeat = playlist repeat", po.plan_next(pl_ok[-1])["id"] == pl_ids[0])
print("ALL PASS" if all(R) else "SOME FAIL")

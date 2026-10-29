"""PlayOrder(再生順)の単体テスト。Qt/エンジン不要。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from miyamizu.playorder import PlayOrder, key_of


class Prov:
    def __init__(self, tracks):
        self.tracks = tracks

    def all_tracks(self):
        return list(self.tracks)

    def folder_tracks(self, t):
        return [x for x in self.tracks if x["g"] == t["g"]]

    def group_of(self, t):
        return t["g"]


def mk(groups):
    out, i = [], 0
    for g, n in groups.items():
        for _ in range(n):
            out.append({"id": i, "g": g}); i += 1
    return out


def order(tracks, view=None, **modes):
    o = PlayOrder(Prov(tracks), seed=1)
    o.set_context(view if view is not None else tracks, playlist=False)
    o.set_modes(modes.get("repeat", "off"), modes.get("shuffle", "off"))
    return o


def walk(o, start, early=False, limit=100):
    """エンジン同様に進める。early=True は need_next(plan_next)を on_started より先に呼ぶ。"""
    seen, cur = [start], start
    o.on_started(start)
    for _ in range(limit):
        nxt = o.plan_next(cur, auto=True)
        if early:
            o.upcoming(cur, 5)
        if nxt is None:
            break
        o.on_started(nxt)
        seen.append(nxt); cur = nxt
    return seen


def test_no_duplicate_early_and_late():
    tr = mk({"a": 8})
    for early in (False, True):
        o = order(tr, shuffle="all")
        ids = [t["id"] for t in walk(o, tr[0], early)]
        assert sorted(ids) == list(range(8)), (early, ids)


def test_need_next_before_on_started_never_returns_cur():
    tr = mk({"a": 6})
    o = order(tr, shuffle="folder", repeat="all")
    o.on_started(tr[0])
    for _ in range(50):
        cur = o.cur
        n1 = o.plan_next(cur, auto=True)       # on_started 前に何度尋ねても
        assert n1["id"] != cur["id"]
        assert all(t["id"] != cur["id"] for t in o.upcoming(cur, 10)[0])
        o.on_started(n1)


def test_parent_folder_shuffle_covers_all_subfolders():
    tr = mk({"c1": 3, "c2": 3, "c3": 3})
    o = order(tr, shuffle="folder")
    ids = {t["id"] for t in walk(o, tr[0])}
    assert ids == set(range(9))


def test_child_folder_list_only_that_folder():
    tr = mk({"c1": 3, "c2": 3})
    view = [t for t in tr if t["g"] == "c2"]
    o = order(tr, view=view, shuffle="folder")
    assert {t["id"] for t in walk(o, view[0])} == {3, 4, 5}


def test_shuffle_all_uses_library():
    tr = mk({"c1": 3, "c2": 3})
    o = order(tr, view=tr[:3], shuffle="all")
    assert {t["id"] for t in walk(o, tr[0])} == set(range(6))


def test_repeat_folder_loops_displayed_list():
    tr = mk({"c1": 2, "c2": 2})
    o = order(tr, repeat="folder")
    cur, out = tr[0], []
    for _ in range(6):
        cur = o.plan_next(cur, auto=True); out.append(cur["id"])
    assert out == [1, 2, 3, 0, 1, 2]


def test_repeat_all_shuffle_reshuffles_without_immediate_repeat():
    tr = mk({"a": 4})
    o = order(tr, shuffle="all", repeat="all")
    o.on_started(tr[0]); cur = tr[0]
    for _ in range(40):
        n = o.plan_next(cur, auto=True)
        assert n["id"] != cur["id"]
        o.on_started(n); cur = n


def test_playlist_scope():
    tr = mk({"a": 5})
    pl = [tr[4], tr[1], tr[2]]
    o = PlayOrder(Prov(tr), seed=2)
    o.set_context(pl, playlist=True)
    o.set_modes("off", "folder")
    assert {t["id"] for t in walk(o, pl[0])} == {4, 1, 2}


def test_single_track_repeat_off_ends():
    tr = mk({"a": 1})
    o = order(tr, shuffle="all")
    o.on_started(tr[0])
    assert o.plan_next(tr[0], auto=True) is None


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v(); print("ok", k)

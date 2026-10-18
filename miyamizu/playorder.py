"""再生順序(リピート/シャッフル/履歴)。Qt に依存しない。

リピート : off / one(1曲) / folder(フォルダ内) / all(全曲)
シャッフル: off / folder(フォルダ内) / all(全曲)
プレイリスト再生中は「フォルダ内」「全曲」の範囲がどちらもそのプレイリストになる(プレイリスト内リピート)。
フォルダ = 再生中の曲が入っているフォルダ(サブフォルダは含めない)。CUE は1枚ごとに1つのフォルダ扱い。
"""
from __future__ import annotations

import random
from typing import Callable, Protocol


class Provider(Protocol):
    def folder_tracks(self, track: dict) -> list[dict]: ...
    def all_tracks(self) -> list[dict]: ...


def key_of(t: dict):
    return t["id"]


def group_of(t: dict):
    """「フォルダ」の単位。通常はフォルダ、CUE は CUE 1枚(=1アルバム)。"""
    return t.get("group_key") or t.get("folder")


class PlayOrder:
    def __init__(self, provider: Provider, seed: int | None = None):
        self.p = provider
        self.repeat = "off"
        self.shuffle = "off"
        self.playlist_mode = False
        self.ctx_tracks: list[dict] = []     # 再生を始めたリスト(表示中の曲リスト or プレイリスト)
        self.history: list[dict] = []
        self.cur: dict | None = None
        self._bag: list = []
        self._bag_map: dict = {}
        self._bag_sig = None
        self._going_back = False
        self.rng = random.Random(seed)

    # ---- コンテキスト ----
    def set_context(self, tracks: list[dict], playlist: bool):
        self.ctx_tracks = list(tracks)
        self.playlist_mode = playlist
        self._bag_sig = None

    def set_modes(self, repeat: str, shuffle: str):
        if (repeat, shuffle) != (self.repeat, self.shuffle):
            self._bag_sig = None
        self.repeat, self.shuffle = repeat, shuffle

    def _folder_set(self, cur: dict) -> list[dict]:
        return self.ctx_tracks if self.playlist_mode else self.p.folder_tracks(cur)

    def _all_set(self) -> list[dict]:
        return self.ctx_tracks if self.playlist_mode else self.p.all_tracks()

    # ---- シャッフル袋 ----
    def _rebuild_bag(self, pool: list[dict], cur: dict | None):
        ids = [key_of(t) for t in pool if cur is None or key_of(t) != key_of(cur)]
        self.rng.shuffle(ids)
        self._bag = ids
        self._bag_map = {key_of(t): t for t in pool}

    def _pool_sig(self, pool: list[dict], cur: dict):
        folder = None if (self.shuffle == "all" or self.playlist_mode) else group_of(cur)
        return (self.shuffle, self.playlist_mode, folder, len(pool),
                key_of(pool[0]) if pool else None, key_of(pool[-1]) if pool else None)

    # ---- 次/前 ----
    def plan_next(self, cur: dict | None, auto: bool = True) -> dict | None:
        """次に再生する曲(状態は変えない)。auto=True は曲が自然に終わった場合。"""
        if cur is None:
            lst = self.ctx_tracks
            return lst[0] if lst else None
        if self.repeat == "one" and auto:
            return cur
        if self.shuffle != "off":
            pool = self._folder_set(cur) if self.shuffle == "folder" else self._all_set()
            if not pool:
                return None
            sig = self._pool_sig(pool, cur)
            if sig != self._bag_sig:
                self._rebuild_bag(pool, cur)
                self._bag_sig = sig
            if not self._bag:
                if self.repeat == "off" and auto:
                    return None
                self._rebuild_bag(pool, cur)
                if not self._bag:
                    return cur if self.repeat != "off" else None
            return self._bag_map.get(self._bag[0])
        if self.repeat == "all":
            seq = self._all_set()
        elif self.repeat == "folder":
            seq = self._folder_set(cur)
        else:
            seq = self.ctx_tracks
        if not seq:
            return None
        idx = next((i for i, t in enumerate(seq) if key_of(t) == key_of(cur)), -1)
        if idx < 0:
            return seq[0] if self.repeat != "off" or not auto else None
        if idx + 1 < len(seq):
            return seq[idx + 1]
        return seq[0] if self.repeat != "off" else None

    def upcoming(self, cur: dict | None, n: int = 50) -> tuple[list[dict], str]:
        """これから再生される予定の曲(状態は変えない)と、順序の説明。"""
        if cur is None:
            return list(self.ctx_tracks[:n]), "再生開始前"
        if self.repeat == "one":
            return [cur], "1曲リピート中"
        if self.shuffle != "off":
            if self.plan_next(cur, auto=True) is None and not self._bag:
                return [], "シャッフル(最後の曲)"
            out = [self._bag_map[k] for k in self._bag[:n] if k in self._bag_map]
            note = "シャッフル: " + ("フォルダ内" if self.shuffle == "folder" and not self.playlist_mode else
                                     ("プレイリスト内" if self.playlist_mode else "全曲"))
            if len(self._bag) < n and self.repeat != "off":
                note += "(以降は再シャッフル)"
            return out, note
        if self.repeat == "all":
            seq, note = self._all_set(), "リピート: " + ("プレイリスト" if self.playlist_mode else "全曲")
        elif self.repeat == "folder":
            seq, note = self._folder_set(cur), "リピート: " + ("プレイリスト" if self.playlist_mode else "フォルダ内")
        else:
            seq, note = self.ctx_tracks, "順番に再生"
        idx = next((i for i, t in enumerate(seq) if key_of(t) == key_of(cur)), -1)
        if idx < 0:
            return [], note
        out = list(seq[idx + 1: idx + 1 + n])
        if self.repeat != "off" and len(out) < n and seq:
            k = 0
            while len(out) < n and k < n:      # 先頭へ折り返す(自分自身に戻るところまで)
                out.append(seq[k % len(seq)])
                k += 1
                if key_of(out[-1]) == key_of(cur):
                    break
        return out, note

    def prev(self, cur: dict | None) -> dict | None:
        if self.history:
            self._going_back = True
            return self.history.pop()
        return cur

    def on_started(self, track: dict):
        """曲が実際に再生され始めたときに呼ぶ(履歴・シャッフル袋の更新)。"""
        k = key_of(track)
        if self.cur is not None and key_of(self.cur) != k and not self._going_back:
            self.history.append(self.cur)
            if len(self.history) > 300:
                self.history = self.history[-300:]
        self._going_back = False
        if self._bag and self._bag[0] == k:
            self._bag.pop(0)
        elif k in self._bag:
            self._bag.remove(k)
        self.cur = track

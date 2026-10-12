"""#79: an owner root whose `ref:` starts with `fm-` never starts a study (ref fm-wave-f-assign).

With `starters = ()` an owner root in a configured channel starts a study; the floor manager's own roots
(`ref: fm-wave-<w>-root`, `fm-wave-f-assign`, ...) are not studies, and the thread stays free for `wave open`.
"""
from __future__ import annotations

from fridica_research import wave
from fridica_research.driver import Driver
from fridica_research.store import Store
from support import CFG, OWNER
from test_issue69_wave import JOBS, REPO, T0

WS, CH, TS = "T1", "C1", "1791817450.960659"
THREAD = f"{WS}:{CH}:{TS}"


def root(text: str, sender: str = OWNER) -> dict:
    return {"cursor": 1, "kind": "message", "workspace": WS, "channel": {"id": CH}, "thread": TS, "ts": TS, "sender": sender, "text": text}


def make(tmp_path) -> tuple[Store, Driver]:
    store = Store(tmp_path / "s.sqlite3")
    return store, Driver(CFG, client=None, store=store, llm=lambda *a: {}, clock=lambda: T0)


def test_owner_fm_root_starts_no_study(tmp_path):
    store, drv = make(tmp_path)
    assert CFG.starters == ()
    for ref in ("fm-wave-e-root", "fm-wave-f-assign"):
        assert drv.translate(root(f"Roster for the wave.\nref: {ref}")) == []
    assert store.load(THREAD) is None
    w = wave.open_wave(THREAD, name="f", repo=REPO, jobs=JOBS, now=T0, cfg=CFG)  # a later `wave open` still takes the thread
    store.set_meta(wave.wave_key(THREAD), w.to_json())
    assert drv.translate(root("next post\nref: fm-wave-f-root")) == []


def test_owner_root_without_fm_ref_still_starts(tmp_path):
    store, drv = make(tmp_path)
    out = drv.translate(root("Study the onset of convection.\nref: study-1"))
    assert [ev.kind for _, ev in out] == ["started"]

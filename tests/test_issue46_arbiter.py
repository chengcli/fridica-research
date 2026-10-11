"""#46 S1 (arbiter, adopt): RED tests T1-T5, written at 9856e26 before the arbiter exists.

The round is stored as `adopt` would write it: store meta `round:<thread>` (JSON). The arbiter
does not exist at the baseline, so every assertion below is on observable driver behaviour or on
that stored round."""
from __future__ import annotations
import dataclasses
import json
import os
import pytest
from fridica_research.client import Client
from fridica_research.driver import Driver
from fridica_research.store import Store
from fake_control import FakeControl
from support import CFG, LLM, OWNER

EXP, DEB, AUD, STRANGER = "UEXP", "UDEB", "UAUD", "USTRANGER"
THREAD = "T1:C1:1791676900.183259"
T0 = 1_700_000_000.0
LINK = "https://github.com/chengcli/fridica-research/issues/46"
DEBATE_BUDGET = 4 * 3600  # driver's ruling: due = now + the per-role budget kept in config (debate 4 h)


class Clock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t


class Board:
    def __init__(self): self.calls = []
    def sync(self, state): self.calls.append(("sync", state.thread)); return []


@pytest.fixture
def world(tmp_path, sock_dir):
    sock = os.path.join(sock_dir, "c.sock")
    server = FakeControl(sock, owner=OWNER).start()
    cfg = dataclasses.replace(CFG, socket=sock, state_path=str(tmp_path / "s.sqlite3"))
    store = Store(cfg.state_file); store.set_cursor(0)
    rnd = {"thread": THREAD, "issue": 46, "repo": "chengcli/fridica-research", "stage": "Explore",
           "roster": {"explorer": {"holders": [EXP], "login": "xw", "delegate": False},
                      "debater": {"holders": [DEB], "login": "zz", "delegate": False},
                      "auditor": {"holders": [AUD], "login": "ll", "delegate": False}},
           "findings": [], "misses": {}}
    store.set_meta(f"round:{THREAD}", json.dumps(rnd))
    clock, board = Clock(), Board()
    drv = Driver(cfg, Client(cfg.socket_path), store, llm=lambda n, p: LLM[n], clock=clock, sleep=lambda s: None, board=board)
    server.root(THREAD, OWNER, "#46 PR round: roles ...", kind=None)  # owner root, starters == ()
    yield server, drv, clock, board
    server.stop()


def drain(drv, limit=20):
    for _ in range(limit):
        n, end = drv.run_once()
        if n == 0 and end: return


def rnd(drv): return json.loads(drv.store.get_meta(f"round:{THREAD}"))
def control(server, suffix): return [p for m, p, b in server.requests if m == "POST" and p.endswith(suffix)]
def posts(server): return [b for m, p, b in server.requests if m == "POST" and p.endswith("/post")]


def test_T1_adopted_round_never_delegates(world):
    server, drv, clock, _ = world
    for i in range(3): server.message(THREAD, [EXP, DEB, AUD][i], f"crew message {i}")
    for dt in (0, 1800, 86400):
        clock.t = T0 + dt; drain(drv)
    assert control(server, "/delegate") == [] and control(server, "/driver") == []


@pytest.mark.parametrize("kind", [None, "reply"], ids=["T2_plain", "T2b_agent_reply"])
def test_T2_handoff_advances(world, kind):
    server, drv, clock, board = world
    server.message(THREAD, EXP, f"Explorer report done. <@{DEB}> over to you: {LINK} Due 10-11 16:00 ET", kind=kind)
    clock.t = T0 + 60; drain(drv)
    r = rnd(drv)
    assert r["stage"] == "Debate"
    assert r["due"] == clock.t + DEBATE_BUDGET  # re-based from the clearing event's now, not the hand-off's stated due
    assert ("sync", THREAD) not in board.calls  # field-level board writes only, never Board.sync / sync_study


def test_T3_missing_mention_fixed_once(world):
    server, drv, clock, _ = world
    e = server.message(THREAD, EXP, f"Explorer report done, over to the debater: {LINK} Due 10-11 16:00 ET")
    drain(drv); server.push(dict(e)); drain(drv)
    fixes = [b for b in posts(server) if f"<@{DEB}>" in b.get("text", "")]
    assert len(fixes) == 1 and "ref:" in fixes[0]["text"]


def test_T4_non_holder_cannot_clear(world):
    server, drv, clock, _ = world
    server.message(THREAD, STRANGER, f"<@{DEB}> over to you: {LINK} Due 10-11 16:00 ET")
    drain(drv)
    r = rnd(drv)
    assert r["stage"] == "Explore"
    assert len(r["findings"]) == 1


def test_T5_no_clock_closure(world):
    server, drv, clock, _ = world
    drain(drv)
    before = len(server.requests)
    for dt in (20 * 60, 23 * 60, 72 * 3600):
        clock.t = T0 + dt; drain(drv)
    after = [(m, p) for m, p, b in server.requests[before:] if m == "POST"]
    st = drv.store.load(THREAD)
    assert after == [] and st is None and rnd(drv)["stage"] == "Explore"

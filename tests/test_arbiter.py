"""#46 slice 1: the arbiter for adopted, people-run rounds (AC1-AC9 of the debate consensus).

The round state is written the way `adopt` writes it (meta key `round:<thread>`), so every
behavioural test here runs against the driver and the fake control server, not against a new
import: at 9856e26 the adopted thread is a study, which is what AC1-AC5 fail on.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re

import pytest

from fridica_research import board as board_mod
from fridica_research.board import Board
from fridica_research.cli import main
from fridica_research.client import Client
from fridica_research.config import Board as BoardCfg
from fridica_research.config import Config
from fridica_research.driver import Driver
from fridica_research.store import Store
from fake_control import FakeControl
from support import CFG, LLM, OWNER
from test_board import FakeGh

EXPLORER, DEBATER, AUDITOR, IMPLEMENTER, STRANGER = "UEXP", "UDEB", "UAUD", "UIMP", "USTRANGER"
T0 = 1_700_000_000.0
HOUR = 3600.0
THREAD = "T1:C1:1700000000.000100"
ISSUE, REPO = 3, "chengcli/fridica-research"
LINK = "https://github.com/chengcli/fridica-research/issues/46"
COMMAND = re.compile(r"fridica-research\s+\w+")  # AC6: no arbiter post asks a person to run a command
BCFG = dataclasses.replace(CFG, board=BoardCfg(enabled=True, owner="chengcli", number=8, repo=REPO))

HANDOFF = f"Stage: Explore handoff to the debater.\n<@{DEBATER}> the explorer report is in.\n{LINK}\nDue 10-11 16:00 ET"
HANDOFF_NO_MENTION = f"Stage: Explore handoff to the debater.\n{LINK}\nDue 10-11 16:00 ET"
HANDOFF_NO_DUE = f"Stage: Explore handoff to the debater.\n<@{DEBATER}> take it.\n{LINK}"
PROGRESS = f"Still reading the inventory, <@{DEBATER}> for visibility.\n{LINK}\nDue 10-11 16:00 ET"


def root_text(text="Study the thing", hours=4): return f"{text}\n\nprojected: {hours} h\ngeneration: 1\nlineage: origin\nref: start"


def round_meta(stage: str = "Explore", now: float = T0, **kw) -> str:
    """The round `adopt` writes: per thread, explicit roster, no delegation anywhere."""
    r = {
        "thread": THREAD, "channel": "C1", "issue": ISSUE, "repo": REPO,
        "stage": stage, "stage_started": now, "adopted_at": now, "due": now + 4 * HOUR,
        "roster": {
            "explorer": {"holders": [EXPLORER], "login": "exp", "delegate": False},
            "debater": {"holders": [DEBATER], "login": "deb", "delegate": False},
            "auditor": {"holders": [AUDITOR], "login": "aud", "delegate": False},
            "implementer": {"holders": [IMPLEMENTER], "login": "imp", "delegate": False},
        },
    }
    r.update(kw)
    return json.dumps(r)


class Clock:
    def __init__(self, t: float = T0): self.t = t
    def __call__(self): return self.t


@pytest.fixture
def world(tmp_path, sock_dir):
    server = FakeControl(os.path.join(sock_dir, "c.sock"), owner=OWNER).start()
    cfg = dataclasses.replace(BCFG, socket=os.path.join(sock_dir, "c.sock"), state_path=str(tmp_path / "state.sqlite3"))
    yield server, cfg
    server.stop()


def adopted(cfg: Config, meta: str | None = None) -> tuple[Store, list[str]]:
    store = Store(cfg.state_file)
    store.set_cursor(0)
    store.set_meta(f"round:{THREAD}", meta if meta is not None else round_meta())
    store.set_meta(f"board:item:{REPO}#{ISSUE}", "PVTI_I_3")  # the project item is known: no items query in these tests
    return store, []


def make(cfg: Config, store: Store, clock: Clock, calls: list[str], gh: FakeGh | None = None) -> tuple[Driver, FakeGh]:
    gh = gh or FakeGh()
    board = Board(cfg, runner=gh, remember=store.set_meta, recall=store.get_meta)
    llm = lambda name, prompt: (calls.append(name), LLM[name])[1]  # noqa: E731 - AC1 counts LLM calls
    return Driver(cfg, Client(cfg.socket_path), store, llm=llm, board=board, clock=clock, sleep=lambda s: None), gh


def drain(drv: Driver, limit: int = 10):
    for _ in range(limit):
        n, at_end = drv.run_once()
        if n == 0 and at_end: return
    raise AssertionError("feed did not settle")


def rnd(store: Store) -> dict: return json.loads(store.get_meta(f"round:{THREAD}") or "{}")
def own_posts(server: FakeControl) -> list[str]: return [m["text"] for m in server.view(THREAD)["messages"] if m["sender"] == OWNER and m["ts"] != THREAD.rsplit(":", 1)[1]]
def posts(server: FakeControl) -> list[dict]: return [b for m, p, b in server.requests if m == "POST" and p.endswith("/post")]


# -- T1 / AC1: an adopted thread never becomes an AI study --------------------------------
def test_adopted_round_never_delegates(world):
    server, cfg = world
    store, calls = adopted(cfg)
    clock = Clock()
    drv, _ = make(cfg, store, clock, calls)
    server.root(THREAD, OWNER, root_text())
    server.message(THREAD, EXPLORER, "reading the inventory now")
    server.message(THREAD, DEBATER, "acknowledged", kind="reply")
    drain(drv)
    for t in (T0 + 30 * 60, T0 + 24 * HOUR):
        clock.t = t
        drv.run_once()
    paths = [p for m, p, _ in server.requests if m == "POST"]
    assert [p for p in paths if p.endswith("/delegate")] == []
    assert [p for p in paths if p.endswith("/driver")] == []
    assert calls == []
    assert store.load(THREAD) is None  # no study state was ever written for an adopted thread


# -- T2 / AC2: a plain hand-off clears the stage at once ----------------------------------
def test_plain_handoff_advances(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, gh = make(cfg, store, Clock(), calls)
    server.message(THREAD, EXPLORER, HANDOFF)
    drain(drv)
    r = rnd(store)
    assert r["stage"] == "Debate"
    assert r["due"] == pytest.approx(T0 + 4 * HOUR)  # re-based from the event's now with the debate budget
    options = {v["field"]: v["v"] for v in gh.sets(board_mod.M_SET_OPTION)}
    assert options["F_stage"] == "O_Debate" and options["F_role"] == "R_debater"
    edits = gh.argv("gh", "issue", "edit")
    assert any("--add-assignee" in a and "deb" in a for a in edits)
    assert not any("--body" in a for a in edits)  # field-level only: #49 stays out
    assert own_posts(server) == []  # the hand-off carried the mention, so the arbiter says nothing


def test_agent_reply_handoff_advances(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    server.message(THREAD, EXPLORER, HANDOFF, kind="reply")
    drain(drv)
    assert rnd(store)["stage"] == "Debate"


def test_progress_post_without_marker_does_not_advance(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    ts = "1700000000.000300"
    server.message(THREAD, EXPLORER, PROGRESS, ts=ts)
    drain(drv)
    r = rnd(store)
    assert r["seen"] == [ts]  # the arbiter saw it: a hand-off marker is what it lacks
    assert r["stage"] == "Explore" and own_posts(server) == []


def test_handoff_without_due_does_not_clear_and_is_recorded(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    server.message(THREAD, EXPLORER, HANDOFF_NO_DUE)
    drain(drv)
    r = rnd(store)
    assert r["stage"] == "Explore" and len(r["findings"]) == 1


# -- T3 / AC3: the missing mention is fixed exactly once ----------------------------------
def test_missing_mention_fixed_once(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    ts = "1700000000.000200"
    server.message(THREAD, EXPLORER, HANDOFF_NO_MENTION, ts=ts)
    drain(drv)
    server.message(THREAD, EXPLORER, HANDOFF_NO_MENTION, ts=ts)  # the same event again
    drain(drv)
    fixes = [t for t in own_posts(server) if f"<@{DEBATER}>" in t]
    assert len(fixes) == 1
    assert LINK in fixes[0] and "ET" in fixes[0] and re.search(r"^ref: \S+$", fixes[0], re.M)
    r = rnd(store)
    assert r["misses"] == {f"Explore/{EXPLORER}": 1}
    assert r["stage"] == "Debate"  # the arbiter fixed the mention, so the round still advances


# -- T4 / AC4: only the holder of the stage's role can clear it ---------------------------
def test_non_holder_cannot_clear(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    server.message(THREAD, STRANGER, HANDOFF)
    drain(drv)
    r = rnd(store)
    assert r["stage"] == "Explore"
    assert len(r["findings"]) == 1 and STRANGER in r["findings"][0]
    assert own_posts(server) == []


def test_own_echo_never_clears(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    ts = "1700000000.000400"
    server.message(THREAD, OWNER, HANDOFF, kind="report", ts=ts)
    drain(drv)
    r = rnd(store)
    assert r["seen"] == [ts] and r["stage"] == "Explore"


# -- T5 / AC5: no clock-driven closure of any kind ----------------------------------------
def test_no_clock_closure(world):
    server, cfg = world
    store, calls = adopted(cfg)
    clock = Clock()
    drv, _ = make(cfg, store, clock, calls)
    server.root(THREAD, OWNER, root_text())
    server.message(THREAD, EXPLORER, "still working")
    drain(drv)
    for t in (T0 + 20 * 60, T0 + 23 * 60, T0 + 72 * HOUR):
        clock.t = t
        drv.run_once()
    r = rnd(store)
    assert r["stage"] == "Explore"
    assert posts(server) == []
    assert [p for m, p, _ in server.requests if p.endswith("/stop")] == []
    assert store.load(THREAD) is None


# -- T6 / AC6: no arbiter post ever asks a person to run a command ------------------------
def test_no_command_text(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    server.message(THREAD, EXPLORER, HANDOFF_NO_MENTION)
    server.message(THREAD, STRANGER, HANDOFF)
    drain(drv)
    texts = own_posts(server)
    assert texts and all(not COMMAND.search(t) for t in texts)
    assert all("```" not in t for t in texts)


def test_due_times_are_new_york_time_in_the_agreed_format():
    import datetime as dt

    from fridica_research import arbiter
    at = lambda iso: dt.datetime.fromisoformat(iso).timestamp()  # noqa: E731
    assert arbiter.due_line(at("2026-10-12T16:00:00+00:00")) == "Due 10-12 12:00 ET"  # EDT
    assert arbiter.eta_line(at("2026-12-01T17:00:00+00:00")) == "ETA 12-01 12:00 ET"  # EST
    assert arbiter.due_line(at("2026-03-08T06:30:00+00:00")) == "Due 03-08 01:30 ET"  # before the spring change
    assert arbiter.due_line(at("2026-03-08T07:30:00+00:00")) == "Due 03-08 03:30 ET"  # after it
    assert arbiter.due_line(None) == "Due (unset)"


def test_arbiter_emits_no_ai_action_kinds():
    from fridica_research import arbiter
    assert set(arbiter.ACTION_KINDS).isdisjoint({"delegate", "set_driver", "llm_call", "stop_worker", "arm_timer"})


def test_the_driver_refuses_to_execute_a_foreign_action_for_a_round(world):
    from fridica_research.machine import Action
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    rnd = drv.load_round(THREAD)
    with pytest.raises(ValueError):
        drv.execute_round(rnd, Action("delegate", "x", {"role": "explorer"}))
    assert drv.execute_round(rnd, Action("notify_owner", f"{THREAD}/round/escalate", {"text": "owner-only question"})) is None
    assert own_posts(server) == [f"<@{OWNER}> owner-only question\nref: {THREAD}/round/escalate"]


def test_a_restart_does_not_post_the_fix_twice(world):
    """The `ref:` probe, not only the fold: a round rolled back to before the post must not post again."""
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    server.message(THREAD, EXPLORER, HANDOFF_NO_MENTION, ts="1700000000.000500")
    drain(drv)
    store.set_meta(f"round:{THREAD}", round_meta())  # the snapshot is back at Explore, with no posted ref and no seen ts
    store.set_cursor(0)
    drv.recovered = True
    drain(drv)
    assert len([t for t in own_posts(server) if f"<@{DEBATER}>" in t]) == 1


# -- T7 / AC7: the roster validator (guard test: argparse-only at the base sha) ------------
def config_file(tmp_path, cfg: Config) -> str:
    p = tmp_path / "research.toml"
    p.write_text(f'state_path = "{cfg.state_file}"\nchannels = ["C1"]\n\n[fridica]\nowner = "{OWNER}"\nsocket = "{cfg.socket}"\n')
    return str(p)


def adopt_argv(tmp_path, cfg, *extra: str) -> list[str]:
    return ["--config", config_file(tmp_path, cfg), "adopt", THREAD, "--issue", str(ISSUE), "--repo", REPO,
            "--role", f"explorer={EXPLORER}", "--role", f"debater={DEBATER}", "--login", "explorer=exp", *extra]


def test_adopt_rejects_delegate_on_person_role(tmp_path, world):
    _, cfg = world
    assert main(adopt_argv(tmp_path, cfg, "--delegate", "explorer")) == 1
    store = Store(cfg.state_file)
    assert store.get_meta(f"round:{THREAD}") is None


def test_adopt_rejects_the_owner_as_a_holder(tmp_path, world):
    _, cfg = world
    assert main(adopt_argv(tmp_path, cfg, "--role", f"auditor={OWNER}")) == 1
    assert Store(cfg.state_file).get_meta(f"round:{THREAD}") is None


def test_adopt_writes_a_round_per_thread(tmp_path, world):
    _, cfg = world
    assert main(adopt_argv(tmp_path, cfg)) == 0
    store = Store(cfg.state_file)
    r = json.loads(store.get_meta(f"round:{THREAD}"))
    assert r["stage"] == "Explore" and r["issue"] == ISSUE and r["roster"]["debater"]["holders"] == [DEBATER]
    assert store.get_meta("round:T1:C1:1700000000.000999") is None  # adoption is keyed per thread, never per channel
    assert main(adopt_argv(tmp_path, cfg)) == 1  # already adopted

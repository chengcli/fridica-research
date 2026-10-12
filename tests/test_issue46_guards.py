"""#46 slice 1: the guard tests the six canonical RED cases do not cover.

`tests/test_issue46_arbiter.py` holds the auditor's T1-T5 (AC1-AC5) verbatim. The cases here are
the ones he listed as uncoverable before the arbiter API existed: AC6 (no post asks a person to
run a command), AC7 (`adopt` refuses AI delegation on a person-held role), the arbiter's own
echoed posts (A4), the `ref:` restart probe and per-thread adoption (A5), the CodeAudit/Merge
board stage mapping (A6), and the negative case for A3 -- a progress post with a link and a due
time but no hand-off marker must not advance the stage. The field-level board writes the driver
ruled on are pinned here too; T2 only asserts that `Board.sync` is never called.

The round is written the way `adopt` writes it, under the same `round:<thread>` meta key.
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

EXPLORER, DEBATER, AUDITOR, IMPLEMENTER, DRIVER, STRANGER = "UEXP", "UDEB", "UAUD", "UIMP", "UDRV", "USTRANGER"
T0 = 1_700_000_000.0
HOUR = 3600.0
THREAD = "T1:C1:1700000000.000100"
ISSUE, REPO = 3, "chengcli/fridica-research"
LINK = "https://github.com/chengcli/fridica-research/issues/46"
COMMAND = re.compile(r"fridica-research\s+\w+")  # AC6: no arbiter post asks a person to run a command

HANDOFF = f"Stage: Explore handoff to the debater.\n<@{DEBATER}> the explorer report is in.\n{LINK}\nDue 10-11 16:00 ET"
HANDOFF_NO_MENTION = f"Stage: Explore handoff to the debater.\n{LINK}\nDue 10-11 16:00 ET"
HANDOFF_NO_DUE = f"Stage: Explore handoff to the debater.\n<@{DEBATER}> take it.\n{LINK}"
PROGRESS = f"Still reading the inventory, <@{DEBATER}> for visibility.\n{LINK}\nDue 10-11 16:00 ET"


HEAD = "a" * 40
APPROVED_PR = {"number": 70, "head": HEAD, "state": "OPEN", "draft": False,  # #46 slice 2: CodeAudit clears only on this
               "review": {"id": "9001", "state": "APPROVED", "commit": HEAD, "login": "aud"}}


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
            "driver": {"holders": [DRIVER], "login": "drv", "delegate": False},
        },
    }
    r.update(kw)
    return json.dumps(r)


class Clock:
    def __init__(self, t: float = T0): self.t = t
    def __call__(self): return self.t


@pytest.fixture
def world(tmp_path, sock_dir):
    sock = os.path.join(sock_dir, "c.sock")
    server = FakeControl(sock, owner=OWNER).start()
    cfg = dataclasses.replace(CFG, socket=sock, state_path=str(tmp_path / "state.sqlite3"),
                              board=BoardCfg(enabled=True, owner="chengcli", number=8, repo=REPO))
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
    llm = lambda name, prompt: (calls.append(name), LLM[name])[1]  # noqa: E731 - an adopted round makes no LLM call
    return Driver(cfg, Client(cfg.socket_path), store, llm=llm, board=board, clock=clock, sleep=lambda s: None), gh


def drain(drv: Driver, limit: int = 10):
    for _ in range(limit):
        n, at_end = drv.run_once()
        if n == 0 and at_end: return
    raise AssertionError("feed did not settle")


def rnd(store: Store) -> dict: return json.loads(store.get_meta(f"round:{THREAD}") or "{}")
def own_posts(server: FakeControl) -> list[str]: return [m["text"] for m in server.view(THREAD)["messages"] if m["sender"] == OWNER and m["ts"] != THREAD.rsplit(":", 1)[1]]
def options(gh: FakeGh) -> dict: return {v["field"]: v["v"] for v in gh.sets(board_mod.M_SET_OPTION)}


# -- A3 (negative): a hand-off marker is required -------------------------------------------
def test_progress_with_link_and_due_but_no_marker_does_not_advance(world):
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


# -- A4: the arbiter's own echoed posts never clear a stage ---------------------------------
def test_own_echo_never_clears(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    ts = "1700000000.000400"
    server.message(THREAD, OWNER, HANDOFF, kind="report", ts=ts)
    drain(drv)
    r = rnd(store)
    assert r["seen"] == [ts] and r["stage"] == "Explore"


# -- A2: board writes are field-level, and A6: CodeAudit/Merge map onto the board's options --
def test_board_writes_are_field_level_and_never_touch_the_body(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, gh = make(cfg, store, Clock(), calls)
    server.message(THREAD, EXPLORER, HANDOFF)
    drain(drv)
    opts = options(gh)
    assert opts["F_stage"] == "O_Debate" and opts["F_role"] == "R_debater" and opts["F_status"] == "S_In Progress"
    edits = gh.argv("gh", "issue", "edit")
    assert any("--add-assignee" in a and "deb" in a for a in edits)
    assert any("--remove-assignee" in a and "exp" in a for a in edits)
    assert not any("--body" in a for a in edits)  # the issue body belongs to #49, never to the arbiter
    assert gh.sets(board_mod.M_SET_DATE)  # Started and Projected finish are written
    assert not any(a[:3] == ["gh", "issue", "comment"] for a in gh.argv("gh", "issue"))


@pytest.mark.parametrize("stage, holder, nxt, mention, stage_option, role_option", [
    ("Implement", IMPLEMENTER, "CodeAudit", AUDITOR, "O_Audit", "R_auditor"),
    ("CodeAudit", AUDITOR, "Merge", DRIVER, "O_Deliver", "R_driver"),
])
def test_code_audit_and_merge_map_onto_the_boards_stage_options(world, stage, holder, nxt, mention, stage_option, role_option):
    """The board's Stage field has no CodeAudit or Merge option (board.STAGE_OPTIONS), so they map onto Audit and Deliver.

    Slice 2: Implement clears only with a recorded PR, and CodeAudit only with the auditor's approval of its head."""
    server, cfg = world
    store, calls = adopted(cfg, round_meta(stage=stage, pr=APPROVED_PR))
    drv, gh = make(cfg, store, Clock(), calls)
    server.message(THREAD, holder, f"Handoff to {nxt}: <@{mention}> over to you.\n{LINK}\nDue 10-12 09:00 ET")
    drain(drv)
    assert rnd(store)["stage"] == nxt
    assert options(gh)["F_stage"] == stage_option and options(gh)["F_role"] == role_option


# -- AC6: no arbiter post ever asks a person to run a command -------------------------------
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
    rd = drv.load_round(THREAD)
    with pytest.raises(ValueError):
        drv.execute_round(rd, Action("delegate", "x", {"role": "explorer"}))
    assert drv.execute_round(rd, Action("notify_owner", f"{THREAD}/round/escalate", {"text": "owner-only question"})) is None
    assert own_posts(server) == [f"<@{OWNER}> owner-only question\nref: {THREAD}/round/escalate"]


# -- A5: the `ref:` restart probe and per-thread adoption -----------------------------------
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


# -- AC7: the roster validator --------------------------------------------------------------
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
    assert Store(cfg.state_file).get_meta(f"round:{THREAD}") is None


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


# -- A5, restart half: a fix post is never lost ---------------------------------------------
def crash_the_client(drv: Driver, why: str = "the process died before the send"):
    """A send that never completes, as a process dying between the round's save and the post would."""
    def boom(*a, **kw): raise RuntimeError(why)
    drv.client.post_message = boom


def fixes(server: FakeControl) -> list[str]: return [t for t in own_posts(server) if f"<@{DEBATER}>" in t]


def test_a_crash_before_the_send_does_not_lose_the_fix_post(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    crash_the_client(drv)
    server.message(THREAD, EXPLORER, HANDOFF_NO_MENTION, ts="1700000000.000600")
    with pytest.raises(RuntimeError):
        drain(drv)
    assert rnd(store)["stage"] == "Debate" and fixes(server) == []  # the stage moved, the post did not go out
    drv2, _ = make(cfg, store, Clock(), calls)  # restart over the same store and the same feed
    drain(drv2)
    assert len(fixes(server)) == 1
    assert rnd(store)["pending"] == []


def test_a_refused_fix_post_is_re_sent_on_the_next_pass(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    server.refuse_post = [(503, "daemon_unavailable")]
    server.message(THREAD, EXPLORER, HANDOFF_NO_MENTION, ts="1700000000.000700")
    drain(drv)
    assert len(fixes(server)) == 1
    assert rnd(store)["pending"] == []


def test_a_re_emitted_fix_post_does_not_post_twice(world):
    """The durable intent and the `ref:` probe together: a crash after the send must not post again."""
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    server.refuse_post = [(503, "daemon_unavailable")]
    server.message(THREAD, EXPLORER, HANDOFF_NO_MENTION, ts="1700000000.000800")
    drv.run_once()
    held = rnd(store)["pending"]
    assert len(held) == 1 and fixes(server) == []  # refused once: the intent is still on the round
    drain(drv)
    assert len(fixes(server)) == 1
    r = rnd(store)
    r["pending"] = held  # as a crash between the successful send and the clear would leave it
    store.set_meta(f"round:{THREAD}", json.dumps(r))
    drain(drv)
    assert len(fixes(server)) == 1 and rnd(store)["pending"] == []


# -- a stage whose next role has nobody in the roster ----------------------------------------
def test_clearing_into_an_unheld_role_makes_no_holder_unset_post(world):
    server, cfg = world
    roster = json.loads(round_meta())["roster"]
    roster.pop("driver")
    store, calls = adopted(cfg, round_meta(stage="CodeAudit", roster=roster, pr=APPROVED_PR))  # slice 2: an approved PR is recorded
    drv, _ = make(cfg, store, Clock(), calls)
    server.message(THREAD, AUDITOR, f"Code audit done, handoff: {LINK}\nDue 10-12 09:00 ET")
    drain(drv)
    r = rnd(store)
    assert r["stage"] == "Merge"
    assert own_posts(server) == []  # nobody to mention: a post reading "(holder unset)" helps no one
    assert len(r["findings"]) == 1 and "driver" in r["findings"][0]
    assert r["misses"] == {}  # a roster gap is not the author's miss


# -- two rounds in one channel are independent ------------------------------------------------
def test_two_rounds_in_one_channel_do_not_interfere(world):
    server, cfg = world
    other = "T1:C1:1700000000.000900"
    store, calls = adopted(cfg)
    store.set_meta(f"round:{other}", round_meta().replace(THREAD, other))
    drv, _ = make(cfg, store, Clock(), calls)
    server.message(THREAD, EXPLORER, HANDOFF)
    drain(drv)
    assert rnd(store)["stage"] == "Debate"
    assert json.loads(store.get_meta(f"round:{other}"))["stage"] == "Explore"
    assert store.load(THREAD) is None and store.load(other) is None
    assert sorted(store.rounds()) == sorted([THREAD, other])


# -- [crew_budget] parsing ---------------------------------------------------------------------
def test_crew_budget_is_parsed_and_kept_apart_from_the_projection_defaults():
    from fridica_research import arbiter, config
    cfg = config.parse('[crew_budget]\nexplore = "6h"\ncode_audit = 900\nmerge = "30m"\n\n[projection]\nexplore = "10m"\n')
    assert cfg.crew_budget["explore"] == 6 * 3600 and cfg.crew_budget["code_audit"] == 900 and cfg.crew_budget["merge"] == 1800
    assert cfg.crew_budget["implement"] == 24 * 3600 and cfg.crew_budget["debate"] == 4 * 3600  # the ruled defaults stay
    assert cfg.projection["explore"] == 600  # the AI projection is untouched by the crew budget
    assert arbiter.budget(cfg, "Explore") == 6 * 3600 and arbiter.budget(cfg, "Merge") == 1800
    assert arbiter.budget(config.Config(), "Merge") == config.Config().stage_timeout  # no ruled merge budget: the documented fallback
    with pytest.raises(ValueError):
        config.parse('[crew_budget]\naudit = "1h"\n')


# -- A3: "over to" is a hand-off only when it names a recipient --------------------------------
PROGRESS_OVER_TO = f"Moving over to section C now; draft at {LINK} ETA 10-11 14:00 ET"


def test_a_holders_own_progress_post_saying_over_to_does_not_clear(world):
    """The auditor's case: a bare "over to" in ordinary prose must not clear a stage, even from the holder."""
    server, cfg = world
    store, calls = adopted(cfg)
    drv, gh = make(cfg, store, Clock(), calls)
    server.message(THREAD, EXPLORER, PROGRESS_OVER_TO)
    drain(drv)
    r = rnd(store)
    assert r["stage"] == "Explore"
    assert own_posts(server) == []
    assert gh.sets(board_mod.M_SET_OPTION) == [] and gh.argv("gh", "issue", "edit") == []
    assert r["misses"] == {} and r["findings"] == []


@pytest.mark.parametrize("text", [
    f"<@{DEBATER}> over to you: {LINK} Due 10-11 16:00 ET",
    f"Explorer report done, over to the debater: {LINK} Due 10-11 16:00 ET",
    f"Handing off to <@{DEBATER}>: {LINK} Due 10-11 16:00 ET",
], ids=["over_to_you", "over_to_the_role", "handing_off"])
def test_the_recipient_forms_of_a_handoff_still_clear(world, text):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    server.message(THREAD, EXPLORER, text)
    drain(drv)
    assert rnd(store)["stage"] == "Debate"


# -- a hand-off with a due time but no link ----------------------------------------------------
def test_handoff_without_a_link_does_not_clear_and_is_recorded(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, gh = make(cfg, store, Clock(), calls)
    server.message(THREAD, EXPLORER, f"Stage: Explore handoff to the debater.\n<@{DEBATER}> over to you.\nDue 10-11 16:00 ET")
    drain(drv)
    r = rnd(store)
    assert r["stage"] == "Explore"
    assert len(r["findings"]) == 1 and "link" in r["findings"][0]
    assert own_posts(server) == [] and gh.sets(board_mod.M_SET_OPTION) == []


# -- checks that lived in the duplicates of the canonical six ----------------------------------
def test_an_adopted_round_makes_no_llm_call(world):
    server, cfg = world
    store, calls = adopted(cfg)
    clock = Clock()
    drv, _ = make(cfg, store, clock, calls)
    server.message(THREAD, EXPLORER, HANDOFF)
    server.message(THREAD, DEBATER, "picking it up", kind="reply")
    drain(drv)
    clock.t = T0 + 24 * HOUR
    drv.run_once()
    assert calls == []
    assert [p for m, p, _ in server.requests if m == "POST" and (p.endswith("/delegate") or p.endswith("/driver"))] == []


def test_a_clean_handoff_produces_no_post(world):
    """Provision 03.1: the hand-off's own mention is the ping, so the arbiter stays silent."""
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    server.message(THREAD, EXPLORER, HANDOFF)
    drain(drv)
    assert rnd(store)["stage"] == "Debate"
    assert own_posts(server) == [] and rnd(store)["misses"] == {}


def test_a_counted_miss_is_fixed_and_then_the_stage_advances(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    server.message(THREAD, EXPLORER, HANDOFF_NO_MENTION)
    drain(drv)
    r = rnd(store)
    assert r["misses"] == {f"Explore/{EXPLORER}": 1}
    assert len(fixes(server)) == 1
    assert r["stage"] == "Debate" and r["clearance"]["Explore"]["mention"] is False


def test_the_finding_names_the_non_holder(world):
    server, cfg = world
    store, calls = adopted(cfg)
    drv, _ = make(cfg, store, Clock(), calls)
    server.message(THREAD, STRANGER, HANDOFF)
    drain(drv)
    r = rnd(store)
    assert r["stage"] == "Explore" and len(r["findings"]) == 1
    assert STRANGER in r["findings"][0] and "explorer" in r["findings"][0]

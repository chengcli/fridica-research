"""R7: replay this bootstrap study's stage sequence (explore -> claim -> debate, 2 rounds both revised ->
implement -> audit -> deliver) through the machine; assert the stage log and the board card sequence
(fridica-research issues #1 parent, #2-#7 stages, as on https://github.com/users/chengcli/projects/8)."""
from __future__ import annotations

import dataclasses
import json

from fridica_research import board
from fridica_research.config import Board as BoardCfg
from fridica_research.config import Config, Reviewer
from support import OWNER, PR, SHA, World, report, result
from test_board import FakeGh

BOOTSTRAP = Config(owner=OWNER, channels=("C0C2D3PCW20",), max_iterations=3, max_debate_rounds=2, settle_window=60, stage_timeout=4 * 3600,
                   reviewers=(Reviewer("U_A", "numerics", "a"), Reviewer("U_B", "scope", "b"), Reviewer("U_C", "api", "c")), people={OWNER: "chengcli"},
                   require_signoffs=True, board=BoardCfg(enabled=True, owner="chengcli", number=8, repo="chengcli/fridica-research"))
THREAD = "TJ6E2EJ2K:C0C2D3PCW20:1791125606.982449"
EXPLORE_REPORT = "Explore: control API, events, conventions.\n\n## Approaches\n- pure-machine-sqlite: pure step + SQLite snapshot -- the issue's design\n- notes-store: state in fridica thread notes -- one store\n- event-sourced-rebuild: replay the feed -- no local state\n"
STAGE_MIN = {"Explore": 6, "Claim": 1, "Debate": 8, "Implement": 90, "Audit": 90, "Deliver": 10}  # from the bootstrap stage log (EDT 10:53 ... )


def tape(w: World, sync=lambda: None):
    """Drive the bootstrap study's events through `w` (the stage sequence and timings of the real run); `sync` runs where the driver would sync the board."""
    w.start(thread=THREAD, problem="Bootstrap: implement fridica-research (fridica #126)")
    sync()
    w.now += STAGE_MIN["Explore"] * 60
    w.finish("explorer", result(report=EXPLORE_REPORT, summary="three package shapes"))
    sync()
    assert w.state.claim["slug"] == "pure-machine-sqlite"
    w.now += STAGE_MIN["Claim"] * 60
    w.tick(60)  # settle: no peer claimed
    sync()
    assert w.state.stage == "Debate" and w.state.round == 1
    # Round 1: both revised; round 2: both revised (max reached) -> synthesis.
    w.finish("mathematician", result(report=report(position="revised", body="state space, transition table, invariants")))
    w.finish("physicist", result(report=report(position="revised", body="scales, balances, five tests")))
    assert w.state.round == 2 and [a["lens"] for a in w.kinds("delegate")[-2:]] == ["mathematician", "physicist"]
    w.now += STAGE_MIN["Debate"] * 60
    w.finish("mathematician", result(report=report(position="revised", body="concede snapshot store; keep waiting ledger")))
    w.finish("physicist", result(report=report(position="revised", body="concede snapshot; hold no log")))
    sync()
    assert w.state.stage == "Implement" and w.state.synthesis["synthesis"]
    w.now += STAGE_MIN["Implement"] * 60
    w.finish("implementer", result(status="done", artifacts=["https://github.com/chengcli/fridica-research/pull/2"], machine_state={"branch": "study/126-bootstrap", "commit": "deadbeef", "dirty": False}))
    sync()
    assert w.state.stage == "Audit"
    req = [p for p in w.kinds("post") if p["post_kind"] == "report"][-1]["text"]
    assert "<@U_A> (numerics)" in req and "<@U_B> (scope)" in req and "<@U_C> (api)" in req and "sha: deadbeef" in req
    assert w.state.phase == "signoff" and "auditor" not in w.workers  # every scope has a peer: no local auditor worker (R13)
    assert sorted(w.state.audit_scopes) == ["api", "numerics", "scope"]
    sync()
    w.now += 20 * 60
    w.ev("sign_off", sender="U_A", pr="https://github.com/chengcli/fridica-research/pull/2", sha="deadbeef", verdict="approve")
    sync()
    assert w.state.stage == "Audit"  # waits for the other two
    w.now += (STAGE_MIN["Audit"] - 20) * 60
    w.ev("sign_off", sender="U_B", pr="https://github.com/chengcli/fridica-research/pull/2", sha="deadbeef", verdict="approve")
    w.ev("sign_off", sender="U_C", pr="https://github.com/chengcli/fridica-research/pull/2", sha="deadbeef", verdict="approve")
    assert w.state.stage == "Delivered"  # deliver LLM call and posts follow at once
    sync()


def test_bootstrap_tape():
    w = World(cfg=BOOTSTRAP, now=1791125606.0)
    gh = FakeGh()
    meta = {}
    b = board.Board(BOOTSTRAP, runner=gh, remember=meta.__setitem__, recall=meta.get)
    tape(w, lambda: b.sync(w.state))
    # Stage log: one row per stage, in order, each closed, projected from config, actual from the clock.
    assert [r["stage"] for r in w.state.stage_log] == ["Explore", "Claim", "Debate", "Implement", "Audit", "Deliver"]
    assert all(r["end"] is not None and r["actual"] >= 0 for r in w.state.stage_log)
    assert [r["projected"] for r in w.state.stage_log] == [600, 120, 1200, 5400, 5400, 600]
    assert round(w.state.stage_log[2]["actual"] / 60) == STAGE_MIN["Debate"] and round(w.state.stage_log[3]["actual"] / 60) == STAGE_MIN["Implement"]
    # Delivery post: projected vs actual (R4), PR, sha, audit verdict.
    res = [p for p in w.kinds("post") if p["post_kind"] == "study_result"][-1]["text"]
    assert "projected: 4 h, actual: 3.27 h" in res and "pr: https://github.com/chengcli/fridica-research/pull/2" in res and "audit: pass" in res
    # Three LLM calls, six delegates (explorer, 2x2 debate, implementer, auditor).
    assert [a["name"] for a in w.kinds("llm_call")] == ["study_brief", "study_synthesis", "study_deliver"]
    assert [a["role"] for a in w.kinds("delegate")] == ["explorer", "debater", "debater", "debater", "debater", "implementer"]
    # Board (R9, R13): #1 is the study; #2-#5 Explore..Implement; #6-#8 one audit card per reviewer; #9 Deliver. Plain issues, one assignee each.
    cards = json.loads(meta[f"board:{THREAD}"])
    assert cards["issue"] == 1 and [[c["issue"] for c in st["cards"]] for st in cards["stages"]] == [[2], [3], [4], [5], [6, 7, 8], [9]] and cards["closed"]
    creates = gh.argv("gh", "issue", "create")
    assert creates[0][6] == "Bootstrap: implement fridica-research (fridica #126)"
    assert [a[6].split(":")[0] for a in creates[1:]] == ["Explore (iteration 1)", "Claim (iteration 1)", "Debate (iteration 1)", "Implement (iteration 1)", "Audit numerics (iteration 1)", "Audit scope (iteration 1)", "Audit api (iteration 1)", "Deliver (iteration 1)"]
    assert [a[a.index("--assignee") + 1] for a in creates] == ["chengcli", "chengcli", "chengcli", "chengcli", "chengcli", "a", "b", "c", "chengcli"]
    assert all(a[8].startswith("Study: #1\n") for a in creates[1:]) and not any("addSubIssue" in (d or {}).get("query", "") for _, d in gh.calls)
    assert [a[3] for a in gh.argv("gh", "issue", "close")] == ["2", "3", "4", "5", "6", "7", "8", "9", "1"]  # #6 on U_A's sign-off, #7/#8 on theirs, then Deliver and the study
    roles = {v["item"]: v["v"] for v in gh.sets(board.M_SET_OPTION) if v["field"] == "F_role"}
    assert roles == {"PVTI_I_1": "R_driver", "PVTI_I_2": "R_explorer", "PVTI_I_3": "R_driver", "PVTI_I_4": "R_debater", "PVTI_I_5": "R_implementer", "PVTI_I_6": "R_peer-reviewer", "PVTI_I_7": "R_peer-reviewer", "PVTI_I_8": "R_peer-reviewer", "PVTI_I_9": "R_driver"}
    status = [(v["item"], v["v"]) for v in gh.sets(board.M_SET_OPTION) if v["field"] == "F_status"]
    assert status[:2] == [("PVTI_I_1", "S_In Progress"), ("PVTI_I_2", "S_In Progress")] and status[-1] == ("PVTI_I_1", "S_Done")  # R14
    assert [i for i, v in status if v == "S_Done"] == [f"PVTI_I_{n}" for n in (2, 3, 4, 5, 6, 7, 8, 9, 1)]
    assert any(v["item"] == "PVTI_I_1" and v["field"] == "F_Peer_reviewers" and v["v"] == "a, b, c" for v in gh.sets(board.M_SET_TEXT))
    stage_opts = [v["v"] for v in gh.sets(board.M_SET_OPTION) if v["item"] == "PVTI_I_1" and v["field"] == "F_stage"]
    assert stage_opts == ["O_Explore", "O_Explore", "O_Claim", "O_Debate", "O_Implement", "O_Audit", "O_Audit", "O_Audit", "O_Delivered"]  # set at creation, then per sync
    # R12: per-role hours for the study; the peer reviewers' actual time is their sign-off latency.
    totals = board.role_totals(w.state)
    assert totals["peer-reviewer"] == {"projected": 4.5, "actual": round((20 + 90 + 90) / 60, 2)} and "auditor" not in totals
    assert totals["implementer"] == {"projected": 1.5, "actual": 1.5}


def test_bootstrap_tape_with_signoffs_required_waits_then_delivers():
    cfg = dataclasses.replace(BOOTSTRAP, require_signoffs=True, board=BoardCfg(), audit_scopes=("numerics", "scope", "api", "docs"))
    w = World(cfg=cfg)
    w.to_audit()
    assert w.state.audit_scopes["docs"]["reviewer"] is None and "docs" in w.kinds("delegate")[-1]["brief"]  # the uncovered scope goes to the local auditor
    w.finish("auditor", result(report=report(verdict="pass")))
    assert w.state.stage == "Audit" and w.state.phase == "signoff"
    w.ev("sign_off", sender="U_A", pr=PR, sha=SHA, verdict="approve")
    w.ev("sign_off", sender="U_B", pr="#9", sha="abc1234", verdict="approve")  # `#N` names the same PR
    assert w.state.stage == "Audit"
    w.tick(cfg.stage_timeout)  # U_C never answers: deliver with the missing sign-off listed
    assert w.state.stage == "Delivered" and w.state.audit["signoffs_missing"] == ["U_C"]
    assert "sign-offs missing: U_C" in [p for p in w.kinds("post") if p["post_kind"] == "study_result"][-1]["text"]

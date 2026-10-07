"""Regenerate the replay corpora under tests/bootstrap (R19): `python tests/record_corpora.py [name ...]`.

Each scenario drives a `World` (tests/support.py) and records its config, start arguments and
event tape; the expected actions and final state are the fold of those events through the
machine on this revision (`fridica_research.replay.record`), never hand-written.
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fridica_research import replay  # noqa: E402
from fridica_research.config import Reviewer  # noqa: E402
from support import CFG, EXPLORER_REPORT, PEER, PR, REV, SHA, World, report, result  # noqa: E402
from test_bootstrap_tape import BOOTSTRAP, tape  # noqa: E402

ROOT = Path(__file__).parent / "bootstrap"
TWO_PEERS = dataclasses.replace(CFG, reviewers=(Reviewer(REV, "scope"), Reviewer("UREV2", "code")), require_signoffs=True)
CLAIM_ALPHA = "Claim (iteration 1): x\napproach: alpha\nwhy: w\nalso considered: none"


def finish_study(w: World, positions=(("agree", "agree"),)):
    """From Explore (after a start or an iteration rollover) to Delivered with a local auditor pass."""
    w.finish("explorer", result(report=EXPLORER_REPORT))
    w.tick(w.cfg.settle_window)
    for pm, pp in positions:
        w.finish("mathematician", result(report=report(position=pm)))
        w.finish("physicist", result(report=report(position=pp)))
        if w.state.stage != "Debate" or w.state.phase != "job": break
    if w.state.stage == "DesignAudit": w.finish("auditor", result(report=report(verdict="pass")))
    w.finish("implementer", result(artifacts=[PR]))
    if "auditor" in w.pending: w.finish("auditor", result(report=report(verdict="pass")))


def sign_both(w: World):
    for who in (REV, "UREV2"): w.ev("sign_off", sender=who, pr=PR, sha=SHA, verdict="approve")


def bootstrap():
    w = World(cfg=BOOTSTRAP, now=1791125606.0)
    tape(w)
    return w


def simple_research():
    w = World()
    w.start()
    finish_study(w)
    return w


def debate_disagreement():
    w = World()
    w.start()
    finish_study(w, positions=(("disagree", "disagree"), ("disagree", "revised")))
    return w


def auditor_return():
    w = World()
    w.to_delivered(verdict="return")
    finish_study(w)
    return w


def worker_failure():
    w = World()
    w.to_debate()
    w.finish("mathematician", result(), job_status="failed", code="backend_crash")
    w.finish("mathematician", result(report=report(position="agree")))
    w.finish("physicist", result(report=report(position="agree")))
    if w.state.stage == "DesignAudit": w.finish("auditor", result(report=report(verdict="pass")))
    w.finish("implementer", result(artifacts=[PR]))
    w.finish("auditor", result(report=report(verdict="pass")))
    return w


def timeout_retry():
    w = World()
    w.start()
    w.tick(w.cfg.stage_timeout)
    finish_study(w)
    return w


def peer_claim_conflict():
    w = World(hold=("study_claim",))
    w.to_claim()
    w.ev("peer_post", ts="1700000000.000700", sender=PEER, kind="study_claim", text=CLAIM_ALPHA)
    claims = [p for p in w.kinds("post") if p["post_kind"] == "study_claim"]
    w.ev("own_post_seen", ts="1700000000.000800", kind="study_claim", text=claims[0]["text"])  # later than the peer: lost, re-pick beta
    claims = [p for p in w.kinds("post") if p["post_kind"] == "study_claim"]
    w.ev("own_post_seen", ts="1700000000.000900", kind="study_claim", text=claims[1]["text"])
    w.tick(w.cfg.settle_window)
    w.finish("mathematician", result(report=report(position="agree")))
    w.finish("physicist", result(report=report(position="agree")))
    if w.state.stage == "DesignAudit": w.finish("auditor", result(report=report(verdict="pass")))
    w.finish("implementer", result(artifacts=[PR]))
    w.finish("auditor", result(report=report(verdict="pass")))
    return w


def partial_delivery():
    w = World(cfg=dataclasses.replace(CFG, max_iterations=1))
    w.to_delivered(verdict="return")
    return w


def changes_then_timeout():
    """T1: a `changes` sign-off, then the other reviewer times out: Audit returns the study (iteration 2), then both approve."""
    w = World(cfg=TWO_PEERS)
    w.to_audit()
    w.ev("sign_off", sender=REV, pr=PR, sha=SHA, verdict="changes")
    w.tick(w.cfg.stage_timeout)
    finish_study(w)
    sign_both(w)
    return w


def refused_request(w: World):
    """The reviewer request is refused before any echo of it reaches the feed: the peers were never asked."""
    req = [p for p in w.kinds("post") if p["post_kind"] == "report"][-1]
    w.ev("post_refused", action_id=req.id, post_kind="report", code="rate_limited", outcome="rejected")


def refused_reviewer():
    """T2: the reviewer request post is refused twice, never echoed: the scopes stay unrequested and the stage Blocks (rule R), no pass on timeout."""
    w = World(cfg=TWO_PEERS, hold=("report",))
    w.to_audit()
    refused_request(w)
    refused_request(w)
    w.tick(w.cfg.stage_timeout)
    return w


def refused_reviewer_retry():
    """T2: the refused request is re-posted by rule R, the re-post is echoed, then both reviewers approve."""
    w = World(cfg=TWO_PEERS, hold=("report",))
    w.to_audit()
    refused_request(w)
    req = [p for p in w.kinds("post") if p["post_kind"] == "report"][-1]
    w.ev("own_post_seen", ts=w.next_ts(), kind="report", text=req["text"])
    sign_both(w)
    return w


def debate_adds_lens():
    w = World()
    w.to_debate()
    proposal = "## Lenses\n### mathematician\n# Mathematician\nCheck invariants.\n### physicist\n# Physicist\nCheck balances.\n### engineer\n# Engineer\nCheck failure paths.\n"
    w.finish("mathematician", result(report=report(position="revised", body=proposal)))
    w.finish("physicist", result(report=report(position="revised")))
    for lane in ("engineer", "mathematician", "physicist"):
        w.finish(lane, result(report=report(position="agree")))
    w.finish("auditor", result(report=report(verdict="pass")))
    w.finish("implementer", result(artifacts=[PR]))
    w.finish("auditor", result(report=report(verdict="pass")))
    return w


def final_round_evidence():
    w = World(cfg=dataclasses.replace(CFG, max_debate_rounds=1))
    w.to_debate()
    w.finish("mathematician", result(report=report(position="revised", body="## Evidence request\nquestion: measure scaling\nsource: repo\nexperiment: probe")))
    w.finish("physicist", result(report=report(position="agree")))
    w.finish("explorer", result(report="FINAL_ROUND_EVIDENCE_41"))
    w.finish("auditor", result(report=report(verdict="pass")))
    w.finish("implementer", result(artifacts=[PR]))
    w.finish("auditor", result(report=report(verdict="pass")))
    return w


SCENARIOS = {
    "000_bootstrap": bootstrap, "001_simple_research": simple_research, "002_debate_disagreement": debate_disagreement, "003_auditor_return": auditor_return,
    "004_worker_failure": worker_failure, "005_timeout_retry": timeout_retry, "006_peer_claim_conflict": peer_claim_conflict, "007_partial_delivery": partial_delivery,
    "008_changes_then_timeout": changes_then_timeout, "009_refused_reviewer": refused_reviewer, "010_refused_reviewer_retry": refused_reviewer_retry, "011_debate_adds_lens": debate_adds_lens, "012_final_round_evidence": final_round_evidence,
}


def start_of(w: World) -> dict:
    s = w.state
    return {"thread": s.thread, "channel": s.channel, "problem": s.problem, "now": s.started_at, "projected_hours": s.projected_hours, "generation": s.generation, "lineage": s.lineage, "spawner": s.spawner}


def record(name: str, root: Path = ROOT) -> Path:
    w = SCENARIOS[name]()
    return replay.record(root / name, w.cfg, start_of(w), w.events)


def main(argv: list[str]) -> int:
    for name in argv or sorted(SCENARIOS):
        print(record(name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

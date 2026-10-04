"""Table-driven tests of the stage table (issue #126) plus the mathematician's edge rows."""
from __future__ import annotations

import dataclasses

import pytest

from fridica_research import machine
from fridica_research.config import Reviewer
from fridica_research.machine import Event, ts_key
from support import CFG, EXPLORER_REPORT, PEER, PR, REV, SHA, THREAD, World, report, result


def test_explore_started_calls_study_brief_then_delegates_explorer():
    w = World()
    w.start()
    assert [a.kind for a in w.actions[:4]] == ["set_driver", "board_update", "arm_timer", "llm_call"]  # the overrun timer, then the brief
    assert w.kinds("llm_call")[0]["name"] == "study_brief"
    d = w.kinds("delegate")[0]
    assert d["role"] == "explorer" and d["ephemeral"] is True and "ref: " + d.id in d["brief"]
    assert w.state.stage == "Explore" and w.state.phase == "job" and [a.id.rsplit("/", 1)[1] for a in w.kinds("arm_timer")] == ["overrun", "timer"]


def test_explore_results_parse_approaches_and_post_claim():
    w = World()
    w.to_claim()
    assert [a["slug"] for a in w.state.approaches] == ["alpha", "beta", "gamma"]
    post = w.kinds("post")[0]
    assert post["post_kind"] == "study_claim"
    assert "Claim (iteration 1): Alpha design" in post["text"] and "approach: alpha" in post["text"] and f"ref: {post.id}" in post["text"]
    assert w.state.stage == "Claim" and w.state.claim["status"] == "settling"  # own echo auto-fed by World


def test_claim_pending_until_own_post_seen():
    w = World(auto_post=False)
    w.to_claim()
    assert w.state.claim["status"] == "pending" and w.state.claim["ts"] is None
    post = w.kinds("post")[0]
    w.ev("own_post_seen", ts="1700000000.000500", kind="study_claim", text=post["text"])
    assert w.state.claim == {"status": "settling", "slug": "alpha", "ts": "1700000000.000500"}
    assert w.state.waiting["kind"] == "timer"


def test_claim_settles_into_debate_with_persistent_pair():
    w = World()
    w.to_debate()
    assert w.state.stage == "Debate" and w.state.round == 1 and w.state.claim["status"] == "owned"
    roles = [a["role"] for a in w.kinds("delegate")]
    assert roles == ["explorer", "debater", "debater"]
    assert all(a["ephemeral"] is False for a in w.kinds("delegate")[1:])
    for a in w.kinds("delegate")[1:]:
        assert "# Debater" in a["instructions"]
        assert f"# {a['lens'].title()}" in a["instructions"]
        assert "needs contract decision" in a["instructions"]


def test_auditor_brief_contains_lower_layer_charter():
    w = World()
    w.to_audit()
    audit = [a for a in w.kinds("delegate") if a["role"] == "auditor"][0]
    assert "fridica-core: general mechanism only" in audit["brief"]
    assert "### Layer boundaries" in audit["instructions"]


def test_claim_lost_to_earlier_peer_repicks():
    w = World()
    w.to_claim()
    w.ev("peer_post", ts="1700000000.000050", sender=PEER, kind="study_claim", text="Claim (iteration 1): x\napproach: alpha\nwhy: w\nalso considered: none")
    assert w.state.excluded == ["alpha"] and w.state.claim["slug"] == "beta"
    assert len([p for p in w.kinds("post") if p["post_kind"] == "study_claim"]) == 2
    assert not w.kinds("delegate")[1:]  # nothing delegated between the two claims


def test_claim_wins_against_later_peer():
    w = World()
    w.to_claim()
    w.ev("peer_post", ts="1700000000.999999", sender=PEER, kind="study_claim", text="Claim (iteration 1): x\napproach: alpha\nwhy: w\nalso considered: none")
    assert w.state.claim["slug"] == "alpha" and w.state.claim["status"] == "settling"


def test_all_approaches_claimed_blocks_with_notice():
    w = World(auto_post=False)
    w.start()
    for slug in ("alpha", "beta", "gamma"):
        w.ev("peer_post", ts="1700000000.000001", sender=PEER, kind="study_claim", text=f"Claim (iteration 1): x\napproach: {slug}\nwhy: w\nalso considered: none")
    w.finish("explorer", result(report=EXPLORER_REPORT))
    assert w.state.stage == "Blocked" and w.state.blocked_from == "Claim" and w.kinds("notify_owner")


def test_debate_rounds_until_agree_or_max():
    w = World()
    w.to_debate()
    w.finish("mathematician", result(report=report(position="revised")))
    assert w.state.round == 1 and w.state.phase == "job"  # one of the pair: no transition
    w.finish("physicist", result(report=report(position="disagree")))
    assert w.state.round == 2
    d = w.kinds("delegate")
    assert d[-1].get("worker_id") == w.workers["physicist"]  # resumed, same ids
    assert "previous round" in d[-1]["brief"]
    w.finish("mathematician", result(report=report(position="disagree")))
    w.finish("physicist", result(report=report(position="disagree")))
    assert w.state.stage == "Implement"  # r >= max -> synthesis -> implement
    assert w.kinds("llm_call")[-1]["name"] == "study_synthesis"
    assert sorted(a["worker_id"] for a in w.kinds("stop_worker")) == sorted(w.workers[r] for r in ("mathematician", "physicist"))


def test_debate_both_agree_converges_early():
    w = World()
    w.to_implement(positions=(("agree", "agree"),))
    assert w.state.stage == "Implement" and w.state.round == 1


def test_missing_stance_is_disagree():
    w = World()
    w.to_debate()
    w.finish("mathematician", result(report="no block"))
    w.finish("physicist", result(report=report(position="agree")))
    assert w.state.round == 2 and w.state.reports["mathematician"]["stance"] == "disagree"


def test_max_debate_rounds_zero_skips_debate():
    w = World(cfg=dataclasses.replace(CFG, max_debate_rounds=0))
    w.to_debate()
    assert w.state.stage == "Implement" and w.state.round == 0
    assert [a["role"] for a in w.kinds("delegate")] == ["explorer", "implementer"]
    assert not w.kinds("stop_worker")
    assert "no debate rounds" in w.kinds("llm_call")[1]["prompt"]


def test_implement_done_goes_to_audit_with_pr_and_sha():
    w = World()
    w.to_audit()
    assert w.state.stage == "Audit" and w.state.implementer["pr"].endswith("/pull/9") and w.state.implementer["sha"] == "abc1234"
    d = w.kinds("delegate")[-1]
    assert d["role"] == "auditor" and d["ephemeral"] and d["backend"] == "other"
    req = [p for p in w.kinds("post") if p["post_kind"] == "report"][-1]
    assert "<@UREV> (scope)" in req["text"] and "sha: abc1234" in req["text"] and "SIGN-OFF" in req["text"]


def test_implement_failed_goes_to_next_iteration():
    w = World()
    w.to_implement()
    w.finish("implementer", result(status="failed", unresolved=["cannot build"]))
    assert w.state.stage == "Explore" and w.state.iteration == 2 and "cannot build" in w.state.findings[0]
    assert w.kinds("llm_call")[-1]["name"] == "study_brief" and "cannot build" in w.kinds("llm_call")[-1]["prompt"]


def test_audit_pass_delivers_and_spawns_followon():
    w = World()
    w.to_delivered()
    assert w.state.stage == "Delivered" and w.state.finished_at
    kinds = [p["post_kind"] for p in w.kinds("post")]
    assert kinds[-2:] == ["study_result", "study_root"]
    res = w.kinds("post")[-2]["text"]
    assert "Stage: Deliver (iteration 1)" in res and "projected: 4 h, actual:" in res and "| Explore |" in res
    root = w.kinds("post")[-1]["text"]
    assert "generation: 2" in root and f"lineage: {THREAD}" in root and "projected: 4 h" in root
    assert w.state.followon.startswith("C1:")
    assert [a["worker_id"] for a in w.kinds("stop_worker")][-1] == w.workers["implementer"]


def test_audit_return_next_iteration_resumes_implementer():
    w = World()
    w.to_delivered(verdict="return")
    assert w.state.stage == "Explore" and w.state.iteration == 2
    w.finish("explorer", result(report=EXPLORER_REPORT))
    w.tick(60)
    w.finish("mathematician", result(report=report(position="agree")))
    w.finish("physicist", result(report=report(position="agree")))
    impls = [a for a in w.kinds("delegate") if a["role"] == "implementer"]
    assert len(impls) == 2 and impls[1].get("worker_id") == w.workers["implementer"] and "supersedes" in impls[1]["brief"]


def test_audit_return_at_max_iterations_delivers_partial():
    w = World(cfg=dataclasses.replace(CFG, max_iterations=1))
    w.to_delivered(verdict="return")
    assert w.state.stage == "Delivered" and w.state.partial
    assert "Stage: Deliver (iteration 1) partial" in w.kinds("post")[-2]["text"]


def test_audit_reject_stops():
    w = World()
    w.to_delivered(verdict="reject")
    assert w.state.stage == "Stopped"
    assert any("reject" in p["text"] for p in w.kinds("post") if p["post_kind"] == "report")
    assert w.kinds("notify_owner")


def test_missing_verdict_is_return():
    w = World()
    w.to_audit()
    w.finish("auditor", result(report="nothing"))
    assert w.state.iteration == 2 and w.state.audit["verdict"] == "return"


def test_owner_stop_from_any_stage():
    w = World()
    w.to_implement()
    w.ev("owner_stop")
    assert w.state.stage == "Stopped" and w.state.timers == {} and w.state.waiting is None
    assert w.kinds("stop_worker")  # implementer stopped
    before = len(w.actions)
    w.finish("implementer")  # late result: recorded, no transition
    assert w.state.stage == "Stopped" and len(w.actions) == before


def test_timeout_retries_then_blocks():
    w = World()
    w.start()
    w.tick(CFG.stage_timeout)
    assert w.state.attempt == 2 and w.state.stage == "Explore"
    assert len(w.kinds("llm_call")) == 2
    w.tick(CFG.stage_timeout)
    assert w.state.stage == "Blocked" and w.state.blocked_from == "Explore" and w.kinds("notify_owner")
    w.ev("owner_resume")
    assert w.state.stage == "Explore" and w.state.attempt == 1 and len(w.kinds("llm_call")) == 3


def test_job_failed_retries_stage_with_resumed_workers():
    w = World()
    w.to_debate()
    w.finish("mathematician", result(), job_status="failed", code="backend_crash")
    assert w.state.attempt == 2 and w.state.stage == "Debate" and w.state.round == 2
    d = w.kinds("delegate")
    assert d[-1]["worker_id"] == w.workers["physicist"] and [a["worker_id"] for a in w.kinds("stop_worker")] == [w.workers["physicist"]]


def test_explorer_without_approaches_is_a_failure():
    w = World()
    w.start()
    w.finish("explorer", result(report="nothing here"))
    assert w.state.attempt == 2 and w.state.stage == "Explore"


def test_stray_events_are_ignored():
    w = World()
    w.to_debate()
    before = (w.state.to_dict(), len(w.actions))
    w.ev("timeout", timer_id="stale/timer")
    w.ev("llm_result", action_id="stale", ok=True, payload={})
    w.ev("own_post_seen", ts="1", kind="report", text="ref: nope")
    w.ev("job_result", join_group="old", job_id="job-0", worker_id="x", role="explorer", attempt=1, job_status="finished", result=result())
    w.ev("peer_post", ts="1", sender=PEER, kind="progress", text="Claim (iteration 1): y\napproach: alpha")
    assert (w.state.to_dict(), len(w.actions)) == before


def test_duplicate_job_result_is_ignored():
    w = World()
    w.to_debate()
    jid, wid = w.pending["mathematician"], w.workers["mathematician"]
    w.finish("mathematician", result(report=report(position="agree")))
    before = len(w.actions)
    w.ev("job_result", join_group="grp", job_id=jid, worker_id=wid, role="mathematician", attempt=1, job_status="finished", result=result())
    assert len(w.actions) == before


def test_control_paused_blocks_and_resume_reenters():
    w = World()
    w.to_implement()
    w.ev("control_changed", control="paused")
    assert w.state.stage == "Blocked" and w.state.blocked_from == "Implement"
    w.ev("owner_resume")
    assert w.state.stage == "Blocked"  # still paused on fridica's side
    w.ev("control_changed", control="active")
    w.ev("owner_resume")
    assert w.state.stage == "Implement" and w.state.attempt == 1


def test_late_contested_claim_notifies_but_continues():
    w = World()
    w.to_implement()
    w.ev("peer_post", ts="1700000000.000001", sender=PEER, kind="study_claim", text="Claim (iteration 1): x\napproach: alpha\nwhy: w\nalso considered: none")
    assert w.state.stage == "Implement" and w.state.contested and "contested" in w.kinds("notify_owner")[-1]["text"]


def test_no_followon_when_not_spawner_or_at_max_generation():
    w = World()
    w.start(generation=CFG.max_generations, spawner=False)
    w.finish("explorer", result(report=EXPLORER_REPORT))
    w.tick(60)
    w.finish("mathematician", result(report=report(position="agree")))
    w.finish("physicist", result(report=report(position="agree")))
    w.finish("implementer")
    w.finish("auditor", result(report=report(verdict="pass")))
    assert w.state.stage == "Delivered" and [p["post_kind"] for p in w.kinds("post")][-1] == "study_result"


def test_action_ids_are_unique_and_carry_refs():
    w = World()
    w.to_delivered()
    ids = [a.id for a in w.actions if a.kind in ("delegate", "post", "llm_call")]
    assert len(ids) == len(set(ids))
    for a in w.actions:
        if a.kind == "delegate": assert f"ref: {a.id}" in a["brief"]
        if a.kind == "post": assert f"ref: {a.id}" in a["text"]


def test_llm_calls_per_iteration_and_delegate_bound():
    w = World()
    w.to_delivered()
    assert len(w.kinds("llm_call")) == 3
    assert len(w.kinds("delegate")) <= 2 + 2 * CFG.max_debate_rounds + 2


@pytest.mark.parametrize("stage", ["Explore", "Claim", "Debate", "Implement", "Audit", "Deliver"])
def test_each_stage_logs_one_row(stage):
    w = World()
    w.to_delivered()
    assert w.stages().count(stage) == 1
    assert all(r["end"] is not None for r in w.state.stage_log)


def test_step_does_not_mutate_input():
    w = World()
    s = w.to_claim()
    snap = s.to_dict()
    machine.step(s, Event("peer_post", 0, {"ts": "1700000000.000001", "sender": PEER, "kind": "study_claim", "text": "Claim (iteration 1): x\napproach: alpha\nwhy: w\nalso considered: none"}), CFG)
    assert s.to_dict() == snap


def test_finding_event_is_recorded_not_injected():
    """R12: a change that arrives mid-stage becomes a finding; the running worker's brief is untouched."""
    w = World()
    w.to_implement()
    brief_before = w.kinds("delegate")[-1]["brief"]
    n = len(w.actions)
    w.ev("finding", text="R99: also support Y")
    assert w.state.findings == ["iteration 1 note during Implement: R99: also support Y"] and len(w.actions) == n
    assert w.kinds("delegate")[-1]["brief"] == brief_before
    w.finish("implementer")
    assert "R99: also support Y" in w.kinds("delegate")[-1]["brief"]  # the auditor checks against it
    w.finish("auditor", result(report=report(verdict="return")))
    assert "R99" in w.kinds("llm_call")[-1]["prompt"]  # and the next iteration's brief carries it


def test_overrun_at_2x_projected_interrupts_the_stage():
    """R12: a job stage past 2x its projected time is interrupted; its partial result is a finding; the loop moves on."""
    w = World(cfg=dataclasses.replace(CFG, stage_timeout=5000))  # the overrun (2400 s) comes before the stage timer
    w.to_debate()
    w.finish("mathematician", result(summary="half done", report=report(position="agree")))
    w.tick(2 * CFG.projection["debate"])
    assert w.state.iteration == 2 and w.state.stage == "Explore"
    assert "Debate interrupted after 40 min (2x the projected 20 min); partial result: mathematician: half done" in w.state.findings[0]
    assert [a["worker_id"] for a in w.kinds("stop_worker")] == [w.workers["mathematician"], w.workers["physicist"]]
    assert not any(t.endswith("/i1/Debate/overrun") for t in w.state.timers) and any(t.endswith("/i2/Explore/overrun") for t in w.state.timers)


def test_overrun_timer_survives_a_retry():
    w = World()
    w.start()
    w.tick(CFG.stage_timeout)  # attempt 2 at t=1000; the overrun (t=2400) is still armed once
    assert w.state.attempt == 2 and [t for t in w.state.timers if t.endswith("overrun")] == [f"{THREAD}/g1/i1/Explore/overrun"]
    w.tick(CFG.stage_timeout)  # t=2000: timer -> Blocked (overrun not yet due)
    assert w.state.stage == "Blocked"


def test_audit_scopes_without_local_auditor_and_peer_changes():
    """R13: every scope taken by a peer -> no auditor worker; a `changes` sign-off returns the study."""
    cfg = dataclasses.replace(CFG, audit_scopes=("scope",), require_signoffs=True)
    w = World(cfg=cfg)
    w.to_audit()
    assert [a["role"] for a in w.kinds("delegate")] == ["explorer", "debater", "debater", "debater", "debater", "implementer"]
    assert w.state.phase == "signoff" and w.state.audit_scopes == {"scope": {"reviewer": "UREV", "verdict": None, "signed_at": None, "requested": True}}
    w.ev("sign_off", sender="USOMEONE", pr=PR, sha=SHA, verdict="approve")  # not a reviewer: recorded, no effect
    assert w.state.stage == "Audit"
    w.ev("sign_off", sender="UREV", pr=PR, sha=SHA, verdict="changes")
    assert w.state.iteration == 2 and w.state.audit["verdict"] == "return" and "scope=changes" in w.state.findings[0]


def test_signoff_for_another_head_is_ignored_and_noted():
    """F2: a sign-off counts only for the reviewed PR and sha; another head never closes a peer's scope."""
    cfg = dataclasses.replace(CFG, audit_scopes=("scope",), require_signoffs=True)
    w = World(cfg=cfg)
    w.to_audit()
    w.ev("sign_off", sender="UREV", pr=PR, sha="0000000", verdict="approve")
    w.ev("sign_off", sender="UREV", pr="https://github.com/o/r/pull/10", sha=SHA, verdict="approve")
    assert w.state.stage == "Audit" and w.state.signoffs == {} and w.state.audit_scopes["scope"]["signed_at"] is None
    assert len(w.state.findings) == 2 and "sign-off from UREV ignored" in w.state.findings[0] and "pull/10" in w.state.findings[1]
    w.ev("sign_off", sender="UREV", pr="9", sha="abc12", verdict="approve")  # a bare number and a sha prefix name the same head
    assert w.state.stage == "Delivered" and w.state.signoffs == {"UREV": "approve"}


def test_changes_verdict_survives_a_timeout_of_the_other_signoff():
    """T1 (post-merge review): a `changes` on the head returns the study even when another sign-off times out; only missing sign-offs are waived."""
    cfg = dataclasses.replace(CFG, reviewers=(Reviewer(REV, "scope"), Reviewer("UREV2", "code")), require_signoffs=True)
    w = World(cfg=cfg)
    w.to_audit()
    w.ev("sign_off", sender=REV, pr=PR, sha=SHA, verdict="changes")
    assert w.state.stage == "Audit"
    w.tick(cfg.stage_timeout)
    assert w.state.iteration == 2 and w.state.audit["verdict"] == "return" and w.state.audit["signoffs_missing"] == ["UREV2"]
    assert "scope=changes" in w.state.findings[0]


def test_refused_audit_request_is_recorded_and_retried():
    """T2 (post-merge review): a refused reviewer request leaves every peer scope unrequested; the stage cannot pass and rule R re-posts it."""
    cfg = dataclasses.replace(CFG, reviewers=(Reviewer(REV, "scope"), Reviewer("UREV2", "code")), require_signoffs=True)
    w = World(cfg=cfg)
    w.to_audit()
    req = [p for p in w.kinds("post") if p["post_kind"] == "report"]
    w.ev("post_refused", action_id=req[-1].id, post_kind="report", code="rate_limited", outcome="rejected")
    assert w.state.stage == "Audit" and w.state.attempt == 2 and all(sc["requested"] for sc in w.state.audit_scopes.values())
    assert len([p for p in w.kinds("post") if p["post_kind"] == "report"]) == 2 and "audit request refused" in w.state.notes[-1]
    w.ev("post_refused", post_kind="report", code="rate_limited", outcome="rejected")  # second refusal: Blocked, never a pass
    assert w.state.stage == "Blocked" and not any(sc["requested"] for sc in w.state.audit_scopes.values())
    w.tick(cfg.stage_timeout)
    assert w.state.stage == "Blocked" and w.state.audit.get("verdict") is None


def test_ts_key_is_fixed_width():
    assert ts_key("1700.12") > ts_key("1700.000012") and ts_key("1700.000012") == (1700, 12) and ts_key("1700.12") == (1700, 120000)
    assert ts_key("1700") < ts_key("1700.000001") < ts_key("1701.000000")


def test_head_matches_table():
    assert machine.head_matches("https://github.com/o/r/pull/9", "abc1234", PR, SHA) and machine.head_matches("#9", "ABC1234", PR, SHA)
    assert not machine.head_matches(PR, "abc1235", PR, SHA) and not machine.head_matches("9", "", PR, SHA) and not machine.head_matches("8", SHA, PR, SHA)
    assert machine.head_matches("anything", "fff", "", "")  # no reviewed head known: nothing to mismatch


def test_won_slug_is_not_treated_as_taken_later():
    """F3: a peer's later claim on our slug loses; the slug must not be excluded from our next pick."""
    w = World()
    w.to_claim()  # settling on alpha, own ts .000101
    w.ev("peer_post", ts="1700000000.999999", sender=PEER, kind="study_claim", text="Claim (iteration 1): x\napproach: alpha\nwhy: w\nalso considered: none")
    assert w.state.claim["slug"] == "alpha" and "alpha" not in w.state.peer_claims
    w.tick(CFG.settle_window)
    w.finish("mathematician", result(report=report(position="agree")))
    w.finish("physicist", result(report=report(position="agree")))
    w.finish("implementer")
    w.finish("auditor", result(report=report(verdict="return")))
    assert w.state.iteration == 2 and w.state.stage == "Explore"
    w.finish("explorer", result(report=EXPLORER_REPORT))
    assert w.state.claim["slug"] == "alpha" and w.state.excluded == []


def test_peer_claim_recorded_while_pending_is_dropped_when_our_echo_is_earlier():
    w = World(auto_post=False)
    w.to_claim()
    w.ev("peer_post", ts="1700000000.000900", sender=PEER, kind="study_claim", text="Claim (iteration 1): x\napproach: alpha\nwhy: w\nalso considered: none")
    assert "alpha" in w.state.peer_claims  # our ts is unknown yet
    w.ev("own_post_seen", ts="1700000000.000500", kind="study_claim", text=w.kinds("post")[0]["text"])
    assert w.state.claim["status"] == "settling" and "alpha" not in w.state.peer_claims and w.state.excluded == []


def test_post_refused_three_posts_then_rule_r():
    """F4: a rate-limited post is sent three times in all before the stage fails (rule R)."""
    w = World(hold=("study_result",))
    w.to_delivered()
    n_llm = len(w.kinds("llm_call"))
    for i in range(3):
        assert w.state.post_tries == i + 1 and w.state.attempt == 1
        w.ev("post_refused", post_kind="study_result", code="rate_limited", outcome="failed", retry_after=30)
        if i < 2: w.tick(30)
    assert len([p for p in w.kinds("post") if p["post_kind"] == "study_result" and "/a1/" in p.id]) == 3
    assert len(w.kinds("llm_call")) == n_llm + 1 and w.state.attempt == 2 and w.state.stage == "Deliver" and w.kinds("llm_call")[-1]["name"] == "study_deliver"  # rule R re-runs the deliver call


def test_same_reviewer_in_two_scopes_keeps_two_scopes():
    """F5: one audit scope per reviewer line; one SIGN-OFF from that reviewer signs all of their scopes."""
    from fridica_research.config import Reviewer
    cfg = dataclasses.replace(CFG, reviewers=(Reviewer("UREV", "scope"), Reviewer("UREV", "code"), Reviewer("UX", ""), Reviewer("UX", "")), audit_scopes=("scope", "code"), require_signoffs=True)
    w = World(cfg=cfg)
    w.to_audit()
    assert sorted(w.state.audit_scopes) == ["code", "review-UX", "review-UX-2", "scope"] and w.state.phase == "signoff"
    w.ev("sign_off", sender="UREV", pr=PR, sha=SHA, verdict="approve")
    assert [s for s, sc in w.state.audit_scopes.items() if sc["signed_at"]] == ["scope", "code"] and w.state.stage == "Audit"


def test_audit_completes_at_once_when_all_scopes_are_peers_and_signoffs_optional():
    """Z3: no local auditor and `require_signoffs = false`: nothing to wait for, Audit completes immediately."""
    cfg = dataclasses.replace(CFG, audit_scopes=("scope",), require_signoffs=False)
    w = World(cfg=cfg)
    w.to_audit()
    assert "auditor" not in [a["role"] for a in w.kinds("delegate")]
    assert w.state.stage == "Delivered" and w.state.audit["verdict"] == "pass" and w.state.audit_scopes["scope"]["signed_at"] is None
    assert [r["stage"] for r in w.state.stage_log] == ["Explore", "Claim", "Debate", "Implement", "Audit", "Deliver"]

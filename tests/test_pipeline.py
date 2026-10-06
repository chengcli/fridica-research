"""Issue 38: checkpoint, input isolation and delegation regressions (importable on base)."""
from fridica_research import briefs, contracts
from support import World, report, result


def consensus_ready(w):
    w.to_debate()
    w.finish("mathematician", result(report=report(position="agree")))
    w.finish("physicist", result(report=report(position="agree")))


def test_design_checkpoint_precedes_implementation():
    w = World()
    consensus_ready(w)
    assert w.state.stage == "DesignAudit"
    assert "implementer" not in w.pending
    w.finish("auditor", result(report=report(verdict="pass")))
    assert w.state.stage == "Implement"


def test_role_text_travels_in_brief_with_host_fields_only():
    w = World()
    w.start()
    action = w.kinds("delegate")[-1]
    assert "instructions" not in action.data
    assert "# Explorer" in action["brief"]


def test_large_input_preserves_final_reference():
    text = briefs.explorer("exact-reference", "study", "x" * 100_000, [], [], {})
    assert len(text) <= 40_000
    assert contracts.ref_of(text) == "exact-reference"
    assert text.rstrip().endswith("ref: exact-reference")


def test_design_return_restarts_rounds_without_reusing_ids():
    w = World()
    consensus_ready(w)
    assert w.state.stage == "DesignAudit"
    old = {a.id for a in w.kinds("delegate") if a.get("lens")}
    w.finish("auditor", result(report=report(verdict="return"), summary="repair boundary"))
    assert w.state.stage == "Debate" and w.state.round == 1
    assert w.state.design_returns == 1
    new = w.state.waiting["actions"]
    assert all("/Debate/a1/r1/debate-1-" in a["action_id"] for a in new)
    assert old.isdisjoint(a["action_id"] for a in new)
    assert not any("DesignAudit" in tid for tid in w.state.timers)


def finish_round(w, body="analysis", position="agree"):
    for lane in sorted(w.state.lenses):
        w.finish(lane, result(report=report(position=position, body=body)))


def request_round(w):
    finish_round(w, body="## Evidence request\nquestion: measure scaling\nsource: prior paper\nexperiment: run probe")


def test_auditors_and_implementer_never_receive_explorer_sentinel():
    w = World()
    w.to_debate()
    w.state.explorer_report += "\nEXPLORER_SENTINEL_38"
    w.state.findings.append("EXPLORER_SENTINEL_38")
    finish_round(w)
    assert w.state.stage == "DesignAudit"
    w.finish("auditor", result(report=report(verdict="pass")))
    impl = w.kinds("delegate")[-1]["brief"]
    assert "EXPLORER_SENTINEL_38" not in impl and "## Study" not in impl
    assert w.state.audited_consensus in impl
    w.finish("implementer", result(artifacts=["https://github.com/o/r/pull/9"], summary="IMPLEMENTER_SENTINEL_38", machine_state={"commit": "abc1234", "base": "def5678"}))
    for action in w.kinds("delegate"):
        if action["role"] == "auditor":
            assert "EXPLORER_SENTINEL_38" not in action["brief"]
            assert "IMPLEMENTER_SENTINEL_38" not in action["brief"]
            assert w.state.audited_consensus in action["brief"]
    assert "def5678..abc1234" in w.kinds("delegate")[-1]["brief"]


def test_three_design_returns_block_fourth_entry_owner_resume_preserves_count():
    w = World()
    consensus_ready(w)
    assert w.state.stage == "DesignAudit"
    for n in range(1, 4):
        w.finish("auditor", result(report=report(verdict="return")))
        assert w.state.design_returns == n and w.state.round == 1
        finish_round(w)
    assert w.state.stage == "Blocked" and w.state.blocked_from == "DesignAudit"
    assert not w.state.timers and "chengcli" in w.state.notes[-1]
    w.ev("owner_resume")
    assert w.state.stage == "DesignAudit" and w.state.design_returns == 3
    w.finish("auditor", result(report=report(verdict="return")))
    finish_round(w)
    assert w.state.stage == "Blocked" and w.state.design_returns == 4


def test_design_overrun_blocks_and_resume_reenters_checkpoint():
    import dataclasses
    from support import CFG
    w = World(cfg=dataclasses.replace(CFG, stage_timeout=5000))
    consensus_ready(w)
    assert w.state.stage == "DesignAudit"
    w.tick(2400)
    assert w.state.stage == "Blocked" and w.state.blocked_from == "DesignAudit"
    assert not w.state.timers
    w.ev("owner_resume")
    assert w.state.stage == "DesignAudit"
    assert any("DesignAudit/overrun" in t for t in w.state.timers)


def test_one_evidence_per_return_and_round_and_request_only_brief():
    w = World()
    w.to_debate()
    request_round(w)
    assert w.state.phase == "evidence" and len(w.state.evidence) == 1
    first = w.state.evidence[0]
    assert (first["design_returns"], first["round"]) == (0, 1)
    brief = w.kinds("delegate")[-1]["brief"]
    assert "measure scaling" in brief and "Findings..." not in brief and "## Study" not in brief
    w.finish("explorer", result(report="EVIDENCE_ANSWER_38"))
    assert w.state.round == 2
    assert all("EVIDENCE_ANSWER_38" in a["brief"] for a in w.state.waiting["actions"])
    finish_round(w)
    assert w.state.stage == "DesignAudit"
    w.finish("auditor", result(report=report(verdict="return")))
    request_round(w)
    assert w.state.phase == "evidence" and len(w.state.evidence) == 2
    assert [(e["design_returns"], e["round"]) for e in w.state.evidence] == [(0, 1), (1, 1)]
    assert "/r1/evidence-1-" in w.state.evidence[-1]["ref"]


def test_evidence_deadline_uses_remaining_projection_and_expiry_continues():
    w = World()
    w.to_debate()
    w.now += 1100
    request_round(w)
    assert w.state.phase == "evidence"
    deadline = next(d for t, d in w.state.timers.items() if t.endswith("/evidence"))
    assert deadline == w.now + 100
    w.tick(100)
    assert w.state.stage == "Debate" and w.state.round == 2
    assert w.state.evidence[0]["answer"].startswith("Evidence deadline expired")


def test_no_evidence_job_when_projection_exhausted():
    w = World()
    w.to_debate()
    w.now += 1200
    request_round(w)
    assert w.state.stage == "DesignAudit"
    assert not w.state.evidence
    assert len([a for a in w.kinds("delegate") if a["role"] == "explorer"]) == 1


def test_evidence_overrun_synthesizes_then_bounds_synthesis_to_five_minutes():
    from support import LLM
    w = World()
    w.to_debate()
    request_round(w)
    assert w.state.phase == "evidence"
    # A delayed driver observes overrun before the evidence timeout; don't synchronously answer its LLM.
    w.llm = lambda name, prompt: LLM[name]
    w.auto_delegate = False
    original = w.react
    w.react = lambda actions: w.actions.extend(actions)
    overrun = next(t for t in w.state.timers if t.endswith("/overrun"))
    w.now += 2400
    w.ev("timeout", timer_id=overrun)
    assert w.state.stage == "Debate" and w.state.phase == "llm"
    assert w.state.waiting["id"].endswith("/synth")
    assert w.state.reports and not w.state.group
    assert next(d for t, d in w.state.timers.items() if t.endswith("/timer")) == w.now + 300
    w.react = original
    w.tick(300)
    assert w.state.stage == "Explore" and w.state.iteration == 2
    assert any("Debate interrupted" in f for f in w.state.findings)


def test_overlay_only_lens_and_removed_lane_stop_before_next_round():
    w = World()
    w.to_debate()
    proposal = "## Lenses\n### physicist\nOVERLAY_PHYSICIST\n### engineer\nOVERLAY_ENGINEER\n"
    w.finish("mathematician", result(report=report(position="revised", body=proposal)))
    w.finish("physicist", result(report=report(position="revised")))
    assert sorted(w.state.lenses) == ["engineer", "physicist"]
    actions = w.state.waiting["actions"]
    assert [a["lens"] for a in actions] == ["engineer", "physicist"]
    assert "OVERLAY_ENGINEER" in actions[0]["brief"]
    assert "OVERLAY_PHYSICIST" in actions[1]["brief"]
    assert not w.state.workers["mathematician"]["live"]
    stop = next(a for a in w.actions if a.kind == "stop_worker")
    assert stop["worker_id"] == w.workers["mathematician"]
    assert w.actions.index(stop) < next(i for i, a in enumerate(w.actions) if a.kind == "delegate" and a.get("lens") == "engineer")


def test_invalid_lens_proposal_becomes_finding_and_keeps_lanes():
    w = World()
    w.to_debate()
    finish_round(w, "## Lenses\n### bad slug\ntext\n", position="revised")
    assert sorted(w.state.lenses) == ["mathematician", "physicist"]
    assert any("Invalid Lenses" in f for f in w.state.findings)


def test_all_three_lenses_must_agree_and_lens_text_is_structural():
    from fridica_research import replay
    import copy
    w = World()
    w.to_debate()
    proposal = "## Lenses\n### mathematician\nM\n### physicist\nP\n### engineer\nE\n"
    finish_round(w, proposal, position="revised")
    assert len(w.state.lenses) == 3
    a = w.kinds("delegate")[-1].to_dict()
    b = copy.deepcopy(a)
    b["lens_sha256"] = "different text digest"
    assert replay.pi_struct(a) != replay.pi_struct(b)
    w.finish("engineer", result(report=report(position="agree")))
    w.finish("mathematician", result(report=report(position="agree")))
    assert w.state.stage == "Debate" and "auditor" not in w.pending
    w.finish("physicist", result(report=report(position="agree")))
    assert w.state.stage == "DesignAudit"


def test_mandatory_sections_cannot_be_truncated():
    import pytest
    with pytest.raises(ValueError, match="mandatory"):
        briefs.fit([("Role", "x" * 40_001), ("Reference", "ref: r")], [])


def test_return_counter_persists_across_code_audit_iterations():
    w = World()
    consensus_ready(w)
    assert w.state.stage == "DesignAudit"
    w.finish("auditor", result(report=report(verdict="return")))
    finish_round(w)
    w.finish("auditor", result(report=report(verdict="pass")))
    w.finish("implementer")
    w.finish("auditor", result(report=report(verdict="return")))
    assert w.state.iteration == 2 and w.state.design_returns == 1


def test_handoffs_name_holder_input_due_and_remind_once_for_repeated_misses():
    w = World()
    consensus_ready(w)
    assert w.state.stage == "DesignAudit"
    posts = [a for a in w.kinds("post") if a.id.endswith("handoff")]
    assert posts and all("<@UOWNER>" in a["text"] and "input:" in a["text"] and "due:" in a["text"] for a in posts)
    for n in range(2):
        w.ev("peer_post", kind="report", ts=str(n), sender="UAUTHOR", text="handoff: revised consensus")
    assert w.state.mention_misses["DesignAudit/UAUTHOR"] == 2
    reminders = [a for a in w.kinds("post") if "mention-reminder" in a.id]
    assert len(reminders) == 1 and "<@UOWNER>" in reminders[0]["text"]
    # The driver's reminder echo is not a second miss.
    assert len(w.state.mention_reminded) == 1


def test_oversized_role_fails_stage_instead_of_corrupting_brief(monkeypatch):
    monkeypatch.setattr(briefs, "instructions", lambda *a: "X" * 40_001, raising=False)
    w = World()
    w.start()
    assert w.state.stage == "Blocked" and "brief" in w.state.notes[-1]
    assert not w.kinds("delegate")


def test_evidence_slot_refusal_recovery_still_routes_explorer_answer():
    w = World()
    w.to_debate()
    w.refuse_delegate = ["too_many_workers"]
    request_round(w)
    assert w.state.phase == "slot" and w.state.waiting.get("evidence")
    w.ev("job_result", job_id="unrelated", worker_id="unrelated", job_status="interrupted")
    assert w.state.phase == "evidence"
    w.finish("explorer", result(report="slot answer"))
    assert w.state.round == 2 and w.state.evidence[0]["answer"] == "slot answer"


def test_large_explorer_report_in_debate_preserves_action_reference():
    w = World()
    w.to_debate()
    w.state.explorer_report = "x" * 100_000
    finish_round(w, position="revised")
    assert w.state.round == 2
    for action in w.state.waiting["actions"]:
        assert len(action["brief"]) <= 40_000
        assert contracts.ref_of(action["brief"]) == action["action_id"]


def evidence_then_code_audit_return():
    from support import EXPLORER_REPORT
    w = World()
    w.to_debate()
    request_round(w)
    w.finish("explorer", result(report="ITERATION_ONE_EVIDENCE"))
    finish_round(w)
    w.finish("auditor", result(report=report(verdict="pass")))
    w.finish("implementer", result())
    w.finish("auditor", result(report=report(verdict="return")))
    assert w.state.iteration == 2
    w.finish("explorer", result(report=EXPLORER_REPORT))
    w.tick(w.cfg.settle_window)
    assert w.state.stage == "Debate" and w.state.round == 1
    return w


def test_evidence_request_after_code_audit_return_is_iteration_scoped():
    w = evidence_then_code_audit_return()
    request_round(w)
    assert w.state.phase == "evidence" and len(w.state.evidence) == 2
    assert [e["iteration"] for e in w.state.evidence] == [1, 2]
    w.finish("explorer", result(report="ITERATION_TWO_EVIDENCE"))
    assert [e["answer"] for e in w.state.evidence] == ["ITERATION_ONE_EVIDENCE", "ITERATION_TWO_EVIDENCE"]
    assert all("ITERATION_TWO_EVIDENCE" in a["brief"] and "ITERATION_ONE_EVIDENCE" not in a["brief"] for a in w.state.waiting["actions"])


def test_evidence_answers_do_not_leak_after_code_audit_return():
    w = evidence_then_code_audit_return()
    assert all("ITERATION_ONE_EVIDENCE" not in a["brief"] for a in w.state.waiting["actions"])


def test_legacy_evidence_restart_preserves_inflight_answer_and_isolation():
    from fridica_research.machine import State
    w = World()
    w.to_debate()
    request_round(w)
    snapshot = w.state.to_dict()
    snapshot["evidence"][0].pop("iteration", None)
    w.state = State.from_dict(snapshot)
    assert w.state.evidence[0]["iteration"] == 1
    w.finish("explorer", result(report="LEGACY_CURRENT_ANSWER"))
    assert all("LEGACY_CURRENT_ANSWER" in a["brief"] for a in w.state.waiting["actions"])
    finish_round(w)
    w.finish("auditor", result(report=report(verdict="pass")))
    w.finish("implementer", result())
    w.finish("auditor", result(report=report(verdict="return")))
    snapshot = w.state.to_dict()
    snapshot["evidence"][0].pop("iteration")
    w.state = State.from_dict(snapshot)
    assert w.state.iteration == 2 and w.state.evidence[0]["iteration"] == 1
    assert "iteration" not in snapshot["evidence"][0]  # Loading does not mutate the caller's snapshot.
    from support import EXPLORER_REPORT
    w.finish("explorer", result(report=EXPLORER_REPORT))
    w.tick(w.cfg.settle_window)
    assert all("LEGACY_CURRENT_ANSWER" not in a["brief"] for a in w.state.waiting["actions"])
    request_round(w)
    assert w.state.phase == "evidence"


def test_legacy_evidence_without_parseable_reference_is_not_attributed():
    from fridica_research.machine import State
    w = World()
    w.to_debate()
    w.state.evidence = [{"design_returns": 0, "round": 1, "lens": "mathematician", "request": "old", "answer": "UNSCOPED_ANSWER", "ref": "unknown"}]
    w.state = State.from_dict(w.state.to_dict())
    request_round(w)
    assert w.state.phase == "evidence" and len(w.state.evidence) == 2
    w.finish("explorer", result(report="CURRENT_ANSWER"))
    assert all("UNSCOPED_ANSWER" not in a["brief"] for a in w.state.waiting["actions"])

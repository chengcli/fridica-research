"""#46 slice 3: the R51 ETA ladder (`ladder.step`) and two arbiter carries (ref fm-wave-e-assign1).

`ladder.step(view, now)` is pure. At the ETA with no deliverable it posts one check-in; it reschedules only
after the check-in and only when the holder has been silent for more than ETA + W, W = clamp(0.25 x est,
15 min, 60 min), counting only a reply after the check-in; the target is the first free alternative, else
the owner. A `quota` or `offline` cause reschedules at the ETA. The Outie is asked only for permission,
hardware or internet; scope and merge questions go to the owner at any time. The `done` set of refs on the
view makes every action happen once.

arbiter: a PR closed without merging is dropped with a finding and no stage move, so the next poll re-adopts;
`adopt` raises a `RosterError` for a register error, still a `RegisterError` too.
"""
from __future__ import annotations

import dataclasses

import pytest

from fridica_research import arbiter, ladder, register
from fridica_research.machine import Event
from support import CFG

MIN = 60.0
T0 = 1_791_800_000.0
EST = 120 * MIN  # W = 30 min
ETA = T0 + EST
W = 30 * MIN
EPS = 1e-3
REF = "fm-wave-e-job2"
HOLDER, OWNER, OUTIE = "UHOLD", "UOWNER", "UOUTIE"


def view(**kw) -> ladder.JobView:
    base = dict(ref=REF, holder=HOLDER, owner=OWNER, outie=OUTIE, est=EST, eta=ETA,
                alternatives=[{"id": "UBUSY", "free": False}, {"id": "UFREE1", "free": True}, {"id": "UFREE2", "free": True}])
    return ladder.JobView(**{**base, **kw})


def kinds(actions) -> list[str]: return [a.kind for a in actions]
def checked_in(**kw) -> ladder.JobView: return view(**{"checkin_at": ETA, "done": {f"{REF}/checkin"}, **kw})


# -- W ------------------------------------------------------------------------------------
@pytest.mark.parametrize("est,w", [(10 * MIN, 15 * MIN), (60 * MIN, 15 * MIN), (120 * MIN, 30 * MIN), (240 * MIN, 60 * MIN), (600 * MIN, 60 * MIN)])
def test_window_is_a_quarter_of_the_estimate_clamped_to_15_and_60_min(est, w):
    assert ladder.window(est) == w


def test_the_clamps_set_the_reschedule_time():
    short = view(est=10 * MIN, eta=T0 + 10 * MIN, checkin_at=T0 + 10 * MIN, done={f"{REF}/checkin"})
    assert ladder.step(short, T0 + 25 * MIN) == []  # ETA + 15 min exactly, not 2.5 min
    assert kinds(ladder.step(short, T0 + 25 * MIN + EPS)) == ["reschedule"]
    long = view(est=600 * MIN, eta=T0 + 600 * MIN, checkin_at=T0 + 600 * MIN, done={f"{REF}/checkin"})
    assert ladder.step(long, T0 + 660 * MIN) == []  # ETA + 60 min exactly, not 150 min
    assert kinds(ladder.step(long, T0 + 660 * MIN + EPS)) == ["reschedule"]


# -- check-in -----------------------------------------------------------------------------
def test_nothing_before_the_eta():
    assert ladder.step(view(), ETA - EPS) == []


def test_one_check_in_at_the_eta_without_a_deliverable():
    out = ladder.step(view(), ETA)
    assert kinds(out) == ["post"] and out[0].id == f"{REF}/checkin"
    assert f"<@{HOLDER}>" in out[0]["text"] and f"ref: {REF}/checkin" in out[0]["text"]


def test_no_check_in_with_a_deliverable():
    assert ladder.step(view(deliverable=True), ETA + 5 * W) == []


# -- reschedule ---------------------------------------------------------------------------
def test_no_reschedule_before_the_check_in_has_gone_out():
    out = ladder.step(view(), ETA + W + EPS)  # late tick: only the check-in goes out
    assert kinds(out) == ["post"]


def test_silent_for_exactly_eta_plus_w_is_not_rescheduled():
    assert ladder.step(checked_in(), ETA + W) == []


def test_silent_for_more_than_eta_plus_w_reschedules_to_the_first_free_alternative():
    out = ladder.step(checked_in(), ETA + W + EPS)
    assert kinds(out) == ["reschedule"]
    assert out[0].id == f"{REF}/reschedule/{HOLDER}" and out[0]["from"] == HOLDER and out[0]["to"] == "UFREE1"


def test_a_reply_after_the_check_in_stops_the_reschedule():
    assert ladder.step(checked_in(last_reply=ETA + 1), ETA + 3 * W) == []


def test_a_reply_before_the_check_in_does_not_count():
    assert kinds(ladder.step(checked_in(last_reply=ETA - 1), ETA + W + EPS)) == ["reschedule"]


def test_nobody_free_wakes_the_deputy():
    out = ladder.step(checked_in(alternatives=[{"id": "UBUSY", "free": False}, {"id": HOLDER, "free": True}]), ETA + W + EPS)
    assert kinds(out) == ["wake_deputy"] and out[0].id == f"{REF}/escalate" and f"<@{OWNER}>" not in out[0]["text"]


@pytest.mark.parametrize("cause", ["quota", "offline"])
def test_quota_or_offline_reschedules_at_the_eta_without_a_check_in(cause):
    assert ladder.step(view(cause=cause), ETA - EPS) == []
    out = ladder.step(view(cause=cause), ETA)
    assert kinds(out) == ["reschedule"] and out[0]["to"] == "UFREE1"


# -- Outie and owner ----------------------------------------------------------------------
@pytest.mark.parametrize("cause", ["permission", "hardware", "internet"])
def test_the_outie_is_asked_for_permission_hardware_or_internet(cause):
    out = ladder.step(view(cause=cause), T0)
    assert kinds(out) == ["ask_outie"] and out[0]["to"] == OUTIE and out[0].id == f"{REF}/outie/{cause}"


@pytest.mark.parametrize("cause", ["quota", "offline", "bug", "scope", None])
def test_the_outie_is_not_asked_for_any_other_cause(cause):
    for t in (T0, ETA, ETA + W + EPS):
        assert "ask_outie" not in kinds(ladder.step(checked_in(cause=cause), t))


@pytest.mark.parametrize("kind", ["merge_substrate", "scope", "rules"])
def test_owner_only_questions_go_to_the_owner_at_any_time(kind):
    q = [{"id": "q1", "kind": kind, "text": "is X in scope?"}]
    for t in (T0, ETA + 10 * W):
        out = ladder.step(view(questions=q, deliverable=True), t)
        assert kinds(out) == ["notify_owner"] and out[0].id == f"{REF}/question/q1" and out[0]["to"] == OWNER


@pytest.mark.parametrize("kind", ["howto", "merge"])
def test_other_questions_wake_the_deputy_not_the_owner(kind):
    out = ladder.step(view(questions=[{"id": "q1", "kind": kind}]), T0)
    assert kinds(out) == ["wake_deputy"] and out[0].id == f"{REF}/question/q1" and f"<@{OWNER}>" not in out[0]["text"]


# -- idempotence --------------------------------------------------------------------------
def test_the_done_set_makes_every_action_happen_once():
    v = view(cause="hardware", questions=[{"id": "q1", "kind": "scope"}])
    first = ladder.step(v, ETA)
    assert sorted(kinds(first)) == ["ask_outie", "notify_owner", "post"]
    v = dataclasses.replace(v, done=set(v.done) | {a.id for a in first}, checkin_at=ETA)
    assert ladder.step(v, ETA) == []
    resched = ladder.step(v, ETA + W + EPS)
    assert kinds(resched) == ["reschedule"]
    v = dataclasses.replace(v, done=set(v.done) | {resched[0].id})
    assert ladder.step(v, ETA + 10 * W) == []
    nobody = checked_in(alternatives=[], done={f"{REF}/checkin", f"{REF}/escalate"})
    assert ladder.step(nobody, ETA + 10 * W) == []


def test_step_is_pure_and_takes_a_plain_mapping():
    d = {"ref": REF, "holder": HOLDER, "owner": OWNER, "est": EST, "eta": ETA, "done": [f"{REF}/checkin"], "checkin_at": ETA,
         "alternatives": [{"id": "UFREE1", "free": True}]}
    snapshot = repr(d)
    a, b = ladder.step(d, ETA + W + EPS), ladder.step(d, ETA + W + EPS)
    assert a == b and kinds(a) == ["reschedule"] and repr(d) == snapshot


# -- arbiter carries ----------------------------------------------------------------------
IMPL = "UIMPL"


def implement_round() -> arbiter.Round:
    return arbiter.Round(thread="T1:C1:1700000000.000100", issue=46, repo="o/r", stage="Implement",
                         roster={"implementer": {"holders": [IMPL], "login": "impl", "delegate": False}},
                         pr={"number": 80, "head": "1" * 40, "state": "OPEN", "draft": False})


def test_a_pr_closed_without_merging_is_dropped_and_the_next_poll_re_adopts():
    r = implement_round()
    r2, actions = arbiter.observe(r, Event("pr_polled", T0, {"view": {"headRefOid": "1" * 40, "state": "CLOSED", "isDraft": False, "mergedAt": None}, "reviews": []}), CFG)
    assert actions == [] and r2.stage == "Implement" and not r2.pr.get("number")
    assert any("#80" in f and "closed" in f for f in r2.findings)
    fresh = {"number": 81, "headRefOid": "2" * 40, "state": "OPEN", "isDraft": False, "author": {"login": "impl"}, "body": "Refs #46"}
    r3, _ = arbiter.observe(r2, Event("pr_polled", T0 + 60, {"prs": [fresh]}), CFG)
    assert r3.pr["number"] == 81 and r3.stage == "Implement"


def test_a_merged_pr_is_not_dropped():
    r2, _ = arbiter.observe(implement_round(), Event("pr_polled", T0, {"view": {"headRefOid": "1" * 40, "state": "MERGED", "isDraft": False, "mergedAt": "2026-10-12T13:00:00Z"}, "reviews": []}), CFG)
    assert r2.pr["number"] == 80 and r2.findings == []


def test_adopt_raises_a_roster_error_for_a_register_error(tmp_path):
    cfg = dataclasses.replace(CFG, register=str(tmp_path / "gone.toml"))
    with pytest.raises(arbiter.RosterError, match="does not exist") as e:
        arbiter.adopt("T1:C1:1700000000.000100", issue=46, repo="o/r", roles=["implementer=UIMPL"], stage="Implement", now=T0, cfg=cfg)
    assert isinstance(e.value, register.RegisterError)

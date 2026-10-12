"""#53 PR 1: the pure ETA estimator and the R48/R52 score. Synthetic data only."""
from datetime import datetime, timedelta

import pytest

from fridica_research.eta import (
    EVIDENCE_RANK,
    InnieBuffer,
    PastJob,
    Score,
    Scored,
    Slot,
    TrialRun,
    WorkloadReport,
    estimate,
    first_committed_eta,
    score,
    work_from_history,
)

NOW = datetime(2026, 10, 11, 18, 0)
BUSY = InnieBuffer(innie="alfa", queue_hours=1.5, rate_limit_hours=0.25, signoff_hours=0.5, offline_hours=0.75)
HISTORY = (
    PastJob(ref="#40", actual_hours=2.0, nature="pr", size="m", innie="alfa", role="implementer"),
    PastJob(ref="#41", actual_hours=3.0, nature="pr", size="m", innie="alfa", role="implementer"),
    PastJob(ref="#42", actual_hours=0.5, nature="pr", size="s", innie="alfa", role="implementer"),
    PastJob(ref="#43", actual_hours=9.0, nature="review", size="m", innie="bravo", role="auditor"),
)


def hours_between(a, b):
    return (b - a) / timedelta(hours=1)


# -- purity ---------------------------------------------------------------------
def test_estimator_is_pure_no_clock_no_io(monkeypatch):
    """`now` is an argument: with the clock and the filesystem broken the estimate is unchanged."""
    import fridica_research.eta as eta_mod

    def boom(*a, **k): raise AssertionError("eta.py must not read a clock or touch the disk")

    monkeypatch.setattr(eta_mod, "datetime", type("NoClock", (), {"now": staticmethod(boom), "utcnow": staticmethod(boom)}))
    monkeypatch.setattr("builtins.open", boom)
    first = estimate(NOW, "alfa", buffer=BUSY, reasoning_hours=2.0)
    again = estimate(NOW, "alfa", buffer=BUSY, reasoning_hours=2.0)
    assert first == again and first.eta == datetime(2026, 10, 11, 23, 0)  # 18:00 + 2 h work + 3 h buffer


def test_same_inputs_give_the_same_estimate_whatever_the_order():
    kw = dict(buffer=BUSY, history=HISTORY, nature="pr", size="m", reasoning_hours=4.0)
    assert estimate(NOW, "alfa", **kw) == estimate(NOW, "alfa", **kw)


# -- input time + work + buffer -------------------------------------------------
def test_eta_is_input_time_plus_work_plus_buffer():
    e = estimate(NOW, "alfa", buffer=BUSY, reasoning_hours=2.0, input_at=NOW)
    assert e.work_hours == 2.0
    assert e.buffer_hours == pytest.approx(1.5 + 0.25 + 0.5 + 0.75)
    assert e.est_hours == pytest.approx(5.0)
    assert hours_between(e.input_at, e.eta) == pytest.approx(5.0)


def test_buffer_is_specific_to_the_innie():
    quick = InnieBuffer(innie="bravo", queue_hours=0.0, signoff_hours=0.0)
    slow = estimate(NOW, "alfa", buffer=BUSY, reasoning_hours=2.0)
    fast = estimate(NOW, "bravo", buffer=quick, reasoning_hours=2.0)
    assert slow.work_hours == fast.work_hours == 2.0
    assert slow.buffer_hours > fast.buffer_hours == 0.0
    assert fast.eta == NOW + timedelta(hours=2)


def test_a_buffer_for_another_innie_is_refused():
    with pytest.raises(ValueError, match="buffer is for"):
        estimate(NOW, "bravo", buffer=BUSY, reasoning_hours=1.0)


def test_no_work_evidence_at_all_is_an_error_not_a_guess():
    with pytest.raises(ValueError, match="no work evidence"):
        estimate(NOW, "alfa", buffer=BUSY)


# -- evidence ranking: workload report > trial run > history --------------------
def test_ranking_is_workload_then_trial_then_history():
    assert EVIDENCE_RANK.index("workload") < EVIDENCE_RANK.index("trial") < EVIDENCE_RANK.index("history")


def test_trial_run_beats_history_for_the_work():
    e = estimate(NOW, "alfa", buffer=BUSY, trial=TrialRun(hours=0.4, scale=0.25), history=HISTORY, nature="pr", size="m", reasoning_hours=99.0)
    assert e.work_basis == "trial" and e.work_hours == pytest.approx(1.6)


def test_history_beats_reasoning_and_picks_the_similar_jobs():
    e = estimate(NOW, "alfa", buffer=BUSY, history=HISTORY, nature="pr", size="m", reasoning_hours=99.0)
    assert e.work_basis == "history" and e.work_hours == pytest.approx(2.5)  # #40 and #41, not #42 or #43


def test_reasoning_is_the_last_resort():
    e = estimate(NOW, "alfa", buffer=BUSY, reasoning_hours=1.25)
    assert e.work_basis == "reasoning" and e.basis == "reasoning" and e.work_hours == 1.25


def test_work_from_history_falls_back_from_size_to_nature_to_everything():
    assert work_from_history(HISTORY, "pr", "m") == pytest.approx(2.5)
    assert work_from_history(HISTORY, "pr", "xl") == pytest.approx(11 / 6)  # no xl: every `pr` job
    assert work_from_history(HISTORY, "deploy", "m") == pytest.approx(3.625)  # no `deploy`: everything
    assert work_from_history(()) is None


def test_a_fresh_workload_report_outranks_the_history_buffer():
    """Calibration lesson 1: the Innie says it is free, so no queue buffer and the input time is now."""
    free = WorkloadReport(innie="alfa", reported_at=NOW - timedelta(minutes=10), slots=1)
    e = estimate(NOW, "alfa", buffer=BUSY, workload=free, trial=TrialRun(hours=2.0), input_at=NOW - timedelta(hours=5))
    assert e.basis == "workload" and e.work_basis == "trial"
    assert e.input_at == NOW  # not the five-hours-ago arrival: a free Innie starts now
    assert "queue" not in e.buffer and e.buffer_hours == pytest.approx(1.25)  # sign-off lag, plus the history offline buffer (#64 note 2)
    assert e.buffer["offline"] == pytest.approx(0.75) and e.eta == NOW + timedelta(hours=3.25)


def test_queue_buffer_only_for_jobs_actually_ahead_in_the_slot():
    report = WorkloadReport(innie="alfa", reported_at=NOW, in_progress=(Slot(ref="#44", remaining_hours=0.75),),
                            queued=(Slot(ref="#45", remaining_hours=2.0), Slot(ref="#46", remaining_hours=4.0, ahead=False)))
    e = estimate(NOW, "alfa", buffer=BUSY, workload=report, trial=TrialRun(hours=1.0), will_prompt_signoff=True)
    assert e.buffer["queue"] == pytest.approx(2.75)  # #44 and #45; #46 runs beside us
    assert e.buffer["offline"] == pytest.approx(0.75)  # the report names no offline_until: the history buffer stays (#64 note 2)
    assert e.buffer_hours == pytest.approx(3.5) and e.eta == NOW + timedelta(hours=4.5)


def test_a_stale_report_is_not_evidence_and_the_history_buffer_comes_back():
    stale = WorkloadReport(innie="alfa", reported_at=NOW - timedelta(hours=6))
    e = estimate(NOW, "alfa", buffer=BUSY, workload=stale, trial=TrialRun(hours=2.0), fresh_within_hours=1.0)
    assert e.basis == "trial" and e.buffer["queue"] == pytest.approx(1.5)


def test_a_report_from_the_future_is_not_fresh():
    assert not WorkloadReport(innie="alfa", reported_at=NOW + timedelta(hours=1)).fresh_at(NOW, 1.0)


def test_a_workload_report_for_another_innie_is_refused():
    with pytest.raises(ValueError, match="workload report is for"):
        estimate(NOW, "alfa", workload=WorkloadReport(innie="bravo", reported_at=NOW), reasoning_hours=1.0)


# -- the buffer's four pieces ---------------------------------------------------
def test_signoff_buffer_drops_when_the_floor_manager_will_prompt():
    kw = dict(buffer=InnieBuffer(innie="alfa", signoff_hours=0.5), reasoning_hours=1.0)
    assert estimate(NOW, "alfa", **kw).buffer["signoff"] == 0.5
    assert "signoff" not in estimate(NOW, "alfa", will_prompt_signoff=True, **kw).buffer


def test_rate_limit_and_offline_come_from_the_report_when_there_is_one():
    report = WorkloadReport(innie="alfa", reported_at=NOW, rate_limited_hours=0.5, offline_until=NOW + timedelta(hours=8))
    e = estimate(NOW, "alfa", buffer=BUSY, workload=report, reasoning_hours=1.0, will_prompt_signoff=True)
    assert e.buffer["rate_limit"] == pytest.approx(0.5) and e.buffer["offline"] == pytest.approx(8.0)


def test_an_offline_window_already_past_costs_nothing():
    report = WorkloadReport(innie="alfa", reported_at=NOW, offline_until=NOW - timedelta(hours=2))
    assert "offline" not in estimate(NOW, "alfa", buffer=BUSY, workload=report, reasoning_hours=1.0, will_prompt_signoff=True).buffer


def test_zero_pieces_are_left_out_of_the_breakdown_but_the_rationale_still_explains():
    e = estimate(NOW, "alfa", buffer=InnieBuffer(innie="alfa"), reasoning_hours=1.0)
    assert e.buffer == {} and e.buffer_hours == 0.0 and "no buffer" in e.rationale
    assert "1.00 h work from reasoning" in e.rationale


def test_a_trial_run_without_a_positive_scale_is_an_error():
    with pytest.raises(ValueError, match="scale must be positive"):
        TrialRun(hours=1.0, scale=0.0).work_hours


# -- scoring the FIRST committed ETA -------------------------------------------
def test_score_is_over_the_first_committed_eta_only():
    committed = [(NOW, NOW + timedelta(hours=2)), (NOW + timedelta(hours=1), NOW + timedelta(hours=9))]
    assert first_committed_eta(committed) == NOW + timedelta(hours=2)
    assert first_committed_eta(reversed(committed)) == NOW + timedelta(hours=2)
    assert first_committed_eta([]) is None
    s = score([Scored(ref="#1", first_eta=first_committed_eta(committed), delivered_at=NOW + timedelta(hours=3))])
    assert (s.late, s.early) == (1, 0)  # scored against the 2 h ETA; the 9 h re-estimate would have read 6 h early
    assert s.bias_hours == pytest.approx(1.0)


def test_mae_bias_late_and_early_over_a_wave():
    d = [-1.88, -1.65, -0.5, -0.25, -0.1, -0.02, 0.27, 0.0]  # Wave A's shape: mostly early, one late
    s = score([Scored(ref=f"#{i}", first_eta=NOW, delivered_at=NOW + timedelta(hours=h)) for i, h in enumerate(d)])
    assert s.jobs_scored == 8
    assert s.mae_hours == pytest.approx(sum(abs(h) for h in d) / 8)
    assert s.bias_hours == pytest.approx(sum(d) / 8)
    assert (s.late, s.early) == (1, 6)  # the 0.0 is on time, neither late nor early
    assert s.max_late_hours == pytest.approx(0.27) and s.max_early_hours == pytest.approx(-1.88)


def test_early_is_an_error_the_same_as_late():
    early = score([Scored(ref="#1", first_eta=NOW, delivered_at=NOW - timedelta(hours=2))])
    late = score([Scored(ref="#2", first_eta=NOW, delivered_at=NOW + timedelta(hours=2))])
    assert early.mae_hours == late.mae_hours == pytest.approx(2.0)
    assert early.bias_hours == pytest.approx(-2.0) and late.bias_hours == pytest.approx(2.0)
    assert (early.early, early.late) == (1, 0) and (late.early, late.late) == (0, 1)


def test_tolerance_band_counts_small_misses_as_on_time():
    rows = [Scored(ref="#1", first_eta=NOW, delivered_at=NOW + timedelta(minutes=6)), Scored(ref="#2", first_eta=NOW, delivered_at=NOW - timedelta(minutes=6))]
    assert (score(rows).late, score(rows).early) == (1, 1)
    loose = score(rows, tolerance_hours=0.25)
    assert (loose.late, loose.early) == (0, 0) and loose.jobs_scored == 2 and loose.mae_hours == pytest.approx(0.1)


def test_scoring_nothing_is_a_zero_score_not_a_crash():
    assert score([]) == Score() and score([]).jobs_scored == 0


def test_error_hours_is_delivered_minus_eta_so_negative_is_early():
    assert Scored(ref="#1", first_eta=NOW, delivered_at=NOW - timedelta(hours=1.5)).error_hours == pytest.approx(-1.5)

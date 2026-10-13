"""#94 job 1 (ref fm-wave-i-assign): `Config.ledger` and the ledger's row writers.

`open_job` keeps the first committed ETA (R52) and notes a re-estimate; `deliver` sets the actual and error hours;
`reschedule` records the cause and opens a `<role> (resched)` row, left out of the score; `write_wave` takes `wave` +
`source` and an `eta.Score` or a `Scorecard`; times are UTC ISO.
"""
from __future__ import annotations

import pytest

from fridica_research import config, eta, ledger

S, REF = "T1:C1:1791854570.721269", "fm-wave-i-job1"


@pytest.fixture
def db(): return ledger.connect()


def row(db, role="implement"): return db.execute("SELECT * FROM jobs WHERE source=? AND ref=? AND role=?", (S, REF, role)).fetchone()


def test_config_ledger_round_trips_and_defaults_to_empty():
    assert config.parse("").ledger == "" and config.Config().to_dict()["ledger"] == ""
    c = config.parse('ledger = "~/.local/state/fridica-research/jobs.sqlite3"\n')
    assert c.ledger == "~/.local/state/fridica-research/jobs.sqlite3" and c.to_dict()["ledger"] == c.ledger


def test_open_job_is_idempotent_and_keeps_the_first_eta(db):
    ledger.open_job(db, source=S, ref=REF, role="implement", innie="imp1", input_at="2026-10-13T02:00:00+00:00", eta="2026-10-13T03:00:00+00:00", est_hours=1.0)
    ledger.open_job(db, source=S, ref=REF, role="implement", innie="imp1", input_at="2026-10-13T02:00:00+00:00", eta="2026-10-13T03:00:00+00:00", est_hours=1.0)
    ledger.open_job(db, source=S, ref=REF, role="implement", innie="imp1", input_at="2026-10-13T02:00:00+00:00", eta="2026-10-13T04:00:00+00:00", est_hours=2.0)
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    r = row(db)
    assert r["eta"] == "2026-10-13T03:00:00+00:00" and r["est_hours"] == 1.0 and "re-estimate 2026-10-13T04:00:00+00:00" in r["notes"]


def test_deliver_sets_actual_and_error_hours(db):
    ledger.open_job(db, source=S, ref=REF, role="implement", innie="imp1", input_at="2026-10-13T02:00:00+00:00", eta="2026-10-13T03:00:00+00:00", est_hours=1.0)
    ledger.deliver(db, source=S, ref=REF, role="implement", delivered_at="2026-10-13T03:30:00+00:00")
    r = row(db)
    assert r["delivered_at"] == "2026-10-13T03:30:00+00:00" and r["actual_hours"] == pytest.approx(1.5) and r["error_hours"] == pytest.approx(0.5)


def test_reschedule_opens_a_resched_row_left_out_of_the_score(db):
    ledger.open_job(db, source=S, ref=REF, role="implement", innie="imp1", input_at="2026-10-13T02:00:00+00:00", eta="2026-10-13T03:00:00+00:00", est_hours=1.0)
    ledger.reschedule(db, source=S, ref=REF, role="implement", to_innie="imp9", eta="2026-10-13T04:00:00+00:00", at="2026-10-13T03:07:00+00:00")
    ledger.reschedule(db, source=S, ref=REF, role="implement", to_innie="imp9", eta="2026-10-13T04:00:00+00:00", at="2026-10-13T03:07:00+00:00")
    rs = row(db, "implement (resched)")
    assert rs["innie"] == "imp9" and rs["eta"] == "2026-10-13T04:00:00+00:00" and "rescheduled 2026-10-13T03:07:00+00:00" in row(db)["delay_causes"]
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    ledger.deliver(db, source=S, ref=REF, role="implement", delivered_at="2026-10-13T05:00:00+00:00")  # scored against its first ETA
    ledger.deliver(db, source=S, ref=REF, role="implement (resched)", delivered_at="2026-10-13T05:00:00+00:00")
    assert ledger.close_text_score(db, S) == "n=1, MAE 2.00h, bias +2.00h, late 1, early 0"


@pytest.mark.parametrize("score", [eta.Score(jobs_scored=3, mae_hours=0.5, bias_hours=-0.25, late=1, early=2),
                                   ledger.Scorecard(jobs=4, delivered=3, mae=0.5, bias=-0.25, late=1, early=2)])
def test_write_wave_takes_wave_and_source_and_either_score(db, score):
    for _ in range(2):
        ledger.write_wave(db, wave="I", source=S, opened="2026-10-13T02:10:00+00:00", closed="2026-10-13T04:00:00+00:00", crew="U1, U2",
                          prs="#114, #115", main_before="e0bd126", main_after="22adf64", validation="pytest", score=score)
    rows = db.execute("SELECT * FROM waves").fetchall()
    assert len(rows) == 1
    w = rows[0]
    assert (w["wave"], w["thread"], w["prs"], w["jobs_scored"], w["mae_hours"], w["bias_hours"], w["late"], w["early"]) == ("I", S, "#114, #115", 3, 0.5, -0.25, 1, 2)


def test_close_text_score_on_an_empty_source(db): assert ledger.close_text_score(db, S) == "n=0, MAE 0.00h, bias +0.00h, late 0, early 0"

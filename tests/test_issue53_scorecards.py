"""#53 PR 2a: ledger scorecards per Innie, role and crew; `score [--tolerance H] [--board]`; and the two #64 notes.

Every row is synthetic. `--board` is tested with the fake gh runner only: no live Project v2 write.
"""
import sqlite3

import pytest

from fridica_research import board as board_mod
from fridica_research import ledger
from fridica_research.cli import main
from test_board import BCFG, FakeGh

E = "2026-10-11T{}:00Z"


def job(i, innie, role, eta, delivered, error_hours=None):
    return dict(id=i, ref=f"#{i}", role=role, innie=innie, eta=E.format(eta) if eta else None,
                delivered_at=E.format(delivered) if delivered else None, error_hours=error_hours)


JOBS = [
    job(1, "alfa", "implementer", "18:00", "17:00", error_hours=99.0),  # 1 h early; the stored error_hours is ignored
    job(2, "alfa", "auditor", "18:00", "20:00"),  # 2 h late
    job(3, "bravo", "implementer", "18:00", "18:30"),  # 0.5 h late
    job(4, "bravo", "implementer", "18:00", None),  # not delivered
]


@pytest.fixture
def db():
    conn = ledger.connect()
    ledger.import_rows(conn, "jobs", JOBS)
    yield conn
    conn.close()


def test_scorecards_per_innie_role_and_crew_with_the_seven_fields(db):
    cards = ledger.scorecards(db)
    alfa = cards["innie"]["alfa"]
    assert (alfa.jobs, alfa.delivered, alfa.coverage, alfa.late, alfa.early) == (2, 2, 1.0, 1, 1)
    assert alfa.mae == pytest.approx(1.5) and alfa.bias == pytest.approx(0.5)  # errors -1 and +2, recomputed from eta and delivered_at
    bravo = cards["innie"]["bravo"]
    assert (bravo.jobs, bravo.delivered, bravo.coverage) == (2, 1, 0.5) and bravo.mae == pytest.approx(0.5)
    impl = cards["role"]["implementer"]
    assert (impl.jobs, impl.delivered, impl.late, impl.early) == (3, 2, 1, 1) and impl.bias == pytest.approx(-0.25)
    crew = cards["crew"]
    assert (crew.jobs, crew.delivered, crew.coverage) == (4, 3, 0.75)
    assert crew.mae == pytest.approx(3.5 / 3) and crew.bias == pytest.approx(1.5 / 3) and (crew.late, crew.early) == (2, 1)
    assert set(ledger.Scorecard.__dataclass_fields__) == {"jobs", "delivered", "coverage", "mae", "bias", "late", "early"}


def test_scorecards_reuse_eta_score_and_its_tolerance(db, monkeypatch):
    calls = []
    real = ledger.eta.score
    monkeypatch.setattr(ledger.eta, "score", lambda results, tol=0.0: (calls.append(tol), real(results, tol))[1])
    crew = ledger.scorecards(db, tolerance_hours=0.75)["crew"]
    assert calls and set(calls) == {0.75}
    assert (crew.late, crew.early) == (1, 1)  # the 0.5 h late job is on time within 0.75 h


def test_score_prints_the_table(tmp_path, capsys):
    path = tmp_path / "jobs.sqlite3"
    ledger.import_rows(ledger.connect(path), "jobs", JOBS)
    assert main(["score", str(path), "--tolerance", "0.75"]) == 0
    out = capsys.readouterr().out
    assert "| innie | alfa | 2 | 2 | 100% | 1.50 | +0.50 | 1 | 1 |" in out
    assert "| crew | all | 4 | 3 | 75% |" in out


def test_score_on_a_missing_ledger_exits_non_zero_and_creates_nothing(tmp_path, capsys):
    path = tmp_path / "nope" / "jobs.sqlite3"
    assert main(["score", str(path)]) != 0
    assert not path.exists() and not path.parent.exists()
    assert "no ledger" in capsys.readouterr().err


def test_score_board_writes_the_project_readme_through_set_readme(tmp_path, monkeypatch, capsys):
    path = tmp_path / "jobs.sqlite3"
    ledger.import_rows(ledger.connect(path), "jobs", JOBS)
    cfg = tmp_path / "research.toml"
    cfg.write_text('[board]\nenabled = true\nowner = "chengcli"\nnumber = 8\nrepo = "chengcli/fridica-research"\n')
    gh = FakeGh()
    readme = []
    real = gh.answer
    def answer(query, v):
        if "updateProjectV2(input:" in query:
            readme.append(v)
            return {"updateProjectV2": {"projectV2": {"id": v["project"]}}}
        return real(query, v)
    gh.answer = answer
    monkeypatch.setattr(board_mod, "subprocess_runner", lambda token_env: gh)
    assert main(["--config", str(cfg), "score", str(path), "--board"]) == 0
    table = capsys.readouterr().out.split("\nwrote")[0].rstrip("\n")
    assert readme == [{"project": "PVT_1", "readme": table}]
    assert all(a[:3] == ["gh", "api", "graphql"] for a, _ in gh.calls)  # discovery plus the one mutation, nothing else


def test_set_readme_is_one_update_project_mutation():
    gh = FakeGh()
    seen = []
    real = gh.answer
    gh.answer = lambda q, v: (seen.append((q, v)), {"updateProjectV2": {"projectV2": {"id": "PVT_1"}}})[1] if "updateProjectV2(" in q else real(q, v)
    board_mod.Projects(BCFG, gh).set_readme("# scores")
    assert [v for q, v in seen] == [{"project": "PVT_1", "readme": "# scores"}]


def test_score_board_without_a_board_config_exits_non_zero(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    ledger.import_rows(ledger.connect(path), "jobs", JOBS)
    cfg = tmp_path / "research.toml"
    cfg.write_text("")
    assert main(["--config", str(cfg), "score", str(path), "--board"]) == 1


# -- #64 note 1 ------------------------------------------------------------------
def test_import_rows_validates_the_whole_batch_before_its_first_write(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    db = ledger.connect(path)
    with pytest.raises(ValueError, match="unknown column"):
        ledger.import_rows(db, "jobs", [JOBS[0], JOBS[1], {**JOBS[2], "bogus": 1}])
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0  # nothing half-imported on disk either


# -- #64 note 2 ------------------------------------------------------------------
def test_offline_buffer_kept_without_offline_until_and_zeroed_only_at_or_before_the_start():
    from datetime import datetime, timedelta, timezone

    from fridica_research.eta import InnieBuffer, WorkloadReport, estimate
    now = datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc)
    buf = InnieBuffer(innie="alfa", offline_hours=0.75)
    def offline(**kw):
        return estimate(now, "alfa", buffer=buf, workload=WorkloadReport(innie="alfa", reported_at=now, **kw), reasoning_hours=1.0).buffer.get("offline", 0.0)
    assert offline() == pytest.approx(0.75)  # a fresh report with no offline_until keeps the history buffer
    assert offline(offline_until=now) == 0.0  # at the start: zero
    assert offline(offline_until=now - timedelta(hours=1)) == 0.0  # before the start: zero
    assert offline(offline_until=now + timedelta(hours=3)) == pytest.approx(3.0)  # later: the hours left

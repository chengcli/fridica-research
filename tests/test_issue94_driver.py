"""#94, #84: the driver writes the job ledger and ingests workload posts (ref fm-wave-i-assign, job 2).

Only with `cfg.ledger` set. Each wave job gets one `jobs` row per role (`implement`, `code_audit`) through
`ledger.open_job`, scored against `first_etas`; `deliver` runs on the original role when the PR is first recorded
and at the auditor's first APPROVED or CHANGES_REQUESTED on the head; a new holder is `reschedule`d once. At the
close the `waves` row is written through `write_wave` and the close post carries `close_text_score`. Crew posts in
a wave thread go through `ledger.ingest_posts`; its findings are wave findings, never posts.
"""
from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from fridica_research import contracts, ledger, wave
from fridica_research.config import Config
from fridica_research.driver import Driver
from support import CFG
from test_issue69_wave import H, HEAD, JOBS, PEOPLE, T0, THREAD
from test_issue69b_driver import CLOSE_REF, WS, CH, ROOT, approve, closes, make, setup, stored
from test_issue84_workload import LINE

JOBS2 = JOBS[:2]
REGISTER = '''
[innie.imp1]
outie = "Imp One"
slack = "U0IMP1"
github = "imp1"
'''


# -- STAND-IN for job 1 (#94, config.py + ledger.py): drop at rebase once job 1 merges ----------------
# Installed only when `ledger` lacks the writer, so job 1's own functions win as soon as they exist.
def _open_job(db, *, source, ref, role, innie, input_at, eta, est_hours, est_basis="", nature="", size=""):
    if db.execute("SELECT 1 FROM jobs WHERE source=? AND ref=? AND role=?", (source, ref, role)).fetchone(): return
    db.execute("INSERT INTO jobs(source, ref, role, innie, input_at, eta, est_hours, est_basis, nature, size) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (source, ref, role, innie, input_at, eta, est_hours, est_basis, nature, size))


def _deliver(db, *, source, ref, role, delivered_at):
    db.execute("UPDATE jobs SET delivered_at=? WHERE source=? AND ref=? AND role=?", (delivered_at, source, ref, role))


def _reschedule(db, *, source, ref, role, to_innie, eta, at):
    db.execute("UPDATE jobs SET delay_causes=? WHERE source=? AND ref=? AND role=?", (f"rescheduled {at}", source, ref, role))
    _open_job(db, source=source, ref=ref, role=f"{role} (resched)", innie=to_innie, input_at=at, eta=eta, est_hours=None)


def _write_wave(db, *, wave, source, opened, closed, crew, prs, main_before, main_after, validation, score):
    db.execute("INSERT OR REPLACE INTO waves(wave, thread, opened, closed, crew, prs, main_before, main_after, validation, jobs_scored, mae_hours, bias_hours, late, early)"
               " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (wave, source, opened, closed, crew, prs, main_before, main_after, validation,
                                                         getattr(score, "jobs_scored", getattr(score, "delivered", 0)), getattr(score, "mae_hours", getattr(score, "mae", 0.0)),
                                                         getattr(score, "bias_hours", getattr(score, "bias", 0.0)), score.late, score.early))


def _close_text_score(db, source):
    c = ledger.card(db.execute("SELECT * FROM jobs WHERE source=? AND role NOT LIKE '% (resched)'", (source,)).fetchall())
    return f"n={c.delivered}, MAE {c.mae:.2f}h, bias {c.bias:+.2f}h, late {c.late}, early {c.early}"


STAND_INS = {"open_job": _open_job, "deliver": _deliver, "reschedule": _reschedule, "write_wave": _write_wave, "close_text_score": _close_text_score}


@pytest.fixture(autouse=True)
def job1_ledger(monkeypatch):
    for name, fn in STAND_INS.items():
        if not hasattr(ledger, name): monkeypatch.setattr(ledger, name, fn, raising=False)


def ledger_cfg(tmp_path) -> Config:
    """CFG with `ledger` and `register` set; a stand-in subclass while job 1's `Config.ledger` is not on main."""
    reg = tmp_path / "register.toml"
    reg.write_text(REGISTER)
    kw = {"ledger": str(tmp_path / "ledger" / "jobs.sqlite3"), "register": str(reg)}
    if "ledger" in {f.name for f in dataclasses.fields(Config)}: return dataclasses.replace(CFG, **kw)
    LedgerConfig = dataclasses.make_dataclass("LedgerConfig", [("ledger", str, "")], bases=(Config,), frozen=True)  # STAND-IN: drop at rebase
    return LedgerConfig(**{**{f.name: getattr(CFG, f.name) for f in dataclasses.fields(CFG)}, **kw})
# -- end of the stand-ins --------------------------------------------------------------------------


def world(tmp_path):
    store, client, gh, clock, drv = setup(tmp_path, jobs=JOBS2)
    drv.cfg = ledger_cfg(tmp_path)
    return store, client, gh, clock, drv


def rows(tmp_path, table="jobs") -> list[dict]:
    path = tmp_path / "ledger" / "jobs.sqlite3"
    if not path.exists(): return []
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY id" if table != "waves" else "SELECT * FROM waves")]


def iso(t: float) -> str: return Driver.utc_iso(t)


def changes(gh, n: int, login: str, rid: int):
    gh.reviews[n] = [{"id": rid, "user": {"login": login, "type": "User"}, "state": "CHANGES_REQUESTED", "commit_id": HEAD, "submitted_at": "2026-10-12T13:00:00Z", "body": ""}]


def run_wave(store, client, gh, clock, drv, tmp_path):
    drv.run_once()  # T0+H: both PRs adopted
    clock.t = T0 + 2 * H
    approve(gh, 71, "aud1", 901)
    changes(gh, 72, "aud2", 902)
    drv.run_once()  # the views and reviews: job 1 approved, job 2 changes requested, both on the head


def test_two_job_wave_rows_opened_once_delivered_on_time_and_kept_across_a_restart(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    run_wave(store, client, gh, clock, drv, tmp_path)
    got = {(r["ref"], r["role"]): r for r in rows(tmp_path)}
    assert sorted(got) == [("fm-wave-d-job1", "code_audit"), ("fm-wave-d-job1", "implement"), ("fm-wave-d-job2", "code_audit"), ("fm-wave-d-job2", "implement")]
    assert all(r["source"] == THREAD for r in got.values())
    assert got[("fm-wave-d-job1", "implement")]["innie"] == "imp1"  # the register's name
    assert got[("fm-wave-d-job2", "implement")]["innie"] == PEOPLE["imp2"]  # not registered: the Slack id
    assert got[("fm-wave-d-job1", "implement")]["eta"] == iso(T0 + 2 * H) and got[("fm-wave-d-job1", "code_audit")]["eta"] == iso(T0 + 4 * H)
    assert [got[("fm-wave-d-job%d" % n, "implement")]["delivered_at"] for n in (1, 2)] == [iso(T0 + H)] * 2  # the PR's first record
    assert [got[("fm-wave-d-job%d" % n, "code_audit")]["delivered_at"] for n in (1, 2)] == [iso(T0 + 2 * H)] * 2  # APPROVED, and CHANGES_REQUESTED
    before = rows(tmp_path)
    drv2 = make(store, client, gh, clock)  # restart: a new driver on the same store and ledger
    drv2.cfg = drv.cfg
    clock.t = T0 + 3 * H
    approve(gh, 72, "aud2", 903)
    drv2.run_once()
    assert rows(tmp_path) == before  # no row twice, no delivery moved


def test_the_close_writes_the_waves_row_once_and_the_close_post_carries_the_score(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    run_wave(store, client, gh, clock, drv, tmp_path)
    gh.merge(71, "1" * 40)
    gh.merge(72, "2" * 40)
    clock.t = T0 + 3 * H
    drv.run_once()
    assert stored(store).closed_at is not None
    for _ in range(2):
        clock.t += H
        drv.run_once()
    waves = rows(tmp_path, "waves")
    assert len(waves) == 1 and waves[0]["wave"] == "d" and waves[0]["thread"] == THREAD
    assert waves[0]["prs"] == "#71, #72" and waves[0]["main_after"] in ("1" * 40, "2" * 40) and waves[0]["jobs_scored"] == 4
    assert waves[0]["closed"] == iso(T0 + 3 * H)
    db = ledger.connect(tmp_path / "ledger" / "jobs.sqlite3")
    line = ledger.close_text_score(db, THREAD)
    assert len(closes(client)) == 1
    text = closes(client)[0]
    assert f"score: {line}" in text and text.splitlines()[-1] == f"ref: {CLOSE_REF}" and line.startswith("n=4,")
    assert len(rows(tmp_path)) == 4


def test_a_reassigned_role_is_rescheduled_once_and_delivered_on_the_original_role(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    drv.run_once()
    w = wave.reassign(stored(store), 2, "auditor", {"slack": "U0AUD3", "login": "aud3"}, T0 + 5 * H, now=clock.t, cfg=CFG)
    store.set_meta(wave.wave_key(THREAD), w.to_json())
    for _ in range(2):
        clock.t += H
        drv.run_once()
    r = rows(tmp_path)
    assert [(x["ref"], x["role"]) for x in r if "resched" in x["role"]] == [("fm-wave-d-job2", "code_audit (resched)")]
    orig = next(x for x in r if (x["ref"], x["role"]) == ("fm-wave-d-job2", "code_audit"))
    assert orig["innie"] == PEOPLE["aud2"] and orig["eta"] == iso(T0 + 4 * H)  # the first ETA stays on the original row
    approve(gh, 72, "aud3", 904)
    clock.t += H
    drv.run_once()
    r = rows(tmp_path)
    assert next(x for x in r if (x["ref"], x["role"]) == ("fm-wave-d-job2", "code_audit"))["delivered_at"] == iso(clock.t)
    assert next(x for x in r if x["role"] == "code_audit (resched)")["delivered_at"] is None  # deliver goes to the original role only


def feed(client, clock, sender: str, text: str) -> str:
    ts = f"{clock.t - 60:.6f}"  # posted a minute before the driver's clock
    client.feed.append({"cursor": len(client.feed) + 1, "kind": "message", "workspace": WS, "channel": {"id": CH}, "thread": ROOT, "ts": ts, "sender": sender, "text": text, "turn_kind": ""})
    return ts


def test_a_workload_report_post_becomes_one_workload_row_and_a_bad_one_a_finding(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    clock.t = 1_791_820_000.0  # after the posts' ts, so intake takes them as posted
    ts = feed(client, clock, PEOPLE["imp1"], "Wave I roll call:\nslots: 1/2\n" + LINE)
    feed(client, clock, "U0STRANGER", "slots: 2/2\n" + LINE)
    n = len(client.posts)
    drv.run_once()
    drv.run_once()
    wl = rows(tmp_path, "workload")
    assert len(wl) == 1 and wl[0]["innie"] == "imp1" and wl[0]["source"] == f"{THREAD}/{ts}" and (wl[0]["free_slots"], wl[0]["total_slots"]) == (1, 2)
    found = [f for f in stored(store).findings if f.startswith("workload ")]
    assert len(found) == 1 and "U0STRANGER" in found[0] and "register" in found[0]
    assert not any("U0STRANGER" in t or "not ingested" in t for _, t in client.posts[n:])  # findings are never posted


def test_without_cfg_ledger_nothing_is_written(tmp_path):
    store, client, gh, clock, drv = setup(tmp_path, jobs=JOBS2)
    assert getattr(drv.cfg, "ledger", "") == ""
    run_wave(store, client, gh, clock, drv, tmp_path)
    gh.merge(71, "1" * 40)
    gh.merge(72, "2" * 40)
    clock.t = T0 + 3 * H
    drv.run_once()
    assert stored(store).closed_at is not None and drv._ledger is None
    assert not (tmp_path / "ledger").exists() and [p.name for p in tmp_path.iterdir() if not p.name.startswith("s.sqlite3")] == []
    assert "score:" not in closes(client)[0]
    assert not any(k.startswith(f"ledger:{THREAD}") for (k,) in store.db.execute("SELECT key FROM meta"))
    assert contracts.ref_of(closes(client)[0]) == CLOSE_REF


# -- audit follow-ups: ledger errors fail soft; one score for the row and the post --------------------
def locked(*a, **kw): raise sqlite3.OperationalError("database is locked")


def test_a_ledger_error_is_a_wave_finding_the_wave_is_saved_and_run_once_returns(tmp_path, monkeypatch):
    store, client, gh, clock, drv = world(tmp_path)
    good = ledger.open_job
    monkeypatch.setattr(ledger, "open_job", locked)
    drv.run_once()  # returns: the error never reaches serve
    w = stored(store)
    assert [j.rnd().pr.get("number") for j in w.jobs] == [71, 72]  # the PR adoption was saved despite the ledger
    assert any(f.startswith("ledger ") and "database is locked" in f for f in w.findings)
    assert not any("database is locked" in t for _, t in client.posts)  # a finding, never a post
    monkeypatch.setattr(ledger, "open_job", good)
    clock.t += H
    drv.run_once()
    assert len(rows(tmp_path)) == 4  # the next pass writes what the failed one could not


def test_a_ledger_error_at_the_close_still_closes_and_posts_the_wave(tmp_path, monkeypatch):
    store, client, gh, clock, drv = world(tmp_path)
    run_wave(store, client, gh, clock, drv, tmp_path)
    monkeypatch.setattr(ledger, "write_wave", locked)
    gh.merge(71, "1" * 40)
    gh.merge(72, "2" * 40)
    clock.t = T0 + 3 * H
    drv.run_once()
    w = stored(store)
    assert w.closed_at is not None and w.pending == [] and len(closes(client)) == 1
    assert any(f.startswith("ledger ") and "database is locked" in f for f in w.findings)


def test_a_failing_ledger_connect_fails_soft(tmp_path, monkeypatch):
    store, client, gh, clock, drv = world(tmp_path)
    monkeypatch.setattr(ledger, "connect", locked)
    drv.run_once()
    w = stored(store)
    assert [j.rnd().pr.get("number") for j in w.jobs] == [71, 72] and any("database is locked" in f for f in w.findings)


def test_the_waves_row_and_the_close_post_carry_the_same_score(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    run_wave(store, client, gh, clock, drv, tmp_path)  # job 2's code_audit is delivered at CHANGES_REQUESTED (T0+2h), not at the merge
    gh.merge(71, "1" * 40)
    gh.merge(72, "2" * 40)
    clock.t = T0 + 3 * H
    drv.run_once()
    row = rows(tmp_path, "waves")[0]
    line = next(ln for ln in closes(client)[0].splitlines() if ln.startswith("score: "))
    assert (row["mae_hours"], row["bias_hours"], row["late"], row["early"]) == (1.5, -1.5, 0, 4)  # implement -1h x2, code_audit -2h x2
    assert line == f"score: n=4, MAE {row['mae_hours']:.2f}h, bias {row['bias_hours']:+.2f}h, late {row['late']}, early {row['early']}"

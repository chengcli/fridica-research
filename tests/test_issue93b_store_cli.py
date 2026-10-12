"""#93 (store and CLI part), Cheng's four rulings for wave G job 4:

1. `<state>.lock` holds the holder's pid, and the error on a held lock names the lock path and that pid.
2. Every wave CLI write runs inside `Store.transaction()`.
3. `wave close --reason` refuses while any job is open or the wave is already closed, and records the reason as a wave finding.
4. `serve` closes the store in a `finally`.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3

import pytest

from fridica_research import cli, store as store_mod, wave
from fridica_research.store import Store
from support import CFG, OWNER

THREAD = "T1:C1:1791823244.369619"
REPO = "chengcli/fridica-research"
T0 = 1_700_000_000.0
H = 3600.0


def write_config(tmp_path) -> str:
    p = tmp_path / "research.toml"
    p.write_text(f'channels = ["C1"]\nstate_path = "{tmp_path / "s.sqlite3"}"\n[fridica]\nsocket = "{tmp_path / "c.sock"}"\nowner = "{OWNER}"\n')
    return str(p)


def spec(issue: int, path: str, imp: str, aud: str) -> str:
    return f"issue={issue};files={path};implementer=U0{imp.upper()}:{imp};auditor=U0{aud.upper()}:{aud};pr_eta=2026-10-12T17:05Z;review_eta=2026-10-12T17:25Z"


def job(issue: int, path: str, imp: str, aud: str) -> dict:
    return {"issue": issue, "files": [path], "implementer": {"slack": f"U0{imp.upper()}", "login": imp}, "auditor": {"slack": f"U0{aud.upper()}", "login": aud},
            "pr_eta": T0 + 2 * H, "review_eta": T0 + 4 * H}


def stored(tmp_path) -> wave.Wave:
    s = Store(tmp_path / "s.sqlite3")
    try: return wave.Wave.from_json(s.get_meta(wave.wave_key(THREAD)))
    finally: s.close()


def put(tmp_path, w: wave.Wave):
    s = Store(tmp_path / "s.sqlite3")
    try: s.set_meta(wave.wave_key(THREAD), w.to_json())
    finally: s.close()


# -- ruling 1: the lock holds the pid, and the error names the lock path and the pid ------
def test_lock_file_holds_the_pid_and_a_held_lock_names_path_and_pid(tmp_path):
    path = tmp_path / "s.sqlite3"
    a, b = Store(path), Store(path)
    try:
        assert a.lock_path == tmp_path / "s.sqlite3.lock"
        with a.lock():
            assert a.lock_path.read_text().strip() == str(os.getpid())
            with pytest.raises(store_mod.LockHeld) as e:
                with b.lock(): pass
            assert str(a.lock_path) in str(e.value) and str(os.getpid()) in str(e.value)
        with b.lock():  # released with the holder's `with`, and the stale pid is gone
            assert b.lock_path.read_text().strip() == str(os.getpid())
    finally:
        a.close(); b.close()


def test_serve_refuses_while_another_holder_has_the_lock(tmp_path, capsys):
    cfgp = write_config(tmp_path)
    holder = Store(tmp_path / "s.sqlite3")
    try:
        with holder.lock():
            assert cli.main(["--config", cfgp, "serve", "--once"]) == 1
            err = capsys.readouterr().err
        assert str(holder.lock_path) in err and str(os.getpid()) in err
    finally:
        holder.close()


def test_two_refused_serves_in_a_row_both_name_the_live_holder(tmp_path, capsys):
    """A refusal takes no flock, so it must not clear the lock file: otherwise the second refusal says 'pid unknown'
    while the first holder is still serving (Linfeng's #107 finding: pid 769 named, file emptied, then 'unknown')."""
    cfgp = write_config(tmp_path)
    holder = Store(tmp_path / "s.sqlite3")
    try:
        with holder.lock():
            for attempt in (1, 2):
                assert cli.main(["--config", cfgp, "serve", "--once"]) == 1, attempt
                err = capsys.readouterr().err
                assert f"is held by pid {os.getpid()}" in err, f"refusal {attempt}: {err!r}"
                assert holder.lock_path.read_text().strip() == str(os.getpid()), f"refusal {attempt} erased the holder's pid"
    finally:
        holder.close()


def test_in_memory_store_locks_nothing(tmp_path):
    s = Store()
    try:
        assert s.lock_path is None
        with s.lock() as fh: assert fh is None
    finally:
        s.close()


# -- ruling 2: every wave CLI write goes through Store.transaction() ----------------------
class SpyStore(Store):
    """Records whether a `set_meta` ran inside `transaction()`, and whether `close()` was called."""

    def __init__(self, path):
        super().__init__(path)
        self.depth, self.outside, self.transactions, self.closed = 0, [], 0, False

    @contextlib.contextmanager
    def transaction(self):
        self.depth += 1
        self.transactions += 1
        try:
            with super().transaction(): yield self
        finally:
            self.depth -= 1

    def set_meta(self, key, value):
        if self.depth == 0: self.outside.append(key)
        return super().set_meta(key, value)

    def close(self):
        self.closed = True
        super().close()


def spy_stores(monkeypatch) -> list[SpyStore]:
    made: list[SpyStore] = []

    def make(path):
        s = SpyStore(path)
        made.append(s)
        return s
    monkeypatch.setattr(cli, "Store", make)
    return made


def test_transaction_is_immediate_and_rolls_back(tmp_path):
    s = Store(tmp_path / "s.sqlite3")
    try:
        with s.transaction():
            assert s.db.in_transaction
            s.set_meta("k", "v")
        assert s.get_meta("k") == "v"
        with pytest.raises(RuntimeError):
            with s.transaction():
                s.set_meta("k", "w")
                raise RuntimeError("boom")
        assert s.get_meta("k") == "v"
    finally:
        s.close()


def test_transaction_takes_the_write_lock_before_it_reads(tmp_path):
    """Pins `BEGIN IMMEDIATE`, not a plain `BEGIN`: the writer must hold the write lock from the start, or a
    second writer slips between the CLI's read and its write and the edit is lost."""
    path = tmp_path / "s.sqlite3"
    a, b = Store(path), Store(path)
    try:
        b.db.execute("PRAGMA busy_timeout=0")  # a deferred BEGIN would succeed here instead of raising
        with a.transaction():
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                b.db.execute("BEGIN IMMEDIATE")
        b.db.execute("BEGIN IMMEDIATE")  # the commit released it
        b.db.execute("ROLLBACK")
    finally:
        a.close(); b.close()


def test_every_wave_cli_write_runs_in_a_transaction(tmp_path, monkeypatch, capsys):
    cfgp = write_config(tmp_path)
    made = spy_stores(monkeypatch)
    run = lambda *a: cli.main(["--config", cfgp, "wave", *a])  # noqa: E731
    assert run("open", THREAD, "--name", "g", "--repo", REPO, "--job", spec(93, "src/cli.py", "imp1", "aud1"), "--job", spec(94, "src/store.py", "imp2", "aud2")) == 0
    assert run("bind-pr", THREAD, "1", "--pr", "101") == 0
    assert run("eta", THREAD, "1", "--pr-eta", "2026-10-12T18:00Z") == 0
    assert run("drop", THREAD, "2", "--reason", "folded into job 1") == 0
    capsys.readouterr()
    assert [s.outside for s in made] == [[], [], [], []]  # no wave write outside a transaction
    assert all(s.transactions == 1 for s in made) and len(made) == 4
    assert run("status", THREAD) == 0 and made[-1].transactions == 0  # a read takes no write lock
    assert len(stored(tmp_path).jobs) == 2 and stored(tmp_path).jobs[1].dropped


def test_wave_close_write_runs_in_a_transaction(tmp_path, monkeypatch):
    cfgp = write_config(tmp_path)
    put(tmp_path, terminal_wave())
    made = spy_stores(monkeypatch)
    assert cli.main(["--config", cfgp, "wave", "close", THREAD, "--reason", "every job terminal, the close post was never written"]) == 0
    assert made[0].outside == [] and made[0].transactions == 1


# -- ruling 3: `wave close --reason` ------------------------------------------------------
def opened() -> wave.Wave:
    return wave.open_wave(THREAD, name="g", repo=REPO, jobs=[job(93, "src/cli.py", "imp1", "aud1"), job(94, "src/store.py", "imp2", "aud2")], now=T0, cfg=CFG)


def terminal_wave() -> wave.Wave:
    """Every job terminal but `closed_at` unset: the store of #93's hand edit, where the fold's close never landed."""
    w = wave.bind_pr(opened(), 1, 101, now=T0 + H)
    w, _ = wave.drop_job(w, 2, "descoped", now=T0 + H)
    j = w.jobs[0]
    j.merged_at, j.merge_sha = T0 + 2 * H, "a" * 40
    for k in ("pr", "review"): j.delivered.setdefault(k, T0 + 2 * H)
    w.closed_at, w.pending = None, []
    return w


def test_close_refuses_while_a_job_is_open(tmp_path, capsys):
    cfgp = write_config(tmp_path)
    put(tmp_path, opened())
    assert cli.main(["--config", cfgp, "wave", "close", THREAD, "--reason", "done enough"]) == 1
    err = capsys.readouterr().err
    assert "1, 2" in err and "still open" in err
    assert stored(tmp_path).closed_at is None and stored(tmp_path).findings == []  # refused: nothing recorded


def test_close_records_the_reason_as_a_wave_finding(tmp_path, capsys):
    cfgp = write_config(tmp_path)
    put(tmp_path, terminal_wave())
    reason = "job 2 descoped and job 1 merged out of band"
    assert cli.main(["--config", cfgp, "wave", "close", THREAD, "--reason", reason]) == 0
    capsys.readouterr()
    w = stored(tmp_path)
    assert w.closed_at is not None
    assert any(reason in f for f in w.findings)
    assert [p["post_kind"] for p in w.pending] == ["report"] and wave.close_ref(w) in w.pending[-1]["text"]


def test_close_refuses_an_already_closed_wave(tmp_path, capsys):
    cfgp = write_config(tmp_path)
    put(tmp_path, terminal_wave())
    assert cli.main(["--config", cfgp, "wave", "close", THREAD, "--reason", "first close"]) == 0
    capsys.readouterr()
    before = stored(tmp_path)
    assert cli.main(["--config", cfgp, "wave", "close", THREAD, "--reason", "second close"]) == 1
    assert "already closed" in capsys.readouterr().err
    after = stored(tmp_path)
    assert after.closed_at == before.closed_at and after.findings == before.findings and len(after.pending) == 1


def test_close_needs_a_reason(tmp_path):
    cfgp = write_config(tmp_path)
    put(tmp_path, terminal_wave())
    with pytest.raises(SystemExit):
        cli.main(["--config", cfgp, "wave", "close", THREAD])
    assert cli.main(["--config", cfgp, "wave", "close", THREAD, "--reason", "   "]) == 1
    assert stored(tmp_path).closed_at is None


# -- ruling 4: `serve` closes the store in a finally --------------------------------------
class FakeDriver:
    def __init__(self, *a, **kw): self.boom = False
    def serve(self, once: bool = False): return None


def fake_serve(monkeypatch, boom: bool = False) -> list[SpyStore]:
    made = spy_stores(monkeypatch)
    monkeypatch.setattr(cli, "claude_runner", lambda model: (lambda name, prompt: {}))

    class D(FakeDriver):
        def serve(self, once: bool = False):
            if boom: raise RuntimeError("the feed went away")
    monkeypatch.setattr(cli, "Driver", D)
    return made


def test_serve_closes_the_store_when_the_driver_returns(tmp_path, monkeypatch):
    cfgp = write_config(tmp_path)
    made = fake_serve(monkeypatch)
    assert cli.main(["--config", cfgp, "serve", "--once"]) == 0
    assert made[0].closed
    with pytest.raises(sqlite3.ProgrammingError): made[0].db.execute("SELECT 1")


def test_serve_closes_the_store_when_the_driver_raises(tmp_path, monkeypatch):
    cfgp = write_config(tmp_path)
    made = fake_serve(monkeypatch, boom=True)
    with pytest.raises(RuntimeError): cli.main(["--config", cfgp, "serve", "--once"])
    assert made[0].closed
    with pytest.raises(sqlite3.ProgrammingError): made[0].db.execute("SELECT 1")


def test_serve_closes_the_store_when_it_refuses_an_open_wave(tmp_path, monkeypatch, capsys):
    cfgp = write_config(tmp_path)
    put(tmp_path, opened())  # #86: an open wave with [github] and [board] off is refused
    made = fake_serve(monkeypatch)
    assert cli.main(["--config", cfgp, "serve", "--once"]) == 1
    assert "open wave(s)" in capsys.readouterr().err and made[0].closed
    assert made[0].lock_path.read_text().strip() == ""  # the refusal released the lock too

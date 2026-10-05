"""`BootstrapBackend` ("feed-journal", R15): the driver without fridica.

One JSONL journal per `journal_dir` (one study lineage) is both the post sink and the polled
feed: `post` appends the owner's `message` line and `events(after)` returns the lines after
the cursor (cursor = line index, `seq` = the same number carried in the line), so the own-post
echo arrives through the poll with a cursor and a fixed-width Slack-style `ts` ("%d.%06d",
strictly increasing over the file's life) and is never synthesised in `execute`. Workers are
subprocesses (`claude -p --output-format json --json-schema <WorkerResult>` or `codex exec`)
run from a git worktree of the subject revision, one worktree per worker id; when one exits
its `job_result` line (join_group, job_id, worker_id, role, attempt, job_status, result, code,
total_cost_usd: the charge, for codex always an estimate, its reservation) is appended by the driver thread. Peer lines (`sender != owner`) are never
written by the backend: only a corpus or a test injects them through `Journal.append`.

Invariants the tests assert (`tests/test_bootstrap_backend.py`):
- single writer: `fcntl.flock` on `journal.lock` around every append, one write of one line,
  contiguous `seq`; a reader stops on a gap (corruption is a stop, not a skip);
- durable completion: the WorkerResult is written to `results/<ActionId key>-a<attempt>.json`
  before the journal line, so a restarted driver finds a finished job by its `ref:` and attempt
  (an orphaned attempt 2 is never answered by attempt 1's file); the code after the worker
  returns is guarded, so unreadable output is a failed job (`worker_output_unreadable`), never a
  dead thread holding its reservation;
- deterministic sessions: the worker's session id is `uuid5(NAMESPACE, ActionId)`; a re-sent
  delegate for the same ActionId (restart probe, rule R) runs `--resume`, never a duplicate;
- bounded: the process group is killed at 2x the stage projection (`OVERRUN_FACTOR`), a retry
  of a seen ActionId backs off exponentially (and never launches once stopped), `--max-budget-usd`
  bounds a job, and the per-study ceiling (0 = none) covers the recorded charges plus a
  `max_budget_usd_per_job` reservation for every job still in flight, checked inside the journal
  lock (a delegate past it is refused); every job's charge comes from one rule, `charge`: the
  reported cost when it is known (finite and >= 0) whatever the job's status, else the full
  reservation for a job whose worker was spawned, else 0 (never spawned); reading a cost never
  raises (a 401-digit integer is an unknown cost);
- recorded before it runs: the default runner starts each worker held (`HOLD`), writes its
  process group id, leader start time and absolute deadline to `runs/<ActionId key>-a<attempt>.json`,
  and only then releases it (a driver that dies first leaves a worker that exits unrun); a
  restarted backend kills the overdue groups of jobs that never reported, while the leader still
  has that start time; a run file goes only once its group is confirmed dead (killed, or `ps`
  reports it gone); an orphan whose liveness is unknown (`ps` failed, no start time recorded)
  counts as alive and keeps its run file, past its deadline too, and the kill is retried on the
  next `events()`; a retry never launches while an orphan of its ActionId is alive; an exception
  in the default runner after the release kills the worker's group before it propagates; a study's worktrees are removed when it reaches a terminal stage (a worktree with a
  job still running in it, or a live orphan, when that job's result is reaped or the orphan is gone);
- redaction-clean: every file the backend writes (journal, `results/`, including codex's
  `--output-last-message` file, `sessions/`, `workers/`, `runs/`) passes `redact`; the brief is
  not kept anywhere but the redacted journal line;
- allowlisted worker environment: `BASE_ENV` (`PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`,
  the POSIX locale names `LC_ALL`, `LC_CTYPE`, `LC_COLLATE`, `LC_MESSAGES`, `LC_MONETARY`, `LC_NUMERIC`,
  `LC_TIME`, `TERM`, `TMPDIR`, `TZ`), the worker CLI's own credentials (`WORKER_CREDENTIALS`), the
  non-secret Bedrock/Vertex settings (`PROVIDER_SETTINGS`) and the owner's `keep_env`; nothing
  else, and never the board's `token_env`; `HOME` is kept, so file-based credentials under it stay
  reachable.

The runner is pluggable (`runner(argv, cwd, timeout) -> (stdout, returncode)`, `returncode`
None when killed at the deadline) so tests use a fake claude.

Frozen after the promotion of R2 (R15): no new features here; the fridica backend takes over.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .. import briefs, contracts
from ..config import Config
from ..machine import OVERRUN_FACTOR
from .protocol import ControlError, Unavailable

log = logging.getLogger("fridica_research.backend.bootstrap")
Runner = Callable[[list[str], str, float], tuple[str, int | None]]
NAMESPACE = uuid.UUID("6f1c2b0e-5a7d-4f1e-9c3a-2b8d7e6f5a41")  # session id = uuid5(NAMESPACE, ActionId)
FINISHED = ("finished", "failed", "interrupted")
REDACT = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"  # PEM private key blocks (an unterminated one to the end)
    r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{4,}"  # OpenAI / Anthropic (`sk-ant-...`), also after `_` (`KEY_sk-proj-...`); "risk-based" stays intact
    r"|(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{4,}|github_pat_[A-Za-z0-9_]{4,}"  # GitHub
    r"|xox[abeoprs]-[A-Za-z0-9-]{4,}|xapp-[A-Za-z0-9-]{4,}"  # Slack (bot, user, app, refresh, rotated, ...)
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{4,}"
    r"|AKIA[0-9A-Z]{16}",  # AWS access key id
    re.DOTALL)
BACKOFF_CAP = 8  # retry backoff doubles per retry up to retry_backoff * BACKOFF_CAP
# What a worker may keep of the driver's environment: the credentials its own CLI authenticates with.
WORKER_CREDENTIALS = {"claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"), "codex": ("OPENAI_API_KEY", "CODEX_API_KEY")}
# Non-secret provider settings a Bedrock or Vertex worker needs (the secret ones, e.g. AWS_SECRET_ACCESS_KEY, still go);
# `[backend.bootstrap] keep_env` keeps more by name.
PROVIDER_SETTINGS = ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "GOOGLE_APPLICATION_CREDENTIALS", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                     "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID")
ORPHAN_POLL = 5.0  # seconds between checks while a retry waits for a live orphan of its ActionId to end or be killed
# The worker's environment is an allowlist: these names (the POSIX locale names only, never any other `LC_*`), the worker CLI's own credentials (`WORKER_CREDENTIALS`),
# the non-secret provider settings (`PROVIDER_SETTINGS`) and the owner's `keep_env`. Everything else is dropped.
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "LC_COLLATE", "LC_MESSAGES", "LC_MONETARY", "LC_NUMERIC", "LC_TIME", "TERM", "TMPDIR", "TZ")
CREDENTIAL_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/?#\s]*@")  # scheme://user[:password]@host
# The default runner starts the worker held: the process (and its group) exists, but runs nothing until the driver has recorded
# it in `runs/` and writes to the pipe whose read end is `fd`. A driver that dies first closes the pipe (EOF), and the held
# process exits 70 unrun. Otherwise it closes `fd` and execs the worker in place (same pid, so the same group and start time).
HOLD = "import os, sys\nfd = int(sys.argv[1])\ngo = os.read(fd, 16)\nos.close(fd)\nif not go: sys.exit(70)\nos.execvp(sys.argv[2], sys.argv[2:])\n"


def held(fd: int, argv: list[str]) -> list[str]:
    return [sys.executable, "-I", "-c", HOLD, str(fd), *argv]


def worker_env(environ, worker: str, drop: tuple[str, ...] = (), keep_env: tuple[str, ...] = ()) -> dict[str, str]:
    """Only the allowlisted names of the driver's environment (`BASE_ENV`, the worker CLI's own credentials, the
    non-secret provider settings and `keep_env`), never those in `drop` (the board's `token_env`). A `keep_env` name whose
    value is a URL with credentials in it (`scheme://user:password@host`) is refused."""
    bad = [k for k in keep_env if CREDENTIAL_URL.match(str(environ.get(k, "")))]
    if bad: raise ValueError(f"[backend.bootstrap] keep_env may not keep a URL with credentials in it: {', '.join(bad)}")
    keep = {*BASE_ENV, *WORKER_CREDENTIALS.get(worker, ()), *PROVIDER_SETTINGS, *keep_env}
    return {k: v for k, v in environ.items() if k in keep and k not in drop}


def process_start(pid: int) -> str | None:
    """The start time of process `pid` (`ps -o lstart=`): "" when `ps` reports no such process, None when `ps` itself failed
    (unknown, never read as gone). A recorded pgid is killed only while its leader is still the process the backend spawned."""
    try: p = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, ValueError): return None
    if p.returncode == 1 and not p.stdout.strip(): return ""  # no such process
    if p.returncode: return None
    return p.stdout.strip() or None


def reported(stdout) -> object:
    """The raw `total_cost_usd` the worker's output carries, whatever it is (None when the output is not a JSON object);
    whether it is a usable cost is `BootstrapBackend.charge`'s decision alone."""
    try: out = json.loads(stdout)
    except (ValueError, TypeError): return None
    return out.get("total_cost_usd") if isinstance(out, dict) else None


def write_redacted(path: Path, text: str):
    """Every file the backend writes goes through here (the journal has its own locked append)."""
    path.write_text(REDACT.sub("[redacted]", text))


def redact(v):
    """Token-like strings anywhere in a value become `[redacted]` before the value reaches the journal."""
    if isinstance(v, str): return REDACT.sub("[redacted]", v)
    if isinstance(v, dict): return {k: redact(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)): return [redact(x) for x in v]
    return v


def next_ts(last: str | None, now: float) -> str:
    """Fixed-width `sec.micro`, strictly after `last` even when the clock did not move (or moved back)."""
    sec, frac = int(now), 0
    if last:
        ls, lf = int(last.split(".")[0]), int(last.split(".")[1])
        if (sec, frac) <= (ls, lf): sec, frac = (ls, lf + 1) if lf < 999_999 else (ls + 1, 0)
    return f"{sec}.{frac:06d}"


def action_key(action_id: str) -> str:
    """A file name for an ActionId: its last segment and a hash of the whole."""
    return f"{re.sub(r'[^A-Za-z0-9_-]', '_', action_id.rsplit('/', 1)[-1])[:40]}-{hashlib.sha1(action_id.encode()).hexdigest()[:16]}"


def run_key(action_id: str, attempt: int) -> str:
    """A file name for one attempt of an ActionId: `results/` and `runs/` are keyed by both, so an orphaned attempt 2 is never
    answered by attempt 1's result file and a same-ActionId retry never overwrites an orphan's process group."""
    return f"{action_key(action_id)}-a{int(attempt)}"


def stage_of(action_id: str) -> str | None:
    """`<thread>/g<n>/i<n>/<Stage>/a<n>/<suffix>` -> the stage, lower-cased."""
    parts = action_id.split("/")
    return parts[-3].lower() if len(parts) >= 6 else None


class Journal:
    """Append-only JSONL with one writer at a time: flock on the lock file, `seq`/`cursor` = line index from 1."""

    def __init__(self, dir_: Path):
        self.dir = Path(dir_)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path, self.lock_path = self.dir / "journal.jsonl", self.dir / "journal.lock"
        self.path.touch()
        self._mutex = threading.Lock()

    def _tail(self) -> dict | None:
        """The last line, read from the end of the file (no full scan)."""
        with self.path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            if end == 0: return None
            f.seek(max(0, end - 1_048_576))
            chunk = f.read().rstrip(b"\n")
            return json.loads(chunk.rsplit(b"\n", 1)[-1]) if chunk else None

    def append(self, line: dict, now: float | None = None, check: Callable[[list[dict]], None] | None = None) -> dict:
        """Write one redacted line with the next `seq`; `now` assigns the next fixed-width `ts` when the line has none.
        `check(rows)` runs inside the lock before the write and may raise to refuse it (the cost ceiling)."""
        with self._mutex, self.lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if check is not None: check(self.all())
                last = self._tail()
                seq = (int(last["seq"]) if last else 0) + 1
                line = redact(dict(line))
                if now is not None and not line.get("ts"): line["ts"] = next_ts(self.last_ts(), now)
                if line.get("kind") == "message" and line.get("thread") is None: line["thread"] = line["ts"]  # a root: its ts is its thread
                line.update(seq=seq, cursor=seq, v=1)
                data = (json.dumps(line, sort_keys=True) + "\n").encode()
                with self.path.open("ab") as f:
                    f.write(data)  # one write of one line
                    f.flush()
                    os.fsync(f.fileno())
                return line
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def last_ts(self) -> str | None:
        last = None
        for line in self.all():
            if line.get("ts") and (last is None or ts_pair(line["ts"]) > ts_pair(last)): last = line["ts"]
        return last

    def all(self) -> list[dict]:
        out = []
        for n, raw in enumerate(self.path.read_text().splitlines(), 1):
            if not raw.strip(): continue
            try: line = json.loads(raw)
            except ValueError as e: raise Unavailable(f"journal corrupt at line {n}: {e}") from e
            if int(line.get("seq", -1)) != n: raise Unavailable(f"journal seq gap at line {n}: seq {line.get('seq')}")
            out.append(line)
        return out

    def read(self, after: int, limit: int) -> list[dict]: return self.all()[after: after + limit]
    def count(self) -> int: return len(self.all())


def ts_pair(ts: str) -> tuple[int, int]:
    sec, _, frac = str(ts).partition(".")
    return int(sec), int((frac or "0")[:6].ljust(6, "0"))


class ProcessRunner:
    """The default runner: a new session per worker so the whole process group dies at the deadline (`os.killpg`).

    `env` is the worker's (allowlisted) environment. The worker is started held (`HOLD`): its process group exists, then
    `on_spawn(argv, pgid)` records it, then the worker is released to run. When `on_spawn` raises, the held worker is killed
    unrun and the call raises (nothing ran, so it is a launch error). A `kill` that arrives before the process was started
    is remembered and applied right after the start."""

    def __init__(self, env: dict[str, str] | None = None, on_spawn: Callable[[list[str], int], None] | None = None):
        self.env, self.on_spawn = env, on_spawn
        self.procs: dict[tuple[str, ...], subprocess.Popen] = {}
        self.killed: set[tuple[str, ...]] = set()
        self._lock = threading.Lock()

    def __call__(self, argv: list[str], cwd: str, timeout: float) -> tuple[str, int | None]:
        key = tuple(argv)
        if shutil.which(argv[0], path=(os.environ if self.env is None else self.env).get("PATH")) is None:
            raise FileNotFoundError(f"worker command not found: {argv[0]}")  # a launch error, before anything ran
        hold, go = os.pipe()
        try:
            with self._lock:
                # errors="replace": a worker that ran and wrote invalid UTF-8 (a truncated multibyte character, a stray byte on stderr) is still read
                p = subprocess.Popen(held(hold, argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True, env=self.env, pass_fds=(hold,))
                self.procs[key] = p
                if key in self.killed: self.killpg(p)
        except BaseException:
            os.close(go)
            raise
        finally:
            os.close(hold)
        try:
            try:
                if self.on_spawn: self.on_spawn(argv, p.pid)  # start_new_session: the group id is the pid; recorded while the worker is held
            except Exception as e:
                os.close(go)  # EOF: the held worker exits without running
                self.killpg(p)
                p.communicate()
                raise RuntimeError(f"worker not started, its process group was not recorded: {e}") from e
            try: os.write(go, b"go\n")  # recorded: release the worker
            except OSError: pass  # already killed (a stop before the release)
            finally: os.close(go)
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.killpg(p)
            out, err = p.communicate()
            log.warning("worker killed at the deadline (%.0f s): %s", timeout, argv[0])
            return out or "", None
        except BaseException:  # the worker may be running: never leave it untracked (its run file goes with the failed job)
            self.killpg(p)
            raise
        finally:
            with self._lock:
                self.procs.pop(key, None)
                self.killed.discard(key)
        if p.returncode: log.warning("worker %s exited %s: %s", argv[0], p.returncode, (err or "")[-300:])
        return out or "", p.returncode

    @staticmethod
    def killpg(p: subprocess.Popen):
        try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError): pass

    def kill(self, argv: list[str]):
        key = tuple(argv)
        with self._lock:
            p = self.procs.get(key)
            if p: self.killpg(p)
            else: self.killed.add(key)

    def forget(self, argv: list[str]):
        """The call for `argv` returned: a kill remembered for it must not hit a later run of the same argv."""
        with self._lock: self.killed.discard(tuple(argv))


class BootstrapBackend:
    def __init__(self, cfg: Config, runner: Runner | None = None, clock=time.time, sleep=time.sleep, proc_start: Callable[[int], str | None] = process_start):
        self.cfg, self.b, self.clock, self.sleep, self.proc_start = cfg, cfg.backend.bootstrap, clock, sleep, proc_start
        self.dir = Path(os.path.expanduser(self.b.journal_dir))
        self.journal = Journal(self.dir)
        self.worktrees = Path(os.path.expanduser(self.b.worktrees_dir)) if self.b.worktrees_dir else self.dir / "worktrees"
        for d in ("results", "sessions", "workers", "runs"): (self.dir / d).mkdir(parents=True, exist_ok=True)
        self.runner = runner or ProcessRunner(env=worker_env(os.environ, self.b.worker, drop=(cfg.board.token_env,), keep_env=tuple(self.b.keep_env)), on_spawn=self._spawned)
        self.done: queue.Queue = queue.Queue()
        self.threads: dict[str, threading.Thread] = {}
        self.jobs: dict[str, dict] = {}  # job_id -> the job line (running, stopped, or an orphan of a crashed driver)
        self.drivers: dict[str, str] = {}
        self._lock = threading.Lock()  # a job's stopped / launched / ran flags (stop() against the worker thread)
        self.pending_release: set[str] = set()  # worker ids whose worktree release() skipped while a job ran in it
        self._recover()

    # -- journal helpers -----------------------------------------------------------
    def place(self, thread: str) -> dict:
        ws, ch, root = thread.split(":", 2)
        return {"workspace": ws, "channel": {"id": ch, "name": None}, "thread": root}

    def lines(self, thread: str | None = None) -> list[dict]:
        rows = self.journal.all()
        if thread is None: return rows
        return [r for r in rows if r.get("thread") and f"{r.get('workspace')}:{r['channel']['id']}:{r['thread']}" == thread]

    def spent(self, rows: list[dict] | None = None) -> float:
        """The recorded charges (`charge`, in each job_result's `total_cost_usd`) over the journal."""
        rows = self.journal.all() if rows is None else rows
        return round(sum(float(r.get("total_cost_usd") or 0) for r in rows if r.get("kind") == "job_result"), 6)

    def reserved(self, rows: list[dict] | None = None) -> float:
        """The per-job budget held for every job still in flight (a job line without its job_result); its result releases it to the actual cost."""
        rows = self.journal.all() if rows is None else rows
        done = {r["job_id"] for r in rows if r.get("kind") == "job_result"}
        return round(sum(float(r.get("reserved_usd", self.b.max_budget_usd_per_job)) for r in rows if r.get("kind") == "job" and r["job_id"] not in done), 6)

    def _check_budget(self, aid: str, rows: list[dict]):
        if self.b.max_cost_usd_per_study <= 0: return  # 0: no ceiling (codex, which reports no cost)
        spent, reserved, new = self.spent(rows), self.reserved(rows), self.b.max_budget_usd_per_job
        if spent + reserved + new > self.b.max_cost_usd_per_study:
            log.warning("delegate %s refused: %.2f USD spent, %.2f reserved in flight, %.2f per job, ceiling %.2f", aid, spent, reserved, new, self.b.max_cost_usd_per_study)
            raise ControlError(429, "budget_exceeded")

    def _recover(self):
        """Job lines without a result line: a result file written before the crash is appended now; the rest is the stage timer's.
        A worker that outlived the crashed driver past its deadline (`runs/`) has its process group killed."""
        rows = self.journal.all()
        results = {r["job_id"] for r in rows if r.get("kind") == "job_result"}
        for r in rows:
            if r.get("kind") == "job" and r["job_id"] not in results:
                f = self.dir / "results" / f"{run_key(r['action_id'], r['attempt'])}.json"
                if f.exists(): self._record(r, json.loads(f.read_text()))
                else: self.jobs[r["job_id"]] = {**r, "stopped": True, "orphan": True}  # no process to adopt; the stage timer retries (rule R)
        self._kill_overdue()

    def _spawned(self, argv: list[str], pgid: int):
        """ProcessRunner's start notice, while the worker is still held: its process group and absolute deadline, durable for a
        restarted driver before the worker may run anything (an exception here keeps it from running at all)."""
        job = next((j for j in list(self.jobs.values()) if j.get("argv") == argv), None)
        if job is None: return
        run = {"job_id": job["job_id"], "action_id": job["action_id"], "attempt": job["attempt"], "pgid": pgid, "start": self.proc_start(pgid), "deadline": self.clock() + float(job["timeout"])}
        write_redacted(self.dir / "runs" / f"{run_key(job['action_id'], job['attempt'])}.json", json.dumps(run, sort_keys=True))
        job["spawned"] = True  # recorded, so released to run: a runner exception from here on is a spawned job (`charge`), not a launch error

    def _kill_overdue(self):
        """Orphaned workers (their driver died) past their deadline: kill the process group, drop the run file.
        The group is killed only while its leader is still the spawned worker (same start time): a pgid reused by an
        unrelated process after a long downtime is left alone. The run file is dropped only once the group is confirmed dead
        (killed, or `ps` reports the leader gone or replaced); when liveness is unknown (`ps` failed, no start time recorded)
        the file stays, the orphan counts as alive (`_orphan_alive`), and the next `events()` tries again."""
        now = self.clock()
        for job in list(self.jobs.values()):
            if not job.get("orphan"): continue
            f = self.dir / "runs" / f"{run_key(job['action_id'], job['attempt'])}.json"
            if not f.exists(): continue
            try: run = json.loads(f.read_text())
            except ValueError: continue
            if float(run.get("deadline", 0)) > now: continue
            try: pgid = int(run["pgid"])
            except (KeyError, TypeError, ValueError): pgid = None
            start = self.proc_start(pgid) if pgid else None
            if pgid and start and start == run.get("start"):
                try: os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError: pass  # gone meanwhile
                except PermissionError:
                    log.warning("overdue worker group %s of %s not killed (permission): kept, retried on the next poll", pgid, job["job_id"])
                    continue
                log.warning("killed the overdue worker group %s of %s (driver restarted)", pgid, job["job_id"])
            elif pgid and (start == "" or (start and run.get("start") is not None)):
                log.warning("overdue worker group %s of %s is gone or no longer the worker (started %r, recorded %r): not killed", pgid, job["job_id"], start, run.get("start"))
            else:  # unknown: `ps` failed, or no start time was recorded; alive until confirmed dead
                log.warning("overdue worker group %s of %s: liveness unknown (started %r, recorded %r), kept as alive and retried on the next poll", pgid, job["job_id"], start, run.get("start"))
                continue
            f.unlink(missing_ok=True)
            self._release_pending(job["worker_id"])

    def _release_pending(self, worker_id: str):
        """A worktree release() skipped while a job (or a live orphan) ran in it: removed once nothing runs there."""
        if worker_id in self.pending_release and not self._busy(worker_id):
            self.pending_release.discard(worker_id)
            self._remove_worktree(worker_id)

    # -- DriverBackend -----------------------------------------------------------------
    def set_driver(self, thread: str, mode: str) -> dict:
        self.drivers[thread] = mode
        return {"driver": mode}

    def post(self, target: str, req: contracts.PostRequest) -> dict:
        body = req.body()
        ws = self.b.workspace
        if ":" not in target: channel, root = target, None  # a study_root with no thread yet
        else:
            place = self.place(target)
            channel, root = place["channel"]["id"], None if req.kind == "study_root" else place["thread"]
        line = {"kind": "message", "workspace": ws, "channel": {"id": channel, "name": None}, "thread": root, "sender": self.cfg.owner, "sender_name": None, "mentions_owner": False,
                "text": body["text"], "meta": body["meta"], "turn_kind": req.kind, "turn_status": req.status, "files": 0, "source": "journal", "time": self.clock()}
        if body.get("details"): line["details"] = body["details"]
        written = self.journal.append(line, now=self.clock())  # a root (thread None) gets ts == thread inside the locked append
        return {"outbox_id": written["seq"], "thread": f"{ws}:{channel}:{written['thread']}", "ts": written["ts"]}

    def events(self, after: int | None, limit: int = 1000) -> dict:
        self.reap()
        self._kill_overdue()
        start = int(after or 0)  # None: a fresh store; the journal is this driver's own record, so it is read from line 1
        page = self.journal.read(start, limit)
        return {"v": 1, "events": page, "next": page[-1]["cursor"] if page else start, "scanned": len(page)}

    def thread_view(self, thread: str) -> dict:
        messages, jobs = [], {}
        for r in self.lines(thread):
            if r["kind"] == "message": messages.append({"ts": r["ts"], "sender": r["sender"], "text": r["text"], "meta": r.get("meta")})
            elif r["kind"] == "job": jobs[r["job_id"]] = {"id": r["job_id"], "worker_id": r["worker_id"], "role": r["role"], "brief": r.get("brief", ""), "tags": r.get("tags", []), "job_status": "running", "result": None, "error": None, "inbox_id": r["inbox_id"], "attempt": r["attempt"]}
            elif r["kind"] == "job_result" and r["job_id"] in jobs: jobs[r["job_id"]].update(job_status=r["job_status"], result=r.get("result"), error=r.get("code"))
        return {"session": {"control": "active"}, "messages": messages, "jobs": list(jobs.values())}

    def stop(self, thread: str, worker_id: str, mode: str = "owner") -> dict:
        """Kill the worker (or keep a retry still in its backoff from launching); its interrupted line, with the cost the
        worker reported, is appended when its thread returns (`reap`)."""
        for job in list(self.jobs.values()):
            if job["worker_id"] == worker_id and not job.get("stopped") and job.get("thread_id") == thread:
                with self._lock:
                    job["stopped"] = True
                    kill = getattr(self.runner, "kill", None)
                    if kill and job.get("launched") and not job.get("ran"): kill(job["argv"])
        return {"ok": True}

    def release(self, thread: str) -> dict:
        """The study in `thread` reached a terminal stage: remove its workers' worktrees (`git worktree remove --force`).
        A worktree with a job still running in it (a stopped worker being killed, say) is removed when that job's result is reaped."""
        removed = []
        for wid in dict.fromkeys(r["worker_id"] for r in self.lines(thread) if r.get("kind") == "job"):
            if self._busy(wid): self.pending_release.add(wid)
            elif self._remove_worktree(wid): removed.append(wid)
        return {"removed": removed}

    def _busy(self, worker_id: str) -> bool:
        """A job of this worker launched in this process and not yet reaped, or a live orphan of a crashed driver in its worktree."""
        return any(j["worker_id"] == worker_id and (not j.get("orphan") or self._orphan_alive(j)) for j in list(self.jobs.values()))

    def _orphan_alive(self, job: dict) -> bool:
        """An orphan whose recorded process group leader is still the worker the crashed driver spawned (the F4 start-time check).
        Fails closed: when liveness is unknown (`ps` failed, the start time was never recorded) the orphan is alive, past its
        deadline too (`_kill_overdue` keeps its run file until the group is confirmed dead); an unreadable run file counts as
        alive until the latest deadline the job could have. No run file means the worker never ran or is confirmed dead: the
        default runner holds it until the run file is written, and the file goes only after the result file or once
        `_kill_overdue` confirmed the group dead."""
        if not job.get("orphan"): return False
        f = self.dir / "runs" / f"{run_key(job['action_id'], job['attempt'])}.json"
        if not f.exists(): return False
        now = self.clock()
        try:
            run = json.loads(f.read_text())
            pgid, _ = int(run["pgid"]), float(run["deadline"])  # a run file without both is unreadable
        except (OSError, ValueError, KeyError, TypeError):  # unreadable: alive until the latest deadline the job could have
            return now < float(job.get("time", 0)) + self.b.retry_backoff * BACKOFF_CAP + float(job.get("timeout", 0))
        start = self.proc_start(pgid)
        if start == "": return False  # `ps` reports no such process
        if start is None or run.get("start") is None: return True  # unknown: alive until confirmed dead
        return start == run["start"]

    def _orphan_of(self, job: dict) -> dict | None:
        """A live orphan of the same ActionId as `job`: its retry must not `--resume` the same session alongside it."""
        return next((j for j in list(self.jobs.values()) if j["job_id"] != job["job_id"] and j.get("action_id") == job["action_id"] and self._orphan_alive(j)), None)

    def _remove_worktree(self, worker_id: str) -> bool:
        path = self.worktrees / worker_id
        if not path.exists(): return False
        if self.b.subject_repo:
            p = subprocess.run(["git", "-C", os.path.expanduser(self.b.subject_repo), "worktree", "remove", "--force", str(path)], capture_output=True, text=True)
            if p.returncode:
                log.warning("worktree remove %s: %s", path, p.stderr.strip()[:200])
                return False
        else: shutil.rmtree(path, ignore_errors=True)
        return True

    def delegate(self, thread: str, req: contracts.DelegateRequest) -> dict:
        aid = req.tags[0] if req.tags else (contracts.ref_of(req.brief) or "")
        if not aid: raise ControlError(400, "missing_ref")
        self._check_budget(aid, self.journal.all())  # before any side effect; re-checked inside the journal lock below
        n = sum(1 for r in self.journal.all() if r.get("kind") == "job") + 1
        worker_id = req.worker_id or f"w-{n}"
        session_id, resume, attempt = self._session(aid, worker_id, req.role)
        cwd = self.worktree(worker_id)
        timeout = OVERRUN_FACTOR * self.cfg.projection.get(stage_of(aid) or "", self.cfg.stage_timeout)
        argv, aux = self.argv(req.role, req.brief, session_id, resume, str(cwd), aid, attempt)
        line = {"kind": "job", "action": "started", **self.place(thread), "job_id": f"job-{n}", "worker_id": worker_id, "role": req.role, "attempt": attempt, "inbox_id": f"grp-{n}",
                "tags": list(req.tags), "action_id": aid, "brief": req.brief, "session_id": session_id, "resume": resume, "backend": self.b.worker, "cwd": str(cwd), "timeout": timeout, "reserved_usd": self.b.max_budget_usd_per_job, "time": self.clock()}
        self.journal.append(line, check=lambda rows: self._check_budget(aid, rows))  # spent + reserved + this job <= ceiling, atomically
        job = {**line, "thread_id": thread, "argv": argv, "aux": aux, "stopped": False, "launched": False, "ran": False}
        self.jobs[job["job_id"]] = job
        delay = min(self.b.retry_backoff * 2 ** (attempt - 2), self.b.retry_backoff * BACKOFF_CAP) if attempt > 1 else 0.0
        t = threading.Thread(target=self._run, args=(job, delay), name=f"worker-{job['job_id']}", daemon=True)
        self.threads[job["job_id"]] = t
        t.start()
        return {"join_group": job["inbox_id"], "jobs": [{"job_id": job["job_id"], "worker_id": worker_id, "role": req.role}]}

    # -- sessions, worktrees, argv ------------------------------------------------------
    def _session(self, aid: str, worker_id: str, role: str) -> tuple[str, bool, int]:
        """(session id, resume?, attempt): the ActionId's session when seen before, else the worker's, else a fresh uuid5 of the ActionId."""
        sf, wf = self.dir / "sessions" / f"{action_key(aid)}.json", self.dir / "workers" / f"{worker_id}.json"
        seen = json.loads(sf.read_text()) if sf.exists() else None
        worker = json.loads(wf.read_text()) if wf.exists() else None
        if seen: session_id, resume, attempt = seen["session_id"], True, int(seen["attempts"]) + 1
        elif worker and worker.get("session_id"): session_id, resume, attempt = worker["session_id"], True, 1
        else: session_id, resume, attempt = str(uuid.uuid5(NAMESPACE, aid)), False, 1
        write_redacted(sf, json.dumps({"action_id": aid, "session_id": session_id, "worker_id": worker_id, "attempts": attempt}))
        write_redacted(wf, json.dumps({"worker_id": worker_id, "role": role, "session_id": session_id}))
        return session_id, resume, attempt

    def worktree(self, worker_id: str) -> Path:
        """`worktrees/<worker id>`: a detached worktree of the subject revision (or a plain directory without a subject repo), reused per worker."""
        path = self.worktrees / worker_id
        if path.exists(): return path
        self.worktrees.mkdir(parents=True, exist_ok=True)
        if self.b.subject_repo:
            repo = os.path.expanduser(self.b.subject_repo)
            p = subprocess.run(["git", "-C", repo, "worktree", "add", "--detach", str(path), self.b.subject_revision], capture_output=True, text=True)
            if p.returncode: raise ControlError(500, f"worktree: {p.stderr.strip()[:200]}")
        else: path.mkdir(parents=True)
        return path

    def argv(self, role: str, brief: str, session_id: str, resume: bool, cwd: str, aid: str, attempt: int = 1) -> tuple[list[str], dict]:
        model, effort = self.b.models.get(role), self.b.efforts.get(role)
        role_file = Path(os.path.expanduser(self.b.roles_dir)) / f"{role}.md" if self.b.roles_dir else None
        if self.b.worker == "codex":
            out = self.dir / "results" / f"{run_key(aid, attempt)}.codex.json"
            schema = self.dir / "worker_result.schema.json"
            if not schema.exists(): schema.write_text(json.dumps(briefs.schema("worker_result")))
            argv = ["codex", "exec", "-C", cwd, "--sandbox", "workspace-write", "--output-schema", str(schema), "--output-last-message", str(out)]
            if model: argv += ["--model", model]
            prompt = (role_file.read_text() + "\n\n" if role_file and role_file.exists() else "") + brief
            return argv + [prompt], {"last_message": str(out)}
        argv = ["claude", "-p", "--output-format", "json", "--json-schema", json.dumps(briefs.schema("worker_result")), "--max-budget-usd", f"{self.b.max_budget_usd_per_job:g}",
                "--permission-mode", self.b.permission_mode, "--add-dir", cwd, "--resume" if resume else "--session-id", session_id]
        if model: argv += ["--model", model]
        if effort: argv += ["--effort", effort]
        if role_file and role_file.exists(): argv += ["--append-system-prompt", role_file.read_text()]
        return argv + [brief], {}

    # -- the worker thread and its result -------------------------------------------------
    def _run(self, job: dict, delay: float):
        if delay: self.sleep(delay)
        while not job["stopped"] and (orphan := self._orphan_of(job)):  # never two live workers resuming one session
            log.warning("%s waits for the live orphan %s of the same ActionId to end or be killed", job["job_id"], orphan["job_id"])
            self.sleep(ORPHAN_POLL)
        with self._lock:  # the lock stop() takes: a retry stopped during its backoff never launches
            if job["stopped"]:
                self.done.put((job["job_id"], {"session_id": job["session_id"], "cost": None, "spawned": False, "result": None, "code": "stopped", "job_status": "interrupted"}))
                return
            job["launched"] = True
        try:
            stdout, rc = self.runner(job["argv"], job["cwd"], job["timeout"])
            job["spawned"] = True  # a runner that returned ran its worker
        except Exception as e:  # noqa: BLE001 - a runner crash is a failed job, not a dead driver (spawned or not: see `charge`)
            stdout, rc = "", f"runner: {e}"[:200]
        with self._lock:
            job["ran"] = True
            stopped = job["stopped"]
            forget = getattr(self.runner, "forget", None)
            if forget: forget(job["argv"])
        key = run_key(job["action_id"], job["attempt"])
        try:
            outcome = self.outcome(job, stdout, rc)
            if stopped: outcome = {**outcome, "job_status": "interrupted", "code": "stopped", "result": None}
            if job["aux"].get("last_message") and Path(job["aux"]["last_message"]).exists():  # codex's own file: redacted in place
                f = Path(job["aux"]["last_message"])
                write_redacted(f, f.read_text())
        except Exception as e:  # noqa: BLE001 - unreadable worker output is a failed job, never a dead thread holding its reservation
            log.warning("worker output of %s unreadable: %s", job["job_id"], e)
            outcome = {"session_id": job["session_id"], "result": None, "job_status": "failed", "code": "worker_output_unreadable", "cost": reported(stdout), "spawned": bool(job.get("spawned"))}
            if job["aux"].get("last_message"):
                f = Path(job["aux"]["last_message"])
                try:
                    if f.exists(): write_redacted(f, f.read_bytes().decode(errors="replace"))
                except OSError: f.unlink(missing_ok=True)
        try: write_redacted(self.dir / "results" / f"{key}.json", json.dumps(redact(outcome), sort_keys=True))  # durable before the journal line
        except Exception as e:  # noqa: BLE001
            log.warning("result file of %s not written: %s", job["job_id"], e)
        (self.dir / "runs" / f"{key}.json").unlink(missing_ok=True)
        self.done.put((job["job_id"], outcome))

    def charge(self, job: dict, outcome: dict) -> float:
        """The one cost rule: what a job is charged against the study's ceiling. The worker's reported `total_cost_usd` when it
        is known (a finite number >= 0; Python's json reads NaN and Infinity), whatever the job's status (finished, failed,
        stopped, timed out). A job whose worker was spawned and reported no known cost is charged its full reservation: a
        killed `claude -p` prints nothing, yet it may have spent up to `max_budget_usd_per_job`. A job whose worker was never
        spawned (stopped in its backoff, a launch error) ran nothing and is charged 0. Reading the cost never raises: one that
        cannot be read as a float (a 401-digit integer overflows, OverflowError) is unknown. For codex, which reports no cost,
        the charge is always an estimate (the reservation)."""
        c = outcome.get("cost")
        try:
            if isinstance(c, (int, float)) and not isinstance(c, bool):
                f = float(c)
                if math.isfinite(f) and f >= 0: return f
        except (OverflowError, TypeError, ValueError): pass  # unknown
        return float(job.get("reserved_usd", self.b.max_budget_usd_per_job)) if outcome.get("spawned") else 0.0

    def outcome(self, job: dict, stdout: str, rc) -> dict:
        """The worker's exit -> job_status, code, WorkerResult (machine_state from the worktree, not the model), the raw
        reported cost and whether the worker was spawned (the two facts `charge` decides from)."""
        base = {"session_id": job["session_id"], "result": None, "code": None, "cost": reported(stdout), "spawned": bool(job.get("spawned"))}
        if rc is None: return {**base, "job_status": "interrupted", "code": "timeout"}
        if isinstance(rc, str): return {**base, "job_status": "failed", "code": rc}  # a runner exception
        if rc: return {**base, "job_status": "failed", "code": f"exit {rc}"}
        payload, code = None, None
        if self.b.worker == "codex":
            f = Path(job["aux"]["last_message"])
            try: payload = json.loads(f.read_text()) if f.exists() else None
            except ValueError: code = "schema"
        else:
            try:
                out = json.loads(stdout)
                base["session_id"] = out.get("session_id") or base["session_id"]
                if out.get("is_error"): code = "is_error"
                payload = out.get("structured_output")
                if payload is None and out.get("result"):
                    try: payload = json.loads(out["result"])
                    except ValueError: payload = None
            except (ValueError, AttributeError, TypeError): code = "schema"  # truncated or not a JSON object: cost unknown
        if code: return {**base, "job_status": "failed", "code": code}
        if not isinstance(payload, dict) or payload.get("status") not in ("done", "partial", "failed", "needs_input") or not isinstance(payload.get("report"), str):
            return {**base, "job_status": "failed", "code": "schema"}
        result = {"status": payload["status"], "summary": str(payload.get("summary", ""))[:1500], "report": payload["report"][:40_000], "changes": list(payload.get("changes") or []), "validation": list(payload.get("validation") or []),
                  "artifacts": list(payload.get("artifacts") or []), "machine_state": machine_state(job["cwd"]), "unresolved": list(payload.get("unresolved") or []), "question": payload.get("question")}
        return {**base, "job_status": "finished", "result": result}

    def reap(self):
        """Finished worker threads -> `job_result` lines, in the driver thread (the one journal writer)."""
        while True:
            try: job_id, outcome = self.done.get_nowait()
            except queue.Empty: return
            job = self.jobs.get(job_id)
            if job is None: continue
            if job.get("stopped"):  # stopped by the driver: interrupted (charged by `charge`, like every job)
                outcome = {**outcome, "job_status": "interrupted", "code": "stopped", "result": None}
            job["stopped"] = True
            self._record(job, outcome)
            self._release_pending(job["worker_id"])

    def _record(self, job: dict, outcome: dict):
        line = {"kind": "job_result", "workspace": job["workspace"], "channel": job["channel"], "thread": job["thread"], "join_group": job["inbox_id"], "job_id": job["job_id"], "worker_id": job["worker_id"], "role": job["role"],
                "attempt": job["attempt"], "job_status": outcome["job_status"], "result": outcome.get("result"), "code": outcome.get("code"), "total_cost_usd": self.charge(job, outcome), "session_id": outcome.get("session_id"), "time": self.clock()}
        self.journal.append(line)
        self.jobs.pop(job["job_id"], None)

    def join(self, timeout: float | None = None):
        """Wait for the worker threads (tests and a graceful shutdown); the results still land on the next `events()`."""
        for t in list(self.threads.values()): t.join(timeout)


def machine_state(cwd: str) -> dict:
    """branch, commit and dirtiness of the worktree, from git (not from the model); empty values without a repository."""
    try:
        branch = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=30)
        if branch.returncode: return {"branch": "", "commit": "", "dirty": False}
        commit = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", cwd, "status", "--porcelain"], capture_output=True, text=True, timeout=30).stdout.strip())
        return {"branch": branch.stdout.strip(), "commit": commit, "dirty": dirty}
    except (OSError, subprocess.SubprocessError): return {"branch": "", "commit": "", "dirty": False}

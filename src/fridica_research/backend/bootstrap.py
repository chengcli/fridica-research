"""`BootstrapBackend` ("feed-journal", R15): the driver without fridica.

One JSONL journal per `journal_dir` (one study lineage) is both the post sink and the polled
feed: `post` appends the owner's `message` line and `events(after)` returns the lines after
the cursor (cursor = line index, `seq` = the same number carried in the line), so the own-post
echo arrives through the poll with a cursor and a fixed-width Slack-style `ts` ("%d.%06d",
strictly increasing over the file's life) and is never synthesised in `execute`. Workers are
subprocesses (`claude -p --output-format json --json-schema <WorkerResult>` or `codex exec`)
run from a git worktree of the subject revision, one worktree per worker id; when one exits
its `job_result` line (join_group, job_id, worker_id, role, attempt, job_status, result, code,
total_cost_usd) is appended by the driver thread. Peer lines (`sender != owner`) are never
written by the backend: only a corpus or a test injects them through `Journal.append`.

Invariants the tests assert (`tests/test_bootstrap_backend.py`):
- single writer: `fcntl.flock` on `journal.lock` around every append, one write of one line,
  contiguous `seq`; a reader stops on a gap (corruption is a stop, not a skip);
- durable completion: the WorkerResult is written to `results/<ActionId key>.json` before the
  journal line, so a restarted driver finds a finished job by its `ref:`;
- deterministic sessions: the worker's session id is `uuid5(NAMESPACE, ActionId)`; a re-sent
  delegate for the same ActionId (restart probe, rule R) runs `--resume`, never a duplicate;
- bounded: the process group is killed at 2x the stage projection (`OVERRUN_FACTOR`), a retry
  of a seen ActionId backs off exponentially (and never launches once stopped), `--max-budget-usd`
  bounds a job, and the per-study ceiling covers the finished jobs' `total_cost_usd` plus a
  `max_budget_usd_per_job` reservation for every job still in flight, checked inside the journal
  lock (a delegate past it is refused); each worker's process group id and absolute deadline are
  kept in `runs/<ActionId key>.json`, and a restarted backend kills the overdue groups of jobs
  that never reported; a study's worktrees are removed when it reaches a terminal stage;
- redaction-clean: every file the backend writes (journal, `results/`, including codex's
  `--output-last-message` file, `sessions/`, `workers/`, `runs/`) passes `redact`; the brief is
  not kept anywhere but the redacted journal line;
- scrubbed workers: the worker's environment drops `FRIDICA_*`, `SLACK_*`, `GH_TOKEN`,
  `GITHUB_TOKEN` and every `*_TOKEN` / `*_SECRET` / `*_API_KEY` except the worker CLI's own
  credentials (`WORKER_CREDENTIALS`).

The runner is pluggable (`runner(argv, cwd, timeout) -> (stdout, returncode)`, `returncode`
None when killed at the deadline) so tests use a fake claude.

Frozen after the promotion of R2 (R15): no new features here; the fridica backend takes over.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
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
    r"|sk-[A-Za-z0-9_-]{4,}"  # OpenAI / Anthropic (`sk-ant-...`)
    r"|(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{4,}|github_pat_[A-Za-z0-9_]{4,}"  # GitHub
    r"|xox[bpa]-[A-Za-z0-9-]{4,}|xapp-[A-Za-z0-9-]{4,}"  # Slack
    r"|Bearer\s+[A-Za-z0-9._~+/=-]{4,}"
    r"|AKIA[0-9A-Z]{16}",  # AWS access key id
    re.DOTALL)
BACKOFF_CAP = 8  # retry backoff doubles per retry up to retry_backoff * BACKOFF_CAP
# What a worker may keep of the driver's environment: the credentials its own CLI authenticates with.
WORKER_CREDENTIALS = {"claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"), "codex": ("OPENAI_API_KEY", "CODEX_API_KEY")}
SECRET_ENV = re.compile(r"^(FRIDICA_.*|SLACK_.*|GH_TOKEN|GITHUB_TOKEN|.*_TOKEN|.*_SECRET|.*_API_KEY)$")


def worker_env(environ, worker: str) -> dict[str, str]:
    """The driver's environment without board, Slack, fridica or other secrets; the worker CLI's own credentials stay."""
    keep = WORKER_CREDENTIALS.get(worker, ())
    return {k: v for k, v in environ.items() if k in keep or not SECRET_ENV.match(k)}


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

    `env` is the worker's (scrubbed) environment; `on_spawn(argv, pgid)` is told the group id as soon as the process exists.
    A `kill` that arrives before the process was started is remembered and applied right after the start."""

    def __init__(self, env: dict[str, str] | None = None, on_spawn: Callable[[list[str], int], None] | None = None):
        self.env, self.on_spawn = env, on_spawn
        self.procs: dict[tuple[str, ...], subprocess.Popen] = {}
        self.killed: set[tuple[str, ...]] = set()
        self._lock = threading.Lock()

    def __call__(self, argv: list[str], cwd: str, timeout: float) -> tuple[str, int | None]:
        key = tuple(argv)
        with self._lock:
            p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, env=self.env)
            self.procs[key] = p
            if key in self.killed: self.killpg(p)
        if self.on_spawn:
            try: self.on_spawn(argv, p.pid)  # start_new_session: the group id is the pid
            except Exception as e:  # noqa: BLE001 - bookkeeping must not orphan the worker
                log.warning("on_spawn failed: %s", e)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.killpg(p)
            out, err = p.communicate()
            log.warning("worker killed at the deadline (%.0f s): %s", timeout, argv[0])
            return out or "", None
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
    def __init__(self, cfg: Config, runner: Runner | None = None, clock=time.time, sleep=time.sleep):
        self.cfg, self.b, self.clock, self.sleep = cfg, cfg.backend.bootstrap, clock, sleep
        self.dir = Path(os.path.expanduser(self.b.journal_dir))
        self.journal = Journal(self.dir)
        self.worktrees = Path(os.path.expanduser(self.b.worktrees_dir)) if self.b.worktrees_dir else self.dir / "worktrees"
        for d in ("results", "sessions", "workers", "runs"): (self.dir / d).mkdir(parents=True, exist_ok=True)
        self.runner = runner or ProcessRunner(env=worker_env(os.environ, self.b.worker), on_spawn=self._spawned)
        self.done: queue.Queue = queue.Queue()
        self.threads: dict[str, threading.Thread] = {}
        self.jobs: dict[str, dict] = {}  # job_id -> the job line (running, stopped, or an orphan of a crashed driver)
        self.drivers: dict[str, str] = {}
        self._lock = threading.Lock()  # a job's stopped / launched / ran flags (stop() against the worker thread)
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
        """The finished jobs' `total_cost_usd` over the journal."""
        rows = self.journal.all() if rows is None else rows
        return round(sum(float(r.get("total_cost_usd") or 0) for r in rows if r.get("kind") == "job_result"), 6)

    def reserved(self, rows: list[dict] | None = None) -> float:
        """The per-job budget held for every job still in flight (a job line without its job_result); its result releases it to the actual cost."""
        rows = self.journal.all() if rows is None else rows
        done = {r["job_id"] for r in rows if r.get("kind") == "job_result"}
        return round(sum(float(r.get("reserved_usd", self.b.max_budget_usd_per_job)) for r in rows if r.get("kind") == "job" and r["job_id"] not in done), 6)

    def _check_budget(self, aid: str, rows: list[dict]):
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
                f = self.dir / "results" / f"{action_key(r['action_id'])}.json"
                if f.exists(): self._record(r, json.loads(f.read_text()))
                else: self.jobs[r["job_id"]] = {**r, "stopped": True, "orphan": True}  # no process to adopt; the stage timer retries (rule R)
        self._kill_overdue()

    def _spawned(self, argv: list[str], pgid: int):
        """ProcessRunner's start notice: the worker's process group and absolute deadline, durable for a restarted driver."""
        job = next((j for j in list(self.jobs.values()) if j.get("argv") == argv), None)
        if job is None: return
        run = {"job_id": job["job_id"], "action_id": job["action_id"], "pgid": pgid, "deadline": self.clock() + float(job["timeout"])}
        write_redacted(self.dir / "runs" / f"{action_key(job['action_id'])}.json", json.dumps(run, sort_keys=True))

    def _kill_overdue(self):
        """Orphaned workers (their driver died) past their deadline: kill the process group, drop the run file."""
        now = self.clock()
        for job in list(self.jobs.values()):
            if not job.get("orphan"): continue
            f = self.dir / "runs" / f"{action_key(job['action_id'])}.json"
            if not f.exists(): continue
            try: run = json.loads(f.read_text())
            except ValueError: continue
            if float(run.get("deadline", 0)) > now: continue
            try: os.killpg(int(run["pgid"]), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, KeyError, ValueError): pass
            log.warning("killed the overdue worker group %s of %s (driver restarted)", run.get("pgid"), job["job_id"])
            f.unlink(missing_ok=True)

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
        """The study in `thread` reached a terminal stage: remove its workers' worktrees (`git worktree remove --force`)."""
        busy = {j["worker_id"] for j in self.jobs.values() if j.get("thread_id") not in (None, thread) and not j.get("stopped")}
        removed = []
        for wid in dict.fromkeys(r["worker_id"] for r in self.lines(thread) if r.get("kind") == "job"):
            path = self.worktrees / wid
            if wid in busy or not path.exists(): continue
            if self.b.subject_repo:
                p = subprocess.run(["git", "-C", os.path.expanduser(self.b.subject_repo), "worktree", "remove", "--force", str(path)], capture_output=True, text=True)
                if p.returncode:
                    log.warning("worktree remove %s: %s", path, p.stderr.strip()[:200])
                    continue
            else: shutil.rmtree(path, ignore_errors=True)
            removed.append(wid)
        return {"removed": removed}

    def delegate(self, thread: str, req: contracts.DelegateRequest) -> dict:
        aid = req.tags[0] if req.tags else (contracts.ref_of(req.brief) or "")
        if not aid: raise ControlError(400, "missing_ref")
        self._check_budget(aid, self.journal.all())  # before any side effect; re-checked inside the journal lock below
        n = sum(1 for r in self.journal.all() if r.get("kind") == "job") + 1
        worker_id = req.worker_id or f"w-{n}"
        session_id, resume, attempt = self._session(aid, worker_id, req.role)
        cwd = self.worktree(worker_id)
        timeout = OVERRUN_FACTOR * self.cfg.projection.get(stage_of(aid) or "", self.cfg.stage_timeout)
        argv, aux = self.argv(req.role, req.brief, session_id, resume, str(cwd), aid)
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

    def argv(self, role: str, brief: str, session_id: str, resume: bool, cwd: str, aid: str) -> tuple[list[str], dict]:
        model, effort = self.b.models.get(role), self.b.efforts.get(role)
        role_file = Path(os.path.expanduser(self.b.roles_dir)) / f"{role}.md" if self.b.roles_dir else None
        if self.b.worker == "codex":
            out = self.dir / "results" / f"{action_key(aid)}.codex.json"
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
        with self._lock:  # the lock stop() takes: a retry stopped during its backoff never launches
            if job["stopped"]:
                self.done.put((job["job_id"], {"session_id": job["session_id"], "total_cost_usd": 0.0, "result": None, "code": "stopped", "job_status": "interrupted"}))
                return
            job["launched"] = True
        try: stdout, rc = self.runner(job["argv"], job["cwd"], job["timeout"])
        except Exception as e:  # noqa: BLE001 - a runner crash is a failed job, not a dead driver
            stdout, rc = "", f"runner: {e}"[:200]
        with self._lock:
            job["ran"] = True
            forget = getattr(self.runner, "forget", None)
            if forget: forget(job["argv"])
        outcome = self.outcome(job, stdout, rc)
        if job["aux"].get("last_message") and Path(job["aux"]["last_message"]).exists():  # codex's own file: redacted in place
            f = Path(job["aux"]["last_message"])
            write_redacted(f, f.read_text())
        write_redacted(self.dir / "results" / f"{action_key(job['action_id'])}.json", json.dumps(redact(outcome), sort_keys=True))  # durable before the journal line
        (self.dir / "runs" / f"{action_key(job['action_id'])}.json").unlink(missing_ok=True)
        self.done.put((job["job_id"], outcome))

    def outcome(self, job: dict, stdout: str, rc) -> dict:
        """The worker's exit -> job_status, code, WorkerResult (machine_state from the worktree, not the model) and cost."""
        base = {"session_id": job["session_id"], "total_cost_usd": 0.0, "result": None, "code": None}
        if rc is None: return {**base, "job_status": "interrupted", "code": "timeout", "total_cost_usd": cost_of(stdout)}  # killed: whatever cost it reported
        if isinstance(rc, str): return {**base, "job_status": "failed", "code": rc}
        if rc: return {**base, "job_status": "failed", "code": f"exit {rc}", "total_cost_usd": cost_of(stdout)}
        payload, cost, code = None, 0.0, None
        if self.b.worker == "codex":
            f = Path(job["aux"]["last_message"])
            try: payload = json.loads(f.read_text()) if f.exists() else None
            except ValueError: code = "schema"
        else:
            try:
                out = json.loads(stdout)
                cost = float(out.get("total_cost_usd") or 0.0)
                base["session_id"] = out.get("session_id") or base["session_id"]
                if out.get("is_error"): code = "is_error"
                payload = out.get("structured_output")
                if payload is None and out.get("result"):
                    try: payload = json.loads(out["result"])
                    except ValueError: payload = None
            except ValueError: code = "schema"
        base["total_cost_usd"] = cost
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
            if job.get("stopped"):  # stopped by the driver: interrupted, at the cost the worker reported
                outcome = {**outcome, "job_status": "interrupted", "code": "stopped", "result": None}
            job["stopped"] = True
            self._record(job, outcome)

    def _record(self, job: dict, outcome: dict):
        line = {"kind": "job_result", "workspace": job["workspace"], "channel": job["channel"], "thread": job["thread"], "join_group": job["inbox_id"], "job_id": job["job_id"], "worker_id": job["worker_id"], "role": job["role"],
                "attempt": job["attempt"], "job_status": outcome["job_status"], "result": outcome.get("result"), "code": outcome.get("code"), "total_cost_usd": float(outcome.get("total_cost_usd") or 0.0), "session_id": outcome.get("session_id"), "time": self.clock()}
        self.journal.append(line)
        self.jobs.pop(job["job_id"], None)

    def join(self, timeout: float | None = None):
        """Wait for the worker threads (tests and a graceful shutdown); the results still land on the next `events()`."""
        for t in list(self.threads.values()): t.join(timeout)


def cost_of(stdout: str) -> float:
    """`total_cost_usd` of a claude result object when the output parses at all (a failed job still cost money)."""
    try: return float((json.loads(stdout) or {}).get("total_cost_usd") or 0.0)
    except (ValueError, AttributeError, TypeError): return 0.0


def machine_state(cwd: str) -> dict:
    """branch, commit and dirtiness of the worktree, from git (not from the model); empty values without a repository."""
    try:
        branch = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=30)
        if branch.returncode: return {"branch": "", "commit": "", "dirty": False}
        commit = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", cwd, "status", "--porcelain"], capture_output=True, text=True, timeout=30).stdout.strip())
        return {"branch": branch.stdout.strip(), "commit": commit, "dirty": dirty}
    except (OSError, subprocess.SubprocessError): return {"branch": "", "commit": "", "dirty": False}

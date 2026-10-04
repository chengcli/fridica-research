"""`DriverBackend`: what the driver needs from the world, in the shape of fridica's feed (R15).

The driver is feed-driven (poll -> translate -> step), so a backend is not `wait(job)` but the
five verbs the driver already uses plus the thread view the restart probe reads. Every backend
must satisfy the three conditions the iteration-3 proof relies on: own posts echo back through
`events()` with a cursor and a fixed-width `ts` (never synthesised in `execute`), `job_result`
lines carry the WorkerResult, and a re-sent delegate for the same ActionId resumes rather than
duplicates (`tests/test_bootstrap_backend.py`).
"""
from __future__ import annotations

from typing import Protocol

from .. import contracts


class ControlError(Exception):
    """A refusal from the backend: an HTTP status and a snake_case code (fridica's `{"error": code}`)."""

    def __init__(self, status: int, code: str):
        super().__init__(f"{status} {code}")
        self.status, self.code = status, code


class Unavailable(ControlError):
    """The backend cannot be reached at all (socket down, journal held); the driver sleeps and retries."""

    def __init__(self, why: str): super().__init__(0, why)


class DriverBackend(Protocol):
    def delegate(self, thread: str, req: contracts.DelegateRequest) -> dict:
        """Start a worker for `req` in `thread`; answers `{join_group, jobs: [{job_id, worker_id, role}]}`; raises ControlError when refused."""

    def stop(self, thread: str, worker_id: str, mode: str = "owner") -> dict:
        """Interrupt a worker; its job comes back on the feed as interrupted."""

    def post(self, target: str, req: contracts.PostRequest) -> dict:
        """Post in `target` (a thread id; or a channel id for a `study_root` with no thread yet); answers `{outbox_id}`.
        The echo arrives later through `events()` as the owner's `message` with a cursor."""

    def events(self, after: int | None, limit: int = 1000) -> dict:
        """One page of the feed after `after`: `{events: [..], next: cursor, scanned: n}`; `after=None` is the ledger's end (a fresh store)."""

    def thread_view(self, thread: str) -> dict:
        """`{session: {control}, messages: [{ts, sender, text, meta}], jobs: [{id, worker_id, role, brief, tags, job_status, result, error, inbox_id, attempt}]}` for the restart probe."""

    def set_driver(self, thread: str, mode: str) -> dict:
        """Hand the thread to an external driver (`external`) or back to the parent."""

    def release(self, thread: str) -> dict:
        """The study in `thread` reached a terminal stage (Delivered, Stopped): free what the backend holds for it (worktrees)."""

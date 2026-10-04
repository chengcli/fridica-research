"""`FridicaBackend`: the pinned #126 PR B routes over the control socket, one call per verb (no behaviour of its own)."""
from __future__ import annotations

from .. import contracts
from ..client import Client


class FridicaBackend:
    def __init__(self, client: Client): self.client = client

    def delegate(self, thread: str, req: contracts.DelegateRequest) -> dict: return self.client.delegate(thread, req.body())
    def stop(self, thread: str, worker_id: str, mode: str = "owner") -> dict: return self.client.stop_worker(thread, worker_id, mode)

    def post(self, target: str, req: contracts.PostRequest) -> dict:
        if ":" not in target: return self.client.post_root(target, req)
        return self.client.post_message(target, req)

    def events(self, after: int | None, limit: int = 1000) -> dict: return self.client.events(after, limit)
    def thread_view(self, thread: str) -> dict: return self.client.get(contracts.thread_route(thread))
    def set_driver(self, thread: str, mode: str) -> dict: return self.client.set_driver(thread, mode)
    def release(self, thread: str) -> dict: return {}  # fridica owns its workers' checkouts

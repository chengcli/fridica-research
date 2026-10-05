"""HTTP/1.1 over fridica's owner-only Unix control socket (stdlib http.client).

Auth has two modes (fridica src/control/server.rs): no header at all (same uid), or
`Authorization: Bearer <64 hex>` read from a private capability file. Bodies are JSON objects
of at most 64 KiB; errors come back as `{"error": "<snake_code>"}` with an HTTP status.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import stat
from pathlib import Path

from . import contracts
from .backend.protocol import ControlError, Unavailable

__all__ = ["Client", "ControlError", "Unavailable", "read_capability"]
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("fridica", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def read_capability(path: str | Path) -> str:
    p = Path(os.path.expanduser(str(path)))
    st = p.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077: raise ValueError("capability file must be a private regular file owned by this user")
    token = p.read_text().rstrip("\n")
    if not _HEX64.match(token): raise ValueError("invalid capability file")
    return token


class Client:
    def __init__(self, socket_path: str | Path, token: str | None = None, timeout: float = 30.0):
        self.path, self.token, self.timeout = str(socket_path), token, timeout

    def request(self, method: str, target: str, body: dict | None = None) -> dict:
        headers = {"Connection": "close", "Accept": "application/json"}
        if self.token: headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            if len(data) > contracts.BODY_LIMIT: raise ControlError(413, "body_too_large")
            headers["Content-Type"] = "application/json"
        conn = _UnixConnection(self.path, self.timeout)
        try:
            conn.request(method, target, body=data, headers=headers)
            r = conn.getresponse()
            raw = r.read()
        except OSError as e:
            raise Unavailable(f"control socket: {e}") from e
        finally:
            conn.close()
        try: payload = json.loads(raw) if raw else {}
        except ValueError as e: raise ControlError(r.status, "invalid_response") from e
        if r.status >= 400: raise ControlError(r.status, str(payload.get("error", "error")) if isinstance(payload, dict) else "error")
        return payload

    def get(self, target: str) -> dict: return self.request("GET", target)
    def post(self, target: str, body: dict) -> dict: return self.request("POST", target, body)

    # -- routes ----------------------------------------------------------------
    def events(self, after: int | None, limit: int = 1000) -> dict:
        return self.get(f"/events?after={after}&limit={limit}" if after is not None else "/events")

    def thread(self, thread: str) -> contracts.ThreadView: return contracts.ThreadView.from_json(self.get(contracts.thread_route(thread)))
    def delegate(self, thread: str, body: dict) -> dict: return self.post(contracts.delegate_route(thread), body)
    def post_message(self, thread: str, req: contracts.PostRequest) -> dict: return self.post(contracts.post_route(thread), req.body())
    def post_root(self, channel: str, req: contracts.PostRequest) -> dict: return self.post(contracts.STUDY_ROOT_ROUTE.format(channel=channel), req.body())
    def stop_worker(self, thread: str, worker_id: str, actor: str = "owner") -> dict: return self.post(contracts.stop_route(thread, worker_id), {"actor": actor})
    def set_driver(self, thread: str, driver: str) -> dict: return self.post(contracts.driver_route(thread), {"driver": driver})

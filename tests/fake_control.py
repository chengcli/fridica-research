"""A fake fridica control server over a Unix socket: GET /events, GET /threads/<id>, and the three #126 routes.

Posts echo back onto the feed as the owner's `message` events (with `turn_kind`), as the real
daemon's Slack round trip would; a successful delivery emits no `outbox` event. Tests script
refusals with `server.refuse_delegate` / `server.refuse_post` and push peer events with `push`.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import UnixStreamServer
from urllib.parse import parse_qs, urlparse


class FakeControl:
    def __init__(self, path: str, owner: str = "UOWNER", workspace: str = "T1"):
        self.path, self.owner, self.workspace = path, owner, workspace
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.threads: dict[str, dict] = {}  # thread id -> {messages, jobs, control}
        self.requests: list[tuple[str, str, dict]] = []
        self.refuse_delegate: list[tuple[int, str]] = []
        self.refuse_post: list[tuple[int, str]] = []
        self.jobs = 0
        self.ts = 1000
        self.drivers: dict[str, str] = {}
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *a): pass
            def do_GET(self): srv.handle(self, "GET")
            def do_POST(self): srv.handle(self, "POST")

        class Server(UnixStreamServer, HTTPServer):
            def server_bind(self): UnixStreamServer.server_bind(self)

        self.server = Server(path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # -- state helpers ---------------------------------------------------------
    def view(self, thread: str) -> dict: return self.threads.setdefault(thread, {"messages": [], "jobs": [], "control": "active"})

    def next_ts(self) -> str:
        self.ts += 1
        return f"1700000000.{self.ts:06d}"

    def push(self, event: dict) -> int:
        with self.lock:
            event = {"v": 1, "cursor": len(self.events) + 1, "time": 1_700_000_000.0 + len(self.events), **event}
            self.events.append(event)
            return event["cursor"]

    def place(self, thread: str) -> dict:
        ws, ch, root = thread.split(":", 2)
        return {"workspace": ws, "channel": {"id": ch, "name": None}, "thread": root}

    def message(self, thread: str, sender: str, text: str, kind: str | None = None, ts: str | None = None) -> dict:
        ts = ts or self.next_ts()
        e = {"kind": "message", **self.place(thread), "ts": ts, "sender": sender, "sender_name": None, "mentions_owner": False, "text": text, "files": 0, "source": "socket"}
        if kind: e["turn_status"], e["turn_kind"] = "complete", kind
        meta = {"kind": kind} if kind else None
        self.view(thread)["messages"].append({"ts": ts, "sender": sender, "text": text, "meta": meta})
        self.push(e)
        return e

    def root(self, thread: str, sender: str, text: str, kind: str | None = "study_root") -> dict:
        return self.message(thread, sender, text, kind, ts=thread.rsplit(":", 1)[1])

    def finish_job(self, thread: str, job_id: str, result: dict | None, status: str = "finished", code: str | None = None):
        for j in self.view(thread)["jobs"]:
            if j["id"] == job_id:
                j["job_status"], j["result"], j["error"] = status, result, code
        e = {"kind": "job", **self.place(thread), "action": status, "job_id": job_id, "attempt": 1}
        e["status" if status == "finished" else "code"] = (result or {}).get("status") if status == "finished" else code
        self.push(e)

    def job_of(self, thread: str, role: str, nth: int = -1) -> dict:
        return [j for j in self.view(thread)["jobs"] if j["role"] == role or (j["role"] == "debater" and f"# {role.title()}" in j.get("instructions", ""))][nth]

    # -- routing ---------------------------------------------------------------
    def handle(self, h: BaseHTTPRequestHandler, method: str):
        url = urlparse(h.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        n = int(h.headers.get("Content-Length") or 0)
        body = json.loads(h.rfile.read(n) or b"{}") if n else {}
        with self.lock: self.requests.append((method, url.path, body))
        try: status, payload = self.route(method, url.path, q, body)
        except KeyError: status, payload = 404, {"error": "not_found"}
        data = json.dumps(payload).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Connection", "close")
        h.end_headers()
        h.wfile.write(data)

    def route(self, method: str, path: str, q: dict, body: dict):
        if method == "GET" and path == "/events":
            if "after" not in q: return 200, {"v": 1, "events": [], "next": len(self.events)}
            after, limit = int(q["after"]), int(q.get("limit", 100))
            page = self.events[after: after + limit]
            return 200, {"v": 1, "events": page, "next": page[-1]["cursor"] if page else after, "scanned": len(page)}
        m = re.match(r"^/threads/([^/]+)(/.*)?$", path)
        if m:
            thread, rest = m.group(1), m.group(2) or ""
            v = self.view(thread)
            if method == "GET" and not rest: return 200, {"session": {"control": v["control"]}, "messages": v["messages"], "jobs": v["jobs"]}
            if rest == "/delegate": return self.delegate(thread, body)
            if rest == "/post": return self.post(thread, body)
            if rest == "/driver":
                self.drivers[thread] = body.get("driver", "")
                return 200, {"driver": self.drivers[thread]}
            if rest.startswith("/workers/") and rest.endswith("/stop"):
                wid = rest.split("/")[2]
                for j in v["jobs"]:
                    if j["worker_id"] == wid and j["job_status"] == "running":
                        j["job_status"] = "interrupted"
                        self.push({"kind": "job", **self.place(thread), "action": "interrupted", "job_id": j["id"], "attempt": 1, "code": "stopped"})
                return 200, {"ok": True}
        m = re.match(r"^/channels/([^/]+)/post$", path)
        if m:
            thread = f"{self.workspace}:{m.group(1)}:{self.next_ts()}"
            self.root(thread, self.owner, body["text"], body["meta"]["kind"])
            return 200, {"outbox_id": len(self.requests), "thread": thread}
        raise KeyError(path)

    def delegate(self, thread: str, body: dict):
        if self.refuse_delegate:
            status, code = self.refuse_delegate.pop(0)
            return status, {"error": code}
        self.jobs += 1
        jid = f"job-{self.jobs}"
        wid = body.get("worker_id") or f"w-{self.jobs}"
        group = f"grp-{self.jobs}"
        self.view(thread)["jobs"].append({"id": jid, "worker_id": wid, "role": body["role"], "brief": body["brief"], "instructions": body.get("instructions", ""), "tags": body.get("tags", []), "job_status": "running", "result": None, "error": None, "inbox_id": group, "attempt": 1})
        self.push({"kind": "job", **self.place(thread), "action": "started", "job_id": jid, "attempt": 1, "worker_id": wid, "machine": "m", "workspace": "w", "backend": "claude"})
        return 200, {"join_group": group, "jobs": [{"job_id": jid, "worker_id": wid, "role": body["role"]}]}

    def post(self, thread: str, body: dict):
        kind = body["meta"]["kind"]
        if self.refuse_post:
            status, code = self.refuse_post.pop(0)
            if status == 200:  # accepted, then refused by the egress gate on delivery
                self.push({"kind": "outbox", **self.place(thread), "outcome": "rejected", "code": code, "post_kind": kind, "outbox_id": len(self.requests), "attempt": 1})
                return 200, {"outbox_id": len(self.requests)}
            return status, {"error": code}
        if kind == "study_root":
            child = f"{self.workspace}:{thread.split(':')[1]}:{self.next_ts()}"
            self.root(child, self.owner, body["text"], kind)
        else:
            self.message(thread, self.owner, body["text"], kind)
        return 200, {"outbox_id": len(self.requests)}

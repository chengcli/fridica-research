"""#80/#83 building blocks (ref fm-wave-g-assign, job 3): the deputy wake-up and executable ladder actions.

`Client.instruct` posts `{actor, text, client_id}` to `/threads/<id>/instruct` (fridica src/control/api.rs);
`deputy.client_id(ref)` is `wake-` + sha256(ref)[:32], so a retry or restart queues one instruction. The
ladder's owner-only kinds are merge_substrate, scope and rules; every other question and a nobody-free
escalation wake the deputy. Ladder actions name the job's thread, carry post_kind "report" and a `ref:` line,
and a reschedule id names the holder (`<ref>/reschedule/<holder>`). The driver does not run these yet.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import UnixStreamServer

import pytest

from fridica_research import deputy, ladder
from fridica_research.client import Client, ControlError

MIN = 60.0
T0 = 1_791_800_000.0
EST = 120 * MIN
ETA = T0 + EST
W = 30 * MIN
EPS = 1e-3
REF = "fm-wave-g-job3"
THREAD = "1791823244.369619"
HOLDER, OWNER, OUTIE = "UHOLD", "UOWNER", "UOUTIE"
_CLIENT_ID = re.compile(r"^[A-Za-z0-9-]{8,80}$")


class FakeInstruct:
    """The instruct route as api.rs checks it: owner route, body fields within {actor, text, client_id},
    text 1-4000 chars, client_id 8-80 of [A-Za-z0-9-]; a reused client_id is not queued again."""
    def __init__(self, path: str):
        self.requests: list[tuple[str, dict]] = []
        self.queued: dict[str, str] = {}
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                srv.requests.append((self.path, body))
                status, out = srv.handle(self.path, body)
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class Server(UnixStreamServer, HTTPServer):
            def server_bind(self): UnixStreamServer.server_bind(self)

        self.server = Server(path, Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def handle(self, path: str, body: dict):
        m = re.fullmatch(r"/threads/([^/]+)/instruct", path)
        if not m: return 404, {"error": "not_found"}
        if any(k not in ("actor", "text", "client_id") for k in body): return 400, {"error": "unknown_body_field"}
        text, cid = body.get("text"), body.get("client_id")
        if not isinstance(text, str) or not isinstance(cid, str) or not 1 <= len(text.strip()) <= 4000 or not _CLIENT_ID.match(cid):
            return 400, {"error": "invalid_instruction"}
        self.queued.setdefault(cid, text)
        return 200, {"instruction_id": cid, "queued": True, "id": m.group(1)}


@pytest.fixture
def control(sock_dir):
    sock = os.path.join(sock_dir, "c.sock")
    srv = FakeInstruct(sock)
    yield srv, Client(sock)
    srv.server.shutdown()
    srv.server.server_close()


def view(**kw) -> ladder.JobView:
    base = dict(ref=REF, holder=HOLDER, owner=OWNER, outie=OUTIE, est=EST, eta=ETA, thread=THREAD,
                alternatives=[{"id": "UFREE1", "free": True}])
    return ladder.JobView(**{**base, **kw})


# -- client.instruct ----------------------------------------------------------------------
def test_instruct_posts_exactly_actor_text_and_client_id_to_the_thread_route(control):
    srv, c = control
    assert c.instruct(THREAD, "rule on this", "wake-0123456789abcdef")["queued"] is True
    assert srv.requests == [(f"/threads/{THREAD}/instruct", {"actor": "owner", "text": "rule on this", "client_id": "wake-0123456789abcdef"})]


def test_instruct_surfaces_a_refusal_as_a_control_error(control):
    _, c = control
    with pytest.raises(ControlError) as e:
        c.instruct(THREAD, "x", "short")
    assert e.value.status == 400 and e.value.code == "invalid_instruction"


# -- deputy -------------------------------------------------------------------------------
def test_client_id_is_wake_plus_32_hex_of_sha256_of_the_ref():
    cid = deputy.client_id(REF + "/escalate")
    assert cid == "wake-" + hashlib.sha256((REF + "/escalate").encode()).hexdigest()[:32]
    assert len(cid) == 37 and _CLIENT_ID.match(cid)
    assert deputy.client_id("a") != deputy.client_id("b")


def test_wake_text_carries_question_signals_options_and_ref():
    t = deputy.wake_text(f"{REF}/escalate", "reschedule or escalate?", {"deliverable": False}, ["reschedule to UX", "forward to the owner"])
    assert "reschedule or escalate?" in t and "deliverable: False" in t and "forward to the owner" in t
    assert t.splitlines()[-1] == f"ref: {REF}/escalate"


def test_a_retried_wake_up_queues_one_instruction(control):
    srv, c = control
    for _ in range(3):
        deputy.wake(c, THREAD, f"{REF}/question/q1", "judgment call")
    assert len(srv.requests) == 3 and list(srv.queued) == [deputy.client_id(f"{REF}/question/q1")]


# -- ladder: executable actions -----------------------------------------------------------
def test_owner_only_kinds_are_exactly_merge_substrate_scope_and_rules():
    assert ladder.OWNER_ONLY == ("merge_substrate", "scope", "rules") and ladder.POST_KIND == "report"


def test_job_view_has_a_thread_field_defaulting_to_empty():
    assert ladder.JobView(ref=REF, holder=HOLDER, owner=OWNER, est=EST, eta=ETA).thread == ""
    assert ladder.JobView.of({"ref": REF, "holder": HOLDER, "owner": OWNER, "est": EST, "eta": ETA, "thread": THREAD}).thread == THREAD


@pytest.mark.parametrize("kind", ["merge", "howto", "estimate"])
def test_a_question_that_is_not_owner_only_wakes_the_deputy_on_the_thread(kind):
    ref = f"{REF}/question/q1"
    (a,) = ladder.step(view(questions=[{"id": "q1", "kind": kind, "text": "which way?"}]), T0)
    assert a.kind == "wake_deputy" and a.id == ref and a["thread"] == THREAD
    assert a["client_id"] == deputy.client_id(ref) and a["text"].endswith(f"ref: {ref}") and "which way?" in a["text"]


def test_nobody_free_wakes_the_deputy_instead_of_the_owner():
    v = view(checkin_at=ETA, done={f"{REF}/checkin"}, alternatives=[])
    (a,) = ladder.step(v, ETA + W + EPS)
    assert a.kind == "wake_deputy" and a.id == f"{REF}/escalate" and a["client_id"] == deputy.client_id(f"{REF}/escalate")
    assert f"<@{OWNER}>" not in a["text"]


def test_posts_name_the_thread_the_report_kind_and_end_with_their_ref():
    out = ladder.step(view(cause="hardware", questions=[{"id": "q2", "kind": "rules", "text": "change R51?"}]), ETA)
    assert sorted(a.kind for a in out) == ["ask_outie", "notify_owner", "post"]
    for a in out:
        assert a["thread"] == THREAD and a["post_kind"] == "report" and a["text"].endswith(f"ref: {a.id}")


def test_reschedule_id_names_the_holder_so_the_next_holder_can_be_rescheduled_again():
    v = view(checkin_at=ETA, done={f"{REF}/checkin"})
    (a,) = ladder.step(v, ETA + W + EPS)
    assert a.kind == "reschedule" and a.id == f"{REF}/reschedule/{HOLDER}" and a["post_kind"] == "report"
    v = dataclasses.replace(v, done=set(v.done) | {a.id})
    assert ladder.step(v, ETA + W + EPS) == []
    nxt = dataclasses.replace(v, holder="UFREE1", alternatives=[{"id": "UFREE2", "free": True}])
    (b,) = ladder.step(nxt, ETA + W + EPS)
    assert b.id == f"{REF}/reschedule/UFREE1" and b["to"] == "UFREE2"


def test_ladder_wake_up_executes_through_instruct_once(control):
    srv, c = control
    (a,) = ladder.step(view(questions=[{"id": "q3", "kind": "merge", "text": "merge now?"}]), T0)
    for _ in range(2):
        c.instruct(a["thread"], a["text"], a["client_id"])
    assert [p for p, _ in srv.requests] == [f"/threads/{THREAD}/instruct"] * 2 and list(srv.queued) == [a["client_id"]]

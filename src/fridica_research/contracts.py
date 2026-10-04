"""Pinned JSON and text contracts between the driver and fridica.

These target fridica #126 PR B (external-driver surface). As of fridica 0.4.0-dev
the three POST routes, the `job_result` and `peer_post` event kinds and the
`threads.driver` flag are **not yet served**; the shapes here are the Python
side's pin so the package is testable against the fake server in `tests/`.
Everything else (GET /events, GET /threads/<id>, `message`/`outbox`/`job`
events, Slack metadata keys) is what the daemon serves today.

Slack metadata keys are fixed (`v, owner, session, turn, status, kind, worker`),
so every correlation datum the driver needs travels as a readable text line:
`ref: <ActionId>`, `approach: <slug>`, `generation: N`, `lineage: <thread>`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

POST_KINDS = ("study_claim", "study_result", "study_root", "report")
BODY_LIMIT = 64 * 1024  # control request body cap (fridica src/control/mod.rs)
BRIEF_LIMIT = 40_000  # fridica-core delegation::prepare
POST_LIMIT = 40_000  # Slack message text cap
STUDY_ROOT_ROUTE = "/channels/{channel}/post"  # pinned: a study_root with no thread yet


def delegate_route(thread: str) -> str: return f"/threads/{thread}/delegate"
def post_route(thread: str) -> str: return f"/threads/{thread}/post"
def stop_route(thread: str, worker_id: str) -> str: return f"/threads/{thread}/workers/{worker_id}/stop"
def driver_route(thread: str) -> str: return f"/threads/{thread}/driver"
def thread_route(thread: str) -> str: return f"/threads/{thread}"


@dataclass(frozen=True)
class DelegateRequest:
    """Body of POST /threads/<id>/delegate; answers `{join_group, jobs:[{job_id, worker_id, role}]}`."""
    role: str
    brief: str
    context: str = "fresh"  # fresh | fork
    worker_id: str | None = None  # resume this worker
    ephemeral: bool = False
    backend: str = "same"  # same | other | <name>
    deliverable: str = "report"
    tags: tuple[str, ...] = ()
    instructions: str = ""  # research role prose; host includes it in the worker instruction fingerprint

    def body(self) -> dict:
        b = {"role": self.role, "brief": self.brief, "context": self.context, "ephemeral": self.ephemeral, "backend": self.backend, "deliverable": self.deliverable, "tags": list(self.tags)}
        if self.worker_id: b["worker_id"] = self.worker_id
        if self.instructions: b["instructions"] = self.instructions
        return b


@dataclass(frozen=True)
class PostRequest:
    """Body of POST /threads/<id>/post; answers `{outbox_id}`. `study_root` creates a new thread."""
    kind: str
    text: str
    details: str | None = None
    status: str = "complete"

    def body(self) -> dict:
        if self.kind not in POST_KINDS: raise ValueError(f"unknown post kind {self.kind}")
        b = {"text": self.text, "meta": {"kind": self.kind, "status": self.status}}
        if self.details: b["details"] = self.details
        return b


@dataclass(frozen=True)
class JobResult:
    """The `job_result` feed event (PR B) or a `job` completion joined with GET /threads/<id> jobs[].result."""
    join_group: str
    job_id: str
    worker_id: str
    role: str
    attempt: int
    job_status: str  # finished | failed | interrupted
    result: dict | None = None  # fridica-core WorkerResult
    code: str | None = None

    @classmethod
    def from_event(cls, e: dict) -> "JobResult":
        return cls(e.get("join_group", ""), e["job_id"], e.get("worker_id", ""), e.get("role", ""), int(e.get("attempt", 1)), e.get("job_status") or e.get("action", "finished"), e.get("result"), e.get("code"))


@dataclass(frozen=True)
class PeerPost:
    """The `peer_post` feed event (PR B), or a `message` event carrying `turn_kind` from another owner."""
    ts: str
    sender: str
    kind: str
    text: str
    owner: str = ""


# --- text lines --------------------------------------------------------------

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_LINE = re.compile(r"^(?P<key>[a-z][a-z _-]*):\s*(?P<value>.*)$", re.M)


def lines(text: str) -> dict[str, str]:
    """First `key: value` line per key, keys lower-cased with spaces and dashes folded to `_`."""
    out: dict[str, str] = {}
    for m in _LINE.finditer(text):
        k = m.group("key").strip().lower().replace(" ", "_").replace("-", "_")
        out.setdefault(k, m.group("value").strip())
    return out


def ref_of(text: str) -> str | None: return lines(text).get("ref")
def stage_line(stage: str, iteration: int) -> str: return f"Stage: {stage} (iteration {iteration})"


@dataclass(frozen=True)
class Claim:
    iteration: int
    slug: str
    title: str
    why: str = ""
    also: tuple[str, ...] = ()


_CLAIM_HEAD = re.compile(r"^Claim \(iteration (?P<i>\d+)\):\s*(?P<title>.+)$", re.M)


def format_claim(c: Claim, ref: str) -> str:
    also = ", ".join(c.also) if c.also else "none"
    return "\n".join([stage_line("Claim", c.iteration), f"Claim (iteration {c.iteration}): {c.title}", f"approach: {c.slug}", f"why: {c.why}", f"also considered: {also}", f"ref: {ref}"])


def parse_claim(text: str) -> Claim | None:
    """Only the fixed lines count; the caller must already know the post carries meta kind `study_claim`."""
    head = _CLAIM_HEAD.search(text)
    kv = lines(text)
    slug = kv.get("approach")
    if not head or not slug or not SLUG.match(slug): return None
    also = tuple(a.strip() for a in kv.get("also_considered", "").split(",") if a.strip() and a.strip() != "none")
    return Claim(int(head.group("i")), slug, head.group("title").strip(), kv.get("why", ""), also)


def format_root(text: str, generation: int, lineage: str | None, projected_hours: float, ref: str, mentions: tuple[str, ...] = ()) -> str:
    """The `study_root` text (R4): projection, generation and lineage lines; `lineage` is None for an origin root."""
    head = (" ".join(f"<@{m}>" for m in mentions) + "\n") if mentions else ""
    tail = [f"projected: {projected_hours:g} h", f"generation: {generation}", f"lineage: {lineage or 'origin'}", f"ref: {ref}"]
    return head + text.rstrip() + "\n\n" + "\n".join(tail)


@dataclass(frozen=True)
class Root:
    text: str
    generation: int | None
    lineage: str | None
    projected_hours: float | None


def parse_root(text: str) -> Root:
    kv = lines(text)
    gen = int(kv["generation"]) if kv.get("generation", "").isdigit() else None
    lineage = kv.get("lineage")
    if lineage == "origin": lineage = None
    m = re.match(r"^\s*([0-9.]+)\s*h", kv.get("projected", ""))
    body = re.split(r"\n\n(?=projected: )", text, maxsplit=1)[0]
    return Root(body, gen, lineage, float(m.group(1)) if m else None)


def format_result(iteration: int, summary: str, lines_: list[str], ref: str, partial: bool = False) -> str:
    head = stage_line("Deliver", iteration) + (" partial" if partial else "")
    return "\n".join([head, summary.rstrip(), *lines_, f"ref: {ref}"])


# --- worker report blocks ----------------------------------------------------

_STANCE = re.compile(r"^##\s*Stance\s*$(?P<block>.*?)(?=^##\s|\Z)", re.M | re.S)
POSITIONS = ("agree", "disagree", "revised")
VERDICTS = ("pass", "return", "reject")
DEFAULT_POSITION = "disagree"
DEFAULT_VERDICT = "return"


@dataclass(frozen=True)
class Stance:
    position: str | None = None
    verdict: str | None = None
    notes: str = ""


def parse_stance(result: dict | None) -> Stance:
    """Research convention: structured annotations or the report block; missing values default to disagree/return at decision sites."""
    if not result: return Stance()
    annotations = result.get("annotations")
    s = annotations.get("stance") if isinstance(annotations, dict) else None
    if isinstance(s, dict):
        return Stance(s.get("position") if s.get("position") in POSITIONS else None, s.get("verdict") if s.get("verdict") in VERDICTS else None, str(s.get("notes") or ""))
    m = _STANCE.search(result.get("report") or "")
    if not m: return Stance()
    kv = lines(m.group("block"))
    pos, ver = kv.get("position", "").split()[:1] or [None], kv.get("verdict", "").split()[:1] or [None]
    return Stance(pos[0] if pos[0] in POSITIONS else None, ver[0] if ver[0] in VERDICTS else None, kv.get("notes", ""))


@dataclass(frozen=True)
class Approach:
    slug: str
    title: str
    why: str = ""


_APPROACH = re.compile(r"^\s*[-*]\s*(?P<slug>[a-z0-9][a-z0-9-]{0,47})\s*:\s*(?P<title>[^—]+?)(?:\s*(?:—|--)\s*(?P<why>.*))?\s*$", re.M)


def parse_approaches(report: str) -> list[Approach]:
    """`- slug: title -- why` lines, preferably under `## Approaches` (the explorer brief asks for that block)."""
    section = re.split(r"^##\s*Approaches\s*$", report, flags=re.M)
    text = section[1] if len(section) > 1 else report
    out, seen = [], set()
    for m in _APPROACH.finditer(text):
        if m.group("slug") in seen: continue
        seen.add(m.group("slug"))
        out.append(Approach(m.group("slug"), m.group("title").strip(), (m.group("why") or "").strip()))
    return out


_SIGNOFF = re.compile(r"SIGN-OFF\s+(?P<pr>\S+)\s+(?P<sha>[0-9a-f]{7,40})\s+(?P<verdict>approve|changes)\b", re.I)


@dataclass(frozen=True)
class SignOff:
    pr: str
    sha: str
    verdict: str


def parse_signoff(text: str) -> SignOff | None:
    m = _SIGNOFF.search(text)
    return SignOff(m.group("pr"), m.group("sha"), m.group("verdict").lower()) if m else None


_LOGIN = re.compile(r"^\s*(?:(?:github|login)(?: login)?:\s*)?@?(?P<login>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))\s*$")


def parse_login_reply(text: str) -> str | None:
    m = _LOGIN.match(text.strip())
    return m.group("login") if m else None


@dataclass(frozen=True)
class ThreadView:
    """The parts of GET /threads/<id> the driver reads on restart."""
    control: str = "active"
    messages: list[dict] = field(default_factory=list)  # {ts, sender, text, meta?}
    jobs: list[dict] = field(default_factory=list)  # {id, worker_id, role, brief, job_status, result, inbox_id, attempt}

    @classmethod
    def from_json(cls, v: dict) -> "ThreadView":
        return cls(str((v.get("session") or {}).get("control", "active")), list(v.get("messages") or []), list(v.get("jobs") or []))

    def jobs_with_ref(self, ref: str) -> list[dict]:
        return [j for j in self.jobs if ref_of(j.get("brief") or "") == ref or ref in (j.get("tags") or [])]

    def own_post_with_ref(self, ref: str, owner: str) -> dict | None:
        for m in self.messages:
            if m.get("sender") == owner and ref_of(m.get("text") or "") == ref: return m
        return None

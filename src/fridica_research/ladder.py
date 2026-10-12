"""The R51 ETA ladder for one job (#46 slice 3): `step(job_view, now) -> list[Action]`.

Pure, like `arbiter.observe`: no I/O and no clock read; `now` is the only time, and the view carries
everything else. The driver builds the view, executes the actions, and adds each executed action's id
to the view's `done` set, which is what makes every rung happen once.

The rungs:
- At the ETA with no deliverable, one check-in to the holder.
- W = clamp(0.25 x est, 15 min, 60 min). After the check-in has gone out, a holder silent for more than
  ETA + W (strictly) is rescheduled. Only a reply after the check-in counts as not silent.
- A reschedule goes to the first free alternative other than the holder; with nobody free the owner is
  escalated to instead.
- A `quota` or `offline` cause reschedules as soon as the ETA is reached, with no check-in wait.
- The Outie is asked only when the cause is permission, hardware or internet.
- Owner-only questions (merge_substrate, scope, rules) go to the owner at any time, deliverable or not;
  every other question, and an escalation with nobody free, wakes the deputy (R54, #80).

Every action is executable (#83): it names the job's `thread`, its `text` ends with a `ref:` line, posts carry
`post_kind` "report", and a `wake_deputy` carries the instruct `client_id` from `deputy.client_id`. A
reschedule id names the holder it moves the job from (`<ref>/reschedule/<holder>`), so the rungs fire again
for the next holder.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from . import deputy
from .machine import Action

MIN = 60.0
W_MIN, W_MAX = 15 * MIN, 60 * MIN
NO_WAIT = ("quota", "offline")  # causes that reschedule at the ETA, without the check-in wait
OUTIE_CAUSES = ("permission", "hardware", "internet")  # the only causes the Outie is asked about
OWNER_ONLY = ("merge_substrate", "scope", "rules")  # question kinds only the owner decides (R54)
POST_KIND = "report"


@dataclass(frozen=True)
class JobView:
    """What the ladder needs to know about one job. Times are epoch seconds; `est` is in seconds."""
    ref: str  # the job's ref, e.g. `fm-wave-e-job2`; every action id is `<ref>/<rung>`
    holder: str
    owner: str
    est: float
    eta: float
    outie: str = ""
    deliverable: bool = False
    checkin_at: float | None = None  # when the check-in went out; None until it has
    last_reply: float | None = None  # the holder's latest post in the thread
    cause: str | None = None
    alternatives: list[dict] = field(default_factory=list)  # [{id, free}] in preference order
    questions: list[dict] = field(default_factory=list)  # [{id, kind, text}]
    done: frozenset[str] | set[str] = frozenset()
    thread: str = ""  # the job's thread; every action is posted or instructed there

    @classmethod
    def of(cls, v: "JobView | Mapping") -> "JobView":
        if isinstance(v, JobView): return v
        known = {k: v[k] for k in v if k in cls.__dataclass_fields__}
        return cls(**known)


def window(est: float) -> float:
    """W = clamp(0.25 x est, 15 min, 60 min)."""
    return min(max(0.25 * float(est), W_MIN), W_MAX)


def step(job_view: "JobView | Mapping", now: float) -> list[Action]:
    """The actions due for this job at `now`, minus those whose ref is already in `done`."""
    v = JobView.of(job_view)
    done = set(v.done or ())
    out: list[Action] = []
    cause = (v.cause or "").lower()

    for q in v.questions or []:
        kind, ref = str(q.get("kind", "")).lower(), f"{v.ref}/question/{q.get('id')}"
        if kind in OWNER_ONLY:
            out.append(_act("notify_owner", ref, v, {"to": v.owner, "kind": q.get("kind")}, f"<@{v.owner}> {q.get('kind')} question on {v.ref}: {q.get('text', '')}".rstrip()))
        else:
            out.append(wake(v, ref, f"{q.get('kind')} question: {q.get('text', '')}".rstrip(), {"kind": q.get("kind")}))
    if cause in OUTIE_CAUSES and v.outie:
        out.append(_act("ask_outie", f"{v.ref}/outie/{cause}", v, {"to": v.outie, "cause": cause}, f"<@{v.outie}> {v.ref} is blocked on {cause} for <@{v.holder}>."))

    if not v.deliverable and now >= v.eta:
        if cause in NO_WAIT:
            out.append(reschedule(v, cause))
        elif v.checkin_at is None:
            ref = f"{v.ref}/checkin"
            out.append(_act("post", ref, v, {"to": v.holder}, f"<@{v.holder}> {v.ref} reached its ETA with no deliverable. Where does it stand?"))
        else:
            replied = v.last_reply is not None and v.last_reply > v.checkin_at
            if not replied and now > v.eta + window(v.est):
                out.append(reschedule(v, "silent"))
    return [a for a in out if a.id not in done]


def _act(kind: str, ref: str, v: JobView, data: dict, text: str) -> Action:
    """An executable post: the thread, the report post kind, and the text ending with its `ref:` line."""
    return Action(kind, ref, {**data, "thread": v.thread, "post_kind": POST_KIND, "text": f"{text}\nref: {ref}"})


def wake(v: JobView, ref: str, question: str, data: dict | None = None) -> Action:
    """A `wake_deputy` instruct on the job's thread, keyed by `ref` (one wake-up per question)."""
    signals = {"holder": v.holder, "deliverable": v.deliverable, "eta": v.eta, "checkin_at": v.checkin_at, "last_reply": v.last_reply, "cause": v.cause}
    return Action("wake_deputy", ref, {**(data or {}), "thread": v.thread, "client_id": deputy.client_id(ref), "text": deputy.wake_text(ref, question, signals)})


def reschedule(v: JobView, why: str) -> Action:
    """To the first free alternative; with nobody free the deputy decides (escalation is not owner-only)."""
    to = next((str(a.get("id")) for a in v.alternatives or [] if a.get("free") and a.get("id") and a.get("id") != v.holder), None)
    if to is None:
        return wake(v, f"{v.ref}/escalate", f"<@{v.holder}> is {why} past the ETA and no alternative is free. Reschedule or escalate?", {"why": why})
    return _act("reschedule", f"{v.ref}/reschedule/{v.holder}", v, {"from": v.holder, "to": to, "why": why}, f"<@{to}> {v.ref} moves to you from <@{v.holder}> ({why}).")

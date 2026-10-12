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
- Scope and merge questions go to the owner at any time, deliverable or not.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .machine import Action

MIN = 60.0
W_MIN, W_MAX = 15 * MIN, 60 * MIN
NO_WAIT = ("quota", "offline")  # causes that reschedule at the ETA, without the check-in wait
OUTIE_CAUSES = ("permission", "hardware", "internet")  # the only causes the Outie is asked about
OWNER_QUESTIONS = ("scope", "merge")  # question kinds that go to the owner at any time


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
        if str(q.get("kind", "")).lower() in OWNER_QUESTIONS:
            out.append(Action("notify_owner", f"{v.ref}/question/{q.get('id')}", {"to": v.owner, "kind": q.get("kind"), "text": f"<@{v.owner}> {q.get('kind')} question on {v.ref}: {q.get('text', '')}".rstrip()}))
    if cause in OUTIE_CAUSES and v.outie:
        out.append(Action("ask_outie", f"{v.ref}/outie/{cause}", {"to": v.outie, "cause": cause, "text": f"<@{v.outie}> {v.ref} is blocked on {cause} for <@{v.holder}>."}))

    if not v.deliverable and now >= v.eta:
        if cause in NO_WAIT:
            out.append(reschedule(v, cause))
        elif v.checkin_at is None:
            ref = f"{v.ref}/checkin"
            out.append(Action("post", ref, {"to": v.holder, "text": f"<@{v.holder}> {v.ref} reached its ETA with no deliverable. Where does it stand?\nref: {ref}"}))
        else:
            replied = v.last_reply is not None and v.last_reply > v.checkin_at
            if not replied and now > v.eta + window(v.est):
                out.append(reschedule(v, "silent"))
    return [a for a in out if a.id not in done]


def reschedule(v: JobView, why: str) -> Action:
    """To the first free alternative, or to the owner when nobody is free."""
    to = next((str(a.get("id")) for a in v.alternatives or [] if a.get("free") and a.get("id") and a.get("id") != v.holder), None)
    if to is None:
        return Action("notify_owner", f"{v.ref}/escalate", {"to": v.owner, "why": why, "text": f"<@{v.owner}> {v.ref}: <@{v.holder}> is {why} past the ETA and no alternative is free."})
    return Action("reschedule", f"{v.ref}/reschedule", {"from": v.holder, "to": to, "why": why, "text": f"<@{to}> {v.ref} moves to you from <@{v.holder}> ({why})."})


"""R49: the floor manager's ETA estimator, and the R48/R52 score over committed ETAs (#53).

Pure: no I/O, no network, no clock. Every time the estimator needs comes in as an argument,
so the same inputs always give the same ETA and the module is replayable.

    eta = input time + work + a buffer specific to the Innie

**Work** is how long the job itself takes. **Buffer** is everything around it that is a property
of the Innie rather than of the job: the queue ahead of it in its slot, its rate limits, the lag
on a formal sign-off, and the hours it is offline.

Evidence is ranked, highest first (calibration lesson 1, issue #53 comment 6112142473):

    fresh workload report (R50)  >  trial run (R49)  >  history

A workload report never says how long the work takes, so it settles the *input time* and the
*buffer* (what is really queued, which limits are really active, when the Innie is really away)
while a trial run or similar past jobs settle the *work*. `Estimate.basis` names the
highest-ranked evidence that was used at all, which is what the ranking above is about;
`Estimate.work_basis` names what the work estimate rests on, in the ledger's own
`jobs.est_basis` vocabulary (`trial`, `history` or `reasoning`).

The lesson's three corrections are the reason the buffer is assembled piece by piece instead of
being one average: a queue buffer only for jobs actually ahead in the slot, a sign-off buffer
only for a known lag and only when the floor manager will not prompt, and `input_at = now` when
the Innie reports itself free. Wave A was early on seven jobs out of eight because an averaged
buffer was added to Innies that had nothing in front of them.

Scoring (R48, R52) is over the **first committed** ETA only. A re-estimate never replaces it;
`error_hours = delivered - eta`, so a negative error is early, and early counts the same as late.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import fmean

HOUR = timedelta(hours=1)
EVIDENCE_RANK = ("workload", "trial", "history", "reasoning")  # best evidence first (lesson 1)
WORK_BASES = ("trial", "history", "reasoning")  # the ledger's `jobs.est_basis` vocabulary


def _hours(delta: timedelta) -> float:
    return delta / HOUR


# -- evidence -------------------------------------------------------------------
@dataclass(frozen=True)
class Slot:
    """One job already in an Innie's slot, with the hours it still needs."""
    ref: str
    remaining_hours: float
    ahead: bool = True  # False for work that runs beside ours and does not delay it


@dataclass(frozen=True)
class WorkloadReport:
    """R50: what an Innie answered when asked what it is carrying. The freshest evidence there is."""
    innie: str
    reported_at: datetime
    slots: int = 1
    in_progress: tuple[Slot, ...] = ()
    queued: tuple[Slot, ...] = ()
    rate_limited_hours: float = 0.0  # a limit the Innie says is biting now
    offline_until: datetime | None = None
    source: str = ""

    @property
    def free(self) -> bool:
        """Free means nothing ahead of our job in the slot, which is what the queue buffer is for."""
        return not self.queue_hours

    @property
    def queue_hours(self) -> float:
        return sum(s.remaining_hours for s in (*self.in_progress, *self.queued) if s.ahead)

    def fresh_at(self, now: datetime, within_hours: float) -> bool:
        """A report goes stale: an Innie that was free an hour ago may not be free now."""
        return 0 <= _hours(now - self.reported_at) <= within_hours


@dataclass(frozen=True)
class TrialRun:
    """A measured piece of the real job. `scale` is the fraction of the job that was run."""
    hours: float
    scale: float = 1.0
    ref: str = ""

    @property
    def work_hours(self) -> float:
        if self.scale <= 0: raise ValueError("TrialRun.scale must be positive")
        return self.hours / self.scale


@dataclass(frozen=True)
class PastJob:
    """A delivered job from the ledger, used both as work evidence and (via `score`) as a result."""
    ref: str
    actual_hours: float
    nature: str = ""
    size: str = ""
    innie: str = ""
    role: str = ""


@dataclass(frozen=True)
class InnieBuffer:
    """What an Innie costs around the work, learned from its own history (the ledger's `innies`)."""
    innie: str
    queue_hours: float = 0.0  # the queue it usually has, used only when no fresh report says otherwise
    rate_limit_hours: float = 0.0
    signoff_hours: float = 0.0  # a *known* lag on the formal sign-off gate
    offline_hours: float = 0.0


# -- the estimate ---------------------------------------------------------------
@dataclass(frozen=True)
class Estimate:
    eta: datetime
    input_at: datetime
    work_hours: float
    buffer_hours: float
    basis: str  # the highest-ranked evidence used: workload | trial | history | reasoning
    work_basis: str  # what the work rests on: trial | history | reasoning (jobs.est_basis)
    buffer: dict[str, float] = field(default_factory=dict)
    rationale: str = ""

    @property
    def est_hours(self) -> float:
        """What goes in `jobs.est_hours`: work plus buffer, the whole span from input to ETA."""
        return self.work_hours + self.buffer_hours


def work_from_history(history, nature: str = "", size: str = "") -> float | None:
    """Mean hours of the most similar past jobs: same nature and size, else same nature, else all."""
    for pick in ([j for j in history if j.nature == nature and j.size == size] if nature and size else [],
                 [j for j in history if j.nature == nature] if nature else [],
                 list(history)):
        if pick: return fmean(j.actual_hours for j in pick)
    return None


def estimate(now: datetime, innie: str, *, buffer: InnieBuffer | None = None, workload: WorkloadReport | None = None,
             trial: TrialRun | None = None, history=(), nature: str = "", size: str = "",
             reasoning_hours: float | None = None, input_at: datetime | None = None,
             fresh_within_hours: float = 1.0, will_prompt_signoff: bool = False) -> Estimate:
    """Estimate when `innie` delivers: input time + work + the Innie's own buffer.

    `now` is passed in, never read from a clock. `workload` is used only while it is fresh
    (`fresh_within_hours`); a stale report falls back to the Innie's history, the same as no report.
    `will_prompt_signoff` is the floor manager's own commitment to chase the sign-off, which is
    what removes that part of the buffer (lesson 1).
    """
    buf = buffer or InnieBuffer(innie=innie)
    if buf.innie != innie: raise ValueError(f"buffer is for {buf.innie!r}, not {innie!r}")
    if workload is not None and workload.innie != innie: raise ValueError(f"workload report is for {workload.innie!r}, not {innie!r}")
    report = workload if workload is not None and workload.fresh_at(now, fresh_within_hours) else None

    # work: trial run, else similar past jobs, else the floor manager's reasoning.
    if trial is not None:
        work, work_basis = trial.work_hours, "trial"
    elif (h := work_from_history(history, nature, size)) is not None:
        work, work_basis = h, "history"
    elif reasoning_hours is not None:
        work, work_basis = float(reasoning_hours), "reasoning"
    else:
        raise ValueError("no work evidence: pass a trial run, history, or reasoning_hours")

    # input time: an Innie that reports itself free starts now, not when its queue would have drained.
    start = now if (report is not None and report.free) else (input_at or now)

    # buffer: each piece only when this job actually pays for it.
    parts = {
        "queue": report.queue_hours if report is not None else buf.queue_hours,
        "rate_limit": report.rate_limited_hours if report is not None else buf.rate_limit_hours,
        "signoff": 0.0 if will_prompt_signoff else buf.signoff_hours,
        "offline": max(0.0, _hours(report.offline_until - start)) if (report is not None and report.offline_until is not None and report.offline_until > start) else (0.0 if report is not None else buf.offline_hours),
    }
    parts = {k: v for k, v in parts.items() if v}
    total = sum(parts.values())
    basis = "workload" if report is not None else work_basis
    why = f"{work:.2f} h work from {work_basis}" + (f" + {total:.2f} h buffer (" + ", ".join(f"{k} {v:.2f}" for k, v in parts.items()) + ")" if parts else " + no buffer")
    return Estimate(eta=start + (work + total) * HOUR, input_at=start, work_hours=work, buffer_hours=total,
                    basis=basis, work_basis=work_basis, buffer=parts, rationale=why)


# -- the score ------------------------------------------------------------------
@dataclass(frozen=True)
class Scored:
    """One job's result, scored against the ETA that was committed **first** (R52)."""
    ref: str
    first_eta: datetime
    delivered_at: datetime

    @property
    def error_hours(self) -> float:
        """delivered - eta, so negative is early. Early is an error too (R48)."""
        return _hours(self.delivered_at - self.first_eta)


@dataclass(frozen=True)
class Score:
    jobs_scored: int = 0
    mae_hours: float = 0.0
    bias_hours: float = 0.0
    late: int = 0
    early: int = 0
    max_late_hours: float = 0.0
    max_early_hours: float = 0.0


def first_committed_eta(etas) -> datetime | None:
    """The first ETA committed for a job, from `(committed_at, eta)` pairs in any order.

    Later re-estimates are kept as notes (R52) and never scored.
    """
    pairs = list(etas)
    return min(pairs, key=lambda c: c[0])[1] if pairs else None


def score(results, tolerance_hours: float = 0.0) -> Score:
    """Mean absolute error, bias, late and early counts over the first committed ETAs (R48, R52).

    Only delivered jobs are scored; `tolerance_hours` is the band around the ETA that counts as
    on time (0 by default: the wave record scores to the hour it committed to).
    """
    errors = [r.error_hours for r in results]
    if not errors: return Score()
    late = [e for e in errors if e > tolerance_hours]
    early = [e for e in errors if e < -tolerance_hours]
    return Score(jobs_scored=len(errors), mae_hours=fmean(abs(e) for e in errors), bias_hours=fmean(errors),
                 late=len(late), early=len(early), max_late_hours=max(late, default=0.0), max_early_hours=min(early, default=0.0))

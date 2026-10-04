"""The pure study stage machine: `step(state, event, config) -> (state, actions)`.

No I/O, no clock (every event carries `now`). The driver persists the returned state as the
study's snapshot after every step, so the snapshot equals the fold of `step` over the events.
Stages: Explore -> Claim -> Debate (rounds, then the synthesis LLM call) -> Implement -> Audit
-> Deliver -> Delivered; Blocked (owner-resumable) and Stopped are terminal for the loop.
Design decisions (debate synthesis, chengcli/fridica#126):
- one `waiting` item at a time, identified by a deterministic ActionId carried as a `ref:` line;
- claims: None -> Pending (posted, own ts unknown) -> Settling (own echo seen, window W) ->
  Owned | Lost (smallest Slack ts owns the slug; loser re-picks; no candidates -> Blocked);
- debate rounds count delegations and `round >= max_debate_rounds` is checked before delegating;
- retry rule R: the first stage failure re-runs the stage with attempt=2, the second Blocks and
  notifies the owner; WorkerResult.status failed/needs_input and audit `return` are not failures
  but go to the next iteration (or a partial delivery at max_iterations);
- post refusals are post problems (re-post, redact on egress), never stage failures;
- the same mathematician/physicist/implementer worker ids are resumed across rounds and
  iterations; explorer and auditor are ephemeral.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field

from . import briefs, contracts
from .roles import instructions as role_instructions
from .config import Config

ORDER = ("Explore", "Claim", "Debate", "Implement", "Audit", "Deliver", "Delivered")
TERMINAL = ("Delivered", "Stopped")
JOB_STAGES = ("Explore", "Debate", "Implement", "Audit")  # stages with a worker; the 2x-projected overrun rule applies
STAGE_ROLE = {"Explore": "explorer", "Claim": "driver", "Debate": "debater", "Implement": "implementer", "Audit": "auditor", "Deliver": "driver"}
OVERRUN_FACTOR = 2.0
PERSISTENT = ("mathematician", "physicist", "implementer")
SLOT_CODES = ("too_many_workers", "worker_limit", "too many persistent workers")
MAX_POST_TRIES = 3  # posts of the same text before a rate-limit/failure refusal becomes a stage failure (rule R)


@dataclass(frozen=True)
class Event:
    kind: str
    now: float
    data: dict = field(default_factory=dict)

    def __getitem__(self, k): return self.data[k]
    def get(self, k, d=None): return self.data.get(k, d)


@dataclass(frozen=True)
class Action:
    kind: str
    id: str
    data: dict = field(default_factory=dict)

    def __getitem__(self, k): return self.data[k]
    def get(self, k, d=None): return self.data.get(k, d)
    def to_dict(self) -> dict: return {"kind": self.kind, "id": self.id, **self.data}


@dataclass
class State:
    thread: str
    channel: str
    problem: str
    generation: int = 1
    lineage: str = ""  # origin thread id
    spawner: bool = True  # only the origin root's owner posts follow-ons
    iteration: int = 1
    stage: str = "Explore"
    phase: str = "idle"  # idle | llm | job | post | settle | slot | signoff
    attempt: int = 1
    round: int = 0
    blocked_from: str | None = None
    control: str = "active"
    waiting: dict | None = None
    workers: dict = field(default_factory=dict)  # role -> {worker_id, live}
    group: dict | None = None  # {join_groups: [..], jobs: {job_id: {role, worker_id, result}}, pending: [roles]}
    approaches: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    claim: dict | None = None  # {status: pending|settling|owned, slug, ts}
    peer_claims: dict = field(default_factory=dict)  # slug -> {ts, sender}
    contested: bool = False
    explorer_report: str = ""
    brief: dict = field(default_factory=dict)
    reports: dict = field(default_factory=dict)  # role -> {report, summary, stance}
    synthesis: dict = field(default_factory=dict)
    implementer: dict = field(default_factory=dict)  # {summary, machine_state, pr, sha}
    audit: dict = field(default_factory=dict)  # {summary, verdict}
    signoffs: dict = field(default_factory=dict)  # sender -> verdict
    audit_scopes: dict = field(default_factory=dict)  # scope -> {reviewer (slack id) | None for the local auditor, verdict, signed_at}
    people: dict = field(default_factory=dict)  # sender -> GitHub login learned in-thread
    findings: list = field(default_factory=list)
    deliverable: dict = field(default_factory=dict)
    partial: bool = False
    redacted: bool = False
    post_tries: int = 0
    timers: dict = field(default_factory=dict)  # timer id -> deadline
    started_at: float = 0.0
    finished_at: float | None = None
    projected_hours: float = 0.0
    stage_log: list = field(default_factory=list)  # {stage, start, projected, end, actual}
    followon: str | None = None  # child thread id once known
    notes: list = field(default_factory=list)  # owner-facing notices, latest last

    def to_dict(self) -> dict: return asdict(self)
    @classmethod
    def from_dict(cls, d: dict) -> "State": return cls(**d)

    def approach(self) -> contracts.Approach:
        slug = (self.claim or {}).get("slug", "")
        for a in self.approaches:
            if a["slug"] == slug: return contracts.Approach(**a)
        return contracts.Approach(slug or "none", "no approach")


class M:
    """One step: mutates a private copy of the state and collects actions."""

    def __init__(self, s: State, ev: Event, cfg: Config):
        self.s, self.ev, self.cfg, self.out = copy.deepcopy(s), ev, cfg, []

    # -- ids and timers -------------------------------------------------------
    def aid(self, suffix: str, stage: str | None = None) -> str:
        s = self.s
        return f"{s.thread}/g{s.generation}/i{s.iteration}/{stage or s.stage}/a{s.attempt}/{suffix}"

    def emit(self, kind: str, id_: str, **data): self.out.append(Action(kind, id_, data))

    def arm(self, suffix: str, seconds: float):
        tid = self.aid(suffix)
        self.s.timers[tid] = self.ev.now + seconds
        self.emit("arm_timer", tid, deadline=self.s.timers[tid])
        return tid

    def cancel(self, *suffixes: str):
        for tid in [t for t in self.s.timers if t.rsplit("/", 1)[1] in suffixes]:
            del self.s.timers[tid]
            self.emit("cancel_timer", tid)

    def cancel_all(self):
        for tid in list(self.s.timers):
            del self.s.timers[tid]
            self.emit("cancel_timer", tid)

    def board(self): self.emit("board_update", self.aid("board"), thread=self.s.thread)

    def notify(self, text: str):
        self.s.notes.append(text)
        self.emit("notify_owner", self.aid(f"notify-{len(self.s.notes)}"), text=text)

    def stop_workers(self, roles=PERSISTENT):
        for role in roles:
            w = self.s.workers.get(role)
            if w and w.get("live"):
                w["live"] = False
                self.emit("stop_worker", self.aid(f"stop-{role}"), worker_id=w["worker_id"])

    # -- stage bookkeeping ----------------------------------------------------
    def close_row(self):
        if self.s.stage_log and self.s.stage_log[-1]["end"] is None:
            row = self.s.stage_log[-1]
            row["end"] = self.ev.now
            row["actual"] = self.ev.now - row["start"]

    def set_stage(self, stage: str):
        self.close_row()
        self.s.stage, self.s.attempt, self.s.phase, self.s.waiting, self.s.group = stage, 1, "idle", None, None
        if stage in ORDER[:-1]:
            self.s.stage_log.append(self.row(stage))
        elif stage == "Delivered":
            self.s.finished_at = self.ev.now
        self.board()

    def row(self, stage: str) -> dict:
        return {"stage": stage, "role": STAGE_ROLE[stage], "iteration": self.s.iteration, "start": self.ev.now, "projected": self.cfg.projection[stage.lower()], "end": None, "actual": None}

    def arm_overrun(self):
        """R12: a job stage is interrupted at 2x its projected duration; one timer per stage run, kept across attempts."""
        s = self.s
        tid = f"{s.thread}/g{s.generation}/i{s.iteration}/{s.stage}/overrun"
        if s.stage in JOB_STAGES and tid not in s.timers:
            s.timers[tid] = s.stage_log[-1]["start"] + OVERRUN_FACTOR * self.cfg.projection[s.stage.lower()]
            self.emit("arm_timer", tid, deadline=s.timers[tid])

    def mirror_scopes(self):
        """Keep the current audit row's copy of the scopes current, so each iteration's cards and hours pair with their own row."""
        for row in reversed(self.s.stage_log):
            if row["stage"] == "Audit":
                row["scopes"] = copy.deepcopy(self.s.audit_scopes)
                return

    def halt(self, stage: str, reason: str):
        """Blocked (owner-resumable) or Stopped: stop everything, keep the state."""
        if stage == "Blocked": self.s.blocked_from = self.s.stage
        self.close_row()
        self.s.stage, self.s.phase, self.s.waiting, self.s.group = stage, "idle", None, None
        self.cancel_all()
        self.stop_workers(roles=list(self.s.workers))
        self.notify(reason)
        self.board()

    # -- retry rule R ---------------------------------------------------------
    def retry(self, reason: str):
        s = self.s
        if s.attempt == 1:
            self.cancel_all()
            self.stop_workers(roles=[j["role"] for j in (s.group or {"jobs": {}})["jobs"].values()])
            s.attempt, s.phase, s.waiting, s.group = 2, "idle", None, None
            s.notes.append(f"retrying {s.stage}: {reason}")
            self.enter(s.stage)
        else:
            self.halt("Blocked", f"{s.stage} failed twice ({reason}); `fridica-research resume {s.thread}` after fixing it")

    # -- stage entries --------------------------------------------------------
    def enter(self, stage: str):
        s = self.s
        if stage != s.stage: self.set_stage(stage)
        self.arm_overrun()
        {"Explore": self.enter_explore, "Claim": self.pick, "Debate": self.enter_debate, "Implement": self.enter_implement, "Audit": self.enter_audit, "Deliver": self.enter_deliver}[stage]()

    def enter_explore(self):
        s = self.s
        s.phase, s.waiting = "llm", {"kind": "llm", "id": self.aid("brief")}
        self.emit("llm_call", s.waiting["id"], name="study_brief", prompt=briefs.prompt_brief(s.problem, s.iteration, s.findings, s.peer_claims))
        self.arm("timer", self.cfg.stage_timeout)

    def delegate(self, suffix: str, role: str, brief: str, ephemeral: bool, backend: str = "same") -> dict:
        s = self.s
        w = s.workers.get(role)
        catalog_role = "debater" if role in ("mathematician", "physicist") else role
        lens = role if catalog_role == "debater" else None
        req = contracts.DelegateRequest(catalog_role, brief, "fresh", w["worker_id"] if w else None, ephemeral, backend, "report", (self.aid(suffix),), role_instructions(catalog_role, lens))
        action = {"action_id": self.aid(suffix), "thread": s.thread, **req.body()}
        if lens: action["lens"] = lens  # local correlation only; the host receives the debater role and role prose
        self.emit("delegate", self.aid(suffix), **action)
        return action

    def pick(self):
        s = self.s
        taken = set(s.excluded) | set(s.peer_claims)
        cands = [a for a in s.approaches if a["slug"] not in taken]
        if not cands:
            self.halt("Blocked", "all approaches are claimed by peers or lost; add approaches or resume")
            return
        a = cands[0]
        also = tuple(x["slug"] for x in cands[1:3])
        text = contracts.format_claim(contracts.Claim(s.iteration, a["slug"], a["title"], a["why"], also), self.aid("claim"))
        s.claim = {"status": "pending", "slug": a["slug"], "ts": None}
        self.post("claim", "study_claim", text)
        self.cancel("timer")
        self.arm("timer", self.cfg.stage_timeout)

    def post(self, suffix: str, kind: str, text: str, details: str | None = None):
        s = self.s
        text, details = briefs.guard_post(text, details)
        action = {"thread": s.thread, "post_kind": kind, "text": text, "details": details}
        s.phase, s.post_tries = "post", 1
        s.waiting = {"kind": "post", "id": self.aid(suffix), "action": action}
        self.emit("post", s.waiting["id"], **action)

    def repost(self):
        s = self.s
        s.post_tries += 1
        self.emit("post", s.waiting["id"], **s.waiting["action"])

    def enter_debate(self):
        s = self.s
        if s.round >= self.cfg.max_debate_rounds:
            self.enter_synthesis()
            return
        s.round += 1
        prior = {r: v.get("report", "") for r, v in s.reports.items()}
        jobs = {}
        for role, other in (("mathematician", "physicist"), ("physicist", "mathematician")):
            brief = briefs.debate(self.aid(f"debate-{s.round}-{role}"), role, other, s.problem, s.approach(), s.explorer_report, prior, s.round, s.findings)
            jobs[role] = self.delegate(f"debate-{s.round}-{role}", role, brief, ephemeral=False)
        s.phase, s.group = "job", {"join_groups": [], "jobs": {}, "pending": list(jobs)}
        s.waiting = {"kind": "group", "id": self.aid(f"debate-{s.round}"), "actions": list(jobs.values())}
        self.cancel("timer")
        self.arm("timer", self.cfg.stage_timeout)

    def enter_synthesis(self):
        s = self.s
        s.phase, s.waiting = "llm", {"kind": "llm", "id": self.aid("synth")}
        self.emit("llm_call", s.waiting["id"], name="study_synthesis", prompt=briefs.prompt_synthesis(s.problem, s.approach(), s.explorer_report, s.reports))
        self.cancel("timer")
        self.arm("timer", self.cfg.stage_timeout)

    def enter_implement(self):
        s = self.s
        superseded = "implementer" in s.workers
        brief = briefs.implementer(self.aid("impl"), s.problem, s.approach(), s.synthesis.get("synthesis", ""), s.synthesis.get("decisions", []), s.synthesis.get("open_questions", []), s.findings, superseded)
        action = self.delegate("impl", "implementer", brief, ephemeral=False)
        s.phase, s.group = "job", {"join_groups": [], "jobs": {}, "pending": ["implementer"]}
        s.waiting = {"kind": "group", "id": self.aid("impl"), "actions": [action]}
        self.arm("timer", self.cfg.stage_timeout)

    def enter_audit(self):
        """R13: one audit scope per peer reviewer; a local auditor worker only for the scopes no peer takes."""
        s = self.s
        impl = s.implementer
        asks = [r.handle for r in self.cfg.reviewers if not self.cfg.login_of(r.handle, s.people)]
        text = briefs.audit_request(self.aid("audit-request"), s.iteration, impl.get("pr", ""), impl.get("sha", ""), [(r.handle, r.focus) for r in self.cfg.reviewers], asks)
        self.emit("post", self.aid("audit-request"), thread=s.thread, post_kind="report", text=text, details=None)
        s.signoffs, s.audit = {}, {}
        s.audit_scopes = {}
        for r in self.cfg.reviewers:  # one entry per reviewer line, even when two lines name the same reviewer without a scope
            key, n = r.focus or f"review-{r.handle}", 1
            while key in s.audit_scopes: n, key = n + 1, f"{r.focus or f'review-{r.handle}'}-{n + 1}"
            s.audit_scopes[key] = {"reviewer": r.handle, "verdict": None, "signed_at": None, "requested": True}
        local = self.cfg.uncovered_scopes()
        for scope in local: s.audit_scopes[scope] = {"reviewer": None, "verdict": None, "signed_at": None, "requested": True}
        self.mirror_scopes()
        self.arm("timer", self.cfg.stage_timeout)
        if not local:
            s.phase, s.waiting, s.group = "signoff", None, None
            if not self.cfg.require_signoffs: self.check_signoffs()  # nothing to wait for: no local auditor and sign-offs optional
            return
        brief = briefs.auditor(self.aid("audit"), s.problem, s.synthesis.get("synthesis", ""), impl.get("summary", ""), impl.get("machine_state"), s.findings, local)
        action = self.delegate("audit", "auditor", brief, ephemeral=True, backend=self.cfg.auditor_backend)
        s.phase, s.group = "job", {"join_groups": [], "jobs": {}, "pending": ["auditor"]}
        s.waiting = {"kind": "group", "id": self.aid("audit"), "actions": [action]}

    def enter_deliver(self):
        s = self.s
        s.phase, s.waiting = "llm", {"kind": "llm", "id": self.aid("deliver")}
        self.emit("llm_call", s.waiting["id"], name="study_deliver", prompt=briefs.prompt_deliver(s.problem, s.approach(), s.synthesis.get("synthesis", ""), s.implementer.get("summary", ""), s.audit.get("summary", ""), s.findings, s.partial))
        self.arm("timer", self.cfg.stage_timeout)

    def next_iteration(self, note: str):
        s = self.s
        s.findings.append(note)
        self.cancel_all()
        if s.iteration < self.cfg.max_iterations:
            s.iteration, s.round, s.claim, s.approaches, s.reports, s.synthesis = s.iteration + 1, 0, None, [], {}, {}
            self.enter("Explore")
        else:
            s.partial = True
            self.enter("Deliver")

    def finish(self):
        self.cancel_all()
        self.stop_workers()
        self.set_stage("Delivered")

    # -- the transition function ---------------------------------------------
    def run(self):
        s, ev = self.s, self.ev
        k = ev.kind
        if k == "owner_stop":
            if s.stage not in TERMINAL: self.halt("Stopped", "stopped by the owner")
            return
        if k == "control_changed":
            s.control = ev["control"]
            if s.control != "active" and s.stage not in TERMINAL + ("Blocked",): self.halt("Blocked", f"thread control is {s.control}")
            return
        if k == "owner_resume":
            if s.stage == "Blocked" and s.control == "active":
                s.stage, s.blocked_from = s.blocked_from or "Explore", None
                s.attempt = 1
                s.stage_log.append(self.row(s.stage))
                self.board()
                self.enter(s.stage)
            return
        if k == "peer_post":
            self.peer_post()
            return
        if k == "sign_off":
            if not head_matches(ev.get("pr", ""), ev.get("sha", ""), s.implementer.get("pr", ""), s.implementer.get("sha", "")):
                # A sign-off names the head it reviewed; one for another PR or sha is noted, never counted.
                s.findings.append(f"iteration {s.iteration} sign-off from {ev['sender']} ignored: {ev.get('pr')} {ev.get('sha')} is not the reviewed head {s.implementer.get('pr') or 'none'} {s.implementer.get('sha') or 'none'}")
                return
            s.signoffs[ev["sender"]] = ev["verdict"]
            for sc in s.audit_scopes.values():
                if sc["reviewer"] == ev["sender"] and sc["signed_at"] is None: sc.update(verdict=ev["verdict"], signed_at=ev.now)
            self.mirror_scopes()
            self.board()
            if s.stage == "Audit" and s.phase == "signoff": self.check_signoffs()
            return
        if k == "finding":
            # R12: changes that arrive while a stage runs never reach the running worker; they are findings for this iteration.
            s.findings.append(f"iteration {s.iteration} note during {s.stage}: {ev['text']}")
            return
        if k == "login_reply":
            s.people[ev["sender"]] = ev["login"]
            self.board()
            return
        if s.stage in TERMINAL or s.stage == "Blocked":
            if k == "job_result": self.mark_dead(ev)
            return
        if k == "timeout":
            self.timeout(ev["timer_id"])
            return
        if k == "llm_result":
            if not (s.waiting and s.waiting["kind"] == "llm" and s.waiting["id"] == ev["action_id"]): return
            if not ev["ok"]: self.retry(f"llm {ev.get('error', 'failed')}")
            else: self.llm(ev["payload"])
            return
        if k == "delegated":
            if self.awaits(ev["action_id"]):
                waiting_action = next(a for a in s.waiting["actions"] if a["action_id"] == ev["action_id"])
                lane = waiting_action.get("lens") or waiting_action["role"]
                s.group["join_groups"].append(ev["join_group"])
                for j in ev["jobs"]:
                    s.group["jobs"][j["job_id"]] = {"role": lane, "action_id": ev["action_id"], "worker_id": j["worker_id"], "result": None}
                    s.workers[lane] = {"worker_id": j["worker_id"], "live": True}
                    if lane in s.group["pending"]: s.group["pending"].remove(lane)
                if not s.group["pending"]: s.waiting["kind"], s.phase = "group", "job"
            return
        if k == "delegate_refused":
            if not self.awaits(ev["action_id"]): return
            if any(c in str(ev.get("code", "")) for c in SLOT_CODES):
                s.phase, s.waiting["kind"] = "slot", "slot"
                s.waiting["refused"] = ev["action_id"]
            else: self.retry(f"delegate refused: {ev.get('code')}")
            return
        if k == "job_result":
            self.job_result(ev)
            return
        if k == "own_post_seen":
            self.own_post(ev)
            return
        if k == "post_refused":
            self.post_refused(ev)
            return

    def awaits(self, action_id: str) -> bool:
        w = self.s.waiting
        return bool(w and w["kind"] in ("group", "slot") and action_id in {a["action_id"] for a in w["actions"]})

    def mark_dead(self, ev: Event):
        for w in self.s.workers.values():
            if w["worker_id"] == ev.get("worker_id") and ev.get("job_status") != "finished": w["live"] = False

    def timeout(self, tid: str):
        s = self.s
        if tid not in s.timers: return
        del s.timers[tid]
        suffix = tid.rsplit("/", 1)[1]
        if suffix == "settle" and s.stage == "Claim" and s.phase == "settle":
            s.claim["status"] = "owned"
            self.cancel_all()
            self.enter("Debate")
        elif suffix == "repost" and s.phase == "post":
            self.repost()
        elif suffix == "timer":
            if s.stage == "Audit" and s.phase == "signoff": self.deliver_without_signoffs()
            else: self.retry("timeout")
        elif suffix == "overrun":
            self.overrun()

    def deliver_without_signoffs(self):
        """The wait ended (stage timer or overrun): only the *missing* sign-offs are waived; a `changes` verdict on the head still returns the study (T1)."""
        s = self.s
        if self.unrequested(): return  # a scope nobody was asked to review cannot pass (T2); rule R is already running
        s.audit["signoffs_missing"] = [sc["reviewer"] for sc in s.audit_scopes.values() if sc["reviewer"] and sc["signed_at"] is None]
        if self.return_for_changes(): return
        s.audit.setdefault("verdict", "pass")
        self.enter("Deliver")

    def unrequested(self) -> bool: return any(not sc.get("requested", True) for sc in self.s.audit_scopes.values())

    def return_for_changes(self) -> bool:
        s = self.s
        peers = [sc for sc in s.audit_scopes.values() if sc["reviewer"]]
        if not any(sc["verdict"] == "changes" for sc in peers): return False
        s.audit["verdict"] = "return"
        self.cancel_all()
        self.next_iteration(f"iteration {s.iteration} peer review asked for changes: " + ", ".join(f"{scope}={sc['verdict']}" for scope, sc in sorted(s.audit_scopes.items()) if sc["reviewer"]))
        return True

    def overrun(self):
        """R12: the stage ran 2x its projected time; stop its workers, keep the partial result as a finding, move on."""
        s = self.s
        if s.stage == "Audit" and s.phase == "signoff":
            self.deliver_without_signoffs()
            return
        done = [f"{j['role']}: {j['result'].get('summary', '')}" for j in (s.group or {"jobs": {}})["jobs"].values() if j["result"]]
        partial = "; ".join(done + [f"{r}: {v.get('summary', '')}" for r, v in s.reports.items() if f"{r}:" not in " ".join(done)]) or "none"
        self.stop_workers(roles=[j["role"] for j in (s.group or {"jobs": {}})["jobs"].values()])
        elapsed = (self.ev.now - s.stage_log[-1]["start"]) / 60
        self.next_iteration(f"iteration {s.iteration} {s.stage} interrupted after {elapsed:.0f} min (2x the projected {self.cfg.projection[s.stage.lower()] / 60:.0f} min); partial result: {partial}")

    def llm(self, payload: dict):
        s = self.s
        s.waiting = None
        if s.stage == "Explore":
            s.brief = payload
            brief = briefs.explorer(self.aid("explorer"), s.problem, payload.get("brief", ""), payload.get("questions", []), s.findings, s.peer_claims)
            action = self.delegate("explorer", "explorer", brief, ephemeral=True)
            s.phase, s.group = "job", {"join_groups": [], "jobs": {}, "pending": ["explorer"]}
            s.waiting = {"kind": "group", "id": self.aid("explorer"), "actions": [action]}
        elif s.stage == "Debate":
            s.synthesis = payload
            self.cancel_all()
            self.stop_workers(("mathematician", "physicist"))
            self.enter("Implement")
        elif s.stage == "Deliver":
            s.deliverable = payload
            self.post_result()

    def post_result(self):
        s = self.s
        d = s.deliverable
        rows = [f"| {r['stage']} | {r['projected'] / 3600:.2f} h | {(r['actual'] or 0) / 3600:.2f} h |" for r in s.stage_log]
        actual = (self.ev.now - s.started_at) / 3600
        lines_ = [f"approach: {(s.claim or {}).get('slug', 'none')}", f"pr: {s.implementer.get('pr') or 'none'}", f"sha: {s.implementer.get('sha') or 'none'}", f"audit: {s.audit.get('verdict', 'none')}" + (f" (sign-offs missing: {', '.join(s.audit['signoffs_missing'])})" if s.audit.get("signoffs_missing") else ""), f"projected: {s.projected_hours:g} h, actual: {actual:.2f} h", "| Stage | Projected | Actual |", "|---|---|---|", *rows]
        if s.redacted: lines_.append("details: withheld by the egress gate; see the fridica files for this thread")
        text = contracts.format_result(s.iteration, d.get("summary", ""), lines_, self.aid("result"), s.partial)
        self.post("result", "study_result", text, None if s.redacted else d.get("details"))

    def job_result(self, ev: Event):
        s = self.s
        jr = contracts.JobResult.from_event(ev.data)
        if s.phase == "slot" and s.waiting:
            # A slot freed (interrupted/finished job outside our group): re-send the refused delegate(s).
            for a in s.waiting["actions"]:
                if s.waiting.get("refused") in (None, a["action_id"]): self.emit("delegate", a["action_id"], **a)
            return
        g = s.group
        if not g or jr.job_id not in g["jobs"]:  # job ids are unique; the join group is informational
            self.mark_dead(ev)
            return
        job = g["jobs"][jr.job_id]
        if job["result"] is not None: return  # duplicate (job_id, attempt)
        if jr.job_status != "finished":
            s.workers.get(job["role"], {}).update(live=False)
            self.retry(f"{job['role']} job {jr.job_status}: {jr.code}")
            return
        job["result"] = jr.result or {}
        {"Explore": self.explore_done, "Debate": self.debate_done, "Implement": self.implement_done, "Audit": self.audit_done}.get(s.stage, lambda j: None)(job)

    def explore_done(self, job):
        s = self.s
        s.workers.pop("explorer", None)
        s.explorer_report = job["result"].get("report", "")
        approaches = contracts.parse_approaches(s.explorer_report)
        if not approaches:
            self.retry("explorer reported no approaches")
            return
        s.approaches = [asdict(a) for a in approaches]
        self.cancel_all()
        self.enter("Claim")

    def debate_done(self, job):
        s = self.s
        st = contracts.parse_stance(job["result"])
        s.reports[job["role"]] = {"report": job["result"].get("report", ""), "summary": job["result"].get("summary", ""), "stance": st.position or contracts.DEFAULT_POSITION}
        if any(j["result"] is None for j in s.group["jobs"].values()) or s.group["pending"]: return
        self.cancel("timer")
        if all(s.reports.get(r, {}).get("stance") == "agree" for r in ("mathematician", "physicist")): self.enter_synthesis()
        else: self.enter_debate()

    def implement_done(self, job):
        s = self.s
        r = job["result"]
        ms = r.get("machine_state") or {}
        pr = next((a for a in (r.get("artifacts") or []) if isinstance(a, str) and "/pull/" in a), "")
        s.implementer = {"summary": r.get("summary", ""), "machine_state": ms, "pr": pr, "sha": str(ms.get("commit") or ""), "status": r.get("status")}
        if r.get("status") in ("done", "partial"):
            self.cancel_all()
            self.enter("Audit")
        else:
            note = "; ".join([*(r.get("unresolved") or []), *([r["question"]] if r.get("question") else [])]) or r.get("summary", "")
            self.next_iteration(f"iteration {s.iteration} implementer {r.get('status')}: {note}")

    def audit_done(self, job):
        s = self.s
        r = job["result"]
        s.workers.pop("auditor", None)
        verdict = contracts.parse_stance(r).verdict or contracts.DEFAULT_VERDICT
        s.audit = {"summary": r.get("summary", ""), "verdict": verdict, "report": r.get("report", "")}
        if verdict == "reject":
            self.emit("post", self.aid("reject"), thread=s.thread, post_kind="report", text="\n".join([contracts.stage_line("Audit", s.iteration), "verdict: reject (out of scope); the owner decides", f"ref: {self.aid('reject')}"]), details=None)
            self.halt("Stopped", f"auditor rejected the study as out of scope: {s.audit['summary']}")
        elif verdict == "return":
            self.next_iteration(f"iteration {s.iteration} audit returned: {s.audit['summary']}")
        else:
            s.phase, s.waiting = "signoff", None
            self.check_signoffs()

    def check_signoffs(self):
        s = self.s
        if self.unrequested(): return
        peers = [sc for sc in s.audit_scopes.values() if sc["reviewer"]]
        if self.cfg.require_signoffs and any(sc["signed_at"] is None for sc in peers): return
        s.audit.setdefault("verdict", "pass")
        if self.return_for_changes(): return
        self.cancel_all()
        self.enter("Deliver")

    # -- posts ---------------------------------------------------------------
    def own_post(self, ev: Event):
        s = self.s
        w = s.waiting
        if not (w and w["kind"] == "post" and ev.get("kind") == w["action"]["post_kind"] and contracts.ref_of(ev.get("text", "")) == w["id"]): return
        kind = ev["kind"]
        if kind == "study_claim" and s.stage == "Claim":
            s.claim.update(status="settling", ts=ev["ts"])
            self.cancel("repost")
            peer = s.peer_claims.get(s.claim["slug"])
            if peer and ts_key(peer["ts"]) < ts_key(ev["ts"]): self.lose()
            else:
                s.peer_claims.pop(s.claim["slug"], None)  # a later peer claim on our slug lost; the slug is ours, not taken
                s.phase, s.waiting = "settle", {"kind": "timer", "id": self.arm("settle", self.cfg.settle_window)}
        elif kind == "study_result" and s.stage == "Deliver":
            s.waiting = None
            if self.cfg.auto_followon and s.spawner and s.generation < self.cfg.max_generations and s.deliverable.get("next_problem"):
                text = contracts.format_root(s.deliverable["next_problem"], s.generation + 1, s.lineage or s.thread, s.projected_hours, self.aid("root"))
                self.post("root", "study_root", text)
            else: self.finish()
        elif kind == "study_root" and s.stage == "Deliver":
            s.followon = ev.get("child_thread") or f"{s.channel}:{ev['ts']}"
            self.finish()

    def post_refused(self, ev: Event):
        s = self.s
        w = s.waiting
        if s.stage == "Audit" and ev.get("post_kind") == "report" and not (w and w["kind"] == "post") and ev.get("action_id") in (None, self.aid("audit-request")):
            # T2: the reviewer request never reached the thread; no peer was asked, so the stage cannot pass: record it and apply rule R.
            for sc in s.audit_scopes.values():
                if sc["reviewer"]: sc["requested"] = False
            self.mirror_scopes()
            self.retry(f"audit request refused: {ev.get('code')}")
            return
        if not (w and w["kind"] == "post" and ev.get("post_kind") == w["action"]["post_kind"]): return
        code = str(ev.get("code", ""))
        if code.startswith("egress"):
            if w["action"]["post_kind"] == "study_result" and not s.redacted:
                s.redacted = True
                self.notify(f"the deliverable was refused by the egress gate ({code}); posting a redacted version")
                self.post_result()
            else: self.retry(f"post refused by the egress gate: {code}")  # the same text cannot pass twice
            return
        if s.post_tries >= MAX_POST_TRIES:
            self.retry(f"post refused {s.post_tries} times: {code}")
            return
        s.timers[self.aid("repost")] = self.ev.now + float(ev.get("retry_after") or 30)
        self.emit("arm_timer", self.aid("repost"), deadline=s.timers[self.aid("repost")])

    def peer_post(self):
        s, ev = self.s, self.ev
        if ev.get("kind") != "study_claim": return
        c = contracts.parse_claim(ev.get("text", ""))
        if not c: return
        ours = s.claim if s.claim and s.claim["slug"] == c.slug and s.claim.get("ts") else None
        if ours and ts_key(ours["ts"]) < ts_key(ev["ts"]): return  # we hold the slug with the earlier ts: the peer lost, nothing is taken
        prev = s.peer_claims.get(c.slug)
        if not prev or ts_key(ev["ts"]) < ts_key(prev["ts"]): s.peer_claims[c.slug] = {"ts": ev["ts"], "sender": ev.get("sender", "")}
        if not s.claim or s.claim["slug"] != c.slug or s.stage in TERMINAL + ("Blocked",): return
        if s.stage == "Claim" and s.claim["status"] == "settling" and ts_key(ev["ts"]) < ts_key(s.claim["ts"]): self.lose()
        elif s.stage != "Claim" and s.claim.get("ts") and ts_key(ev["ts"]) < ts_key(s.claim["ts"]) and not s.contested:
            s.contested = True
            self.notify(f"claim on {c.slug} contested late by {ev.get('sender', '?')} (earlier ts); both studies continue")

    def lose(self):
        s = self.s
        s.excluded.append(s.claim["slug"])
        s.claim = None
        self.cancel("settle", "repost")
        s.waiting = None
        self.pick()


def head_matches(pr: str, sha: str, reviewed_pr: str, reviewed_sha: str) -> bool:
    """A sign-off counts only for the reviewed head: the same PR (URL, `#N` or `N`) and a prefix of the same sha; an unknown head cannot be matched and is accepted."""
    def pr_id(x: str) -> str: return str(x).strip().rstrip("/").rsplit("/", 1)[-1].lstrip("#")
    pr_ok = not reviewed_pr or pr_id(pr) == pr_id(reviewed_pr)
    sha_ok = not reviewed_sha or (bool(sha) and reviewed_sha.lower().startswith(str(sha).lower()))
    return pr_ok and sha_ok


def ts_key(ts: str) -> tuple[int, int]:
    """Slack ts as (seconds, microseconds): the fraction is fixed-width, so `1700.12` (120000 us) sorts after `1700.000012` (12 us)."""
    sec, _, frac = str(ts).partition(".")
    return int(sec), int((frac or "0")[:6].ljust(6, "0"))


def start(thread: str, channel: str, problem: str, now: float, *, generation: int = 1, lineage: str = "", spawner: bool = True, projected_hours: float, cfg: Config) -> tuple[State, list[Action]]:
    """The `started` event for a thread with no state: a fresh State entering Explore."""
    s = State(thread, channel, problem, generation=generation, lineage=lineage or thread, spawner=spawner, started_at=now, projected_hours=projected_hours)
    m = M(s, Event("started", now), cfg)
    m.emit("set_driver", m.aid("driver"), thread=thread, driver="external")
    m.s.stage_log.append(m.row("Explore"))
    m.board()
    m.arm_overrun()
    m.enter_explore()
    return m.s, m.out


def step(state: State, event: Event, cfg: Config) -> tuple[State, list[Action]]:
    m = M(state, event, cfg)
    m.run()
    return m.s, m.out

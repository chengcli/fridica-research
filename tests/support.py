"""Test helpers: a config, canned worker results, and `World`, which drives the machine by answering its actions."""
from __future__ import annotations

from fridica_research import machine
from fridica_research.config import Config, Reviewer
from fridica_research.machine import Event

OWNER, PEER, REV = "UOWNER", "UPEER", "UREV"
CFG = Config(owner=OWNER, channels=("C1",), settle_window=60, stage_timeout=1000, reviewers=(Reviewer(REV, "scope"),), audit_scopes=("scope", "code"), people={OWNER: "chengcli", REV: "reviewer"}, require_signoffs=False,
             projection={"explore": 1200, "claim": 120, "debate": 1200, "implement": 5400, "audit": 5400, "deliver": 600})
THREAD = "T1:C1:1700000000.000100"
PR, SHA = "https://github.com/o/r/pull/9", "abc1234"  # the reviewed head: `World.to_audit` and `result()` defaults

EXPLORER_REPORT = "Findings...\n\n## Approaches\n- alpha: Alpha design -- cheapest\n- beta: Beta design -- robust\n- gamma: Gamma design -- exotic\n"


def report(position=None, verdict=None, body="analysis"):
    block = ""
    if position or verdict:
        block = "\n\n## Stance\n" + (f"position: {position}\n" if position else "") + (f"verdict: {verdict}\n" if verdict else "") + "notes: n\n"
    return body + block


def result(status="done", **kw):
    r = {"status": status, "summary": kw.pop("summary", "sum"), "report": kw.pop("report", "rep"), "changes": [], "validation": [], "artifacts": kw.pop("artifacts", []), "machine_state": kw.pop("machine_state", {"branch": "b", "commit": "abc1234", "dirty": False}), "unresolved": kw.pop("unresolved", [])}
    r.update(kw)
    return r


LLM = {
    "study_brief": {"brief": "map it", "questions": ["q1"]},
    "study_synthesis": {"synthesis": "do alpha", "decisions": ["d1"], "open_questions": []},
    "study_deliver": {"summary": "delivered", "details": "long details", "next_problem": "next thing"},
}


class World:
    """Answers actions with the events the driver would feed: llm results, `delegated`, own-post echoes, timers."""

    def __init__(self, cfg: Config = CFG, now: float = 1_700_000_000.0, auto_post: bool = True, auto_delegate: bool = True, llm=None, hold: tuple[str, ...] = ()):
        self.cfg, self.now, self.auto_post, self.auto_delegate, self.hold = cfg, now, auto_post, auto_delegate, hold
        self.llm = llm or (lambda name, prompt: LLM[name])
        self.state: machine.State | None = None
        self.actions: list[machine.Action] = []
        self.events: list[Event] = []
        self.jobs = 0
        self.ts = 100
        self.workers: dict[str, str] = {}
        self.pending: dict[str, str] = {}  # role -> job id awaiting a result
        self.refuse_delegate: list[str] = []  # codes to answer the next delegates with

    def next_ts(self) -> str:
        self.ts += 1
        return f"1700000000.{self.ts:06d}"

    def start(self, thread=THREAD, problem="Study the thing", **kw):
        self.state, actions = machine.start(thread, "C1", problem, self.now, projected_hours=4.0, cfg=self.cfg, **kw)
        self.react(actions)
        return self.state

    def feed(self, ev: Event):
        self.events.append(ev)
        self.state, actions = machine.step(self.state, ev, self.cfg)
        self.react(actions)
        return self.state

    def ev(self, kind_, **data): return self.feed(Event(kind_, self.now, data))

    def react(self, actions):
        self.actions.extend(actions)
        for a in actions:
            if a.kind == "llm_call":
                self.ev("llm_result", action_id=a.id, ok=True, payload=self.llm(a["name"], a["prompt"]))
            elif a.kind == "delegate" and self.auto_delegate:
                lane = a.get("lens") or a["role"]
                if self.refuse_delegate:
                    self.ev("delegate_refused", action_id=a.id, code=self.refuse_delegate.pop(0), status=409, role=a["role"])
                    continue
                self.jobs += 1
                wid = a.get("worker_id") or self.workers.get(lane) or f"w-{lane}-{self.jobs}"
                self.workers[lane] = wid
                self.pending[lane] = f"job-{self.jobs}"
                self.ev("delegated", action_id=a.id, join_group=f"grp-{a.id}", jobs=[{"job_id": f"job-{self.jobs}", "worker_id": wid, "role": a["role"]}])
            elif a.kind == "post" and self.auto_post and a["post_kind"] not in self.hold:
                self.ev("own_post_seen", ts=self.next_ts(), kind=a["post_kind"], text=a["text"])

    def finish(self, role: str, res: dict | None = None, job_status="finished", code=None):
        jid = self.pending.pop(role)
        return self.ev("job_result", join_group=f"grp-{jid}", job_id=jid, worker_id=self.workers[role], role=role, attempt=1, job_status=job_status, result=res if res is not None else result(), code=code)

    def tick(self, seconds: float):
        self.now += seconds
        for tid, deadline in sorted(self.state.timers.items(), key=lambda kv: kv[1]):
            if deadline <= self.now: self.ev("timeout", timer_id=tid)

    def kinds(self, kind: str): return [a for a in self.actions if a.kind == kind]

    # -- canned paths ----------------------------------------------------------
    def to_claim(self):
        self.start()
        self.finish("explorer", result(report=EXPLORER_REPORT))
        return self.state

    def to_debate(self):
        self.to_claim()
        self.tick(self.cfg.settle_window)
        return self.state

    def to_implement(self, positions=(("disagree", "revised"), ("agree", "agree"))):
        self.to_debate()
        for pm, pp in positions[: self.cfg.max_debate_rounds]:
            self.finish("mathematician", result(report=report(position=pm)))
            self.finish("physicist", result(report=report(position=pp)))
            if self.state.stage != "Debate" or self.state.phase != "job": break
        return self.state

    def to_audit(self, pr=PR):
        self.to_implement()
        self.finish("implementer", result(artifacts=[pr]))
        return self.state

    def to_delivered(self, verdict="pass"):
        self.to_audit()
        self.finish("auditor", result(report=report(verdict=verdict)))
        return self.state

    def stages(self): return [r["stage"] for r in self.state.stage_log]

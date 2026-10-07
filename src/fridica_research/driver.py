"""The serve loop: poll the feed, translate feed events, timers and LLM results into machine
events, run `step`, persist the snapshot, execute the actions.

Every execution result becomes a machine event (`delegated`, `delegate_refused`, `llm_result`,
`post_refused`), so the machine learns about the world only through events and the snapshot
stays the fold of `step`. Restart is idempotent: the one `waiting` action is probed on
GET /threads/<id> by its `ref:` line and re-sent only when absent; in-flight LLM calls are re-run;
timers are re-armed from the absolute deadlines in the snapshot.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from typing import Callable

from . import briefs, contracts, machine
from .client import Client, ControlError, Unavailable
from .config import Config
from .machine import Action, Event, State
from .store import Store

log = logging.getLogger("fridica_research.driver")
LlmRunner = Callable[[str, str], dict]  # (schema name, prompt) -> payload


def claude_runner(model: str = "haiku", run=subprocess.run) -> LlmRunner:
    def call(name: str, prompt: str) -> dict:
        argv = ["claude", "-p", "--model", model, "--output-format", "json", "--json-schema", json.dumps(briefs.schema(name)), prompt]
        p = run(argv, capture_output=True, text=True, timeout=600)
        if p.returncode: raise RuntimeError(p.stderr.strip()[:300] or f"claude exited {p.returncode}")
        out = json.loads(p.stdout)
        payload = out.get("structured_output")
        if payload is None: payload = json.loads(out["result"])
        return payload
    return call


def thread_id(e: dict) -> str | None:
    root = e.get("thread")
    return f"{e.get('workspace', '')}:{e['channel']['id']}:{root}" if root and e.get("channel") else None


class Driver:
    def __init__(self, cfg: Config, client: Client, store: Store, llm: LlmRunner | None = None, board=None, clock=time.time, sleep=time.sleep, github=None):
        self.cfg, self.client, self.store, self.clock, self.sleep = cfg, client, store, clock, sleep
        self.llm = llm or claude_runner(cfg.llm_model)
        self.board = board
        self.github = github  # github.GitHub when `[github] enabled` (R21, R23, R24); None keeps Slack-only sign-offs
        self.recovered = False

    # -- translation --------------------------------------------------------------
    def translate(self, e: dict) -> list[tuple[str, Event]]:
        """A feed event -> machine events for the thread it concerns (empty when it is none of ours)."""
        now = self.clock()  # the driver's clock, not the daemon's record time: timers compare against it
        thread = thread_id(e)
        kind = e.get("kind")
        if not thread: return []
        state = self.store.load(thread)
        if kind in ("message", "peer_post"):
            meta_kind = e.get("turn_kind") or (e.get("meta") or {}).get("kind")
            own = e.get("sender") == self.cfg.owner
            text = e.get("text") or ""
            if state is None:
                if e.get("ts") != e.get("thread") or e["channel"]["id"] not in self.cfg.channels: return []
                if not (meta_kind == "study_root" or (own and self.cfg.starters == ()) or e.get("sender") in self.cfg.starters): return []
                root = contracts.parse_root(text)
                gen = root.generation if root.generation is not None else (1 if own else self.cfg.max_generations)
                out = [(thread, Event("started", now, {"channel": e["channel"]["id"], "problem": root.text, "generation": gen, "lineage": root.lineage or "", "spawner": own, "projected_hours": root.projected_hours or self.cfg.default_projected_hours}))]
                parent = self.parent_waiting_on(contracts.ref_of(text)) if own else None
                if parent: out.insert(0, (parent, Event("own_post_seen", now, {"ts": e["ts"], "kind": "study_root", "text": text, "child_thread": thread})))
                return out
            if meta_kind:
                if own: return [(thread, Event("own_post_seen", now, {"ts": e["ts"], "kind": meta_kind, "text": text}))]
                return [(thread, Event("peer_post", now, {"ts": e["ts"], "sender": e.get("sender", ""), "kind": meta_kind, "text": text}))]
            if own: return []
            so = contracts.parse_signoff(text)
            if so: return [(thread, Event("sign_off", now, {"sender": e.get("sender", ""), "pr": so.pr, "sha": so.sha, "verdict": so.verdict}))]
            login = contracts.parse_login_reply(text)
            if login and any(r.handle == e.get("sender") for r in self.cfg.reviewers): return [(thread, Event("login_reply", now, {"sender": e["sender"], "login": login}))]
            return []
        if state is None: return []
        if kind == "outbox":
            return [(thread, Event("post_refused", now, {"post_kind": e.get("post_kind"), "code": e.get("code"), "outcome": e.get("outcome"), "retry_after": e.get("retry_after")}))]
        if kind == "job_result":
            return [(thread, Event("job_result", now, e))]
        if kind == "job" and e.get("action") in ("finished", "failed", "interrupted"):
            return [(thread, Event("job_result", now, self.job_from_view(thread, e)))]
        if kind == "thread_control":
            control = {"paused": "paused", "closed": "closed", "archived": "archived"}.get(e.get("action"), "active" if e.get("action") in ("resumed", "restored") else None)
            return [(thread, Event("control_changed", now, {"control": control}))] if control else []
        return []

    def parent_waiting_on(self, ref: str | None) -> str | None:
        """A follow-on root echoes in the child thread; its `ref:` names the parent study's waiting post."""
        if not ref or "/g" not in ref: return None
        parent = self.store.load(ref.split("/g", 1)[0])
        return parent.thread if parent and parent.waiting and parent.waiting.get("id") == ref else None

    def job_from_view(self, thread: str, e: dict) -> dict:
        """Today's `job` event carries no result; join it with GET /threads/<id> jobs[]."""
        d = {"job_id": e["job_id"], "attempt": e.get("attempt", 1), "job_status": e.get("action"), "code": e.get("code"), "worker_id": e.get("worker_id", ""), "role": "", "join_group": ""}
        try:
            for j in self.client.thread(thread).jobs:
                if j.get("id") == e["job_id"]:
                    d.update(worker_id=j.get("worker_id", d["worker_id"]), role=j.get("role", ""), join_group=j.get("inbox_id") or "", result=j.get("result"))
        except ControlError as err:
            log.warning("thread view unavailable for %s: %s", thread, err)
        return d

    # -- one event through the machine ---------------------------------------------
    def apply(self, thread: str, ev: Event, cursor: int | None = None) -> State:
        state = self.store.load(thread)
        if ev.kind == "started":
            if state is not None: return state
            state, actions = machine.start(thread, ev["channel"], ev["problem"], ev.now, generation=ev["generation"], lineage=ev["lineage"], spawner=ev["spawner"], projected_hours=ev["projected_hours"], cfg=self.cfg)
        else:
            state, actions = machine.step(state, ev, self.cfg)
        self.store.save(state, cursor, self.clock())
        for a in actions: log.info("%s %s %s", thread, a.kind, a.id)
        for follow in [x for a in actions for x in self.execute(state, a)]:
            state = self.apply(thread, follow)
        return state

    def execute(self, state: State, a: Action) -> list[Event]:
        now = self.clock()
        try:
            if a.kind == "delegate":
                body = {k: v for k, v in a.data.items() if k not in ("thread", "action_id", "lens", "lens_sha256")}
                r = self.client.delegate(state.thread, body)
                jobs = r.get("jobs") or []
                return [Event("delegated", now, {"action_id": a.id, "join_group": r.get("join_group", ""), "jobs": jobs})]
            if a.kind == "post":
                self.client.post_message(state.thread, contracts.PostRequest(a["post_kind"], a["text"], a.get("details")))
            elif a.kind == "stop_worker":
                self.client.stop_worker(state.thread, a["worker_id"])
            elif a.kind == "set_driver":
                self.client.set_driver(state.thread, a["driver"])
            elif a.kind == "notify_owner":
                text = f"<@{self.cfg.owner}> {a['text']}\nref: {a.id}"
                self.client.post_message(state.thread, contracts.PostRequest("report", text))
            elif a.kind == "llm_call":
                return [self.run_llm(a.id, a["name"], a["prompt"])]
            elif a.kind == "board_update" and self.board is not None:
                return [Event("finding", now, {"text": finding}) for finding in self.board.sync(state) or []]
        except ControlError as e:
            log.warning("%s %s failed: %s", a.kind, a.id, e)
            if a.kind == "delegate": return [Event("delegate_refused", now, {"action_id": a.id, "code": e.code, "status": e.status, "role": a["role"]})]
            if a.kind == "post": return [Event("post_refused", now, {"action_id": a.id, "post_kind": a["post_kind"], "code": e.code, "outcome": "rejected"})]
        return []

    def run_llm(self, action_id: str, name: str, prompt: str) -> Event:
        try:
            return Event("llm_result", self.clock(), {"action_id": action_id, "ok": True, "payload": self.llm(name, prompt)})
        except Exception as e:  # noqa: BLE001 - any LLM failure is a retryable stage failure
            return Event("llm_result", self.clock(), {"action_id": action_id, "ok": False, "error": str(e)[:200]})

    # -- timers, commands, recovery ------------------------------------------------
    def fire_timers(self):
        now = self.clock()
        for s in self.store.all():
            for tid, deadline in sorted(s.timers.items(), key=lambda kv: kv[1]):
                if deadline <= now: self.apply(s.thread, Event("timeout", now, {"timer_id": tid}))

    def commands(self):
        for s in self.store.all():
            while (cmd := self.store.command(s.thread)) is not None:
                if cmd in ("stop", "resume"): self.apply(s.thread, Event(f"owner_{cmd}", self.clock()))
                elif cmd.startswith("note:"): self.apply(s.thread, Event("finding", self.clock(), {"text": cmd[5:]}))

    def recover(self):
        """Idempotent restart: confirm or re-send the one waiting action per study."""
        self.recovered = True
        for s in self.store.all():
            w = s.waiting
            if not w or s.stage in machine.TERMINAL: continue
            try: view = self.client.thread(s.thread)
            except ControlError as e:
                log.warning("cannot probe %s: %s", s.thread, e)
                continue
            if w["kind"] == "llm":
                name = {"brief": "study_brief", "synth": "study_synthesis", "deliver": "study_deliver"}[w["id"].rsplit("/", 1)[1]]
                prompt = {"study_brief": lambda: briefs.prompt_brief(s.problem, s.iteration, s.findings, s.peer_claims), "study_synthesis": lambda: briefs.prompt_synthesis(s.problem, s.approach(), s.explorer_report, s.reports), "study_deliver": lambda: briefs.prompt_deliver(s.problem, s.approach(), s.synthesis.get("synthesis", ""), s.implementer.get("summary", ""), s.audit.get("summary", ""), s.findings, s.partial)}[name]()
                self.apply(s.thread, self.run_llm(w["id"], name, prompt))
            elif w["kind"] in ("group", "slot"):
                known = {j.get("action_id") for j in s.group["jobs"].values()}
                for a in w["actions"]:
                    if a["action_id"] in known: continue
                    jobs = view.jobs_with_ref(a["action_id"])
                    if not jobs:
                        for ev in self.execute(s, Action("delegate", a["action_id"], a)): self.apply(s.thread, ev)
                        continue
                    self.apply(s.thread, Event("delegated", self.clock(), {"action_id": a["action_id"], "join_group": jobs[0].get("inbox_id") or "", "jobs": [{"job_id": j["id"], "worker_id": j.get("worker_id", ""), "role": j.get("role", "")} for j in jobs]}))
                    for j in jobs:
                        if j.get("job_status") in ("finished", "failed", "interrupted"):
                            self.apply(s.thread, Event("job_result", self.clock(), {"job_id": j["id"], "attempt": j.get("attempt", 1), "job_status": j["job_status"], "code": j.get("error"), "worker_id": j.get("worker_id", ""), "role": j.get("role", ""), "join_group": j.get("inbox_id") or "", "result": j.get("result")}))
            elif w["kind"] == "post":
                m = view.own_post_with_ref(w["id"], self.cfg.owner)
                if m: self.apply(s.thread, Event("own_post_seen", self.clock(), {"ts": m["ts"], "kind": w["action"]["post_kind"], "text": m["text"]}))
                else:
                    for ev in self.execute(s, Action("post", w["id"], w["action"])): self.apply(s.thread, ev)

    def poll_github(self):
        """R21/R24: each study's PR reviews become machine events; mirror and acknowledgement lines go to the thread as `report` posts."""
        if self.github is None: return
        for s in self.store.all():
            try: self.github.poll(s, self.clock(), lambda item, thread=s.thread: self.deliver_github(thread, item))
            except Exception as e:  # noqa: BLE001 - GitHub never stalls a stage; an undelivered item comes again on the next poll
                log.warning("github poll for %s failed: %s", s.thread, e)

    def deliver_github(self, thread: str, item):
        """One poll item: its posts, then its events; raising leaves it unseen, so the next poll delivers it again."""
        for suffix, text in item.posts: self.client.post_message(thread, contracts.PostRequest("report", f"{text}\nref: {thread}/github/{suffix}"))
        for ev in item.events: self.apply(thread, ev)

    # -- the loop ------------------------------------------------------------------
    def run_once(self) -> tuple[int, bool]:
        """Commands, one page of the feed, due timers. Returns (feed events applied, page reached the ledger's end)."""
        if not self.recovered: self.recover()
        self.commands()
        try: page = self.client.events(self.store.cursor, 1000)
        except Unavailable as e:
            log.warning("daemon unavailable: %s", e)
            return 0, True
        n = 0
        for e in page.get("events", []):
            cursor = int(e.get("cursor", page["next"]))
            for thread, ev in self.translate(e):
                self.apply(thread, ev, cursor)
                n += 1
        self.store.set_cursor(int(page["next"]))
        self.fire_timers()
        self.poll_github()
        return n, int(page.get("scanned", 0)) < 1000

    def serve(self, once: bool = False):
        while True:
            _, at_end = self.run_once()
            if once: return
            if at_end: self.sleep(self.cfg.idle_sleep)

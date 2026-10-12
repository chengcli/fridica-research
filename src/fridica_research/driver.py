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
import re
import subprocess
import time
from typing import Callable

from . import arbiter, briefs, contracts, deputy, github, ladder, machine, provenance, wave as wave_mod
from .client import Client, ControlError, Unavailable
from .config import Config
from .machine import Action, Event, State
from .store import Store

log = logging.getLogger("fridica_research.driver")
LlmRunner = Callable[[str, str], dict]  # (schema name, prompt) -> payload
ETA_FIELD = "ETA"  # the board text field a wave job's `dates["eta_text"]` goes to (#110); `Board.set_dates` takes dates only
NOT_FOUND = re.compile(r"could not resolve to a pullrequest|no pull requests? found|http 404", re.I)  # gh's answers for a PR number that is not a PR; never a bare "not found"
GH_FAILING_PASSES = 3  # consecutive wave polling passes with a failed GitHub read before the owner is told (#85)


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


def round_event(e: dict, now: float) -> Event | None:
    """A feed event as the arbiter sees it: every message of an adopted thread, typed by a person
    (no `turn_kind`) or posted by a crew member's agent (`turn_kind`, usually "reply")."""
    if e.get("kind") not in ("message", "peer_post"): return None
    return Event("crew_post", now, {"ts": e.get("ts", ""), "sender": e.get("sender", ""), "text": e.get("text") or "",
                                    "post_kind": e.get("turn_kind") or (e.get("meta") or {}).get("kind") or "", "files": int(e.get("files") or 0)})


class Driver:
    def __init__(self, cfg: Config, client: Client, store: Store, llm: LlmRunner | None = None, board=None, clock=time.time, sleep=time.sleep, github=None):
        self.cfg, self.client, self.store, self.clock, self.sleep = cfg, client, store, clock, sleep
        self.llm = llm or claude_runner(cfg.llm_model)
        self.board = board
        self.github = github  # github.GitHub when `[github] enabled` (R21, R23, R24); None keeps Slack-only sign-offs
        self.recovered = False
        self.producer = provenance.running_revision()
        self.last_poll: dict[tuple[str, int], float] = {}  # (wave thread, job) -> when its PR was last read (#85)
        self.gh_failures: dict[str, int] = {}  # wave thread -> consecutive passes with a failed read (#85); memory only, never the store
        self.round_polled: dict[str, float] = {}  # round thread -> when its PR was last read (#85)
        self.reviews_requested: set[tuple[str, int, str]] = set()  # (wave thread, job, head) whose auditor review was requested (#88)

    # -- translation --------------------------------------------------------------
    def translate(self, e: dict) -> list[tuple[str, Event]]:
        """A feed event -> machine events for the thread it concerns (empty when it is none of ours)."""
        now = self.clock()  # the driver's clock, not the daemon's record time: timers compare against it
        thread = thread_id(e)
        kind = e.get("kind")
        if not thread: return []
        if self.store.get_meta(wave_mod.wave_key(thread)) is not None: return []  # #69: a wave thread is the wave's
        if self.store.get_meta(arbiter.round_key(thread)) is not None: return []  # #46: an adopted thread is the arbiter's, never the study machine's
        state = self.store.load(thread)
        if kind in ("message", "peer_post"):
            meta_kind = e.get("turn_kind") or (e.get("meta") or {}).get("kind")
            own = e.get("sender") == self.cfg.owner
            text = e.get("text") or ""
            if state is None:
                if e.get("ts") != e.get("thread") or e["channel"]["id"] not in self.cfg.channels: return []
                if own and (contracts.ref_of(text) or "").startswith("fm-"): return []  # #79: a floor manager's root is never a study
                if not (meta_kind == "study_root" or (own and self.cfg.starters == ()) or e.get("sender") in self.cfg.starters): return []
                root = contracts.parse_root(text)
                gen = root.generation if root.generation is not None else (1 if own else self.cfg.max_generations)
                subject, bootstrap = root.subject, root.bootstrap
                if own and gen > 1:
                    bootstrap = {**(bootstrap or {}), "parent_revision": None}
                if own and subject and gen > 1:
                    parent_sha = self.store.get_meta(f"github:revision:{subject['repo']}:g{gen - 1}")
                    parent = provenance.identity({"repo": subject["repo"], "sha": parent_sha, "tree": None})
                    if parent:
                        subject = parent
                        bootstrap = {**(bootstrap or {}), "parent_revision": parent_sha}
                out = [(thread, Event("started", now, {"channel": e["channel"]["id"], "problem": root.text, "generation": gen, "lineage": root.lineage or "", "spawner": own, "projected_hours": root.projected_hours or self.cfg.default_projected_hours, "subject": subject, "bootstrap": bootstrap}))]
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
            state, actions = machine.start(thread, ev["channel"], ev["problem"], ev.now, generation=ev["generation"], lineage=ev["lineage"], spawner=ev["spawner"], projected_hours=ev["projected_hours"], cfg=self.cfg, producer=self.producer, subject=ev.get("subject"), bootstrap=ev.get("bootstrap"))
        else:
            ev = Event(ev.kind, ev.now, {**ev.data, "producer": self.producer})
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
                body = {k: v for k, v in a.data.items() if k not in ("thread", "action_id", "lens", "lens_sha256", "generation", "producer", "subject", "target", "bootstrap")}
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
                prompt = {"study_brief": lambda: briefs.prompt_brief(s.problem, s.iteration, s.findings, s.peer_claims), "study_synthesis": lambda: briefs.prompt_synthesis(s.problem, s.approach(), s.explorer_report, s.reports, s.evidence, iteration=s.iteration), "study_deliver": lambda: briefs.prompt_deliver(s.problem, s.approach(), s.synthesis.get("synthesis", ""), s.implementer.get("summary", ""), s.audit.get("summary", ""), s.findings, s.partial)}[name]()
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

    # -- adopted people-run rounds (#46, R47) --------------------------------------
    def load_round(self, thread: str) -> arbiter.Round | None:
        raw = self.store.get_meta(arbiter.round_key(thread))
        return arbiter.Round.from_json(raw) if raw else None

    def save_round(self, rnd: arbiter.Round): self.store.set_meta(arbiter.round_key(rnd.thread), rnd.to_json())

    def observe_round(self, e: dict) -> bool:
        """A feed event in an adopted thread: the arbiter's fold, then its actions. True when the thread is adopted.

        No study is ever started here, so an adopted thread has no delegation, no LLM call and no timer."""
        thread = thread_id(e)
        rnd = self.load_round(thread) if thread else None
        if rnd is None: return False
        ev = round_event(e, self.clock())
        if ev is None: return True
        rnd, actions = arbiter.observe(rnd, ev, self.cfg)
        self.save_round(rnd)  # the intent is durable before anything is sent: `pending` carries what must reach the thread
        self.drain_pending(rnd)
        for a in actions:
            if a.kind in arbiter.DURABLE: continue  # sent by drain_pending, which clears it only once the daemon took it
            self.execute_round(rnd, a)
        return True

    def flush_rounds(self):
        """Re-emit what an adopted round still has pending. Called on every pass, so a restart, a crash between
        the round's save and the send, and a post the daemon refused are all retried rather than lost (A5)."""
        for thread in self.store.rounds():
            rnd = self.load_round(thread)
            if rnd is not None and rnd.pending: self.drain_pending(rnd)

    def drain_pending(self, rnd: arbiter.Round):
        """Send the round's pending actions; an action leaves `pending` only when the send succeeded.

        A refused action stays for the next pass. A hard failure propagates without saving, so nothing is
        dropped. The `ref:` probe in `execute_round` keeps a post that did go out from being sent twice."""
        kept = [d for d in rnd.pending if self.execute_round(rnd, arbiter.action_from_dict(d))]
        if kept != rnd.pending:
            rnd.pending = kept
            self.save_round(rnd)

    def execute_round(self, rnd: arbiter.Round, a: Action, thread: str | None = None) -> str | None:
        """Execute one arbiter action through the existing primitives; posts go to `thread` (a wave's) when given.

        Returns None when the action is done with (sent, or already in the thread by its `ref:`), and the
        refusal when the daemon would not take it, which keeps a durable action pending for the next pass."""
        if a.kind not in arbiter.ACTION_KINDS: raise ValueError(f"the arbiter may not emit {a.kind}")
        where = thread or rnd.thread
        log.info("%s %s %s", where, a.kind, a.id)
        try:
            if a.kind in ("post", "notify_owner"):
                seen = self.posted_with_ref(where, a.id)
                if seen is None: return f"{a.kind} {a.id} skipped: the ref probe failed"  # #85: unknown, so no send this pass
                if seen: return None  # restart: the post is already in the thread
            if a.kind == "post":
                self.client.post_message(where, contracts.PostRequest(a["post_kind"], a["text"]))
            elif a.kind == "notify_owner":
                self.client.post_message(where, contracts.PostRequest("report", f"<@{self.cfg.owner}> {a['text']}\nref: {a.id}"))
            elif a.kind == "board_update":
                self.board_fields(a)
        except ControlError as e:
            log.warning("round %s %s failed: %s", a.kind, a.id, e)
            return f"{a.kind} {a.id} refused: {e}"
        return None

    # -- wave threads (#69 part b) -------------------------------------------------
    def waves(self) -> list[str]:
        """Threads holding an open or closed wave: `wave:<thread>` meta keys."""
        return [r[0].split(":", 1)[1] for r in self.store.db.execute("SELECT key FROM meta WHERE key LIKE 'wave:%' ORDER BY key")]

    def load_wave(self, thread: str) -> wave_mod.Wave | None:
        raw = self.store.get_meta(wave_mod.wave_key(thread))
        return wave_mod.Wave.from_json(raw) if raw else None

    def save_wave(self, w: wave_mod.Wave): self.store.set_meta(wave_mod.wave_key(w.thread), w.to_json())

    def observe_wave(self, e: dict) -> bool:
        """A feed event in a wave thread: `wave.observe`, then its actions. True when the thread holds a wave."""
        thread = thread_id(e)
        w = self.load_wave(thread) if thread else None
        if w is None: return False
        ev = round_event(e, self.clock())
        if ev is not None: self.wave_event(w, ev)
        self.note_reply(w, str(e.get("sender") or ""))
        return True

    def wave_event(self, w: wave_mod.Wave, ev: Event) -> wave_mod.Wave:
        """One event through the wave: saved before anything is sent, then the pending posts and the other actions."""
        w, actions = wave_mod.observe(w, ev, self.cfg)
        self.save_wave(w)
        self.drain_wave(w)
        for a in actions:
            if a.kind in arbiter.DURABLE or a.kind == "wave_close": continue  # durable ones are drained; the `waves` row is slice 4
            self.execute_round(arbiter.Round(thread=w.thread), a, thread=w.thread)
        return w

    def drain_wave(self, w: wave_mod.Wave):
        """Each job's pending posts, then the close, into the wave thread; a refused one stays for the next pass."""
        changed = False
        for j in w.jobs:
            pending = list(j.round.get("pending") or [])
            kept = [d for d in pending if self.execute_round(j.rnd(), arbiter.action_from_dict(d), thread=w.thread)]
            if kept != pending: j.round["pending"], changed = kept, True
        kept = [d for d in w.pending if self.execute_round(arbiter.Round(thread=w.thread), arbiter.action_from_dict(d), thread=w.thread)]
        if kept != w.pending: w.pending, changed = kept, True
        if changed: self.save_wave(w)

    def flush_waves(self):
        for thread in self.waves():
            w = self.load_wave(thread)
            if w is not None and (w.pending or any(j.round.get("pending") for j in w.jobs)): self.drain_wave(w)

    def poll_waves(self):
        """One `read_pr` per open job at most every `github.poll_interval` (#85); a merged or dropped job and a closed
        wave are never polled (#93). A PR read as MERGED is fed as a `merged` event; with no PR adopted, a MERGED PR
        that closes or refers to the issue and merged after the wave opened is too (#91). A pass with any failed read counts against the wave and a clean one
        resets the count; the third failing pass in a row puts one notify on `Wave.pending`. Each new head of an
        open PR gets the auditor's review requested (#88)."""
        run = self.gh_runner()
        if run is None: return
        for thread in self.waves():
            w = self.load_wave(thread)
            if w is None or w.closed_at is not None: continue
            reads, error = 0, None
            for n, j in enumerate(w.jobs, 1):
                rnd = j.rnd()
                if j.terminal or not rnd.repo or not rnd.issue: continue
                now = self.clock()
                if now < self.last_poll.get((thread, n), float("-inf")) + self.cfg.github.poll_interval: continue
                self.last_poll[(thread, n)], reads = now, reads + 1
                had = rnd.pr.get("candidate")
                try: ev = self.read_pr(rnd, run)
                except Exception as e:  # noqa: BLE001 - a GitHub read never stalls a wave; the next pass reads again
                    log.warning("pr read for wave %s job %d failed: %s", thread, n, e)
                    error = error or e
                    continue
                if had and "candidate" not in rnd.pr:  # read_pr dropped a not-found candidate; `rnd.pr` may be `w`'s own dict, so always save
                    (w.jobs[n - 1].round.get("pr") or {}).pop("candidate", None)  # `w` is the latest fold, not the wave this loop started from
                    self.save_wave(w)
                if ev is None: continue
                view = ev.get("view") or {}
                if view.get("state") == "MERGED":
                    ev = Event("merged", ev.now, {"job": n, "pr": rnd.pr["number"], "sha": str((view.get("mergeCommit") or {}).get("oid") or "")})
                elif "prs" in ev.data and arbiter.pick_pr(rnd, ev.get("prs") or [], self.cfg) is None:
                    try: ev = self.unwatched_merge(rnd, run, ev, n, w.opened_at)
                    except Exception as e:  # noqa: BLE001 - as a failed read: counted, and read again on the next pass
                        log.warning("issue PR lookup for wave %s job %d failed: %s", thread, n, e)
                        error = error or e
                        continue
                else: ev = Event(ev.kind, ev.now, {**ev.data, "job": n})
                w = self.wave_event(w, ev)
                self.request_review(w, n, run)
            if reads: self.count_gh_failure(w, error)

    # -- the R51 ladder for wave jobs (#83, #80, #92, #88) ---------------------------
    def ladder_key(self, thread: str, n: int) -> str: return f"ladder:{wave_mod.job_thread(thread, n)}"

    def ladder_meta(self, thread: str, n: int) -> dict:
        raw = self.store.get_meta(self.ladder_key(thread, n))
        return json.loads(raw) if raw else {}

    def save_ladder(self, thread: str, n: int, m: dict): self.store.set_meta(self.ladder_key(thread, n), json.dumps(m, sort_keys=True))

    @staticmethod
    def ladder_stage(j: wave_mod.Job) -> tuple[str, str, float | None, float | None, bool]:
        """(stage, holder, eta, start, deliverable): the implementer toward `pr_eta` until a PR is recorded, then the auditor toward `review_eta`."""
        if not (j.round.get("pr") or {}).get("number") and "pr" not in j.delivered:
            return "pr", j.person("implementer").get("slack", ""), j.pr_eta, j.input_at, False
        return "review", j.person("auditor").get("slack", ""), j.review_eta, j.delivered.get("pr", j.input_at), "review" in j.delivered

    def note_reply(self, w: wave_mod.Wave, sender: str):
        """`last_reply` on the driver clock for each open job the sender holds (the Slack ts is not compared with driver times)."""
        if not sender or sender == self.cfg.owner: return
        for n, j in enumerate(w.jobs, 1):
            m = self.ladder_meta(w.thread, n)
            if j.terminal or not m or m.get("key", [None, None])[1] != sender: continue
            m["last_reply"] = self.clock()
            self.save_ladder(w.thread, n, m)

    def tick_waves(self):
        """One ladder step per open job of each open wave, after `poll_waves`. The state lives in `ladder:<thread>:job<N>`,
        keyed on (stage, holder, eta); a new key resets `done` and `checkin_at`. Besides the ETA rungs: one deputy wake per
        `Round.held` post still held W after it (#92), and one nudge to the job's auditor when their Slack SIGN-OFF approve
        has no approving GitHub review on the head (#88). An action that fails stays out of `done` and is tried next pass."""
        now = self.clock()
        for thread in self.waves():
            w = self.load_wave(thread)
            if w is None or w.closed_at is not None: continue
            for n, j in enumerate(w.jobs, 1):
                if j.terminal: continue
                stage, holder, eta, start, deliverable = self.ladder_stage(j)
                if not holder or eta is None: continue
                m, key = self.ladder_meta(thread, n), [stage, holder, eta]
                if m.get("key") != key: m = {**m, "key": key, "done": [], "checkin_at": None}
                est = max(float(eta) - float(start if start is not None else eta), 0.0)
                v = ladder.JobView(ref=f"fm-wave-{w.name}-job{n}", holder=holder, owner=self.cfg.owner, est=est, eta=float(eta), deliverable=deliverable,
                                   checkin_at=m.get("checkin_at"), last_reply=m.get("last_reply"), done=frozenset(m.get("done") or ()), thread=thread)
                actions = ladder.step(v, now) + self.held_wakes(v, j, now) + self.review_nudge(v, j)
                for a in actions:
                    if a.id in m["done"] or self.execute_ladder(thread, a): continue
                    m["done"] = m["done"] + [a.id]
                    if a.id == f"{ladder.key(v)}/checkin": m["checkin_at"] = now  # #108 ids carry holder and ETA
                self.save_ladder(thread, n, m)

    @staticmethod
    def held_wakes(v: ladder.JobView, j: wave_mod.Job, now: float) -> list[Action]:
        """#92: a blocking finding is an entry in `Round.held`; W runs from the held post's ts, one wake per held post."""
        out = []
        for stage, h in sorted((j.round.get("held") or {}).items()):
            try: at = float(h.get("ts"))
            except (TypeError, ValueError): continue
            if now > at + ladder.window(v.est):
                out.append(ladder.wake(v, f"{v.ref}/held/{h.get('ts')}", f"{stage} is held since {h.get('ts')}: {str(h.get('text', ''))[:300]}", {"stage": stage}))
        return [a for a in out if a.id not in v.done]

    @staticmethod
    def review_nudge(v: ladder.JobView, j: wave_mod.Job) -> list[Action]:
        """#88: the job's auditor (and only them) signed off approve on the head in Slack, but GitHub has no approving review there."""
        rnd, aud = j.rnd(), j.person("auditor").get("slack", "")
        head = str(rnd.pr.get("head") or "")
        if not aud or not head or arbiter.approved(rnd): return []
        if not any(so.get("sender") == aud and so.get("verdict") == "approve" and head.startswith(str(so.get("sha") or "-")) for so in rnd.pr.get("signoffs") or []): return []
        ref = f"{v.ref}/gh-review/{head[:12]}"
        text = f"<@{aud}> your SIGN-OFF approve on PR #{rnd.pr.get('number')} is in Slack, but GitHub has no approving review at {head[:12]}. Please submit it on GitHub."
        return [] if ref in v.done else [Action("post", ref, {"thread": v.thread, "post_kind": ladder.POST_KIND, "to": aud, "text": f"{text}\nref: {ref}"})]

    def execute_ladder(self, thread: str, a: Action) -> str | None:
        """A ladder action: `wake_deputy` as an instruct with its client id; every other kind (`notify_owner` included,
        whose text already mentions the owner) as a plain post of its text, probed by its `ref:` first. None when done."""
        try:
            if a.kind == "wake_deputy":
                try: deputy.wake(self.client, a.get("thread") or thread, a.id, a["text"])
                except Exception as e:  # noqa: BLE001 - a wake that did not go out stays out of `done`: the next pass tries it again
                    log.warning("ladder wake %s failed: %s", a.id, e)
                    return f"wake_deputy {a.id} failed: {e}"
                return None
            seen = self.posted_with_ref(thread, a.id)
            if seen is None: return f"{a.kind} {a.id} skipped: the ref probe failed"
            if not seen: self.client.post_message(thread, contracts.PostRequest(a.get("post_kind") or ladder.POST_KIND, a["text"]))
        except ControlError as e:
            log.warning("ladder %s %s failed: %s", a.kind, a.id, e)
            return f"{a.kind} {a.id} refused: {e}"
        return None

    def unwatched_merge(self, rnd: arbiter.Round, run, ev: Event, n: int, opened_at: float | None) -> Event:
        """#91: adoption found nothing, so look for the job's PR that merged while nobody was watching: MERGED at or after
        the wave opened, with a Closes/Fixes/Resolves/Refs line for the issue; the lowest number wins. That PR is the job's
        `merged` event (sha empty: the issue listing carries none); otherwise the list as read. Without `opened_at` or a
        PR's `mergedAt` nothing can be bounded in time, so nothing matches."""
        def merged_since(p: dict) -> bool:
            if str(p.get("state", "")).upper() != "MERGED" or opened_at is None or not p.get("mergedAt"): return False
            try: return github.parse_iso(str(p["mergedAt"])) >= float(opened_at)
            except ValueError: return False
        ok = [p for p in github.list_prs_for_issue(run, rnd.repo, rnd.issue) if merged_since(p) and arbiter.closes_or_refs(p.get("body") or "", rnd.issue, rnd.repo)]
        if not ok: return Event(ev.kind, ev.now, {**ev.data, "job": n})
        p = min(ok, key=lambda p: int(p["number"]))
        return Event("merged", ev.now, {"job": n, "pr": int(p["number"]), "sha": str((p.get("mergeCommit") or {}).get("oid") or "")})

    def request_review(self, w: wave_mod.Wave, n: int, run):
        """#88: request the job's auditor on GitHub once per (job, head) of an open, non-draft PR, remembered in memory.
        A 422 is final for that head: one durable notify (`ref: fm-wave-<name>-job<n>-review-refused-<head12>`).
        Any other failure is logged and the request is tried again on the next poll."""
        j = w.jobs[n - 1]
        pr, login = j.round.get("pr") or {}, j.person("auditor").get("login", "")
        head = str(pr.get("head") or "")
        if j.terminal or not pr.get("number") or not head or not login or pr.get("draft") or str(pr.get("state", "OPEN")).upper() != "OPEN": return
        key = (w.thread, n, head)
        if key in self.reviews_requested: return
        try: github.request_reviewers(run, f"{j.rnd().repo}#{pr['number']}", [login])
        except github.ReviewRequestRefused as e:
            ref = f"fm-wave-{w.name}-job{n}-review-refused-{head[:12]}"
            text = f"job {n} (#{j.issue}): GitHub refused the review request for {login} on PR #{pr['number']} at {head[:12]}: {str(e)[:200]}"
            w.pending = w.pending + [Action("notify_owner", ref, {"thread": w.thread, "text": text}).to_dict()]
            self.save_wave(w)
            self.drain_wave(w)
        except Exception as e:  # noqa: BLE001 - not remembered, so the next poll requests again
            log.warning("review request for wave %s job %d failed: %s", w.thread, n, e)
            return
        self.reviews_requested.add(key)

    def count_gh_failure(self, w: wave_mod.Wave, error: Exception | None):
        """#85: consecutive failing passes per wave; at the third, one durable notify (`ref: fm-wave-<name>-gh-failing`)."""
        if error is None:
            self.gh_failures.pop(w.thread, None)
            return
        k = self.gh_failures[w.thread] = self.gh_failures.get(w.thread, 0) + 1
        if k != GH_FAILING_PASSES: return
        ref = f"fm-wave-{w.name}-gh-failing"
        text = f"GitHub reads failing for wave {w.name} ({k} passes in a row): {str(error)[:200]}"
        w.pending = w.pending + [Action("notify_owner", ref, {"thread": w.thread, "text": text}).to_dict()]
        self.save_wave(w)
        self.drain_wave(w)

    def gh_runner(self):
        """The gh runner a round's PR is read with: the GitHub one when `[github]` is on, else the enabled board's; None reads nothing."""
        if self.github is not None: return self.github.run
        return self.board.api.run if self.board is not None and self.cfg.board.enabled and getattr(self.board, "api", None) is not None else None

    def poll_rounds(self):
        """#46 slice 2: one PR read per round per `github.poll_interval` while the stage is Implement or CodeAudit -- `gh pr list`
        before adoption, then `gh pr view` plus the reviews call -- fed to the arbiter as a `pr_polled` event.
        A failed read is logged and retried on the next due poll (#85)."""
        run = self.gh_runner()
        if run is None: return
        for thread in self.store.rounds():
            rnd = self.load_round(thread)
            if rnd is None or rnd.stage not in arbiter.PR_STAGES or not rnd.repo or not rnd.issue: continue
            now = self.clock()
            if now < self.round_polled.get(thread, float("-inf")) + self.cfg.github.poll_interval: continue
            self.round_polled[thread] = now
            try: ev = self.read_pr(rnd, run)
            except Exception as e:  # noqa: BLE001 - a GitHub read never stalls a round; the next pass reads again
                log.warning("pr read for round %s failed: %s", thread, e)
                continue
            if ev is None: continue
            rnd, actions = arbiter.observe(rnd, ev, self.cfg)
            self.save_round(rnd)
            self.drain_pending(rnd)
            for a in actions:
                if a.kind not in arbiter.DURABLE: self.execute_round(rnd, a)

    def read_pr(self, rnd: arbiter.Round, run) -> Event | None:
        """`pr_polled` for the round. With no PR recorded: the candidate's view first (job 4's candidate may be another author's
        PR, which `list_prs --author` would miss); when it cannot be adopted (closed, another author, no Closes/Refs line) or its
        view fails, the implementer's own listing follows on the same pass, after the view, so the arbiter can still say why the
        candidate was not adopted. A not-found candidate is dropped from `rnd.pr`; the caller saves that."""
        now = self.clock()
        cand: list[dict] = []
        if not rnd.pr.get("number") and rnd.pr.get("candidate"):
            n = int(rnd.pr["candidate"])  # the adoption fields (`LIST_FIELDS`: number, author, body, ...): `view_pr` reads no body
            try:
                view = json.loads(run(["gh", "pr", "view", str(n), "-R", rnd.repo, "--json", github.LIST_FIELDS], None) or "{}")
                view.setdefault("number", n)
                cand = [view]
                if arbiter.pick_pr(rnd, cand, self.cfg) is not None: return Event("pr_polled", now, {"prs": cand})
            except Exception as e:  # noqa: BLE001 - a bad candidate never blocks adoption: the author listing below runs this pass
                gone = bool(NOT_FOUND.search(str(e)))
                log.warning("candidate PR #%s for %s %s: %s", n, rnd.thread, "not found, dropped" if gone else "view failed", e)
                if gone: rnd.pr.pop("candidate", None)
        if not rnd.pr.get("number"):
            me = rnd.login("implementer", self.cfg)
            if not me: return Event("pr_polled", now, {"prs": cand}) if cand else None  # whose PR is "own" is unknown: only the candidate
            seen = {int(p.get("number") or 0) for p in cand}
            return Event("pr_polled", now, {"prs": cand + [p for p in github.list_prs(run, rnd.repo, me) if int(p.get("number") or 0) not in seen]})
        pr = f"{rnd.repo}#{rnd.pr['number']}"
        return Event("pr_polled", now, {"view": github.view_pr(run, pr), "reviews": github.pr_reviews(run, pr)})

    def posted_with_ref(self, thread: str, ref: str) -> bool | None:
        """The `ref:` probe `recover` uses, applied before every arbiter post: re-feeding an event never posts twice.
        None when the probe failed: whether the post is there is unknown, so the caller skips the send (#85)."""
        try: return self.client.thread(thread).own_post_with_ref(ref, self.cfg.owner) is not None
        except ControlError as e:
            log.warning("cannot probe %s for %s: %s", thread, ref, e)
            return None

    def board_fields(self, a: Action):
        """R47.2 board writes: Status, Stage, Role, dates and the assignee, field by field; a wave job's `dates["eta_text"]` goes to the `ETA` text field (#110). Never `sync_study`, never `--body` (#49)."""
        if self.board is None or not self.cfg.board.enabled or not a.get("issue"): return
        api, n = self.board.api, int(a["issue"])
        writes = [*[(f, lambda f=f, o=o: api.set_option(n, f, o)) for f, o in (a.get("options") or {}).items()],
                  *[(f, lambda f=f, v=v: api.set_text(n, f, v)) for f, v in (a.get("texts") or {}).items()],
                  *([("dates", lambda: api.set_dates(n, **{k: v for k, v in a["dates"].items() if k != "eta_text"}))] if a.get("dates") else []),
                  *([("eta_text", lambda: api.set_text(n, ETA_FIELD, a["dates"]["eta_text"]))] if (a.get("dates") or {}).get("eta_text") else []),
                  *[("assignee", lambda login=login: api.edit_issue(n, "--remove-assignee", login)) for login in a.get("remove_assignees") or []],
                  *([("assignee", lambda: api.edit_issue(n, "--add-assignee", a["assignee"]))] if a.get("assignee") else [])]
        for name, write in writes:  # one field at a time: a field the project lacks never suppresses the others, and the board never stalls a round
            try: write()
            except Exception as e:  # noqa: BLE001 - as in Board.sync, a board failure is logged and retried on the next transition
                log.warning("board %s for %s failed: %s", name, a.get("thread"), e)

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
        self.flush_rounds()
        self.flush_waves()
        try: page = self.client.events(self.store.cursor, 1000)
        except Unavailable as e:
            log.warning("daemon unavailable: %s", e)
            self.poll_waves()  # #85: a Slack outage does not stop merge detection
            return 0, True
        n = 0
        events = page.get("events", [])
        results = {e.get("job_id") for e in events if e.get("kind") == "job_result"}
        for e in events:
            if e.get("kind") == "job" and e.get("action") == "failed" and e.get("job_id") in results: continue  # #45: the job_result beside it is the completion
            cursor = int(e.get("cursor", page["next"]))
            if self.observe_wave(e) or self.observe_round(e):
                n += 1
                continue
            for thread, ev in self.translate(e):
                self.apply(thread, ev, cursor)
                n += 1
        self.store.set_cursor(int(page["next"]))
        self.fire_timers()
        self.poll_rounds()
        self.poll_waves()
        self.tick_waves()
        self.poll_github()
        return n, int(page.get("scanned", 0)) < 1000

    def serve(self, once: bool = False):
        while True:
            _, at_end = self.run_once()
            if once: return
            if at_end: self.sleep(self.cfg.idle_sleep)

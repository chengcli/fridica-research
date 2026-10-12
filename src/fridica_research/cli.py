"""`fridica-research start | adopt | list | stop | resume | note | pr | serve | replay | score | wave` (argparse, stdlib only).
`wave` has open, status [--json], drop, add-job, bind-pr, eta and close."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict

from . import arbiter, config, contracts, ledger, provenance, wave
from .board import Board, Projects, role_totals
from .client import Client, read_capability
from .driver import Driver, claude_runner
from .github import GitHub, MilestoneMissing, PrHygieneError
from .replay import replay
from .roles import ROLES, LENSES
from .store import LockHeld, Store


def build_client(cfg: config.Config) -> Client:
    token = read_capability(cfg.capability_file) if cfg.capability_file else None
    return Client(cfg.socket_path, token)


def cmd_start(cfg: config.Config, args) -> int:
    hours = args.projected_hours if args.projected_hours is not None else cfg.default_projected_hours
    ref = f"start/{args.channel}/g1"
    subject = provenance.revision(args.repo, args.revision) if args.repo else None
    if args.repo and subject is None:
        print("input repository revision is unavailable", file=sys.stderr)
        return 1
    text = contracts.format_root(args.text, 1, None, hours, ref, tuple(r.handle for r in cfg.reviewers),
                                 producer=provenance.running_revision(), subject=subject, bootstrap=asdict(provenance.BootstrapPolicy()))
    client = build_client(cfg)
    r = client.post_root(args.channel, contracts.PostRequest("study_root", text))
    if args.issue is not None:
        store = Store(cfg.state_file)
        store.set_meta(f"issue:{args.channel}", str(args.issue))
        store.close()
    print(f"posted study root to {args.channel} (outbox {r.get('outbox_id', '?')}), projected {hours:g} h; the serving driver picks the thread up from the feed")
    return 0


def cmd_adopt(cfg: config.Config, args) -> int:
    """#46/R47: the driver adopts a people-run thread. No study is started on it and no AI worker is ever delegated for it."""
    store = Store(cfg.state_file)
    try:
        if store.load(args.thread) is not None:
            print(f"{args.thread} is an AI study; adopt is for people-run threads", file=sys.stderr)
            return 1
        if store.get_meta(arbiter.round_key(args.thread)) is not None:
            print(f"{args.thread} is already adopted", file=sys.stderr)
            return 1
        try:
            rnd, _ = arbiter.adopt(args.thread, issue=args.issue, repo=args.repo, roles=args.role, logins=args.login, delegate=args.delegate, stage=args.stage, now=time.time(), cfg=cfg)
        except arbiter.RosterError as e:
            print(str(e), file=sys.stderr)
            return 1
        store.set_meta(arbiter.round_key(rnd.thread), rnd.to_json())
        print(f"adopted {rnd.thread} at {rnd.stage} ({arbiter.ROUND_ROLE[rnd.stage]}); {arbiter.due_line(rnd.due)}; the serving driver observes the thread from the feed")
        return 0
    finally:
        store.close()


def cmd_list(cfg: config.Config, args) -> int:
    store = Store(cfg.state_file)
    board = Board(cfg, remember=store.set_meta, recall=store.get_meta) if args.board and cfg.board.enabled else None
    for s in store.all():
        print(f"{s.thread}\t{s.stage}/{s.phase}\titeration {s.iteration}\tgeneration {s.generation}\tapproach {(s.claim or {}).get('slug', '-')}\tattempt {s.attempt}")
        if args.board:
            for role, h in sorted(role_totals(s).items()): print(f"  {role:<14} projected {h['projected']:.2f} h  actual {h['actual']:.2f} h")
        if board:
            cards = json.loads(store.get_meta(f"board:{s.thread}") or "{}")
            for label, number in [("study", cards.get("issue")), *[(f"stage {i + 1}" + (f" {c['scope']}" if c.get("scope") else ""), c["issue"]) for i, st in enumerate(cards.get("stages", [])) for c in st.get("cards", [])]]:
                if number: print(f"  #{number} {label}: {board.api.verify(number)}")
    for thread in store.rounds():
        r = arbiter.Round.from_json(store.get_meta(arbiter.round_key(thread)) or "{}")
        holders = ", ".join(f"{role}={'+'.join(e.get('holders') or [])}" for role, e in sorted(r.roster.items()))
        print(f"{r.thread}\tround {r.stage}/{arbiter.ROUND_ROLE.get(r.stage, '?')}\t{arbiter.due_line(r.due)}\tissue {r.issue or '-'}\t{holders}")
    for thread, w in waves(store):
        print(f"{thread}\twave {w.name or '-'}\t{'closed ' + arbiter.et_time(w.closed_at) if w.closed_at else 'open'}\t{sum(j.merged for j in w.jobs)}/{len(w.jobs)} merged, "
              f"{sum(j.dropped for j in w.jobs)} dropped\t{len(w.findings)} findings")
    store.close()
    return 0


def waves(store: Store) -> list[tuple[str, wave.Wave]]:
    """Every `wave:<thread>` key, open or closed, with its wave."""
    rows = store.db.execute("SELECT key, value FROM meta WHERE key LIKE 'wave:%' ORDER BY key").fetchall()
    return [(k.split(":", 1)[1], wave.Wave.from_json(v)) for k, v in rows]


def cmd_command(cfg: config.Config, args, cmd: str) -> int:
    store = Store(cfg.state_file)
    if store.load(args.thread) is None:
        print(f"unknown study {args.thread}", file=sys.stderr)
        return 1
    store.command(args.thread, cmd)
    store.close()
    print(f"{cmd.split(':', 1)[0]} queued for {args.thread}; the serving driver applies it on its next pass")
    return 0


def cmd_serve(cfg: config.Config, args) -> int:
    """#93: one serve per state file (`<state>.lock` holds the pid), and the store is closed on every exit, refusal or not."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    store = Store(cfg.state_file)
    try:
        with store.lock():
            if not (cfg.github.enabled or cfg.board.enabled):  # #86: an open wave is fed only by GitHub polls; without them it never closes
                stuck = [t for t, w in waves(store) if w.closed_at is None]
                if stuck:
                    print(f"open wave(s) {', '.join(stuck)} but [github] and [board] are both off: no PR or merge would ever be seen; enable [github] or close the wave", file=sys.stderr)
                    return 1
            board = Board(cfg, remember=store.set_meta, recall=store.get_meta) if cfg.board.enabled else None
            github = GitHub(cfg, remember=store.set_meta, recall=store.get_meta) if cfg.github.enabled else None
            Driver(cfg, build_client(cfg), store, llm=claude_runner(cfg.llm_model), board=board, github=github).serve(once=args.once)
            return 0
    except LockHeld as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        store.close()


def cmd_pr(cfg: config.Config, args) -> int:
    """R23: open a study's PR with `Closes #N`, the thread, the board link and the milestone, or refuse."""
    store = Store(cfg.state_file)
    try:
        s = store.load(args.thread)
        if s is None:
            print(f"unknown study {args.thread}", file=sys.stderr)
            return 1
        closes = args.closes if args.closes is not None else json.loads(store.get_meta(f"board:{s.thread}") or "{}").get("issue")
        gh = GitHub(cfg, remember=store.set_meta, recall=store.get_meta)
        try:
            if args.milestone:  # the override: an existing milestone only, never created
                m = {"number": gh.milestone(args.repo, args.milestone, create=False), "title": args.milestone}
            else: m = gh.adopted_milestone(s.thread, args.repo) or {}
            url = gh.open_pr(args.repo, args.head, args.base, args.title, gh.next_pr_body(args.repo, args.summary, closes, s.thread, s.generation, m.get("title", "")), m.get("number"))
        except (PrHygieneError, MilestoneMissing) as e:
            print(str(e), file=sys.stderr)
            return 1
        print(url)
        return 0
    finally:
        store.close()


def cmd_wave(cfg: config.Config, args) -> int:
    """#69 part a: `wave open <thread> --job …` records a wave and its jobs (the floor manager's ETAs, never computed); `wave status <thread>` prints it.
    #93: `wave drop|add-job|bind-pr|eta|close` edit the stored wave; a drop or a close leaves the close post on `pending` for the driver to send.
    Every edit reads and writes the `wave:` blob inside one `Store.transaction()`, so a CLI edit never loses an update to the serving driver."""
    store = Store(cfg.state_file)
    try:
        key = wave.wave_key(args.thread)
        if args.wave_cmd == "status":
            raw = store.get_meta(key)
            if raw is None:
                print(f"{args.thread} is not an open wave", file=sys.stderr)
                return 1
            w = wave.Wave.from_json(raw)
            if args.json: print(json.dumps(w.to_dict(), sort_keys=True, indent=2))
            else:
                for line in wave.status_lines(w): print(line)
            return 0
        if args.wave_cmd != "open":
            with store.transaction():
                raw = store.get_meta(key)
                if raw is None:
                    print(f"{args.thread} is not an open wave", file=sys.stderr)
                    return 1
                w = wave.Wave.from_json(raw)
                now = time.time()
                try:
                    if args.wave_cmd == "drop": w, _ = wave.drop_job(w, args.job, args.reason, now=now)
                    elif args.wave_cmd == "add-job": w = wave.add_job(w, wave.parse_job(args.job), now=now, cfg=cfg)
                    elif args.wave_cmd == "bind-pr": w = wave.bind_pr(w, args.job, args.pr, head=args.head, now=now)
                    elif args.wave_cmd == "close": w = close_wave(w, args.reason, now=now)
                    else: w = wave.set_eta(w, args.job, pr_eta=args.pr_eta, review_eta=args.review_eta)
                except (wave.WaveError, ValueError) as e:
                    print(str(e), file=sys.stderr)
                    return 1
                store.set_meta(key, w.to_json())
            print(f"wave {w.name or '-'}: {args.wave_cmd} recorded" + ("; closed, the close post waits for the serving driver" if w.closed_at else ""))
            for line in wave.status_lines(w): print(line)
            return 0
        with store.transaction():
            if store.load(args.thread) is not None or store.get_meta(arbiter.round_key(args.thread)) is not None:
                print(f"{args.thread} is an AI study or an adopted round; wave open is for a wave thread", file=sys.stderr)
                return 1
            if store.get_meta(key) is not None:
                print(f"{args.thread} is already an open wave", file=sys.stderr)
                return 1
            if not str(args.repo or "").strip():
                print("wave open needs --repo owner/name: without it no PR or merge of the wave is ever polled (#86)", file=sys.stderr)
                return 1
            try:
                w = wave.open_wave(args.thread, name=args.name, repo=args.repo, jobs=[wave.parse_job(j) for j in args.job], now=time.time(), cfg=cfg)
            except (wave.WaveError, ValueError) as e:
                print(str(e), file=sys.stderr)
                return 1
            store.set_meta(key, w.to_json())
        print(f"opened wave {w.name or '-'} on {w.thread} with {len(w.jobs)} job(s); the ETAs are recorded as given (R49); the serving driver feeds the thread in slice 3")
        for line in wave.status_lines(w): print(line)
        return 0
    finally:
        store.close()


def close_wave(w: wave.Wave, reason: str, *, now: float) -> wave.Wave:
    """#93: the floor manager closes a wave by hand. Refused while any job is still open and on a wave already
    closed, so a close never pre-empts a job or writes a second R52 record; the reason is kept as a wave finding."""
    if not str(reason or "").strip(): raise wave.WaveError("close needs a reason")
    if w.closed_at is not None: raise wave.WaveError(f"wave {w.name or '-'} is already closed ({arbiter.et_time(w.closed_at)}); a wave closes once")
    still_open = [str(n) for n, j in enumerate(w.jobs, 1) if not j.terminal]
    if still_open:
        raise wave.WaveError(f"job(s) {', '.join(still_open)} of wave {w.name or '-'} are still open; merge or `wave drop` them before closing")
    w.findings.append(f"closed by hand: {str(reason).strip()}")
    wave.close(w, now)  # sets `closed_at` and puts the close post on `pending`; the serving driver sends it
    return w


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fridica-research", description="Drive fridica's auto-research studies over the control socket.")
    p.add_argument("--config", default=None, help=f"research.toml (default {config.DEFAULT_PATH})")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="post a study root in a research channel")
    s.add_argument("channel")
    s.add_argument("text")
    s.add_argument("--repo", help="local Git checkout of the study input (no fetch)")
    s.add_argument("--revision", default="HEAD", help="input revision in that checkout")
    s.add_argument("--projected-hours", type=float, default=None)
    s.add_argument("--issue", type=int, default=None, help="attach an existing GitHub issue as the study card")
    ad = sub.add_parser("adopt", help="adopt a people-run thread as a round the arbiter drives (#46, R47); the driver runs this, never the crew")
    ad.add_argument("thread", help="the thread id, workspace:channel:root_ts")
    ad.add_argument("--issue", type=int, default=None, help="the GitHub issue whose board fields the round moves")
    ad.add_argument("--repo", default="", help="owner/name of that issue")
    ad.add_argument("--role", action="append", default=[], help="role=U… (repeatable; several holders as role=U1,U2 for a person and their agent)")
    ad.add_argument("--login", action="append", default=[], help="role=github-login for the issue assignee (repeatable)")
    ad.add_argument("--delegate", action="append", default=[], help="opt a role in to an AI worker; refused in this slice, and always refused on a person-held role (R47.1)")
    ad.add_argument("--stage", default=arbiter.ORDER[0], choices=arbiter.ORDER, help="the stage the round is in now")
    ls = sub.add_parser("list", help="list studies and their stages")
    ls.add_argument("--board", action="store_true", help="also print projected/actual hours per role and read each card back from the project")
    sub.add_parser("roles", help="list packaged research roles and debater lenses")
    for name in ("stop", "resume"):
        c = sub.add_parser(name, help=f"{name} a study")
        c.add_argument("thread")
    n = sub.add_parser("note", help="record a requirement or design change as a finding for the current iteration (never injected into a running worker)")
    n.add_argument("thread")
    n.add_argument("text")
    pr = sub.add_parser("pr", help="open a study's pull request with Closes #N, the study thread, the board link and the milestone (R23)")
    pr.add_argument("thread")
    pr.add_argument("--repo", required=True, help="owner/name")
    pr.add_argument("--head", required=True)
    pr.add_argument("--base", default="main")
    pr.add_argument("--title", required=True)
    pr.add_argument("--summary", default="")
    pr.add_argument("--closes", type=int, default=None, help="the issue the PR closes (default: the study card)")
    pr.add_argument("--milestone", default=None, help="an existing milestone title (R<n>) overriding the adopted issue's or the generation's; never created")
    v = sub.add_parser("serve", help="run the driver loop")
    v.add_argument("--once", action="store_true", help="one pass over the feed, then exit")
    r = sub.add_parser("replay", help="fold a replay corpus (or every corpus under a directory) through the machine and compare (R19)")
    r.add_argument("path", help="a corpus directory or a parent such as tests/bootstrap")
    r.add_argument("--strict", action="store_true", help="textual differences (D1) fail too")
    r.add_argument("--accept-added-fields", action="store_true", help="fields the fold adds to actions or state (D3) pass")
    sc = sub.add_parser("score", help="print the job ledger's scorecards per Innie, role and crew (#53)")
    sc.add_argument("ledger", help="path of an existing ledger; a missing path is an error, never a new ledger")
    sc.add_argument("--tolerance", type=float, default=0.0, metavar="H", help="hours around the ETA that count as on time")
    sc.add_argument("--board", action="store_true", help="also write the table to the Project v2 readme")
    wv = sub.add_parser("wave", help="a wave thread holding N parallel jobs (#69, R53); the driver runs this, never the crew")
    wsub = wv.add_subparsers(dest="wave_cmd", required=True)
    wo = wsub.add_parser("open", help="record a wave and its jobs; the ETAs and assignments are the floor manager's (R49) and are recorded, never computed")
    wo.add_argument("thread", help="the wave thread id, workspace:channel:root_ts")
    wo.add_argument("--name", default="", help="the <w> of the thread's `ref: fm-wave-<w>-jobN` lines (e.g. d)")
    wo.add_argument("--repo", default="", help="owner/name of the jobs' issues and PRs (required: an empty repo is refused)")
    wo.add_argument("--job", action="append", default=[], required=True,
                    help="issue=N;files=a.py,b/;implementer=U…:login;auditor=U…:login;pr_eta=2026-10-12T02:20Z;review_eta=…[;input_at=…][;config_change=yes] (repeatable, one per job)")
    ws = wsub.add_parser("status", help="print the wave's jobs: stage, PR, ETAs, people, findings")
    ws.add_argument("thread")
    ws.add_argument("--json", action="store_true", help="the whole stored wave as JSON")
    wd = wsub.add_parser("drop", help="drop a job with a reason; terminal, and the wave closes when every job is merged or dropped")
    wd.add_argument("thread")
    wd.add_argument("job", type=int, help="the job number N of fm-wave-<w>-jobN")
    wd.add_argument("--reason", required=True)
    wa = wsub.add_parser("add-job", help="add one job under the opening's checks (ETAs, people, register, disjoint files, one Config job)")
    wa.add_argument("thread")
    wa.add_argument("--job", required=True, help="the same spec as wave open --job")
    wb = wsub.add_parser("bind-pr", help="record a job's PR by hand")
    wb.add_argument("thread")
    wb.add_argument("job", type=int)
    wb.add_argument("--pr", type=int, required=True)
    wb.add_argument("--head", default="", help="the PR's head sha, when known")
    we = wsub.add_parser("eta", help="re-estimate a job's ETAs; the first ETAs are kept and scored at close (R49)")
    we.add_argument("thread")
    we.add_argument("job", type=int)
    we.add_argument("--pr-eta", default=None, help="ISO 8601 or epoch seconds")
    we.add_argument("--review-eta", default=None)
    wc = wsub.add_parser("close", help="close a wave whose jobs are all merged or dropped; refused while a job is open or the wave is already closed (#93)")
    wc.add_argument("thread")
    wc.add_argument("--reason", required=True, help="why the wave is closed by hand; recorded as a wave finding")
    return p


def cmd_score(args) -> int:
    """#53: the ledger's scorecards per Innie, role and crew; `--board` writes them to the Project v2 readme."""
    try: db = ledger.open_existing(args.ledger)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    try: table = ledger.scorecard_table(ledger.scorecards(db, args.tolerance))
    finally: db.close()
    print(table)
    if args.board:
        cfg = config.load(args.config)
        if not (cfg.board.enabled and cfg.board.owner and cfg.board.number):
            print("--board needs [board] enabled with owner and number", file=sys.stderr)
            return 1
        Projects(cfg).set_readme(table)
        print(f"wrote the scorecards to the readme of project {cfg.board.owner}/{cfg.board.number}")
    return 0


def cmd_replay(args) -> int:
    report = replay(args.path, strict=args.strict, accept_added_fields=args.accept_added_fields)
    print(report.text())
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.cmd == "replay": return cmd_replay(args)
    if args.cmd == "score": return cmd_score(args)
    if args.cmd == "roles":
        print("driver and arbitrator (policy only; never delegated)")
        for role in ROLES:
            print(role + (" (lenses: " + ", ".join(LENSES) + ")" if role == "debater" else ""))
        return 0
    cfg = config.load(args.config)
    if args.cmd == "start": return cmd_start(cfg, args)
    if args.cmd == "adopt": return cmd_adopt(cfg, args)
    if args.cmd == "list": return cmd_list(cfg, args)
    if args.cmd in ("stop", "resume"): return cmd_command(cfg, args, args.cmd)
    if args.cmd == "note": return cmd_command(cfg, args, f"note:{args.text}")
    if args.cmd == "pr": return cmd_pr(cfg, args)
    if args.cmd == "wave": return cmd_wave(cfg, args)
    return cmd_serve(cfg, args)


if __name__ == "__main__":
    sys.exit(main())

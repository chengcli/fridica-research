"""`fridica-research start | list | stop | resume | serve | replay` (argparse, stdlib only)."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import config, contracts
from .board import Board, role_totals
from .client import Client, read_capability
from .driver import Driver, claude_runner
from .replay import replay
from .roles import ROLES, LENSES
from .store import Store


def build_client(cfg: config.Config) -> Client:
    token = read_capability(cfg.capability_file) if cfg.capability_file else None
    return Client(cfg.socket_path, token)


def cmd_start(cfg: config.Config, args) -> int:
    hours = args.projected_hours if args.projected_hours is not None else cfg.default_projected_hours
    ref = f"start/{args.channel}/g1"
    text = contracts.format_root(args.text, 1, None, hours, ref, tuple(r.handle for r in cfg.reviewers))
    client = build_client(cfg)
    r = client.post_root(args.channel, contracts.PostRequest("study_root", text))
    if args.issue is not None:
        store = Store(cfg.state_file)
        store.set_meta(f"issue:{args.channel}", str(args.issue))
        store.close()
    print(f"posted study root to {args.channel} (outbox {r.get('outbox_id', '?')}), projected {hours:g} h; the serving driver picks the thread up from the feed")
    return 0


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
    store.close()
    return 0


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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    store = Store(cfg.state_file)
    board = Board(cfg, remember=store.set_meta, recall=store.get_meta, issue_numbers=_issue_numbers(store)) if cfg.board.enabled else None
    Driver(cfg, build_client(cfg), store, llm=claude_runner(cfg.llm_model), board=board).serve(once=args.once)
    return 0


def _issue_numbers(store: Store) -> dict:
    rows = store.db.execute("SELECT key, value FROM meta WHERE key LIKE 'issue:%'").fetchall()
    return {k.split(":", 1)[1]: v for k, v in rows}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fridica-research", description="Drive fridica's auto-research studies over the control socket.")
    p.add_argument("--config", default=None, help=f"research.toml (default {config.DEFAULT_PATH})")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="post a study root in a research channel")
    s.add_argument("channel")
    s.add_argument("text")
    s.add_argument("--projected-hours", type=float, default=None)
    s.add_argument("--issue", type=int, default=None, help="attach an existing GitHub issue as the study card")
    ls = sub.add_parser("list", help="list studies and their stages")
    ls.add_argument("--board", action="store_true", help="also print projected/actual hours per role and read each card back from the project")
    sub.add_parser("roles", help="list packaged research roles and debater lenses")
    for name in ("stop", "resume"):
        c = sub.add_parser(name, help=f"{name} a study")
        c.add_argument("thread")
    n = sub.add_parser("note", help="record a requirement or design change as a finding for the current iteration (never injected into a running worker)")
    n.add_argument("thread")
    n.add_argument("text")
    v = sub.add_parser("serve", help="run the driver loop")
    v.add_argument("--once", action="store_true", help="one pass over the feed, then exit")
    r = sub.add_parser("replay", help="fold a replay corpus (or every corpus under a directory) through the machine and compare (R19)")
    r.add_argument("path", help="a corpus directory or a parent such as tests/bootstrap")
    r.add_argument("--strict", action="store_true", help="textual differences (D1) fail too")
    r.add_argument("--accept-added-fields", action="store_true", help="fields the fold adds to actions or state (D3) pass")
    return p


def cmd_replay(args) -> int:
    report = replay(args.path, strict=args.strict, accept_added_fields=args.accept_added_fields)
    print(report.text())
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.cmd == "replay": return cmd_replay(args)
    if args.cmd == "roles":
        for role in ROLES:
            print(role + (" (lenses: " + ", ".join(LENSES) + ")" if role == "debater" else ""))
        return 0
    cfg = config.load(args.config)
    if args.cmd == "start": return cmd_start(cfg, args)
    if args.cmd == "list": return cmd_list(cfg, args)
    if args.cmd in ("stop", "resume"): return cmd_command(cfg, args, args.cmd)
    if args.cmd == "note": return cmd_command(cfg, args, f"note:{args.text}")
    return cmd_serve(cfg, args)


if __name__ == "__main__":
    sys.exit(main())

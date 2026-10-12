"""Waking the deputy floor manager (#80, R54): the wake text and the instruct client id.

A judgment call the rules do not settle goes to the deputy (the owner's Innie) as an owner instruction through
`Client.instruct` on the job's thread. The client id is derived from the action's ref, so a retry or a restart
never queues the same wake-up twice. Not wired into the driver yet (Wave H).
"""
from __future__ import annotations

import hashlib

PREFIX = "wake-"


def client_id(ref: str) -> str:
    """`wake-` + the first 32 hex chars of sha256(ref): 37 chars of [a-z0-9-], inside fridica's 8-80 limit."""
    return PREFIX + hashlib.sha256(ref.encode()).hexdigest()[:32]


def wake_text(ref: str, question: str, signals: dict | None = None, options: list[str] | None = None) -> str:
    """The instruction body: the question, the observed signals, the options the rules allow, and the ref line."""
    lines = [f"Deputy judgment call (R54) on {ref.split('/')[0]}: {question}".rstrip()]
    if signals: lines += ["Signals:"] + [f"- {k}: {v}" for k, v in signals.items()]
    if options: lines += ["Options the rules allow:"] + [f"- {o}" for o in options]
    lines.append(f"ref: {ref}")
    return "\n".join(lines)


def wake(client, thread: str, ref: str, text: str) -> dict:
    """Queue the wake-up on `thread`; the same ref always gives the same client id."""
    return client.instruct(thread, text, client_id(ref))

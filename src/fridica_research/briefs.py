"""Brief templates and LLM prompts. Pure string builders; every brief and post carries a `ref:` line.

Role prose lives in this package's `roles/` catalog (R5); these templates only say what
the stage needs from the role and in which format. `fit` keeps every brief under fridica-core's
40 000-character brief cap and every post under Slack's 40 000 characters (64 KiB body cap), by
dropping the oldest findings first and then truncating the longest section.
"""
from __future__ import annotations

import json
from importlib import resources

from . import contracts

ROLES = {"explore": "explorer", "debate": "debater", "implement": "implementer", "audit": "auditor"}
CHARTERS = (
    "fridica-core: general mechanism only; no names, enums, defaults or prompt text specific to one host.",
    "fridica-store-sqlite: storage contract only.",
    "fridica: daemon, Slack, placement, egress; no research protocol.",
    "fridica-research: research policy (roles, stages, stance, trees, rewards).",
)


def schema(name: str) -> dict:
    return json.loads(resources.files("fridica_research").joinpath(f"schemas/{name}.json").read_text())


def fit(sections: list[tuple[str, str]], findings: list[str], limit: int = contracts.BRIEF_LIMIT) -> str:
    """Compose `## heading` sections plus a findings section; shrink to `limit` characters."""
    def render(fs: list[str]) -> str:
        parts = [f"## {h}\n{b.strip()}" for h, b in sections if b and b.strip()]
        if fs: parts.append("## Findings from earlier iterations\n" + "\n".join(f"- {f}" for f in fs))
        return "\n\n".join(parts) + "\n"
    fs = list(findings)
    text = render(fs)
    while len(text) > limit and fs:
        fs.pop(0)
        text = render(fs)
    if len(text) > limit:
        cut = "\n\n[truncated to fit the brief cap]\n"
        text = text[: limit - len(cut)] + cut
    return text


def explorer(ref: str, problem: str, brief: str, questions: list[str], findings: list[str], peer_claims: dict[str, str]) -> str:
    peers = "\n".join(f"- {s}: claimed by a peer; do not propose it under another name" for s in sorted(peer_claims)) or "none"
    return fit([
        ("Study", problem), ("Brief", brief), ("Questions", "\n".join(f"- {q}" for q in questions)),
        ("Approaches already claimed by peers", peers),
        ("Output format", "End the report with a `## Approaches` section: one line per viable approach, `- <slug>: <title> -- <why>`, slug `^[a-z0-9][a-z0-9-]{0,47}$`, most promising first."),
        ("Reference", f"ref: {ref}"),
    ], findings)


def debate(ref: str, role: str, other: str, problem: str, approach: contracts.Approach, explorer_report: str, prior: dict[str, str], round_: int, findings: list[str]) -> str:
    sections = [("Study", problem), ("Approach under study", f"{approach.slug}: {approach.title}\n{approach.why}"), ("Explorer report", explorer_report)]
    if round_ > 1:
        sections.append((f"Round {round_}: the {other}'s report from the previous round", prior.get(other, "(none)")))
        sections.append(("Your previous report", prior.get(role, "(none)")))
        sections.append(("Task", f"Rebut or revise. Address the {other}'s points directly; keep what survives."))
    else:
        sections.append(("Task", f"Analyse the approach from the {role}'s side. The {other} works in parallel; you will see each other's reports next round."))
    sections.append(("Output format", "End the report with a `## Stance` block:\nposition: agree|disagree|revised\nnotes: one line per point that decides your position"))
    sections.append(("Reference", f"ref: {ref}"))
    return fit(sections, findings)


def implementer(ref: str, problem: str, approach: contracts.Approach, synthesis: str, decisions: list[str], open_questions: list[str], findings: list[str], superseded: bool) -> str:
    sections = [("Study", problem), ("Approach", f"{approach.slug}: {approach.title}"), ("Synthesis", synthesis), ("Decisions", "\n".join(f"- {d}" for d in decisions)), ("Open questions", "\n".join(f"- {q}" for q in open_questions))]
    if superseded: sections.append(("Note", "This brief supersedes the earlier iteration's brief; the findings below say what the auditor returned."))
    sections.append(("Output", "Report the implementation summary, validation performed, known limitations and `machine_state` (branch, commit). Status `partial` is acceptable."))
    sections.append(("Reference", f"ref: {ref}"))
    return fit(sections, findings)


def auditor(ref: str, problem: str, synthesis: str, implementer_summary: str, machine_state: dict | None, findings: list[str], scopes: tuple[str, ...] = ()) -> str:
    ms = json.dumps(machine_state or {}, sort_keys=True)
    scope = ", ".join(scopes) or "scope"
    return fit([
        ("Study", problem), ("Synthesis the implementer followed", synthesis), ("Implementer summary", implementer_summary),
        ("Checkout", f"machine_state: {ms}\nIf the checkout is not reachable from your workspace, audit the text and say so."),
        ("Layer charters", "\n".join(CHARTERS)),
        ("Task", f"Audit scope(s): {scope}. Peers audit the other scopes; do not duplicate them. Does the delivered work solve the study within scope, and does it meet the findings noted below? Report findings as the role prescribes."),
        ("Output format", "End the report with a `## Stance` block:\nverdict: pass|return|reject\nnotes: one line per finding that decides the verdict"),
        ("Reference", f"ref: {ref}"),
    ], findings)


def audit_request(ref: str, iteration: int, pr: str, sha: str, reviewers: list[tuple[str, str]], asks: list[str]) -> str:
    """The R6 post: PR link, exact head SHA, reviewers with focus, and the SIGN-OFF line format."""
    who = "\n".join(f"<@{h}> ({f})" if f else f"<@{h}>" for h, f in reviewers) or "(no peer reviewers configured)"
    ask = "".join(f"\n<@{h}> please reply with your GitHub login" for h in asks)
    return "\n".join([contracts.stage_line("Audit", iteration), f"pr: {pr or 'none'}", f"sha: {sha or 'none'}", "reviewers:", who, f"Reply `SIGN-OFF {pr or '<pr>'} {sha or '<sha>'} approve|changes` or accept/refute/needs contract decision items. No merge without a human." + ask, f"ref: {ref}"])


# --- LLM prompts (three calls per iteration) ---------------------------------

def prompt_brief(problem: str, iteration: int, findings: list[str], peer_claims: dict[str, str]) -> str:
    return fit([("Task", f"Write the explorer's brief for iteration {iteration} of this study. Return JSON per the schema."), ("Study", problem), ("Peer claims (approaches taken elsewhere)", "\n".join(sorted(peer_claims)) or "none")], findings)


def prompt_synthesis(problem: str, approach: contracts.Approach, explorer_report: str, reports: dict[str, dict]) -> str:
    rs = [(f"{role} report (stance: {r.get('stance') or 'none'})", r.get("report", "")) for role, r in sorted(reports.items())] or [("Debate", "no debate rounds; synthesise from the explorer report alone")]
    return fit([("Task", "Synthesise the design the implementer follows. Return JSON per the schema."), ("Study", problem), ("Approach", f"{approach.slug}: {approach.title}"), ("Explorer report", explorer_report), *rs], [])


def prompt_deliver(problem: str, approach: contracts.Approach, synthesis: str, implementer_summary: str, audit_summary: str, findings: list[str], partial: bool) -> str:
    return fit([("Task", f"Write the delivery message{' for a partial result' if partial else ''} and propose the follow-on problem (or null). Return JSON per the schema."), ("Study", problem), ("Approach", f"{approach.slug}: {approach.title}"), ("Synthesis", synthesis), ("Implementer summary", implementer_summary), ("Audit summary", audit_summary)], findings)


def guard_post(text: str, details: str | None) -> tuple[str, str | None]:
    """Keep a post under the Slack text cap and the control body cap; details are dropped first."""
    if len(text) > contracts.POST_LIMIT:
        text = text[: contracts.POST_LIMIT - 40] + "\n[truncated]"
    budget = contracts.BODY_LIMIT - 1024 - len(text.encode())
    if details and len(details.encode()) > budget:
        details = details.encode()[: max(budget, 0)].decode(errors="ignore") if budget > 0 else None
    return text, details

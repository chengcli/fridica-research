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
from .roles import instructions

ROLES = {"explore": "explorer", "debate": "debater", "implement": "implementer", "audit": "auditor"}
CHARTERS = (
    "fridica-core: general mechanism only; no names, enums, defaults or prompt text specific to one host.",
    "fridica-store-sqlite: storage contract only.",
    "fridica: daemon, Slack, placement, egress; no research protocol.",
    "fridica-research: research policy (roles, stages, stance, trees, rewards).",
)


class BriefOverflow(ValueError):
    """Mandatory brief content cannot fit the host cap; a stage may retry this failure."""


def schema(name: str) -> dict:
    return json.loads(resources.files("fridica_research").joinpath(f"schemas/{name}.json").read_text())


def fit(sections: list[tuple[str, str]], findings: list[str], limit: int = contracts.BRIEF_LIMIT) -> str:
    """Findings shrink oldest first, then longest input; mandatory sections and final ref survive."""
    protected = {"Role", "Task", "Output", "Output format", "Reference", "Consensus", "Audited consensus"}
    sections = list(sections)
    refs = [(h, b) for h, b in sections if h == "Reference"]
    sections = [(h, b) for h, b in sections if h != "Reference"]
    fs = list(findings)
    def render():
        parts = [f"## {h}\n{b.strip()}" for h, b in sections if b and b.strip()]
        if fs: parts.append("## Findings from earlier iterations\n" + "\n".join(f"- {f}" for f in fs))
        parts.extend(f"## {h}\n{b.strip()}" for h, b in refs)
        return "\n\n".join(parts) + "\n"
    text = render()
    while len(text) > limit and fs:
        fs.pop(0)
        text = render()
    while len(text) > limit:
        candidates = [(len(b), i) for i, (h, b) in enumerate(sections) if h not in protected and b]
        if not candidates: raise BriefOverflow("mandatory brief sections exceed the 40,000-character cap")
        _, i = max(candidates)
        h, body = sections[i]
        sections[i] = (h, body[:max(0, len(body) - (len(text) - limit))])
        text = render()
    return text


def explorer(ref: str, problem: str, brief: str, questions: list[str], findings: list[str], peer_claims: dict[str, str]) -> str:
    peers = "\n".join(f"- {s}: claimed by a peer; do not propose it under another name" for s in sorted(peer_claims)) or "none"
    return fit([
        ("Role", instructions("explorer")), ("Study", problem), ("Brief", brief), ("Questions", "\n".join(f"- {q}" for q in questions)),
        ("Approaches already claimed by peers", peers),
        ("Task", "Search literature and ecosystem prior art; propose sourced findings for debate, then viable approaches."),
        ("Output format", "Include `## Findings for debate`, one line per finding: `- F<n>: <claim> -- source: <URL|DOI|repo@sha:path:line> -- test: <check>`. End the report with a `## Approaches` section: one line per viable approach, `- <slug>: <title> -- <why>`, plain slug without backticks or bold markup matching `^[a-z0-9][a-z0-9-]{0,47}$`, most promising first."),
        ("Reference", f"ref: {ref}"),
    ], findings)


def debate(ref: str, role: str, others: list[str], problem: str, approach: contracts.Approach, explorer_report: str, prior: dict[str, str], round_: int, findings: list[str], lenses: dict[str, str] | None = None, evidence: list[dict] = (), design_return: str = "", *, iteration: int = 1) -> str:
    sections = [("Role", instructions("debater", role, lenses)), ("Study", problem), ("Approach under study", f"{approach.slug}: {approach.title}\n{approach.why}"), ("Explorer report", explorer_report)]
    if design_return: sections.append(("Design audit return", design_return))
    for entry in evidence:
        if entry.get("iteration") == iteration and entry.get("answer"): sections.append((f"Evidence answer (return {entry['design_returns']}, round {entry['round']}, {entry['lens']})", entry["answer"]))
    if round_ > 1:
        for other in others: sections.append((f"Round {round_}: the {other}'s report from the previous round", prior.get(other, "(none)")))
        sections.append(("Your previous report", prior.get(role, "(none)")))
        task = f"Rebut or revise. Address the other lenses ({', '.join(others)}) directly; keep what survives."
    else:
        task = f"Analyse the approach from the {role}'s side. The other lenses ({', '.join(others)}) work in parallel; you will see their reports next round."
    sections.extend([("Task", task), ("Output format", "Optionally include ## Lenses with ### <slug> and text per lens (2 to max_lenses, default 3; at most 4,000 chars), and at most one ## Evidence request with question:, source:, experiment:. End with ## Stance:\nposition: agree|disagree|revised\nnotes: one line per deciding point"), ("Reference", f"ref: {ref}")])
    return fit(sections, findings)


def consensus(payload: dict, ref: str) -> str:
    return "\n".join(["## Consensus", payload.get("synthesis", ""), "\n### Decisions", *[f"- {d}" for d in payload.get("decisions", [])], "\n### Open questions", *[f"- {q}" for q in payload.get("open_questions", [])], f"\nConsensus reference: {ref}"])


def implementer(ref: str, audited_consensus: str) -> str:
    return fit([("Role", instructions("implementer")), ("Audited consensus", audited_consensus), ("Task", "Implement exactly the audited consensus. Other thread material is a finding, not implementation input."), ("Output format", "Report validation, limitations and machine_state (branch, full commit, base). Include the PR URL in artifacts."), ("Reference", f"ref: {ref}")], [])


def design_auditor(ref: str, consensus_text: str) -> str:
    return fit([("Role", instructions("auditor")), ("Consensus", consensus_text), ("Layer charters", "\n".join(CHARTERS)), ("Task", "Design audit: reuse before build, layer boundaries and design violations. Audit only this consensus, before implementation."), ("Output format", "End with ## Stance:\nverdict: pass|return|reject\nnotes: deciding findings"), ("Reference", f"ref: {ref}")], [])


def evidence_explorer(ref: str, request: str) -> str:
    return fit([("Role", instructions("explorer")), ("Evidence request", request), ("Task", "Answer only the specific evidence request. Supply sources and experiment results in the thread; no new study exploration."), ("Output format", "Report the answer and sources, distinguishing executed checks from hypotheses."), ("Reference", f"ref: {ref}")], [])


def auditor(ref: str, audited_consensus: str, impl: dict, scopes: tuple[str, ...] = ()) -> str:
    ms = impl.get("machine_state") or {}
    scope = ", ".join(scopes) or "scope"
    diff = f"PR: {impl.get('pr') or 'unavailable'}\nDiff: {ms.get('base') or 'PR base'}..{impl.get('sha') or ms.get('commit') or 'head unavailable'}"
    return fit([("Role", instructions("auditor")), ("Audited consensus", audited_consensus), ("Diff to audit", diff), ("Task", f"Code audit, scope(s): {scope}. Read the PR base..head diff and compare against the audited consensus verbatim. Apply rules 7–14 and 21; peers audit other scopes. Report unavailable checkout/diff access."), ("Output format", "End with ## Stance:\nverdict: pass|return|reject\nnotes: deciding findings"), ("Reference", f"ref: {ref}")], [])


def audit_request(ref: str, iteration: int, pr: str, sha: str, reviewers: list[tuple[str, str]], asks: list[str]) -> str:
    """The R6 post: PR link, exact head SHA, reviewers with focus, and the SIGN-OFF line format."""
    who = "\n".join(f"<@{h}> ({f})" if f else f"<@{h}>" for h, f in reviewers) or "(no peer reviewers configured)"
    ask = "".join(f"\n<@{h}> please reply with your GitHub login" for h in asks)
    return "\n".join([contracts.stage_line("Audit", iteration), f"pr: {pr or 'none'}", f"sha: {sha or 'none'}", "reviewers:", who, f"Reply `SIGN-OFF {pr or '<pr>'} {sha or '<sha>'} approve|changes` or accept/refute/needs contract decision items. No merge without a human." + ask, f"ref: {ref}"])


# --- LLM prompts (three calls per iteration) ---------------------------------

def prompt_brief(problem: str, iteration: int, findings: list[str], peer_claims: dict[str, str]) -> str:
    return fit([("Task", f"Write the explorer's brief for iteration {iteration} of this study. Return JSON per the schema."), ("Study", problem), ("Peer claims (approaches taken elsewhere)", "\n".join(sorted(peer_claims)) or "none")], findings)


def prompt_synthesis(problem: str, approach: contracts.Approach, explorer_report: str, reports: dict[str, dict], evidence: list[dict] = (), *, iteration: int = 1) -> str:
    rs = [(f"{role} report (stance: {r.get('stance') or 'none'})", r.get("report", "")) for role, r in sorted(reports.items())] or [("Debate", "no debate rounds; synthesise from the explorer report alone")]
    answers = [(f"Evidence answer (return {e['design_returns']}, round {e['round']}, {e['lens']})", e["answer"]) for e in evidence if e.get("iteration") == iteration and e.get("answer")]
    return fit([("Task", "Produce the debater consensus block for design audit before implementation. Return JSON per the schema."), ("Study", problem), ("Approach", f"{approach.slug}: {approach.title}"), ("Explorer report", explorer_report), *rs, *answers], [])


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

"""Packaged research role policy."""

from importlib import resources

ROLES = ("explorer", "debater", "implementer", "auditor")
LENSES = ("mathematician", "physicist")


def instructions(role: str, lens: str | None = None, overlay: dict[str, str] | None = None) -> str:
    if role not in ROLES or (lens is not None and (role != "debater" or lens not in LENSES and lens not in (overlay or {}))):
        raise ValueError(f"unknown research role or lens: {role}/{lens}")
    root = resources.files(__package__)
    text = root.joinpath(f"{role}.md").read_text()
    if lens:
        text += "\n## Assigned lens\n\n" + (overlay[lens] if overlay and lens in overlay else root.joinpath(f"lenses/{lens}.md").read_text())
    return text


def default_lenses() -> dict[str, str]:
    root = resources.files(__package__)
    return {lens: root.joinpath(f"lenses/{lens}.md").read_text() for lens in LENSES}


ROLE_ROWS = (
    ("explorer", "Search literature and ecosystem prior art; findings for debate"),
    ("debater", "Debate through lenses, request evidence, produce consensus"),
    ("auditor", "Design audit before implementation; code audit against audited consensus"),
    ("implementer", "Implement the audited consensus only; retain revisions"),
    ("driver and arbitrator", "Holds no PR role; runs threads, assignments and rotation, hand-offs, board, ETAs, replay gate and merge on auditor approval under maintainer authority; arbitrates disagreements and unclear rules; escalates owner-only questions to the study owner"),
)


def roles_table(holders: dict[str, str] | None = None) -> str:
    holders = holders or {}
    return "\n".join(["| Role | Holder | Responsibility |", "|---|---|---|", *[
        f"| {role} | {holders.get(role, 'assigned per study')} | {scope} |" for role, scope in ROLE_ROWS]])


def holders(state, cfg) -> dict[str, str]:
    owner = cfg.login_of(cfg.owner, state.people) or cfg.owner or "unassigned"
    out = {role: owner for role, _ in ROLE_ROWS}
    for role in ("explorer", "implementer", "auditor"):
        if role in state.workers: out[role] = owner + " / " + state.workers[role]["worker_id"]
    out["debater"] = owner + " / " + ", ".join(sorted(state.lenses))
    if cfg.reviewers: out["auditor"] += "; code: " + ", ".join(cfg.login_of(r.handle, state.people) or r.handle for r in cfg.reviewers)
    return out

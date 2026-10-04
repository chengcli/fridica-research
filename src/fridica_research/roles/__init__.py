"""Packaged research role policy."""

from importlib import resources

ROLES = ("explorer", "debater", "implementer", "auditor")
LENSES = ("mathematician", "physicist")


def instructions(role: str, lens: str | None = None) -> str:
    if role not in ROLES or (lens is not None and (role != "debater" or lens not in LENSES)):
        raise ValueError(f"unknown research role or lens: {role}/{lens}")
    root = resources.files(__package__)
    text = root.joinpath(f"{role}.md").read_text()
    if lens:
        text += "\n## Assigned lens\n\n" + root.joinpath(f"lenses/{lens}.md").read_text()
    return text

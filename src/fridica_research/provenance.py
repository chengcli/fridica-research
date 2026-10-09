"""Study provenance: producer is the running driver revision; subject identifies
the input repository revision examined by this study, not an in-toto output subject.
Target is a committed output revision, or None until one exists. Bootstrap fields
describe intended policy only; activation and child-health execution belong to #29.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class BootstrapPolicy:
    self_host: bool = False
    parent_revision: str | None = None
    require_replay: bool = True
    require_external_audit: bool = True
    require_child_boot: bool = False


def policy(value: dict | None = None) -> dict:
    out = asdict(BootstrapPolicy())
    for key, val in (value or {}).items():
        if key == "parent_revision":
            if isinstance(val, str) and re.fullmatch(r"[0-9a-f]{40}", val): out[key] = val
        elif key in out and type(val) is bool: out[key] = val
    return out


def identity(value: dict | None) -> dict | None:
    if not isinstance(value, dict): return None
    repo, sha, tree = value.get("repo"), value.get("sha"), value.get("tree")
    if not isinstance(repo, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo): return None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha): return None
    if tree is not None and (not isinstance(tree, str) or not re.fullmatch(r"[0-9a-f]{40}", tree)): return None
    return {"repo": repo.lower(), "sha": sha, "tree": tree, **({"dirty": True} if value.get("dirty") else {})}


def revision(path: str | Path, ref: str = "HEAD") -> dict | None:
    """Resolve a local Git revision without fetching; never guess when unavailable."""
    def git(*args):
        return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.DEVNULL, text=True, timeout=10).strip()
    try:
        remote = git("remote", "get-url", "origin")
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?", remote)
        if not match: return None
        sha = git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
        tree = git("rev-parse", "--verify", "--end-of-options", sha + "^{tree}")
        dirty = ref == "HEAD" and bool(git("status", "--porcelain", "--untracked-files=all"))
        return identity({"repo": match[1], "sha": sha, "tree": tree, "dirty": dirty})
    except (OSError, subprocess.SubprocessError):
        return None


def running_revision() -> dict | None:
    return revision(Path(__file__).resolve().parents[2])


def lines(producer: dict | None, subject: dict | None, target: dict | None, generation: int) -> list[str]:
    return [f"generation: {generation}", *[f"{name}: {json.dumps(value, sort_keys=True, separators=(',', ':'))}" for name, value in (("producer", producer), ("subject", subject), ("target", target))]]

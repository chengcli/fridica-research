"""Pure path levels and read-only Git checks for candidate revisions (R20).

An optional ``[levels]`` TOML table is a plain mapping of path globs to levels 1..3.
Its entries augment the defaults; the highest matching level always wins.
Unmatched paths are level 3. No function here changes the repository.
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
from collections.abc import Mapping
from functools import lru_cache

DEFAULT_LEVELS: dict[str, int] = {
    "roles/**": 1,
    "skills/**": 1,
    "templates/**": 1,
    "docs/**": 1,
    "README.md": 1,
    "src/fridica_research/roles/**": 1,
    "src/fridica_research/skills/**": 1,
    "src/fridica_research/templates/**": 1,
    "protocols/**": 2,
    "src/fridica_research/protocols/**": 2,
    "src/fridica_research/machine.py": 2,
    "src/fridica_research/briefs.py": 2,
    "schemas/**": 2,
    "src/fridica_research/schemas/**": 2,
    "src/fridica_research/backend/**": 3,
    "src/fridica_research/driver.py": 3,
    "src/fridica_research/client.py": 3,
    "src/fridica_research/store.py": 3,
    "src/fridica_research/git.py": 3,
    "src/fridica_research/github.py": 3,
    "src/fridica_research/contracts.py": 3,
    "tests/**": 3,
    "pyproject.toml": 3,
    "*.lock": 3,
    "**/*.lock": 3,
    "requirements*.txt": 3,
    ".github/**": 3,
}


def _table(levels: Mapping[str, int] | None) -> dict[str, int]:
    if levels is None:
        return DEFAULT_LEVELS
    if not isinstance(levels, Mapping) or not levels:
        raise ValueError("levels must be a nonempty path-glob mapping")
    for pattern, level in levels.items():
        if not isinstance(pattern, str) or not pattern or pattern.startswith("/") or "\\" in pattern or "//" in pattern or any(part in ("", ".", "..") for part in pattern.split("/")):
            raise ValueError(f"invalid level path glob: {pattern!r}")
        if type(level) is not int or level not in (1, 2, 3):
            raise ValueError(f"invalid level for {pattern!r}: {level!r}")
    return {**DEFAULT_LEVELS, **levels}


def _matches(path: str, pattern: str) -> bool:
    parts, globs = tuple(path.split("/")), tuple(pattern.split("/"))

    @lru_cache(maxsize=None)
    def match(i: int, j: int) -> bool:
        if j == len(globs):
            return i == len(parts)
        if globs[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], globs[j]) and match(i + 1, j + 1)

    return match(0, 0)


def classify(path: str | os.PathLike[str], levels: Mapping[str, int] | None = None) -> int:
    """Return the highest matching level, or 3 for an unknown repository path."""
    table = _table(levels)
    name = os.fspath(path)
    if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError(f"invalid repository path: {name!r}")
    return max((level for glob, level in table.items() if _matches(name, glob)), default=3)


def _git(repo: str | os.PathLike[str], *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", os.fspath(repo), *args], stderr=subprocess.PIPE)


def require_clean(worktree: str | os.PathLike[str]) -> None:
    """Refuse tracked, staged, and untracked changes without modifying the worktree."""
    if _git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise ValueError("dirty candidate worktree")


def diff_level(repo: str | os.PathLike[str], base: str, head: str, levels: Mapping[str, int] | None = None) -> int:
    """Classify the base/head candidate diff from their merge-base; 0 means no changed paths.

    Both sides of a detected rename or copy count. A dirty checkout is refused even
    when the referenced commits themselves are clean.
    """
    table = _table(levels)
    require_clean(repo)
    ancestor = _git(repo, "merge-base", base, head).decode().strip()
    changed = _git(repo, "diff", "--name-status", "-M", "-z", ancestor, head, "--").split(b"\0")
    i, highest = 0, 0
    while i < len(changed) and changed[i]:
        status = changed[i].decode("ascii")
        count = 2 if status.startswith(("R", "C")) else 1
        if i + count >= len(changed):
            raise ValueError("malformed git name-status output")
        for raw_path in changed[i + 1:i + count + 1]:
            highest = max(highest, classify(os.fsdecode(raw_path), table))
        i += count + 1
    return highest

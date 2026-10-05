"""Level classification against real Git histories and worktrees."""
from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from fridica_research.levels import classify, diff_level, require_clean


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo: Path, name: str, content: str = "content\n") -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    git(repo, "add", "--", name)
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "README.md")
    return tmp_path


@pytest.mark.parametrize(("path", "level"), [
    ("roles/auditor.md", 1), ("skills/check/SKILL.md", 1),
    ("templates/study.md", 1), ("docs/protocol.md", 1),
    ("src/fridica_research/roles/lenses/physicist.md", 1),
    ("protocols/default.yaml", 2), ("src/fridica_research/machine.py", 2),
    ("src/fridica_research/briefs.py", 2),
    ("src/fridica_research/schemas/study_brief.json", 2),
    ("src/fridica_research/backend/bootstrap.py", 3),
    ("src/fridica_research/driver.py", 3), ("src/fridica_research/client.py", 3),
    ("src/fridica_research/store.py", 3), ("src/fridica_research/git.py", 3),
    ("pyproject.toml", 3), ("uv.lock", 3), (".github/workflows/ci.yml", 3),
    ("src/fridica_research/contracts.py", 3), ("tests/test_levels.py", 3),
    ("unclassified/file.txt", 3),
])
def test_default_levels(path: str, level: int):
    assert classify(path) == level


def test_plain_toml_levels_and_highest_match():
    levels = tomllib.loads('''[levels]\n"docs/**" = 1\n"docs/authority/**" = 3\n"protocols/**" = 2\n''')["levels"]
    assert classify("docs/guide.md", levels) == 1
    assert classify("docs/authority/rules.md", levels) == 3
    assert classify("protocols/default.yaml", levels) == 2
    assert classify("anything/else", levels) == 3
    assert classify("docs/authority/rules.md", {"docs/**": 3, "docs/authority/**": 1}) == 3


@pytest.mark.parametrize("table", [
    {}, {"docs/**": 0}, {"docs/**": 4}, {"docs/**": True},
    {"docs/**": "1"}, {"": 1}, {"/absolute/**": 1},
    {"../outside/**": 1}, {"docs\\**": 1}, {42: 1},
    ["docs/**"],
])
def test_bad_tables_are_rejected(table):
    with pytest.raises((TypeError, ValueError)):
        classify("docs/a.md", table)


def test_mixed_diff_and_empty_diff(repo: Path):
    base = git(repo, "rev-parse", "HEAD")
    assert diff_level(repo, base, base) == 0
    commit(repo, "roles/auditor.md")
    first = git(repo, "rev-parse", "HEAD")
    assert diff_level(repo, base, first) == 1
    commit(repo, "src/fridica_research/machine.py")
    second = git(repo, "rev-parse", "HEAD")
    assert diff_level(repo, base, second) == 2
    commit(repo, "tests/test_levels.py")
    assert diff_level(repo, base, "HEAD") == 3


def test_diff_level_uses_loaded_table_and_rejects_bad_table(repo: Path):
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "docs/authority/rule.md")
    levels = tomllib.loads('''[levels]\n"docs/authority/**" = 3\n''')["levels"]
    assert diff_level(repo, base, "HEAD") == 1
    assert diff_level(repo, base, "HEAD", levels) == 3
    with pytest.raises(ValueError, match="invalid level"):
        diff_level(repo, base, "HEAD", {"docs/**": 9})


@pytest.mark.parametrize(("old", "new"), [
    ("roles/old.md", "src/fridica_research/backend/new.md"),
    ("src/fridica_research/backend/old.md", "roles/new.md"),
])
def test_rename_counts_both_paths(repo: Path, old: str, new: str):
    commit(repo, old, "unchanged content\n")
    base = git(repo, "rev-parse", "HEAD")
    (repo / new).parent.mkdir(parents=True, exist_ok=True)
    git(repo, "mv", old, new)
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "rename")
    assert diff_level(repo, base, "HEAD") == 3


def test_merge_base_excludes_changes_made_only_on_advanced_base(repo: Path):
    ancestor = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-qc", "candidate")
    commit(repo, "roles/auditor.md")
    candidate = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    commit(repo, "src/fridica_research/backend/new.py")
    advanced_main = git(repo, "rev-parse", "HEAD")
    assert git(repo, "merge-base", advanced_main, candidate) == ancestor
    assert diff_level(repo, advanced_main, candidate) == 1


@pytest.mark.parametrize("kind", ["modified", "staged", "untracked"])
def test_require_clean_refuses_dirty_tree_without_mutation(repo: Path, kind: str):
    tracked = repo / "README.md"
    if kind == "modified":
        tracked.write_text("changed\n")
    elif kind == "staged":
        tracked.write_text("changed\n")
        git(repo, "add", "README.md")
    else:
        (repo / "new.txt").write_text("new\n")
    before = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"])
    with pytest.raises(ValueError, match="dirty"):
        require_clean(repo)
    with pytest.raises(ValueError, match="dirty"):
        diff_level(repo, "HEAD", "HEAD")
    after = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"])
    assert before == after


def test_require_clean_accepts_clean_tree(repo: Path):
    assert require_clean(repo) is None

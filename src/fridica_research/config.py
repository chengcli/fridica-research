"""research.toml: the driver's configuration (stdlib tomllib)."""
from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PATH = "~/.config/fridica-research/research.toml"
STAGES = ("explore", "claim", "debate", "implement", "audit", "deliver")
DEFAULT_PROJECTION = {"explore": 600, "claim": 120, "debate": 1200, "implement": 5400, "audit": 5400, "deliver": 600}
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def duration(value, default: float) -> float:
    """`"10m"`, `"2h"`, `90` (seconds) -> seconds."""
    if value is None: return default
    if isinstance(value, (int, float)): return float(value)
    m = _DURATION.match(str(value))
    if not m: raise ValueError(f"bad duration {value!r}")
    return float(m.group(1)) * _UNIT[m.group(2)]


@dataclass(frozen=True)
class Reviewer:
    """A peer auditor (R13): the Slack id, the GitHub login that owns the audit card, and the scope they take."""
    handle: str  # Slack user id (U…)
    focus: str = ""  # the audit scope this reviewer takes
    login: str = ""  # GitHub login; empty -> the driver asks in the thread (R8) and leaves the card unassigned


@dataclass(frozen=True)
class Board:
    enabled: bool = False
    owner: str = ""
    number: int = 0
    repo: str = ""  # owner/name of the study repository where cards are real issues
    token_env: str = "GH_TOKEN"
    owner_type: str = "user"  # user | organization


@dataclass(frozen=True)
class Bootstrap:
    """`[backend.bootstrap]`: the feed-journal backend (R15). One journal (one study lineage) per `journal_dir`."""
    journal_dir: str = "~/.local/state/fridica-research/journal"
    worktrees_dir: str = ""  # default <journal_dir>/worktrees
    worker: str = "claude"  # claude | codex
    models: dict[str, str] = field(default_factory=dict)  # role -> model (claude --model / codex --model)
    efforts: dict[str, str] = field(default_factory=dict)  # role -> effort level (claude --effort)
    max_budget_usd_per_job: float = 5.0  # claude --max-budget-usd
    max_cost_usd_per_study: float = 50.0  # ceiling on the sum of total_cost_usd over the journal; a delegate past it is refused; 0 = no ceiling (required with codex)
    subject_repo: str = ""  # git repository the workers check out; empty -> plain directories under worktrees_dir
    subject_revision: str = "HEAD"  # `git worktree add --detach <worktree> <revision>`
    workspace: str = "journal"  # the workspace id of journal threads (`<workspace>:<channel>:<root ts>`)
    permission_mode: str = "bypassPermissions"  # claude --permission-mode for an unattended worker in its worktree
    roles_dir: str = ""  # `<roles_dir>/<role>.md` is appended to the worker's system prompt when present (R5)
    retry_backoff: float = 30.0  # seconds before a re-delegate of a seen ActionId starts; doubles per retry, capped at 8x
    keep_env: tuple[str, ...] = ()  # more environment variables the worker keeps by name (beyond its CLI's credentials and the Bedrock/Vertex settings)


@dataclass(frozen=True)
class Backend:
    kind: str = "fridica"  # fridica | bootstrap
    bootstrap: Bootstrap = field(default_factory=Bootstrap)


@dataclass(frozen=True)
class Config:
    socket: str = "~/.local/state/fridica/control.sock"
    capability_file: str | None = None
    owner: str = ""  # the owner's Slack user id; own posts are `sender == owner`
    state_path: str = "~/.local/state/fridica-research/state.sqlite3"
    channels: tuple[str, ...] = ()
    starters: tuple[str, ...] = ()
    max_iterations: int = 3
    max_debate_rounds: int = 2
    auditor_backend: str = "other"
    auto_followon: bool = True
    max_generations: int = 5
    stage_timeout: float = 7200.0
    settle_window: float = 60.0
    idle_sleep: float = 2.0
    default_projected_hours: float = 4.0
    projection: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_PROJECTION))
    board: Board = field(default_factory=Board)
    reviewers: tuple[Reviewer, ...] = ()
    audit_scopes: tuple[str, ...] = ()  # all scopes; those no reviewer takes go to a local auditor worker
    require_signoffs: bool = True
    people: dict[str, str] = field(default_factory=dict)  # Slack user id -> GitHub login
    llm_model: str = "haiku"
    backend: Backend = field(default_factory=Backend)

    @property
    def socket_path(self) -> Path: return Path(os.path.expanduser(self.socket))

    def login_of(self, slack_id: str, learned: dict | None = None) -> str:
        """GitHub login for a Slack id: the reviewer entry, `[people]`, or a login learned in the thread."""
        for r in self.reviewers:
            if r.handle == slack_id and r.login: return r.login
        return self.people.get(slack_id) or (learned or {}).get(slack_id, "")

    def uncovered_scopes(self) -> tuple[str, ...]:
        """Audit scopes no peer reviewer takes: the local auditor's work (R13). No scopes at all -> one local audit."""
        taken = {r.focus for r in self.reviewers if r.focus}
        return tuple(s for s in self.audit_scopes if s not in taken) or (() if self.reviewers else ("scope",))
    @property
    def state_file(self) -> Path: return Path(os.path.expanduser(self.state_path))

    def to_dict(self) -> dict: return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        """Inverse of `to_dict` (the replay corpus stores the config as JSON)."""
        d = dict(d)
        d["board"] = Board(**d.get("board", {}))
        b = dict(d.get("backend") or {})
        bs = dict(b.get("bootstrap") or {})
        if "keep_env" in bs:
            if not isinstance(bs["keep_env"], (list, tuple)): raise ValueError("[backend.bootstrap] keep_env must be a list of environment variable names")
            bs["keep_env"] = tuple(bs["keep_env"])
        kind = str(b.get("kind", "fridica"))
        if kind not in ("fridica", "bootstrap"): raise ValueError(f"[backend] kind must be fridica or bootstrap, not {kind!r}")
        d["backend"] = Backend(kind, check_bootstrap(Bootstrap(**bs), d["board"].token_env))  # the replay path gets the same checks as `parse`
        d["reviewers"] = tuple(Reviewer(**r) for r in d.get("reviewers", ()))
        for k in ("channels", "starters", "audit_scopes"): d[k] = tuple(d.get(k, ()))
        return cls(**d)


def parse(text: str) -> Config:
    raw = tomllib.loads(text)
    f = raw.get("fridica", {})
    proj = dict(DEFAULT_PROJECTION)
    for k, v in raw.get("projection", {}).items():
        if k not in STAGES: raise ValueError(f"[projection] unknown stage {k}")
        proj[k] = duration(v, proj[k])
    b = raw.get("board", {})
    a = raw.get("audit", {})
    reviewers = tuple(_reviewer(r) for r in a.get("reviewers", []))
    scopes = tuple(a.get("scopes", [])) or tuple(dict.fromkeys(r.focus for r in reviewers if r.focus))
    return Config(
        socket=f.get("socket", Config.socket), capability_file=f.get("capability_file"), owner=str(f.get("owner", "")),
        state_path=raw.get("state_path", Config.state_path),
        channels=tuple(raw.get("channels", [])), starters=tuple(raw.get("starters", [])),
        max_iterations=int(raw.get("max_iterations", 3)), max_debate_rounds=int(raw.get("max_debate_rounds", 2)),
        auditor_backend=str(raw.get("auditor_backend", "other")), auto_followon=bool(raw.get("auto_followon", True)),
        max_generations=int(raw.get("max_generations", 5)), stage_timeout=duration(raw.get("stage_timeout"), 7200.0),
        settle_window=duration(raw.get("settle_window"), 60.0), idle_sleep=duration(raw.get("idle_sleep"), 2.0),
        default_projected_hours=float(raw.get("default_projected_hours", 4.0)), projection=proj,
        board=Board(bool(b.get("enabled", False)), str(b.get("owner", "")), int(b.get("number", 0)), str(b.get("repo", "")), str(b.get("token_env", "GH_TOKEN")), str(b.get("owner_type", "user"))),
        reviewers=reviewers, audit_scopes=scopes, require_signoffs=bool(a.get("require_signoffs", True)),
        people={str(k): str(v) for k, v in raw.get("people", {}).items()}, llm_model=str(raw.get("llm_model", "haiku")),
        backend=_backend(raw.get("backend", {}), str(b.get("token_env", "GH_TOKEN"))),
    )


def _backend(b: dict, token_env: str = "GH_TOKEN") -> Backend:
    kind = str(b.get("kind", "fridica"))
    if kind not in ("fridica", "bootstrap"): raise ValueError(f"[backend] kind must be fridica or bootstrap, not {kind!r}")
    bs = b.get("bootstrap", {})
    if str(bs.get("worker", "claude")) not in ("claude", "codex"): raise ValueError("[backend.bootstrap] worker must be claude or codex")
    if str(bs.get("worker", "claude")) == "codex" and "max_cost_usd_per_study" not in bs:
        raise ValueError(CODEX_CEILING)
    d = Bootstrap()
    budgets = {k: _budget(k, bs.get(k, getattr(d, k))) for k in ("max_budget_usd_per_job", "max_cost_usd_per_study")}
    if isinstance(bs.get("retry_backoff"), bool): raise ValueError("[backend.bootstrap] retry_backoff must be a duration, not a boolean")
    keep_env = bs.get("keep_env", [])
    if not isinstance(keep_env, list): raise ValueError("[backend.bootstrap] keep_env must be a list of environment variable names")
    return Backend(kind, check_bootstrap(Bootstrap(
        journal_dir=str(bs.get("journal_dir", d.journal_dir)), worktrees_dir=str(bs.get("worktrees_dir", "")), worker=str(bs.get("worker", "claude")),
        models={str(k): str(v) for k, v in bs.get("models", {}).items()}, efforts={str(k): str(v) for k, v in bs.get("efforts", {}).items()},
        max_budget_usd_per_job=budgets["max_budget_usd_per_job"], max_cost_usd_per_study=budgets["max_cost_usd_per_study"],
        subject_repo=str(bs.get("subject_repo", "")), subject_revision=str(bs.get("subject_revision", "HEAD")), workspace=str(bs.get("workspace", d.workspace)),
        permission_mode=str(bs.get("permission_mode", d.permission_mode)), roles_dir=str(bs.get("roles_dir", "")), retry_backoff=duration(bs.get("retry_backoff"), d.retry_backoff),
        keep_env=tuple(keep_env),
    ), token_env))


CODEX_CEILING = "[backend.bootstrap] worker = \"codex\" needs max_cost_usd_per_study = 0 (no ceiling), stated explicitly: codex reports no cost, so a ceiling would never trip; or use worker = \"claude\""
# Names `keep_env` may not bring back into a worker's environment: the board's token_env and these (the driver's own secrets).
KEEP_ENV_FORBIDDEN = re.compile(r"^(FRIDICA_.*|SLACK_.*|GH_TOKEN|GITHUB_TOKEN|.*_SECRET.*)$")


def _budget(k: str, v) -> float:
    """A budget in USD: a finite number >= 0 (nan, inf or a negative value would turn the ceiling check off silently; a boolean is not a number)."""
    if isinstance(v, bool): raise ValueError(f"[backend.bootstrap] {k} must be a number, not a boolean ({v})")
    try: f = float(v)
    except (TypeError, ValueError): raise ValueError(f"[backend.bootstrap] {k} must be a number, not {v!r}") from None
    if not math.isfinite(f) or f < 0: raise ValueError(f"[backend.bootstrap] {k} must be a finite number >= 0 (0 = no ceiling for max_cost_usd_per_study), not {f}")
    return f


def check_bootstrap(bs: Bootstrap, token_env: str) -> Bootstrap:
    """The `[backend.bootstrap]` rules, for `parse` and for `Config.from_dict` (the replay path) alike."""
    for k in ("max_budget_usd_per_job", "max_cost_usd_per_study"): _budget(k, getattr(bs, k))
    if isinstance(bs.retry_backoff, bool) or not isinstance(bs.retry_backoff, (int, float)) or not math.isfinite(bs.retry_backoff) or bs.retry_backoff < 0:
        raise ValueError(f"[backend.bootstrap] retry_backoff must be a finite duration >= 0, not {bs.retry_backoff!r}")
    if bs.worker not in ("claude", "codex"): raise ValueError("[backend.bootstrap] worker must be claude or codex")
    if bs.worker == "codex" and float(bs.max_cost_usd_per_study) != 0: raise ValueError(CODEX_CEILING)
    if float(bs.max_cost_usd_per_study) > 0 and float(bs.max_budget_usd_per_job) <= 0:  # the reservation and the charge of a costless failed job would be $0
        raise ValueError(f"[backend.bootstrap] max_budget_usd_per_job must be > 0 when max_cost_usd_per_study is set ({bs.max_cost_usd_per_study:g}), not {bs.max_budget_usd_per_job:g}")
    if not all(isinstance(k, str) for k in bs.keep_env): raise ValueError("[backend.bootstrap] keep_env must be a list of environment variable names")
    bad = [k for k in bs.keep_env if k == token_env or KEEP_ENV_FORBIDDEN.match(k)]
    if bad: raise ValueError(f"[backend.bootstrap] keep_env may not keep the board token or the driver's secrets: {', '.join(bad)}")
    return bs


def _reviewer(r) -> Reviewer:
    if not isinstance(r, dict): return Reviewer(str(r))
    return Reviewer(str(r.get("handle") or r.get("slack") or ""), str(r.get("scope") or r.get("focus") or ""), str(r.get("login", "")))


def load(path: str | Path | None = None) -> Config:
    p = Path(os.path.expanduser(str(path or DEFAULT_PATH)))
    return parse(p.read_text()) if p.exists() else Config()

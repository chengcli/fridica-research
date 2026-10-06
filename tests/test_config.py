"""research.toml parsing: defaults, durations, projections, board, audit reviewers, people."""
import pytest

from fridica_research import config

TOML = '''
channels = ["C0123456789"]
starters = ["U1"]
max_iterations = 2
max_debate_rounds = 1
auditor_backend = "codex"
auto_followon = false
max_generations = 3
stage_timeout = "90m"
settle_window = 30
default_projected_hours = 4

[fridica]
socket = "/tmp/x.sock"
capability_file = "~/cap"
owner = "UOWN"

[projection]
explore = "5m"
implement = "2h"

[board]
enabled = true
owner = "chengcli"
number = 8
repo = "chengcli/fridica-research"
token_env = "GH_BOARD"

[audit]
reviewers = [{handle = "U2", focus = "scope"}, "U3"]
require_signoffs = false

[people]
UOWN = "chengcli"
U2 = "peer"
'''


def test_defaults():
    c = config.parse("")
    assert c.max_iterations == 3 and c.max_debate_rounds == 2 and c.auditor_backend == "other" and c.auto_followon and c.max_generations == 5
    assert c.projection == {"explore": 600, "claim": 120, "debate": 1200, "design_audit": 1200, "evidence": 900, "implement": 5400, "audit": 5400, "deliver": 600}
    assert c.stage_timeout == 7200 and c.settle_window == 60 and c.idle_sleep == 2 and not c.board.enabled and c.require_signoffs


def test_full_file():
    c = config.parse(TOML)
    assert c.channels == ("C0123456789",) and c.starters == ("U1",) and c.max_iterations == 2 and c.max_debate_rounds == 1
    assert c.auditor_backend == "codex" and not c.auto_followon and c.max_generations == 3
    assert c.stage_timeout == 5400 and c.settle_window == 30 and c.default_projected_hours == 4
    assert c.socket == "/tmp/x.sock" and c.capability_file == "~/cap" and c.owner == "UOWN"
    assert c.projection["explore"] == 300 and c.projection["implement"] == 7200 and c.projection["claim"] == 120
    assert c.board.enabled and c.board.owner == "chengcli" and c.board.number == 8 and c.board.repo == "chengcli/fridica-research" and c.board.token_env == "GH_BOARD"
    assert [(r.handle, r.focus) for r in c.reviewers] == [("U2", "scope"), ("U3", "")] and not c.require_signoffs
    assert c.people == {"UOWN": "chengcli", "U2": "peer"}


@pytest.mark.parametrize("value,seconds", [("10m", 600), ("2h", 7200), ("1d", 86400), (90, 90), ("45s", 45), ("1.5h", 5400)])
def test_duration(value, seconds):
    assert config.duration(value, 0) == seconds


def test_bad_projection_stage():
    with pytest.raises(ValueError):
        config.parse("[projection]\nsynthesis = '1m'\n")


def test_load_missing_file_gives_defaults(tmp_path):
    assert config.load(tmp_path / "none.toml") == config.Config()


def test_reviewers_with_login_and_scope_and_uncovered_scopes():
    c = config.parse('[audit]\nreviewers = [{slack = "U1", login = "alice", scope = "numerics"}, {handle = "U2", focus = "api"}]\nscopes = ["numerics", "api", "docs"]\n[people]\nU2 = "bob"\n')
    assert [(r.handle, r.focus, r.login) for r in c.reviewers] == [("U1", "numerics", "alice"), ("U2", "api", "")]
    assert c.audit_scopes == ("numerics", "api", "docs") and c.uncovered_scopes() == ("docs",)
    assert c.login_of("U1") == "alice" and c.login_of("U2") == "bob" and c.login_of("U3", {"U3": "late"}) == "late" and c.login_of("U9") == ""
    assert config.parse('[audit]\nreviewers = ["U1"]\n').uncovered_scopes() == ()  # a peer with no scope takes the whole audit
    assert config.Config().uncovered_scopes() == ("scope",)  # no peers: one local audit


def test_max_lenses_respects_host_default_worker_limit():
    for value in (1, 4):
        with pytest.raises(ValueError, match="max_lenses"):
            config.parse(f"max_lenses = {value}")
    assert config.parse("max_lenses = 2").max_lenses == 2
    assert config.parse("max_lenses = 3").max_lenses == 3

import dataclasses
import subprocess

from fridica_research import machine
from support import World, result

DRIVER = {"repo": "o/driver", "sha": "a" * 40, "tree": "b" * 40}
INPUT = {"repo": "o/input", "sha": "c" * 40, "tree": "d" * 40}
OUTPUT = {"repo": "o/input", "sha": "e" * 40, "tree": "f" * 40}


def test_snapshot_ignores_unknown_keys_once_without_logging_values():
    state = machine.State("T", "C", "study")
    raw = state.to_dict() | {"future_a": {"private": "secret-value"}, "future_b": 3}
    loaded = machine.State.from_dict(raw)
    assert len(loaded.findings) == 1
    assert "future_a" in loaded.findings[0] and "future_b" in loaded.findings[0]
    assert "secret-value" not in str(loaded.to_dict())
    loaded.findings.append("changed")
    assert raw["findings"] == []
    assert machine.State.from_dict(loaded.to_dict()).findings == loaded.findings


def test_input_output_identity_and_action_snapshots_are_separate():
    w = World()
    w.start(producer=DRIVER, subject=INPUT)
    initial = w.actions[0].to_dict()
    assert w.state.target is None and initial["target"] is None
    assert initial["producer"] == DRIVER and initial["subject"] == INPUT
    assert initial["generation"] == 1
    w.finish("explorer", result(report="## Approaches\n- alpha: test"))
    w.tick(w.cfg.settle_window)
    for lane in ("mathematician", "physicist"):
        from support import report
        w.finish(lane, result(report=report(position="agree")))
    w.finish("auditor", result(report=report(verdict="pass")))
    w.finish("implementer", result(artifacts=["https://github.com/o/input/pull/1"],
        machine_state={"commit": OUTPUT["sha"], "tree": OUTPUT["tree"], "base": INPUT["sha"], "dirty": False}))
    assert w.state.producer == DRIVER and w.state.subject == INPUT and w.state.target == OUTPUT
    assert initial["target"] is None
    restored = machine.State.from_dict(w.state.to_dict())
    assert restored.producer == DRIVER and restored.subject == INPUT and restored.target == OUTPUT


def test_failed_or_uncommitted_result_never_becomes_output_revision():
    for status, commit, dirty in (("failed", "e" * 40, False), ("done", "abc1234", False), ("done", "e" * 40, True)):
        w = World()
        w.to_implement()
        w.finish("implementer", result(status=status, artifacts=["https://github.com/o/input/pull/1"],
            machine_state={"commit": commit, "dirty": dirty}))
        assert w.state.target is None


def test_checkout_identity_resolves_real_commit_and_reports_dirty_input(tmp_path):
    from fridica_research.provenance import revision
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("remote", "add", "origin", "git@github.com:o/input.git")
    (tmp_path / "data").write_text("input")
    git("add", "data")
    git("commit", "-qm", "input")
    sha, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    assert revision(tmp_path) == {"repo": "o/input", "sha": sha, "tree": tree}
    (tmp_path / "data").write_text("changed")
    assert revision(tmp_path)["dirty"] is True
    assert revision(tmp_path, "not-a-revision") is None


def test_bootstrap_metadata_does_not_start_activation_or_add_a_generation():
    from fridica_research.provenance import BootstrapPolicy
    policy = dataclasses.asdict(BootstrapPolicy(self_host=True, parent_revision="a" * 40))
    w = World()
    w.start(generation=2, bootstrap=policy)
    assert w.state.bootstrap == policy and w.state.generation == 2
    assert all("/g2/" in a.id for a in w.actions)
    assert not any(a.kind in ("promote", "reexec", "rollback", "child_boot") for a in w.actions)


def test_root_delivery_and_card_render_same_revision_metadata():
    from fridica_research import board, contracts
    text = contracts.format_root("Study", 1, None, 2, "start", producer=DRIVER, subject=INPUT)
    root = contracts.parse_root(text)
    assert root.subject == INPUT and root.text == "Study"
    w = World()
    w.start(producer=DRIVER, subject=INPUT)
    w.finish("explorer", result(report="## Approaches\n- alpha: test"))
    w.tick(w.cfg.settle_window)
    from support import report
    for lane in ("mathematician", "physicist"): w.finish(lane, result(report=report(position="agree")))
    w.finish("auditor", result(report=report(verdict="pass")))
    w.finish("implementer", result(artifacts=["https://github.com/o/input/pull/1"],
        machine_state={"commit": OUTPUT["sha"], "tree": OUTPUT["tree"], "dirty": False}))
    w.finish("auditor", result(report=report(verdict="pass")))
    post = next(a for a in w.kinds("post") if a["post_kind"] == "study_result")
    card = board.stage_table(w.state)
    for rendered in (post["text"], card):
        assert DRIVER["sha"] in rendered and INPUT["sha"] in rendered and OUTPUT["sha"] in rendered
        assert "generation: 1" in rendered


def test_partial_delivery_keeps_committed_target_after_audit_return():
    from support import CFG, report
    w = World(cfg=dataclasses.replace(CFG, max_iterations=1))
    w.to_implement()
    w.finish("implementer", result(artifacts=["https://github.com/o/input/pull/1"],
        machine_state={"commit": OUTPUT["sha"], "tree": OUTPUT["tree"], "dirty": False}))
    assert w.state.target == OUTPUT
    w.finish("auditor", result(report=report(verdict="return")))
    assert w.state.partial and w.state.target == OUTPUT
    post = next(a for a in w.kinds("post") if a["post_kind"] == "study_result")
    assert OUTPUT["sha"] in post["text"]


def test_followon_does_not_inherit_its_grandparent_revision():
    from fridica_research import contracts
    from support import CFG, report
    w = World(cfg=dataclasses.replace(CFG, max_generations=3))
    w.start(generation=2, subject=INPUT,
        bootstrap={"self_host": True, "parent_revision": "a" * 40})
    w.finish("explorer", result(report="## Approaches\n- alpha: test"))
    w.tick(w.cfg.settle_window)
    for lane in ("mathematician", "physicist"):
        w.finish(lane, result(report=report(position="agree")))
    w.finish("auditor", result(report=report(verdict="pass")))
    w.finish("implementer", result(artifacts=["https://github.com/o/input/pull/1"],
        machine_state={"commit": OUTPUT["sha"], "tree": OUTPUT["tree"], "dirty": False}))
    w.finish("auditor", result(report=report(verdict="pass")))
    post = next(a for a in w.kinds("post") if a["post_kind"] == "study_root")
    root = contracts.parse_root(post["text"])
    assert root.generation == 3 and root.subject == OUTPUT
    assert root.bootstrap["parent_revision"] is None
    assert root.bootstrap["self_host"] is True
    assert w.state.bootstrap["parent_revision"] == "a" * 40

"""CLI: start posts the root with projection/generation/lineage lines; list, stop, resume, serve --once."""
from __future__ import annotations

import os

from fridica_research import cli, contracts
from fridica_research.store import Store
from fake_control import FakeControl
from support import OWNER, REV


def write_config(tmp_path, sock):
    p = tmp_path / "research.toml"
    state = tmp_path / "s.sqlite3"
    p.write_text(f'channels = ["C1"]\ndefault_projected_hours = 3\nstate_path = "{state}"\n[fridica]\nsocket = "{sock}"\nowner = "{OWNER}"\n[audit]\nreviewers = ["{REV}"]\n')
    return str(p)


def test_help_and_subcommands():
    p = cli.parser()
    assert p.prog == "fridica-research"
    assert p.parse_args(["start", "C1", "text", "--projected-hours", "2", "--issue", "7"]).issue == 7
    assert p.parse_args(["serve", "--once"]).once


def test_roles_lists_packaged_catalog(capsys):
    assert cli.main(["roles"]) == 0
    assert "debater (lenses: mathematician, physicist)" in capsys.readouterr().out


def test_start_posts_root_and_list_stop_resume(tmp_path, sock_dir, capsys, monkeypatch):
    monkeypatch.setattr(cli, "claude_runner", lambda model: (lambda name, prompt: {"brief": "b", "questions": []}))
    sock = os.path.join(sock_dir, "c.sock")
    server = FakeControl(sock, owner=OWNER).start()
    try:
        cfgp = write_config(tmp_path, sock)
        assert cli.main(["--config", cfgp, "start", "C1", "Study X", "--projected-hours", "2.5", "--issue", "9"]) == 0
        method, path, body = server.requests[-1]
        assert (method, path) == ("POST", "/channels/C1/post") and body["meta"]["kind"] == "study_root"
        root = contracts.parse_root(body["text"])
        assert root.generation == 1 and root.lineage is None and root.projected_hours == 2.5 and root.text.endswith("Study X")
        assert f"<@{REV}>" in body["text"]
        assert "posted study root" in capsys.readouterr().out
        store = Store(tmp_path / "s.sqlite3")
        assert store.get_meta("issue:C1") == "9"
        store.close()
        assert cli.main(["--config", cfgp, "stop", "nope"]) == 1
        assert cli.main(["--config", cfgp, "list"]) == 0
        # serve --once picks the root up from the feed (cursor starts at the ledger's end, so push a root after).
        assert cli.main(["--config", cfgp, "serve", "--once"]) == 0
        thread = f"T1:C1:{server.next_ts()}"
        server.root(thread, OWNER, "Study Y\n\nprojected: 1 h\ngeneration: 1\nlineage: origin\nref: r")
        assert cli.main(["--config", cfgp, "serve", "--once"]) == 0
        assert cli.main(["--config", cfgp, "stop", thread]) == 0
        store = Store(tmp_path / "s.sqlite3")
        assert cli.main(["--config", cfgp, "note", thread, "R99: new rule"]) == 0
        assert store.load(thread).stage == "Explore" and store.get_meta(f"cmd:{thread}") == '["stop", "note:R99: new rule"]'
        store.close()
        out = capsys.readouterr().out
        assert "stop queued" in out and "note queued" in out
    finally:
        server.stop()

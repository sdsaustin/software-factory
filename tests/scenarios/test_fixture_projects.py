"""The projects the live scenarios start from, checked without Claude so a live run never fails on a bad fixture."""
import shlex

from helpers import FACTORY_TEMPLATES, TEMPLATES, plugin_version, read_json
from helpers.projects import OLD_BACKLOG_ITEMS, TEAM_NOTES, TEST_COMMAND, old_project, section, toy_project


def test_toy_project_is_ready_for_a_run(fx):
    repo = toy_project(fx)
    assert fx.git(repo, "status", "--porcelain") == "", "a run's preflight needs a clean tree"
    board = read_json(repo / "factory" / "board.json")
    assert board["plugin_version"] == plugin_version() and board["default_branch"] == "main"
    assert "- [ ] B1 " in (repo / "factory" / "backlog.md").read_text()
    assert f"`{TEST_COMMAND}`" in (repo / "CLAUDE.md").read_text()
    fx.run(shlex.split(TEST_COMMAND), cwd=repo)


def test_harness_gate_fails_once_outside_git_only(fx):
    sentinel = fx.root / "sentinel" / "verify-failed-once"
    repo = toy_project(fx, gate_sentinel=sentinel)
    fx.run(shlex.split(TEST_COMMAND), cwd=repo)  # inside the checkout: the builder's runs pass
    assert not sentinel.exists()
    copy = fx.root / "verify-copy"
    copy.mkdir()
    fx.run(["sh", "-c", f"git archive HEAD | tar -x -C {shlex.quote(str(copy))}"], cwd=repo)
    first = fx.run(shlex.split(TEST_COMMAND), cwd=copy, check=False)
    assert first.returncode != 0 and "TEST HARNESS" in first.stderr, first.stderr
    assert sentinel.exists()
    fx.run(shlex.split(TEST_COMMAND), cwd=copy)  # the verify after the builder's fix passes


def test_old_project_looks_pre_1_8(fx):
    repo = old_project(fx)
    board = read_json(repo / "factory" / "board.json")
    assert "plugin_version" not in board and "run" not in board
    assert not (repo / "factory" / "server.py").exists()
    backlog = (repo / "factory" / "backlog.md").read_text()
    assert [line for line in backlog.splitlines() if line.startswith("- [")] == OLD_BACKLOG_ITEMS
    assert backlog.splitlines()[1] != (FACTORY_TEMPLATES / "backlog.md").read_text().splitlines()[1]
    claude_md = (repo / "CLAUDE.md").read_text()
    assert TEAM_NOTES in claude_md
    template = section((TEMPLATES / "project-context.md").read_text(), "## Software factory")
    assert section(claude_md, "## Software factory").strip() != template.strip(), "nothing for the upgrade to refresh"

"""Live: /software-factory:init on a new project, an upgrade from before 1.8.0, and a no-op re-run."""
from helpers import FACTORY_TEMPLATES, FIXTURES, TEMPLATES, plugin_version, read_json, tree_digest, write_json
from helpers.live import Live
from helpers.projects import OLD_BACKLOG_ITEMS, TEAM_NOTES, old_project, section

COPIED = ["dashboard.html", "server.py", "userguide.html", "backlog.md", "learnings.md", "factoryctl.py", "config.json",
          "events.jsonl"]
REFRESHED = ["factoryctl.py", "dashboard.html", "server.py", "userguide.html"]


def test_init_new_project(fx):
    repo = fx.make_repo()
    head = fx.git(repo, "rev-parse", "HEAD")
    with Live(fx, repo) as live:
        live.claude("/software-factory:init")
        live.claude("No, do not add the merge permission. Do not commit anything.", cont=True)

    board = read_json(repo / "factory" / "board.json")
    assert board["plugin_version"] == plugin_version(), "the session did not load this checkout's plugin"
    assert board["version"] == 2 and board["default_branch"] == "main" and board["jobs"] == [] and board["run"] is None
    for name in COPIED:
        assert (repo / "factory" / name).read_bytes() == (FACTORY_TEMPLATES / name).read_bytes(), name
    assert (repo / "factory" / "jobs" / ".gitkeep").exists()
    assert "<!-- software-factory:project-context -->" in (repo / "CLAUDE.md").read_text()
    assert ".DS_Store" in (repo / ".gitignore").read_text()
    assert not (repo / ".claude" / "settings.local.json").exists(), "added the merge rule without a yes"
    assert fx.git(repo, "rev-parse", "HEAD") == head, "committed without being asked"


def test_upgrade_from_before_1_8_then_rerun_is_a_no_op(fx):
    repo = old_project(fx)
    head = fx.git(repo, "rev-parse", "HEAD")
    fixture_board = read_json(repo / "factory" / "board.json")
    user_files = ["factory/learnings.md", "factory/jobs/001-greet", "factory/jobs/002-farewell"]
    kept = tree_digest(repo, user_files)
    with Live(fx, repo) as live:
        live.claude("/software-factory:init")
        live.claude("Yes, apply all of it. No to proposing dependencies, no to the merge permission, "
                    "and do not commit.", cont=True)

        board = read_json(repo / "factory" / "board.json")
        assert board["plugin_version"] == plugin_version()
        assert board["default_branch"] == "main"
        assert board["version"] == 2 and board["run"] is None and board["retro"] == {"done_since": 0, "last": None}
        for was, now in zip(fixture_board["jobs"], board["jobs"]):
            assert {k: now[k] for k in was} == was, f"the upgrade rewrote job {was['id']}"
            assert now["evidence"]["verified_commit"] is None and now["workspace"]["kind"] == "legacy-checkout"
        assert len(board["jobs"]) == len(fixture_board["jobs"])
        assert read_json(repo / "factory" / "config.json") == read_json(FACTORY_TEMPLATES / "config.json")
        assert '"kind": "migrate"' in (repo / "factory" / "events.jsonl").read_text()
        for name in REFRESHED:
            assert (repo / "factory" / name).read_bytes() == (FACTORY_TEMPLATES / name).read_bytes(), name
        backlog = (repo / "factory" / "backlog.md").read_text().splitlines()
        template_header = (FACTORY_TEMPLATES / "backlog.md").read_text().splitlines()[1]
        assert backlog[1] == template_header, "backlog header comment not refreshed"
        items = [line for line in backlog if line.startswith("- [")]
        expected = [f"{line[:6]}B{i} {line[6:]}" for i, line in enumerate(OLD_BACKLOG_ITEMS, 1)]
        assert items == expected, items
        claude_md = (repo / "CLAUDE.md").read_text()
        template_section = section((TEMPLATES / "project-context.md").read_text(), "## Software factory")
        assert (section(claude_md, "## Software factory") or "").strip() == template_section.strip()
        assert TEAM_NOTES in claude_md, "a user-owned section was edited"
        assert "`python3 -m unittest -q`" in claude_md, "the test command was lost"
        assert tree_digest(repo, user_files) == kept, "an upgrade must never touch learnings or job files"
        assert (repo / "factory" / "jobs" / ".gitkeep").exists(), "init creates a missing jobs/.gitkeep (U3)"
        assert fx.git(repo, "rev-parse", "HEAD") == head, "committed without being asked"

        # A second init on the upgraded project must change nothing.
        before = tree_digest(repo, ["factory", "CLAUDE.md", ".gitignore"])
        live.claude("/software-factory:init")
        live.claude("No to everything you ask. Do not commit.", cont=True)
        assert tree_digest(repo, ["factory", "CLAUDE.md", ".gitignore"]) == before, "a re-run upgrade changed files"


def test_newer_project_stops_without_writing(fx):
    repo = fx.make_repo()
    (repo / "factory").mkdir()
    write_json(repo / "factory" / "board.json", read_json(FIXTURES / "boards" / "newer-than-plugin.json"))
    fx.git(repo, "add", "-A")
    fx.git(repo, "commit", "-q", "-m", "fixture: stamped by a newer plugin")
    before = tree_digest(repo, ["factory", "CLAUDE.md", ".gitignore"])
    with Live(fx, repo) as live:
        reply = live.claude("/software-factory:init")
    # Writing nothing is only a pass if init ran and refused for this reason, not if the skill never loaded.
    assert "9.9.0" in reply, f"init did not name the newer stamp: {reply[:300]}"
    assert tree_digest(repo, ["factory", "CLAUDE.md", ".gitignore"]) == before
    assert not fx.git(repo, "status", "--porcelain")

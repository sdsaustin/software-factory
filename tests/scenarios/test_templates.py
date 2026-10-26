"""The files `init` copies into a project, and the fixture projects later upgrade scenarios start from."""
import json
import py_compile
import re

from helpers import FACTORY_TEMPLATES, FIXTURES, TEMPLATES, read_json


def test_template_board(fx):
    board = read_json(FACTORY_TEMPLATES / "board.json")
    assert board == {"version": 1, "plugin_version": None, "default_branch": None, "jobs": [],
                     "retro": {"done_since": 0, "last": None}, "run": None}


def test_init_copies_exactly_these_files(fx):
    """init step 2 and upgrade U3 name these files; a new template must be added to both."""
    shipped = sorted(p.name for p in FACTORY_TEMPLATES.iterdir())
    assert shipped == ["backlog.md", "board.json", "dashboard.html", "learnings.md", "server.py", "userguide.html"]
    init = (TEMPLATES.parent / "skills" / "init" / "SKILL.md").read_text()
    for name in shipped:
        assert f"`{name}`" in init, name


def test_backlog_and_learnings_headers(fx):
    backlog = (FACTORY_TEMPLATES / "backlog.md").read_text().splitlines()
    assert backlog[0] == "# Backlog" and backlog[1].startswith("<!--") and backlog[1].endswith("-->")
    assert (FACTORY_TEMPLATES / "learnings.md").read_text().startswith("# Learnings\n")


def test_project_context_block(fx):
    text = (TEMPLATES / "project-context.md").read_text()
    assert "<!-- software-factory:project-context -->" in text
    assert "## Software factory" in text and "Test command:" in text


def test_server_compiles_on_this_python(fx):
    py_compile.compile(str(FACTORY_TEMPLATES / "server.py"), cfile=str(fx.root / "server.pyc"), doraise=True)


def test_upgrade_fixture_boards(fx):
    """Fixtures for upgrade scenarios: each must stay what its name claims."""
    boards = {p.stem: read_json(p) for p in sorted((FIXTURES / "boards").glob("*.json"))}
    assert set(boards) >= {"pre-1.8", "v1.8-in-flight", "newer-than-plugin"}
    old = boards["pre-1.8"]
    assert "plugin_version" not in old and "run" not in old and "retro" not in old
    assert any(j["stage"] == "needs-human" for j in old["jobs"])
    inflight = boards["v1.8-in-flight"]
    assert inflight["plugin_version"] == "1.8.0"
    assert any(j["stage"] in {"build", "review", "triage", "decision"} for j in inflight["jobs"])
    newer = boards["newer-than-plugin"]
    assert tuple(map(int, newer["plugin_version"].split("."))) >= (9, 0, 0)
    for name, board in boards.items():
        for job in board["jobs"]:
            assert re.fullmatch(r"\d{3}-[a-z0-9-]+", job["id"]), (name, job["id"])
        json.dumps(board)

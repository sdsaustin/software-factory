"""Fixture projects shared by the live scenarios and the deterministic checks that keep them honest."""
import re

from .fixture import FIXTURES, TEMPLATES, read_json, write_json

TEST_COMMAND = "python3 -m unittest -q"

TOY = {
    ".gitignore": "__pycache__/\n.DS_Store\n",
    "textkit.py": '"""Small text helpers."""\n\n\ndef word_count(text):\n'
                  '    """Number of whitespace-separated words in text."""\n    return len(text.split())\n',
    "test_textkit.py": "import unittest\n\nfrom textkit import word_count\n\n\n"
                       "class WordCountTest(unittest.TestCase):\n"
                       "    def test_counts_words(self):\n        self.assertEqual(word_count('a b  c'), 3)\n\n"
                       "    def test_empty(self):\n        self.assertEqual(word_count(''), 0)\n",
}
TOY_ITEM = "- [ ] B1 Add slugify(text) to textkit.py: lowercase words joined by single hyphens, punctuation dropped\n"

# Fails exactly once, the first time the suite runs outside any git checkout. The orchestrator's verify
# (a `git archive` copy) is the first such run; the builder and spec-writer run inside a checkout.
GATE = '''import os
import unittest

SENTINEL = {sentinel!r}


def _inside_git_checkout():
    d = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


class HarnessGate(unittest.TestCase):
    def test_first_clean_copy_run(self):
        if _inside_git_checkout() or os.path.exists(SENTINEL):
            return
        open(SENTINEL, "w").close()
        self.fail("TEST HARNESS: this failure is injected once, on purpose, the first time the suite runs from a "
                  "clean copy outside git, to exercise the factory's verify-fix path. The code is not wrong. "
                  "Record that in build.md and change nothing; the next run passes.")
'''


def toy_project(fx, gate_sentinel=None):
    """A small Python project with the factory installed, one backlog item, and everything committed."""
    files = dict(TOY)
    if gate_sentinel:
        gate_sentinel.parent.mkdir(parents=True, exist_ok=True)
        files["test_harness_gate.py"] = GATE.format(sentinel=str(gate_sentinel))
    repo = fx.make_repo(files=files)
    factory = fx.install_factory(repo, test_command=TEST_COMMAND)
    (factory / "backlog.md").write_text((factory / "backlog.md").read_text() + TOY_ITEM)
    fx.git(repo, "add", "-A")
    fx.git(repo, "commit", "-q", "-m", "fixture: factory initialized")
    return repo


def section(text, heading):
    """From `heading` to the next '## ' heading or the end."""
    m = re.search(rf"^{re.escape(heading)}\n.*?(?=^## |\Z)", text, re.M | re.S)
    return m.group(0) if m else None


OLD_BACKLOG_ITEMS = [
    "- [x] Greet by name",
    "- [ ] Say goodbye",
    "- [ ] Greet in French",
    "- [ ] Greet in Spanish",
]
TEAM_NOTES = "## Team notes\nDeploys go out on Thursdays. Ask the platform team before touching the CI config.\n"


def old_project(fx):
    """A project set up by a plugin from before 1.8.0: old board, old files, unnumbered backlog, user notes."""
    context = (TEMPLATES / "project-context.md").read_text()
    context = context.replace(section(context, "## Software factory"),
                              "## Software factory\nRun jobs with `/factory <feature>`. State lives in `factory/board.json`.\n\n")
    context = context.replace("<TBD - the factory's readiness gate requires this>", f"`{TEST_COMMAND}`")
    files = {
        "greet.py": "def greet(name):\n    return f'Hello, {name}!'\n",
        "CLAUDE.md": context.rstrip("\n") + "\n\n" + TEAM_NOTES,
        "factory/backlog.md": "# Backlog\n<!-- One feature per line. -->\n" + "\n".join(OLD_BACKLOG_ITEMS) + "\n",
        "factory/learnings.md": "# Learnings\n- 2026-09-30: greeting tests must cover empty names (001-greet)\n",
        "factory/dashboard.html": "<!doctype html><title>old dashboard from 1.3.0</title>\n",
        "factory/userguide.html": "<!doctype html><title>old guide</title>\n",
        "factory/jobs/001-greet/spec.md": "# Greet by name\n",
        "factory/jobs/002-farewell/decision.md": "ESCALATE\nNeeds a human look at the copy.\n",
    }
    repo = fx.make_repo(files=files)
    write_json(repo / "factory" / "board.json", read_json(FIXTURES / "boards" / "pre-1.8.json"))
    fx.git(repo, "add", "-A")
    fx.git(repo, "commit", "-q", "-m", "fixture: project set up by an old plugin")
    return repo

"""Job-loop helpers shared by the factoryctl scenarios: agents are replaced by the files they would write."""
import json
import re

from . import read_json, tree_digest
from .projects import TEST_COMMAND

JOB = "001-slugify"
TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
SLUGIFY = ('\n\ndef slugify(text):\n    """Lowercase words joined by single hyphens, punctuation dropped."""\n'
           '    import re\n    return "-".join(re.findall(r"[a-z0-9]+", text.lower()))\n')
SLUG_TEST = ("\n\nclass SlugifyTest(unittest.TestCase):\n    def test_slugify(self):\n"
             "        from textkit import slugify\n        self.assertEqual(slugify('Hello, World!'), 'hello-world')\n")


def board(repo):
    return read_json(repo / "factory" / "board.json")


def job(repo, jid=JOB):
    return next(j for j in board(repo)["jobs"] if j["id"] == jid)


def events(repo):
    text = (repo / "factory" / "events.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def state(repo):
    return tree_digest(repo, ["factory"])


def refused(fx, repo, *args, code=1):
    """Run a command that must be refused, and prove it wrote nothing."""
    before = state(repo)
    r = fx.ctl(repo, *args, check=False)
    assert r.returncode == code, f"{args} exited {r.returncode}, expected {code}\n{r.stdout}{r.stderr}"
    assert state(repo) == before, f"{args} was refused but changed factory/"
    return r.stdout + r.stderr


def build_commit(fx, repo, files, jid=JOB, message="build"):
    """What a builder leaves behind: a commit on factory/<id>, with the default branch checked out again."""
    branch = f"factory/{jid}"
    exists = fx.run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=repo, check=False).returncode == 0
    fx.git(repo, "switch", "-q", *([branch] if exists else ["-c", branch]))
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((p.read_text() if p.exists() else "") + text)
    fx.git(repo, "add", *files)
    fx.git(repo, "commit", "-q", "-m", message)
    fx.git(repo, "switch", "-q", "main")
    return fx.git(repo, "rev-parse", branch)


def review_files(repo, rnd, verdicts, jid=JOB):
    """{reviewer: (VERDICT, findings or None for no FINDINGS line, or a raw first line)}"""
    folder = repo / "factory" / "jobs" / jid / f"round-{rnd}"
    folder.mkdir(parents=True, exist_ok=True)
    for name, spec in verdicts.items():
        if isinstance(spec, str):
            (folder / f"{name}.md").write_text(spec)
            continue
        verdict, findings = spec
        second = "" if findings is None else f"FINDINGS: {findings}\n"
        (folder / f"{name}.md").write_text(f"VERDICT: {verdict}\n{second}\nNotes.\n")


def to_verified(fx, repo, track="quick", extra=None):
    """A job built and verified: spec -> build -> verify (PASS)."""
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify", "--item", "B1")
    fx.ctl(repo, "set", f"jobs.{JOB}.track={track}", f"jobs.{JOB}.assumptions=1", *(extra or []))
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    build_commit(fx, repo, {"textkit.py": SLUGIFY, "test_textkit.py": SLUG_TEST})
    fx.ctl(repo, "transition", JOB, "verify", "--reason", "built")
    r = fx.ctl(repo, "verify", JOB, "--test", TEST_COMMAND)
    assert r.stdout.startswith("VERIFY: PASS"), r.stdout

"""Per-scenario fixture: a unique temp directory, isolated git, a throwaway HOME."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "templates"
FACTORY_TEMPLATES = TEMPLATES / "factory"
FIXTURES = REPO / "tests" / "fixtures"


class Skip(Exception):
    """Raised by a scenario that cannot run here (missing tool, opt-in only)."""


def plugin_version():
    return json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())["version"]


class Fixture:
    def __init__(self, name, options=None):
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
        self.name = name
        self.options = options or {}
        self.root = Path(tempfile.mkdtemp(prefix=f"sfx-{slug}-")).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        gitconfig = self.root / "gitconfig"
        gitconfig.write_text("[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")
        # Git never reads the developer's global or system config, and commits use a fixture identity.
        self.git_env = {
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Factory Test",
            "GIT_AUTHOR_EMAIL": "factory-test@example.invalid",
            "GIT_COMMITTER_NAME": "Factory Test",
            "GIT_COMMITTER_EMAIL": "factory-test@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        }
        # Deterministic scenarios also get a throwaway HOME. Live scenarios must not: claude needs the real login.
        self.env = dict(os.environ, **self.git_env, HOME=str(self.home))

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run(self, args, cwd=None, check=True, env=None, timeout=60):
        r = subprocess.run(args, cwd=cwd or self.root, env=env or self.env, capture_output=True,
                           text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise AssertionError(f"{args} exited {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def git(self, repo, *args, check=True):
        return self.run(["git", *args], cwd=repo, check=check).stdout.strip()

    def make_repo(self, name="proj", branch="main", files=None):
        """A git repo with one commit holding `files` ({relative path: text})."""
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", branch)
        for rel, text in (files or {"README.md": "# fixture\n"}).items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "fixture: initial")
        return repo

    def install_factory(self, repo, default_branch="main", test_command=None, version=None):
        """Lay out factory/ as a fresh `init` would, without running Claude. For live job fixtures."""
        factory = repo / "factory"
        (factory / "jobs").mkdir(parents=True)
        (factory / "jobs" / ".gitkeep").write_text("")
        for src in FACTORY_TEMPLATES.iterdir():
            shutil.copy2(src, factory / src.name)
        board = json.loads((FACTORY_TEMPLATES / "board.json").read_text())
        board["plugin_version"] = version or plugin_version()
        board["default_branch"] = default_branch
        write_json(factory / "board.json", board)
        context = (TEMPLATES / "project-context.md").read_text()
        if test_command:
            context = context.replace("<TBD - the factory's readiness gate requires this>", f"`{test_command}`")
            context = context.replace("- Lint/typecheck/build command: <TBD>", "- Lint/typecheck/build command: none")
        claude_md = repo / "CLAUDE.md"
        prior = claude_md.read_text() + "\n" if claude_md.exists() else ""
        claude_md.write_text(prior + context)
        return factory


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2) + "\n")


def read_json(path):
    return json.loads(Path(path).read_text())


def tree_digest(root, rel_paths):
    """{path: bytes} for files under the given relative paths, to compare before and after a step."""
    out = {}
    for rel in rel_paths:
        base = Path(root) / rel
        files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file()) if base.exists() else []
        for p in files:
            out[str(p.relative_to(root))] = p.read_bytes()
    return out

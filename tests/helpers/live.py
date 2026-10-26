"""Drive real `claude -p` sessions against a fixture project, with this repo loaded as the plugin.

Claude runs on the developer's normal login: HOME and CLAUDE_CONFIG_DIR are never changed for it.
Isolation comes from the temp fixture repo, the fixture's git config, and the agents' own throwaway
HOME for project commands. Every installed plugin is disabled for the session (via --settings, which
writes nothing), so only this checkout's software-factory loads. Each session's transcript folder under
~/.claude/projects/ is copied to tests/out/ and then deleted.
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .fixture import REPO, Skip

OUT = REPO / "tests" / "out"
CLAUDE_DIR = Path.home() / ".claude"


def _installed_plugins():
    try:
        data = json.loads((CLAUDE_DIR / "plugins" / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        return []
    plugins = data.get("plugins", data) if isinstance(data, dict) else {}
    return [k for k in plugins if isinstance(k, str) and "@" in k]


class Live:
    def __init__(self, fx, repo):
        self.claude_bin = shutil.which("claude")
        if not self.claude_bin:
            raise Skip("claude is not on PATH")
        self.fx, self.repo, self.calls = fx, repo, 0
        slug = re.sub(r"[^a-z0-9]+", "-", fx.name.split("::")[-1].lower()).strip("-")
        self.out = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}"
        # Claude names a project's transcript folder after its path with every non-alphanumeric made "-".
        self.token = re.sub(r"[^A-Za-z0-9]", "-", fx.root.name)
        self.env = dict(os.environ, **fx.git_env)  # the real HOME: claude needs the normal login

    def __enter__(self):
        self.out.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        moved = self.collect_transcripts()
        print(f"  live output: {self.out} ({len(moved)} transcript folder(s))")
        return False

    def claude(self, prompt, cont=False, timeout=1800):
        settings = {"enabledPlugins": {k: False for k in _installed_plugins()}}
        # --add-dir lets init read the plugin's templates from this checkout.
        cmd = [self.claude_bin, "-p", prompt, "--plugin-dir", str(REPO), "--add-dir", str(REPO),
               "--output-format", "json",
               "--permission-mode", self.fx.options.get("permission_mode", "auto"),
               "--settings", json.dumps(settings),
               "--allowedTools", "Bash(git merge --no-ff factory/*)"]
        if cont:
            cmd.append("--continue")
        self.calls += 1
        (self.out / f"call-{self.calls}.prompt.txt").write_text(prompt)
        try:
            r = subprocess.run(cmd, cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(f"claude call {self.calls} timed out after {timeout}s")
        (self.out / f"call-{self.calls}.json").write_text(r.stdout or "")
        if r.stderr:
            (self.out / f"call-{self.calls}.stderr.txt").write_text(r.stderr)
        try:
            res = json.loads(r.stdout)
        except ValueError:
            res = {"is_error": True, "result": r.stdout}
        if r.returncode != 0 or res.get("is_error"):
            raise AssertionError(f"claude call {self.calls} failed (exit {r.returncode}): "
                                 f"{str(res.get('result') or r.stderr)[:800]}")
        return res.get("result") or ""

    def collect_transcripts(self):
        """Move this fixture's transcript folders out of ~/.claude/projects (matched by the unique temp-dir name)."""
        projects = CLAUDE_DIR / "projects"
        moved = []
        if not projects.is_dir():
            return moved
        for d in projects.iterdir():
            if d.is_dir() and self.token in d.name:
                dest = self.out / "transcripts" / d.name
                shutil.copytree(d, dest, dirs_exist_ok=True)
                shutil.rmtree(d)
                moved.append(dest)
        return moved

    def tool_events(self):
        """Every tool call in the copied transcripts (main session and subagents), oldest first."""
        events = []
        for path in sorted((self.out / "transcripts").rglob("*.jsonl")):
            for line in path.read_text(errors="replace").splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                msg = obj.get("message") if isinstance(obj, dict) else None
                if obj.get("type") != "assistant" or not isinstance(msg, dict):
                    continue
                for c in msg.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        events.append({"ts": obj.get("timestamp") or "", "tool": c.get("name"),
                                       "input": c.get("input") or {}, "file": path.name})
        return sorted(events, key=lambda e: e["ts"])


def role_of(event):
    """'builder', 'reviewer-code', 'triager', ... for an agent spawn or a SendMessage to a named agent; else None."""
    inp = event["input"]
    if event["tool"] in ("Agent", "Task"):
        name = str(inp.get("subagent_type") or "")
    elif event["tool"] == "SendMessage":
        name = str(inp.get("to") or inp.get("recipient") or "")
    else:
        return None
    name = name.split(":")[-1]
    for role in ("builder", "reviewer-security", "reviewer-code", "reviewer-ui", "reviewer-ux", "triager",
                 "approver", "spec-writer"):
        if name == role or name.startswith(role + "-"):
            return role
    return None

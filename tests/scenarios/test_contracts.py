"""Cross-file contracts: the prose in SKILL.md and agents/*.md must agree with each other and with the shipped code."""
import json
import re
import subprocess

from helpers import FACTORY_TEMPLATES, REPO, plugin_version
from helpers.server import load_module

SKILL = REPO / "skills" / "factory" / "SKILL.md"
AGENTS = REPO / "agents"
REVIEWERS = ["reviewer-security", "reviewer-code", "reviewer-ui", "reviewer-ux", "reviewer-architecture"]


def _text(path):
    return path.read_text(encoding="utf-8")


def _skill():
    """The orchestrator's full instructions: SKILL.md plus the command files it reads on demand."""
    parts = sorted(SKILL.parent.glob("*.md"), key=lambda p: (p.name != "SKILL.md", p.name))
    return "\n".join(_text(p) for p in parts)


def _frontmatter(path):
    m = re.match(r"---\n(.*?)\n---\n", _text(path), re.S)
    assert m, f"{path.name}: no frontmatter"
    return dict(line.split(": ", 1) for line in m.group(1).splitlines() if ": " in line)


def _skill_stages():
    m = re.search(r"^Stages: (.+?)\. Update it", _skill(), re.M)
    assert m, "SKILL.md has no Stages: line"
    return re.findall(r"`([a-z-]+)`", m.group(1))


def _factoryctl():
    return load_module(FACTORY_TEMPLATES, "factoryctl.py")


def test_stage_list_matches_dashboard_and_factoryctl(fx):
    m = re.search(r"const STAGES=(\[.*?\]);", _text(FACTORY_TEMPLATES / "dashboard.html"))
    assert m, "dashboard.html has no STAGES"
    assert json.loads(m.group(1)) == _skill_stages(), "a stage missing from the dashboard drops its jobs off the board"
    assert _factoryctl().STAGES == _skill_stages()


def test_documented_moves_match_factoryctl(fx):
    """SKILL.md's list of allowed moves is what factoryctl enforces."""
    m = re.search(r"Allowed moves: (.+?); any of these → cancelled; `done` and `cancelled` are final\.", _skill())
    assert m, "SKILL.md has no Allowed moves list"
    documented = {}
    for part in m.group(1).split("; "):
        src, dsts = part.split(" → ")
        documented[src] = {d.strip() for d in dsts.split(",")} | {"cancelled"}
    enforced = {k: v for k, v in _factoryctl().TRANSITIONS.items() if v}
    assert documented == enforced, (documented, enforced)


def test_documented_settable_fields_match_factoryctl(fx):
    fc = _factoryctl()
    m = re.search(r"`jobs\.<id>\.<field>` for (.+?); top-level", _skill())
    assert m
    assert set(re.findall(r"`([a-z_]+)`", m.group(1))) == fc.SETTABLE_JOB


def test_factoryctl_reasons_and_version(fx):
    fc = _factoryctl()
    js = re.search(r'"reason":"([A-Z |-]+)"', _skill())
    assert set(fc.REASONS) == {r.strip() for r in js.group(1).split("|")}
    assert fc.VERSION == plugin_version(), "bump VERSION in templates/factory/factoryctl.py with the plugin"


def test_agents_have_valid_frontmatter(fx):
    for path in sorted(AGENTS.glob("*.md")):
        fm = _frontmatter(path)
        assert fm.get("name") == path.stem, f"{path.name}: name {fm.get('name')!r}"
        assert fm.get("description"), f"{path.name}: no description"
        assert fm.get("model") in {"opus", "sonnet", "haiku"}, f"{path.name}: model {fm.get('model')!r}"
        assert fm.get("tools"), f"{path.name}: no tools"
    for path in sorted((REPO / "skills").glob("*/SKILL.md")):
        fm = _frontmatter(path)
        assert fm.get("name") == path.parent.name and fm.get("description"), path


def test_every_referenced_agent_or_skill_exists(fx):
    text = _skill()
    names = set(re.findall(r"`software-factory:([a-z-]+)`", text))
    names |= set(re.findall(r"spawn (?:a fresh )?`([a-z-]+)`", text))
    assert names, "found no agent references"
    for name in sorted(names):
        assert (AGENTS / f"{name}.md").exists() or (REPO / "skills" / name / "SKILL.md").exists(), name


def test_reviewer_grammar(fx):
    for name in REVIEWERS:
        text = _text(AGENTS / f"{name}.md")
        assert "`VERDICT: PASS` or `VERDICT: CHANGES`" in text, name
        assert "`FINDINGS: <n>`" in text, f"{name}: the triage skip depends on the FINDINGS line"
        assert "Always write this file" in text, name
        assert f"round-<N>/{name.split('-')[1]}.md" in text, name
    triager = _text(AGENTS / "triager.md")
    for word in ("CLEAN", "PATCH", "DECISION"):
        assert f"`TRIAGE: {word}`" in triager
    assert "`APPROVE` or `ESCALATE`" in _text(AGENTS / "approver.md")


def test_orchestrator_knows_the_grammar(fx):
    text = _skill()
    for needle in ("`VERDICT: PASS`", "`FINDINGS: 0`", "triager not run", "`TRIAGE: CLEAN`", "`VERIFY: PASS`",
                   "`Candidate: factory/<id> @ <sha>`", "`Default: <default_branch> @ <sha>`", "factoryctl round",
                   "factoryctl.py verify"):
        assert needle in text, needle


def test_verify_evidence_is_read_not_rerun(fx):
    """v1.9.0: the orchestrator runs the suite once per build; agents read verify.md."""
    for name in ("reviewer-code", "triager"):
        text = _text(AGENTS / f"{name}.md")
        assert "verify.md" in text and re.search(r"(do not|never) re-run the whole suite", text), name
    approver = _text(AGENTS / "approver.md")
    assert "Do not re-run the tests" in approver and "may re-run" not in approver
    assert "## Verify fix" in _text(AGENTS / "builder.md")
    assert "3a. **Verify**" in _skill()


def test_action_types_match_server(fx):
    server = _text(FACTORY_TEMPLATES / "server.py")
    m = re.search(r"TYPES = \{(.*?)\}", server)
    server_types = set(re.findall(r'"([a-z]+)"', m.group(1)))
    skill = re.search(r'Each line is `\{"id","at","type":"([a-z|]+)"', _skill())
    assert skill, "SKILL.md has no actions.jsonl type list"
    assert set(skill.group(1).split("|")) == server_types


def test_inbox_reason_codes_agree(fx):
    text = _skill()
    md = re.search(r"## <job id> - <reason: ([A-Z |-]+)>", text)
    js = re.search(r'"reason":"([A-Z |-]+)"', text)
    assert md and js
    as_md = {r.strip() for r in md.group(1).split("|")}
    as_json = {r.strip() for r in js.group(1).split("|")}
    assert as_md == as_json, as_md ^ as_json


def test_version_is_documented(fx):
    v = plugin_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v)
    assert f"<tr><td>{v}</td>" in _text(REPO / "docs" / "factoryarchitecture.html"), "add a version history row"
    assert v in _text(REPO / "docs" / "upgradepath.html"), "add a row to the upgrade table"
    for path in (REPO / ".claude-plugin" / "plugin.json", REPO / ".claude-plugin" / "marketplace.json"):
        json.loads(_text(path))


def test_no_excluded_integrations(fx):
    """The factory depends on Claude Code and git only (upgrade spec section 2)."""
    patterns = [r"(?i)\borca\b", r"(?i)\bherdr\b", r"(?i)\bcursor[- ](cli|agent)\b", r"\bLinear\b",
                r"(?i)linear\.app", r"(?i)\bmem0\b", r"(?i)opendeepwiki"]
    files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for rel in files:
        if rel.startswith(("tests/", "work/")):
            continue
        try:
            text = (REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hits += [f"{rel}: {p}" for p in patterns if re.search(p, text)]
    assert not hits, hits


def test_command_files_exist_and_are_referenced(fx):
    """SKILL.md loads its command files on demand; each must exist, be listed, and say whose part it is."""
    core = _text(SKILL)
    listed = re.findall(r"^- `([a-z-]+\.md)`:", core.split("## Command files", 1)[1].split("\n## ", 1)[0], re.M)
    on_disk = sorted(p.name for p in SKILL.parent.glob("*.md") if p.name != "SKILL.md")
    assert sorted(listed) == on_disk, (listed, on_disk)
    for name in on_disk:
        assert _text(SKILL.parent / name).startswith("<!-- Part of the factory skill"), name
    assert len(core.encode()) < 45000, "SKILL.md is loaded for every command: keep command-specific text in its file"


def test_planner_headings_match_plan_check(fx):
    """The planner is told the exact headings `factoryctl plan-check` looks for."""
    planner = _text(AGENTS / "planner.md")
    for heading in ("## Components and boundaries", "## Quality attributes", "## Run and test commands",
                    "## Core and extension seams", "## Pre-mortem", "### Components and interfaces",
                    "### Quality attributes", "### Pre-mortem", "### Core and extension seams"):
        assert f"`{heading}" in planner, heading
    for name in _factoryctl().QUALITY:
        assert f"`- {name}:`" in planner, name
    assert "factoryctl plan-check" in _skill()

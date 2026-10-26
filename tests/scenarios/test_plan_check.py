"""`factoryctl plan-check`: the gate after a planner pass (FR-6). Documents are written here as the planner would."""
from helpers.projects import toy_project

ARCH = """# Architecture
## Components and boundaries
- api: HTTP layer; owns request validation. Interface: `POST /items` -> store.
- store: persistence; owns the schema. Interface: `Store.save(item)`.

## Quality attributes
- Security: tokens in the OS keychain (AD2).
- Privacy: no personal data stored.
- Observability: structured logs to stdout.
- Reliability: single process; restart on crash.
- Support: not applicable: a local tool with no operators.

## Run and test commands
- Test: `python3 -m unittest -q`
- Run: `python3 -m app`
- Lint: none (no linter chosen yet, AD3)
- Build: none (pure Python)

## Pre-mortem
1. The store corrupts on concurrent writes (AD1 prevents it).
"""
DECISIONS = """# Decisions
## AD1 SQLite store
Status: accepted (D2)
Context: one user, local data.
Decision: SQLite.
Consequences: no concurrent writers.

## AD2 Keychain for tokens
Status: assumed
Context: tokens must not sit in plain files.
Decision: OS keychain.
Consequences: needs a keychain on Linux.
"""


def write(repo, files):
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def run(fx, repo, *args, ok=True):
    r = fx.ctl(repo, "plan-check", *args, check=False)
    assert (r.returncode == 0) == ok, r.stdout + r.stderr
    return r.stdout


def test_complete_architecture_passes_and_lists_assumptions(fx):
    repo = toy_project(fx)
    write(repo, {"docs/architecture.md": ARCH, "docs/decisions.md": DECISIONS, "docs/product-brief.md": "# Brief\n"})
    out = run(fx, repo, "--pass", "2")
    assert "PASS 2: ok" in out
    assert "ASSUMED (show these to the human at the gate): AD2 Keychain for tokens" in out


def test_pass_2_needs_exact_commands_and_every_section(fx):
    repo = toy_project(fx)
    arch = (ARCH.replace("- Test: `python3 -m unittest -q`", "- Test: the usual unittest run")
                .replace("- Privacy: no personal data stored.\n", "")
                .replace("## Components and boundaries", "## Parts"))
    write(repo, {"docs/architecture.md": arch, "docs/decisions.md": DECISIONS.replace("Consequences: no concurrent writers.\n", "")
                 .replace("Status: accepted (D2)\n", "")})
    out = run(fx, repo, "--pass", "2", ok=False)
    for problem in ("`- Test:` must give the exact command in backticks", "lacks a `- <name>:` line for Privacy",
                    "no `## Components and boundaries` section", "AD1: no `Status:", "AD1: no Consequences"):
        assert problem in out, (problem, out)


def test_accelerator_needs_core_and_extension_seams(fx):
    repo = toy_project(fx)
    write(repo, {"docs/architecture.md": ARCH, "docs/decisions.md": DECISIONS,
                 "docs/product-brief.md": "# Brief\nA GRC accelerator: one core, configured per customer.\n"})
    assert "Core and extension seams" in run(fx, repo, "--pass", "2", ok=False)
    write(repo, {"docs/architecture.md": ARCH + "\n## Core and extension seams\n- Core: controls engine.\n"})
    run(fx, repo, "--pass", "2")


def test_document_pass_allows_no_tests_and_evidence_instead_of_context(fx):
    repo = toy_project(fx)
    arch = ("# Architecture\n## Components and boundaries\n- cli.py: entry point.\n\n## Run and test commands\n"
            "- Test: none (no test suite yet)\n- Run: `python3 cli.py`\n- Lint: none\n- Build: none\n")
    decisions = "## AD1 Python CLI\n**Status: inferred**\nEvidence: cli.py\nDecision: a CLI.\nConsequences: none known.\n"
    write(repo, {"docs/architecture.md": arch, "docs/decisions.md": decisions})
    assert "PASS DOC: ok" in run(fx, repo, "--pass", "DOC")
    assert "exact command" in run(fx, repo, "--pass", "2", ok=False)  # a product plan may not say "none"


def test_feature_pass_2_checks_its_own_section_and_decisions(fx):
    repo = toy_project(fx)
    section = ("\n## Feature: tags\n### Components and interfaces\n- tags module: `Tags.add(note, tag)`.\n"
               "### Quality attributes\n- Security: tag names are escaped.\n- Privacy: not applicable: no personal data.\n"
               "- Observability: logged with the note id.\n- Reliability: same store as notes.\n"
               "- Support: not applicable: no operators.\n### Pre-mortem\n1. Tags drift from notes (AD3).\n")
    new_decision = "\n## AD3 Tags table\nFeature: tags\nStatus: assumed\nContext: tags are many-to-many.\nDecision: a join table.\nConsequences: a migration.\n"
    write(repo, {"docs/architecture.md": ARCH + section, "docs/decisions.md": DECISIONS + new_decision,
                 "docs/features/tags/brief.md": "# Tags brief\n"})
    out = run(fx, repo, "--pass", "2", "--feature", "tags")
    assert "FEATURE tags PASS 2: ok" in out and "AD3 Tags table" in out and "AD2" not in out
    write(repo, {"docs/architecture.md": ARCH + section.replace("### Pre-mortem", "### Risks")})
    assert "no `### Pre-mortem` section" in run(fx, repo, "--pass", "2", "--feature", "tags", ok=False)
    assert "has no `## Feature: other`" in run(fx, repo, "--pass", "2", "--feature", "other", ok=False)


def test_backlog_ids_and_dependencies(fx):
    repo = toy_project(fx)
    write(repo, {"factory/backlog.md": "# Backlog\n- [ ] B1 Scaffold\n- [ ] B2 Parser (depends: B1)\n"})
    run(fx, repo, "--pass", "3")
    write(repo, {"factory/backlog.md": "# Backlog\n- [ ] B1 Scaffold (depends: B2)\n- [ ] B2 Parser\n"
                                       "- [ ] Formatter\n- [ ] B2 Again (depends: B9)\n"})
    out = run(fx, repo, "--pass", "3", ok=False)
    for problem in ("B1 depends on B2, which is not earlier", "backlog line without a B<n> id", "duplicate backlog id B2",
                    "depends on B9, which is not in the backlog"):
        assert problem in out, (problem, out)


def test_missing_documents_and_bad_pass(fx):
    repo = toy_project(fx)
    out = run(fx, repo, "--pass", "2", ok=False)
    assert "docs/architecture.md is missing" in out and "docs/decisions.md is missing" in out
    r = fx.ctl(repo, "plan-check", "--pass", "1", check=False)
    assert r.returncode == 1 and "pass 2, 3 or DOC" in r.stderr

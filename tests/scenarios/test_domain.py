"""Domain architecture artifacts (L1/L2, glossary, ownership) and their `plan-check` rules. Step 1 of work/domain_architecture_spec.md."""
import importlib.util
import json

from helpers.projects import toy_project
from scenarios.test_plan_check import ARCH, DECISIONS, run, write

L1 = """# L1
## Actors and external systems
- Customer (person); Stripe (external payments).

## Contexts
- billing: core - Invoices and payments
- identity: generic - Sign-in and accounts

## Relationships
- billing -> identity: customer-supplier
"""
CONTEXT_BILLING = "# billing\n## Aggregates and invariants\n- Invoice: total equals the sum of its lines.\n"
CONTEXT_IDENTITY = "# identity\n## Aggregates and invariants\n"
GLOSSARY = "# Glossary\n- **Invoice** (billing): a request for payment. Avoid: bill.\n- **Account** (identity): a sign-in.\n"
OWNERSHIP = {"billing": ["src/billing/**"], "identity": ["src/identity/**"], "_shared": ["src/common/**"]}
CONTAINERS = "\n## Containers\n- api: hosts billing, identity\n"
EPICS = "# Epics\n## E1 Invoicing\nContext: billing\nGoal: bill.\n\n## E2 Scaffold\nContext: _platform\n"
BACKLOG = ("# Backlog\n- [ ] B1 [E1] [ctx:billing] Create invoice (M1)\n- [ ] B2 [E1] [ctx:billing] Pay invoice (M2)\n"
           "- [ ] B3 [E1] [ctx:identity] Sign in (M3)\n- [ ] B4 [E1] [ctx:billing] Refund (M4)\n"
           "- [ ] B5 [E2] [ctx:_platform] CI (enabler)\n")


def domain(repo, **over):
    files = {"docs/domain/l1-context.md": L1, "docs/domain/contexts/billing.md": CONTEXT_BILLING,
             "docs/domain/contexts/identity.md": CONTEXT_IDENTITY, "docs/domain/glossary.md": GLOSSARY,
             "docs/domain/ownership.json": json.dumps(OWNERSHIP)}
    files.update(over)
    write(repo, files)


def tagged(decisions, value="global"):
    return decisions.replace("Status:", f"Bounded-context: {value}\nStatus:")


def test_clean_domain_passes_pass_2a(fx):
    repo = toy_project(fx)
    domain(repo)
    out = run(fx, repo, "--pass", "2A")
    assert "PASS 2A: ok" in out
    assert "WARN" not in out


def test_domain_problems_are_each_reported(fx):
    repo = toy_project(fx)
    bad_l1 = (L1.replace("billing: core - Invoices and payments", "billing: central - ")
                .replace("customer-supplier", "friends").replace("## Actors and external systems", "## Who"))
    domain(repo, **{
        "docs/domain/l1-context.md": bad_l1,
        "docs/domain/glossary.md": GLOSSARY + "- **Invoice** (billing): dup.\n- **Order** (shipping): x.\n- not a term\n",
        "docs/domain/ownership.json": json.dumps({"billing": ["src/billing/**", "src/**"], "identity": ["src/identity/**"],
                                                  "ghost": ["x/**"]}),
    })
    (repo / "docs/domain/contexts/identity.md").unlink()
    out = run(fx, repo, "--pass", "2A", ok=False)
    for problem in ("no `## Actors and external systems`", "subdomain type must be core, supporting or generic",
                    "billing: no purpose", "type must be one of", "ownership overlap", "ownership.json names 'ghost'",
                    "contexts/identity.md is missing", "defined twice in billing", "owned by shipping",
                    "glossary line must be"):
        assert problem in out, (problem, out)


def test_core_context_without_invariants_warns_only(fx):
    repo = toy_project(fx)
    domain(repo, **{"docs/domain/contexts/billing.md": "# billing\n## Aggregates and invariants\n"})
    out = run(fx, repo, "--pass", "2A")
    assert "WARN core context billing lists no aggregates or invariants" in out


def test_ownership_parser_rejects_bad_shapes(fx):
    repo = toy_project(fx)
    domain(repo, **{"docs/domain/ownership.json": json.dumps({"billing": "src/billing", "identity": ["../etc"]})})
    out = run(fx, repo, "--pass", "2A", ok=False)
    assert "must be a list of non-empty path globs" in out and "outside the project" in out
    domain(repo, **{"docs/domain/ownership.json": "{nope"})
    assert "not valid JSON" in run(fx, repo, "--pass", "2A", ok=False)


def test_pass_2_requires_containers_only_when_a_domain_exists(fx):
    repo = toy_project(fx)
    write(repo, {"docs/architecture.md": ARCH, "docs/decisions.md": DECISIONS})
    run(fx, repo, "--pass", "2")  # no docs/domain: unchanged behaviour
    domain(repo)
    write(repo, {"docs/decisions.md": tagged(DECISIONS)})
    assert "no `## Containers` section" in run(fx, repo, "--pass", "2", ok=False)
    write(repo, {"docs/architecture.md": ARCH + CONTAINERS.replace("billing, identity", "billing")})
    assert "context identity is not hosted by any container" in run(fx, repo, "--pass", "2", ok=False)
    write(repo, {"docs/architecture.md": ARCH + CONTAINERS})
    run(fx, repo, "--pass", "2")


def test_pass_3_requires_context_tags_and_caps_platform(fx):
    repo = toy_project(fx)
    write(repo, {"factory/backlog.md": "# Backlog\n- [ ] B1 Scaffold\n"})
    run(fx, repo, "--pass", "3")  # no domain: tags not required
    domain(repo)
    write(repo, {"docs/epics.md": EPICS, "factory/backlog.md": BACKLOG})
    run(fx, repo, "--pass", "3")
    write(repo, {"docs/epics.md": EPICS.replace("Context: billing\n", "").replace("Context: _platform", "Context: ghost"),
                 "factory/backlog.md": BACKLOG.replace("[ctx:identity] ", "").replace("[ctx:billing] Pay", "[ctx:ghost] Pay")})
    out = run(fx, repo, "--pass", "3", ok=False)
    for problem in ("E1: no `Context:", "E2: context 'ghost'", "B3: no `[ctx:", "B2: context 'ghost'"):
        assert problem in out, (problem, out)
    write(repo, {"docs/epics.md": EPICS, "factory/backlog.md":
                 "# Backlog\n- [ ] B1 [E2] [ctx:_platform] a\n- [ ] B2 [E1] [ctx:billing] b\n- [ ] B3 [E2] [ctx:_platform] c\n"})
    assert "more than 20% of backlog items are _platform" in run(fx, repo, "--pass", "3", ok=False)


def test_brownfield_doc_pass_checks_owned_paths_exist(fx):
    repo = toy_project(fx)
    arch = ("# Architecture\n## Components and boundaries\n- cli.py\n\n## Run and test commands\n- Test: none (x)\n"
            "- Run: `python3 cli.py`\n- Lint: none\n- Build: none\n" + CONTAINERS)
    decisions = "## AD1 CLI\nBounded-context: global\n**Status: inferred**\nEvidence: cli.py\nDecision: a CLI.\nConsequences: none.\n"
    write(repo, {"docs/architecture.md": arch, "docs/decisions.md": decisions})
    domain(repo)
    out = run(fx, repo, "--pass", "DOC", ok=False)
    assert "billing path `src/billing/**` does not exist" in out
    for d in ("billing", "identity", "common"):
        (repo / "src" / d).mkdir(parents=True)
    assert "PASS DOC: ok" in run(fx, repo, "--pass", "DOC")


def test_path_owner_and_overlap_helpers(fx):
    repo = toy_project(fx)
    spec = importlib.util.spec_from_file_location("factoryctl", repo / "factory" / "factoryctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    own = {"billing": ["src/billing/**"], "_shared": ["src/common/**"]}
    assert mod.path_owner(own, "src/billing/a/b.py") == "billing"
    assert mod.path_owner(own, "src/common/x.py") == "_shared"
    assert mod.path_owner(own, "README.md") is None
    assert mod.globs_overlap("src/**", "src/billing/**") and not mod.globs_overlap("src/a/**", "src/b/**")


# ---- step 2: job contexts and scope-check ----
from helpers.jobs import JOB, build_commit, job  # noqa: E402
from scenarios.test_scope import check, snap  # noqa: E402


def started_job(fx, repo, contexts=None, spec_line=None):
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify", "--item", "B1")
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    if contexts is not None:
        fx.ctl(repo, "set", f"jobs.{JOB}.contexts={json.dumps(contexts)}")
    if spec_line is not None:
        (repo / "factory" / "jobs" / JOB / "spec.md").write_text(f"# Add slugify\nTrack: quick\n{spec_line}\n")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")


def test_job_contexts_are_validated(fx):
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    assert job(repo)["contexts"] == []
    r = fx.ctl(repo, "set", f"jobs.{JOB}.contexts=[\"Bad Id\"]", check=False)
    assert r.returncode == 1 and "bad contexts" in r.stdout + r.stderr
    fx.ctl(repo, "set", f"jobs.{JOB}.contexts=[\"anything\"]")  # no domain yet: ids are not checked against it
    domain(repo)
    r = fx.ctl(repo, "set", f"jobs.{JOB}.contexts=[\"ghost\"]", check=False)
    assert r.returncode == 1 and "unknown context ghost" in r.stdout + r.stderr
    fx.ctl(repo, "set", f"jobs.{JOB}.contexts=[\"billing\", \"_platform\"]")
    assert job(repo)["contexts"] == ["billing", "_platform"]


def test_builder_may_change_only_its_contexts(fx):
    repo = toy_project(fx)
    domain(repo)
    started_job(fx, repo, ["billing"])
    snap(fx, repo, "builder")
    build_commit(fx, repo, {"src/billing/invoice.py": "x = 1\n", "src/common/util.py": "y = 1\n", "scratch.py": "z = 1\n"})
    out = check(fx, repo, "builder")
    assert "scope: ok" in out and "not owned by any context: scratch.py" in out
    snap(fx, repo, "builder")
    build_commit(fx, repo, {"src/identity/login.py": "x = 1\n"}, message="reach into identity")
    out = check(fx, repo, "builder", ok=False)
    assert "owned by context identity, but the job declares ['billing']" in out
    assert "SCOPE VIOLATION" in out and job(repo)["execution"]["blocked"].startswith("builder:")


def test_job_with_no_contexts_may_not_touch_owned_paths(fx):
    repo = toy_project(fx)
    domain(repo)
    started_job(fx, repo)
    snap(fx, repo, "builder")
    build_commit(fx, repo, {"src/billing/invoice.py": "x = 1\n"})
    assert "declares no contexts" in check(fx, repo, "builder", ok=False)


def test_provisional_ownership_reports_but_does_not_fail(fx):
    repo = toy_project(fx)
    domain(repo, **{"docs/domain/ownership.json": json.dumps(dict(OWNERSHIP, _provisional=["identity"]))})
    run(fx, repo, "--pass", "2A")
    started_job(fx, repo, ["billing"])
    snap(fx, repo, "builder")
    build_commit(fx, repo, {"src/identity/login.py": "x = 1\n"})
    out = check(fx, repo, "builder")
    assert "provisional ownership: reported only" in out
    snap(fx, repo, "builder")
    build_commit(fx, repo, {"src/billing/a.py": "x = 1\n"}, message="more")
    domain(repo, **{"docs/domain/ownership.json": json.dumps(dict(OWNERSHIP, _provisional=["ghost"]))})
    assert "`_provisional` names 'ghost'" in run(fx, repo, "--pass", "2A", ok=False)


def test_board_and_spec_contexts_must_agree(fx):
    repo = toy_project(fx)
    domain(repo)
    started_job(fx, repo, ["billing"], spec_line="Contexts: billing, identity")
    snap(fx, repo, "builder")
    build_commit(fx, repo, {"src/billing/a.py": "x = 1\n"})
    out = check(fx, repo, "builder", ok=False)
    assert "spec's Contexts: line says ['billing', 'identity']" in out


def test_projects_without_a_domain_are_not_affected(fx):
    repo = toy_project(fx)
    started_job(fx, repo)
    snap(fx, repo, "builder")
    build_commit(fx, repo, {"src/billing/a.py": "x = 1\n"})
    assert "scope: ok" in check(fx, repo, "builder")


def test_decisions_name_their_bounded_context_once_a_domain_exists(fx):
    repo = toy_project(fx)
    domain(repo)
    write(repo, {"docs/architecture.md": ARCH + CONTAINERS, "docs/decisions.md": DECISIONS})
    out = run(fx, repo, "--pass", "2", ok=False)
    assert "AD1: no `Bounded-context: <context>|global` line" in out
    write(repo, {"docs/decisions.md": tagged(DECISIONS, "ghost")})
    assert "bounded context 'ghost' is not in l1-context.md" in run(fx, repo, "--pass", "2", ok=False)
    write(repo, {"docs/decisions.md": tagged(DECISIONS, "billing")})
    run(fx, repo, "--pass", "2")


def test_planner_may_write_the_domain_docs_in_the_right_passes(fx):
    repo = toy_project(fx)
    snap(fx, repo, "planner", "--pass", "2A")
    write(repo, {"docs/domain/l1-context.md": L1, "docs/domain/contexts/billing.md": CONTEXT_BILLING,
                 "docs/open-questions.md": "QUESTIONS: 0\nNONE\n"})
    check(fx, repo, "planner", "--pass", "2A")
    snap(fx, repo, "planner", "--pass", "2A")
    write(repo, {"docs/architecture.md": ARCH})
    assert "changed docs/architecture.md" in check(fx, repo, "planner", "--pass", "2A", ok=False)
    snap(fx, repo, "planner", "--pass", "3")
    write(repo, {"docs/domain/glossary.md": GLOSSARY})
    assert "changed docs/domain/glossary.md" in check(fx, repo, "planner", "--pass", "3", ok=False)


# ---- step 4: reviewer-architecture ----
from helpers.jobs import review_files  # noqa: E402
from helpers.projects import TEST_COMMAND  # noqa: E402

CONTEXT_BILLING_IFACE = CONTEXT_BILLING + "## Published interfaces\n- Invoices API (`src/billing/api/**`): create and read invoices.\n"


def to_review(fx, repo, files, track="quick", contexts=("billing",)):
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify", "--item", "B1")
    fx.ctl(repo, "set", f"jobs.{JOB}.track={track}", f"jobs.{JOB}.contexts={json.dumps(list(contexts))}")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    build_commit(fx, repo, files)
    fx.ctl(repo, "transition", JOB, "verify", "--reason", "built")
    r = fx.ctl(repo, "verify", JOB, "--test", TEST_COMMAND)
    assert r.stdout.startswith("VERIFY: PASS"), r.stdout
    return fx.ctl(repo, "transition", JOB, "review", "--reason", "verified").stdout


def arch_state(repo):
    return job(repo)["reviews"]["1"].get("architecture")


def test_quick_job_inside_one_context_skips_the_architecture_reviewer(fx):
    repo = toy_project(fx)
    domain(repo, **{"docs/domain/contexts/billing.md": CONTEXT_BILLING_IFACE})
    to_review(fx, repo, {"src/billing/core/invoice.py": "x = 1\n"})
    assert arch_state(repo) == "N/A"


def _trigger(fx, files, why, contexts=("billing",), track="quick"):
    repo = toy_project(fx)
    domain(repo, **{"docs/domain/contexts/billing.md": CONTEXT_BILLING_IFACE})
    out = to_review(fx, repo, files, track=track, contexts=contexts)
    assert arch_state(repo) == "pending", out
    assert why in out or track == "full", out
    return repo


def test_trigger_shared_path(fx):
    _trigger(fx, {"src/common/util.py": "x = 1\n"}, "shared path src/common/util.py")


def test_trigger_forbidden_synonym(fx):
    _trigger(fx, {"src/billing/core/a.py": "def make_bill():\n    pass\n"}, "forbidden synonym 'bill'")


def test_trigger_job_spanning_contexts(fx):
    _trigger(fx, {"src/billing/core/a.py": "x = 1\n"}, "declares 2 contexts", contexts=("billing", "identity"))


def test_full_track_always_reviews_architecture_and_round_reads_its_file(fx):
    repo = _trigger(fx, {"src/billing/core/a.py": "x = 1\n"}, "", track="full")
    assert job(repo)["reviews"]["1"]["architecture"] == "pending"
    review_files(repo, 1, {"security": ("PASS", 0), "code": ("PASS", 0), "ux": ("PASS", 0), "ui": ("PASS", 0)})
    out = fx.ctl(repo, "round", JOB, check=False).stdout
    assert "architecture: missing" in out and "NEXT: rerun reviewer-architecture" in out
    review_files(repo, 1, {"architecture": ("CHANGES", 1)})
    out = fx.ctl(repo, "round", JOB, check=False).stdout
    assert "architecture: CHANGES findings=1" in out and "NEXT: spawn triager" in out


def test_projects_without_a_domain_never_get_an_architecture_round(fx):
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    fx.ctl(repo, "set", f"jobs.{JOB}.track=full")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    build_commit(fx, repo, {"a.py": "x = 1\n"})
    fx.ctl(repo, "transition", JOB, "verify", "--reason", "built")
    fx.ctl(repo, "verify", JOB, "--test", TEST_COMMAND)
    fx.ctl(repo, "transition", JOB, "review", "--reason", "verified")
    assert "architecture" not in job(repo)["reviews"]["1"]


def test_trigger_published_interface_path(fx):
    _trigger(fx, {"src/billing/api/routes.py": "x = 1\n"}, "implements a published interface (src/billing/api/**)")


def test_published_interface_path_outside_owned_paths_warns(fx):
    repo = toy_project(fx)
    domain(repo, **{"docs/domain/contexts/billing.md": CONTEXT_BILLING_IFACE})
    assert "WARN" not in run(fx, repo, "--pass", "2A")
    outside = CONTEXT_BILLING + "## Published interfaces\n- Login API (`src/identity/api/**`): sign in.\n"
    domain(repo, **{"docs/domain/contexts/billing.md": outside})
    out = run(fx, repo, "--pass", "2A")  # a warning, not a failure
    assert "WARN context billing: published interface path `src/identity/api/**` is outside its owned paths" in out

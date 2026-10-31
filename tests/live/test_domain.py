"""Live: the domain-architecture paths (work/domain_architecture_spec.md) that the other live scenarios skip.

1. A product with two distinct bounded contexts is planned unattended, with Pass 2A required: the planner writes the
   domain model and every gate (2A, 2, 3) accepts it.
2. A job runs in a project that already has a domain model: the job records its contexts, every scope check is ok, and
   the architecture reviewer (triggered because the job touches a published interface) writes a well-formed review.
"""
import json
import re

from helpers import read_json
from helpers.live import Live
from helpers.projects import domain_project

VISION = ("A small invoicing tool for freelancers, written in Python 3 with only the standard library, data in local "
          "JSON files. It has two distinct areas with their own vocabulary: BILLING (create invoices with line items, "
          "record payments, show what is overdue) and ACCOUNTS (a freelancer's sign-in profile and client address "
          "book). One user at a time.")
UNATTENDED = ("Plan it unattended: skip brainstorm, run grill headless, and approve every pass yourself, taking the "
              "planner's recommended option for any question. Do NOT skip Pass 2A: the product has two bounded "
              "contexts, billing and accounts, so model them. Do not commit anything.")
CONTINUE = "Approve and continue to the next pass, taking the recommended option for any question. Do not commit."


def test_multi_context_plan_writes_a_domain_model(fx):
    repo = fx.make_repo()
    fx.install_factory(repo)
    fx.git(repo, "add", "-A")
    fx.git(repo, "commit", "-q", "-m", "fixture: factory initialized")
    with Live(fx, repo) as live:
        live.claude(f"/software-factory:factory plan {VISION}\n\n{UNATTENDED}", timeout=2400)
        for _ in range(6):
            plan = read_json(repo / "factory" / "board.json").get("plan") or {}
            if plan.get("status") == "done":
                break
            live.claude(CONTINUE, cont=True, timeout=2400)
    board = read_json(repo / "factory" / "board.json")
    assert (board.get("plan") or {}).get("status") == "done", f"plan did not finish: {board.get('plan')}"
    dom = repo / "docs" / "domain"
    for f in ("l1-context.md", "glossary.md", "ownership.json"):
        assert (dom / f).exists(), f"Pass 2A did not write docs/domain/{f}"
    l1 = (dom / "l1-context.md").read_text()
    contexts = re.findall(r"^- ([a-z][a-z0-9-]*): (core|supporting|generic)\b", l1, re.M)
    assert len(contexts) >= 2, f"expected at least two contexts, found {contexts}"
    for ctx, _ in contexts:
        assert (dom / "contexts" / f"{ctx}.md").exists(), f"no L2 file for {ctx}"
    for p in ("2A", "2", "3"):
        r = fx.ctl(repo, "plan-check", "--pass", p, check=False)
        assert r.returncode == 0, f"plan-check --pass {p}:\n{r.stdout}"
    ownership = read_json(dom / "ownership.json")
    assert set(ownership.get("_provisional", [])) >= {c for c, _ in contexts}, "proposed paths must start provisional"
    backlog = (repo / "factory" / "backlog.md").read_text()
    assert re.search(r"\[ctx:[a-z_-]+\]", backlog), "backlog lines carry no [ctx:] tag"
    epics = (repo / "docs" / "epics.md").read_text()
    assert re.search(r"^Context: \S+", epics, re.M), "epics carry no Context: line"
    log = [json.loads(line) for line in (repo / "factory" / "events.jsonl").read_text().splitlines() if line.strip()]
    planner = [e for e in log if e.get("kind") == "scope" and e.get("role") == "planner"]
    assert len(planner) >= 4 and all(e["result"] == "ok" for e in planner), planner  # passes 1, 2A, 2, 3


def test_job_in_a_domain_project_records_contexts_and_is_architecture_reviewed(fx):
    repo = domain_project(fx)
    with Live(fx, repo) as live:
        live.claude("/software-factory:factory run --max-jobs 1", timeout=3600)
    jobs = read_json(repo / "factory" / "board.json")["jobs"]
    assert len(jobs) == 1 and jobs[0]["item"] == "B1", jobs
    job = jobs[0]
    jobdir = repo / "factory" / "jobs" / job["id"]
    assert job["stage"] in ("done", "needs-human"), f"job stopped mid-loop in {job['stage']}"
    assert job["contexts"] == ["text"], f"the board kept contexts {job.get('contexts')}"
    spec = (jobdir / "spec.md").read_text()
    assert re.search(r"^Contexts: *`?text`?\s*$", spec, re.M), "the spec has no `Contexts: text` line"
    log = [json.loads(line) for line in (repo / "factory" / "events.jsonl").read_text().splitlines() if line.strip()]
    scope = [e for e in log if e.get("job") == job["id"] and e["kind"] == "scope"]
    assert scope and all(e["result"] == "ok" for e in scope), [e for e in scope if e["result"] != "ok"]
    rounds = job["reviews"]
    assert rounds, "the job never reached review"
    assert "architecture" in rounds["1"], f"round 1 has no architecture entry: {rounds['1']}"
    # textkit.py is a published interface of `text`, so a quick job that changes it gets the architecture review.
    if job["track"] == "quick":
        assert rounds["1"]["architecture"] != "N/A", "a published-interface change skipped the architecture reviewer"
    review = (jobdir / "round-1" / "architecture.md").read_text().splitlines() + ["", ""]
    assert review[0].strip() in ("VERDICT: PASS", "VERDICT: CHANGES"), review[0]
    assert re.fullmatch(r"FINDINGS: \d+", review[1].strip()), review[1]

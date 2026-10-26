"""Live: one backlog item through `/software-factory:factory run --max-jobs 1` on a toy Python project.

Covers the v1.9.0 paths: verification right after the build (before any reviewer), the FINDINGS line,
the orchestrator's triage skip on a clean round, and the builder's verify fix when the clean-copy run fails.
"""
import json
import re

from helpers import read_json
from helpers.live import Live, role_of
from helpers.projects import toy_project

REVIEW_FILES = {"security": "reviewer-security", "code": "reviewer-code", "ui": "reviewer-ui", "ux": "reviewer-ux"}


def _run_one_job(fx, repo, live):
    live.claude("/software-factory:factory run --max-jobs 1", timeout=3600)
    jobs = read_json(repo / "factory" / "board.json")["jobs"]
    assert len(jobs) == 1, f"expected one job, board has {len(jobs)}"
    job = jobs[0]
    assert job.get("item") == "B1"
    if job["stage"] != "done":
        reason = job.get("parked_reason") or ""
        assert job["stage"] == "needs-human", f"job stopped mid-loop in stage {job['stage']}"
        assert not reason.startswith(("AGENT FAILURE", "VERIFY FAIL")), f"parked on plumbing: {reason}"
        print(f"  note: job parked for a human ({reason}); the v1.9.0 checks still apply")
    assert fx.git(repo, "branch", "--show-current") == "main", "the project checkout left the default branch"
    assert job["workspace"]["kind"] == "worktree", "the builder did not get its own worktree"
    if job["stage"] == "done":
        assert job["workspace"]["path"] is None, "a finished job's clean worktree should be removed"
    return job, repo / "factory" / "jobs" / job["id"]


def _check_verify_md(fx, repo, job, jobdir):
    lines = (jobdir / "verify.md").read_text().splitlines()
    assert lines[0].strip() == "VERIFY: PASS", lines[0]
    text = "\n".join(lines)
    cand = re.search(rf"^Candidate: factory/{re.escape(job['id'])} @ ([0-9a-f]{{7,40}})", text, re.M)
    assert cand, "verify.md has no Candidate: line"
    assert re.search(r"^Default: main @ [0-9a-f]{7,40}", text, re.M), "verify.md has no Default: line"
    assert re.search(r"^Tested: (branch|merge result)", text, re.M), "verify.md has no Tested: line"
    head = fx.git(repo, "rev-parse", f"factory/{job['id']}")
    assert head.startswith(cand.group(1)), f"verify.md tested {cand.group(1)}, branch head is {head}"


def _events(repo, job):
    lines = (repo / "factory" / "events.jsonl").read_text().splitlines()
    return [e for e in (json.loads(line) for line in lines if line.strip()) if e.get("job") == job["id"]]


def _check_scopes(repo, job):
    """Every agent the job ran was checked, and none overstepped."""
    scope = [e for e in _events(repo, job) if e["kind"] == "scope"]
    roles = {e["role"] for e in scope}
    assert {"spec-writer", "builder", "reviewers", "approver"} <= roles, f"scope checks seen: {sorted(roles)}"
    assert all(e["result"] == "ok" for e in scope), [e for e in scope if e["result"] != "ok"]


def _round_one(jobdir):
    """{reviewer file stem: (verdict, findings)} for round 1, and the triage text."""
    rnd = jobdir / "round-1"
    reviews = {}
    for stem in REVIEW_FILES:
        p = rnd / f"{stem}.md"
        if p.exists():
            lines = p.read_text().splitlines() + ["", ""]
            m = re.fullmatch(r"FINDINGS: (\d+)", lines[1].strip())
            assert m, f"round-1/{stem}.md line 2 is {lines[1]!r}, expected FINDINGS: <n>"
            reviews[stem] = (lines[0].strip(), int(m.group(1)))
    assert reviews, "no round-1 review files"
    return reviews, (rnd / "triage.md").read_text()


def test_clean_job_verifies_before_review(fx):
    repo = toy_project(fx)
    with Live(fx, repo) as live:
        job, jobdir = _run_one_job(fx, repo, live)
    _check_verify_md(fx, repo, job, jobdir)
    log = [(e["kind"], e.get("to") or e.get("result")) for e in _events(repo, job)]
    assert log.index(("verify", "PASS")) < log.index(("transition", "review")), log
    _check_scopes(repo, job)
    reviews, triage = _round_one(jobdir)
    clean = all(v == "VERDICT: PASS" and n == 0 for v, n in reviews.values())
    events = live.tool_events()
    roles = [role_of(e) for e in events]
    if clean:
        assert triage.startswith("TRIAGE: CLEAN") and "triager not run" in triage, "clean round was triaged by an agent"
        assert "triager" not in roles, "triager spawned on a clean round"
        print("  observed: clean round, triager skipped")
    else:
        assert "triager not run" not in triage, "skipped triage although a reviewer had findings"
        print(f"  observed: findings in round 1 ({reviews}), triager ran")
    reviewer_at = [e["ts"] for e, r in zip(events, roles) if r and r.startswith("reviewer-")]
    verify_writes = [e["ts"] for e in events
                     if (e["tool"] == "Write" and str(e["input"].get("file_path", "")).endswith("/verify.md"))
                     or (e["tool"] == "Bash" and "verify.md" in str(e["input"].get("command", ""))
                         and ">" in str(e["input"].get("command", "")))]
    if reviewer_at and verify_writes:
        assert min(verify_writes) < min(reviewer_at), "reviewers ran before verify.md was written"
    else:
        print("  note: transcripts did not show both the verify write and reviewer spawns; ordering not checked")


def test_red_build_gets_a_verify_fix_before_review(fx):
    sentinel = fx.root / "sentinel" / "verify-failed-once"
    repo = toy_project(fx, gate_sentinel=sentinel)
    with Live(fx, repo) as live:
        job, jobdir = _run_one_job(fx, repo, live)
    assert sentinel.exists(), "no test run ever happened outside a git checkout: the orchestrator did not verify " \
                              "from a git archive copy"
    assert "## Verify fix" in (jobdir / "build.md").read_text(), "builder was not given the verify fix"
    _check_verify_md(fx, repo, job, jobdir)
    log = [(e["kind"], e.get("to") or e.get("result")) for e in _events(repo, job)]
    fail, review = log.index(("verify", "FAIL")), log.index(("transition", "review"))
    assert fail < log.index(("verify", "PASS")) < review, f"reviewers started before the verify fix passed: {log}"
    assert ("transition", "build") in log[fail:review], log
    _check_scopes(repo, job)
    first_review = min((jobdir / "round-1" / f"{s}.md").stat().st_mtime
                       for s in REVIEW_FILES if (jobdir / "round-1" / f"{s}.md").exists())
    assert sentinel.stat().st_mtime < first_review, "a reviewer wrote its file before the failing verify"
    roles = [role_of(e) for e in live.tool_events()]
    if "builder" in roles and any(r and r.startswith("reviewer-") for r in roles):
        first_reviewer = next(i for i, r in enumerate(roles) if r and r.startswith("reviewer-"))
        assert roles[:first_reviewer].count("builder") >= 2, "reviewers spawned before the builder's verify fix"
    else:
        print("  note: transcripts did not show builder and reviewer spawns; ordering checked by file times only")

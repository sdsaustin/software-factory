"""Native git worktrees (FR-4), recover and cancel (FR-11). Scenario numbers refer to work/upgrade_spec.md §10.

The builder works in the job's own worktree outside the project; the project checkout never leaves the default branch.
"""
from helpers.jobs import JOB, SLUG_TEST, SLUGIFY, events, job, refused, review_files
from helpers.projects import TEST_COMMAND, toy_project


def building(fx, repo):
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify", "--item", "B1")
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    out = fx.ctl(repo, "worktree", "create", JOB).stdout
    assert out.startswith("WORKTREE: "), out
    return fx.root / ".software-factory-worktrees" / "proj" / JOB


def commit_in(fx, wt, files, message="build"):
    for rel, text in files.items():
        p = wt / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((p.read_text() if p.exists() else "") + text)
    fx.git(wt, "add", *files)
    fx.git(wt, "commit", "-q", "-m", message)


def primary_on_main(fx, repo):
    assert fx.git(repo, "branch", "--show-current") == "main", "the project checkout left the default branch"


def test_job_runs_in_its_worktree_and_the_checkout_stays_on_main(fx):
    repo = toy_project(fx)
    wt = building(fx, repo)
    assert wt.is_dir() and fx.git(wt, "branch", "--show-current") == f"factory/{JOB}"
    ws = job(repo)["workspace"]
    assert ws["kind"] == "worktree" and ws["path"] == str(wt.resolve())
    assert ws["base_commit"] == fx.git(repo, "rev-parse", "main")
    assert "(reused;" in fx.ctl(repo, "worktree", "create", JOB).stdout  # idempotent
    fx.ctl(repo, "snapshot", "--role", "builder", "--job", JOB)
    commit_in(fx, wt, {"textkit.py": SLUGIFY, "test_textkit.py": SLUG_TEST})
    (repo / "factory" / "jobs" / JOB / "build.md").write_text("# build\n")
    fx.ctl(repo, "scope-check", "--role", "builder", "--job", JOB)
    primary_on_main(fx, repo)
    fx.ctl(repo, "transition", JOB, "verify", "--reason", "built")
    fx.ctl(repo, "verify", JOB, "--test", TEST_COMMAND)
    fx.ctl(repo, "transition", JOB, "review", "--reason", "verified")
    refused(fx, repo, "worktree", "remove", JOB)  # kept while rework is possible
    review_files(repo, 1, {"security": ("PASS", 0), "code": ("PASS", 0)})
    fx.ctl(repo, "round", JOB)
    for stage in ("triage", "decision"):
        fx.ctl(repo, "transition", JOB, stage, "--reason", "next")
    fx.ctl(repo, "set", f"jobs.{JOB}.decision=APPROVE")
    fx.git(repo, "merge", "-q", "--no-ff", f"factory/{JOB}", "-m", "merge")
    fx.ctl(repo, "transition", JOB, "done", "--reason", "merged")
    assert "removed" in fx.ctl(repo, "worktree", "remove", JOB).stdout
    assert not wt.exists() and fx.git(repo, "rev-parse", "--verify", f"factory/{JOB}")  # branch kept
    assert job(repo)["workspace"]["path"] is None
    primary_on_main(fx, repo)
    actions = [e["action"] for e in events(repo) if e["kind"] == "worktree"]
    assert actions == ["created", "reused", "removed"], actions


def test_worktree_create_refuses_unsafe_states(fx):
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    refused(fx, repo, "worktree", "create", JOB)  # still in spec
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    target = fx.root / ".software-factory-worktrees" / "proj" / JOB
    target.mkdir(parents=True)
    (target / "notes.txt").write_text("someone else's files\n")
    assert "never reused" in refused(fx, repo, "worktree", "create", JOB)
    (target / "notes.txt").unlink()
    target.rmdir()
    other = fx.make_repo("other")  # a worktree of another repository at the job's path
    fx.git(other, "worktree", "add", "-q", "-b", "x", str(target))
    assert "never reused" in refused(fx, repo, "worktree", "create", JOB)
    fx.git(other, "worktree", "remove", str(target))
    fx.git(repo, "branch", f"factory/{JOB}")
    fx.git(repo, "worktree", "add", "-q", str(fx.root / "elsewhere"), f"factory/{JOB}")
    assert "already checked out" in refused(fx, repo, "worktree", "create", JOB)
    fx.git(repo, "worktree", "remove", str(fx.root / "elsewhere"))
    (repo / "wip.py").write_text("print('unsaved')\n")
    assert "uncommitted changes outside" in refused(fx, repo, "worktree", "create", JOB)
    (repo / "wip.py").unlink()
    fx.git(repo, "switch", "-q", "-c", "side")
    assert "switch back first" in refused(fx, repo, "worktree", "create", JOB)
    fx.git(repo, "switch", "-q", "main")
    fx.ctl(repo, "set", 'jobs.001-slugify.title=x')  # unrelated writes still work
    fx.ctl(repo, "worktree", "create", JOB)


def test_rewritten_branch_is_refused(fx):
    """Branch identity and base commit are validated before every builder run."""
    repo = toy_project(fx)
    wt = building(fx, repo)
    fx.git(wt, "checkout", "-q", "--orphan", "rewrite")
    fx.git(wt, "commit", "-q", "-m", "unrelated history")
    fx.git(wt, "branch", "-f", f"factory/{JOB}", "rewrite")
    fx.git(wt, "checkout", "-q", f"factory/{JOB}")
    assert "rewritten" in refused(fx, repo, "worktree", "create", JOB)


def test_interrupted_build_recovers_its_worktree(fx):
    """Scenario 22: the worktree directory vanished mid-build; recover explains, create repairs, commits survive."""
    import shutil
    repo = toy_project(fx)
    wt = building(fx, repo)
    commit_in(fx, wt, {"textkit.py": SLUGIFY})
    shutil.rmtree(wt)
    r = fx.ctl(repo, "worktree", "validate", JOB, check=False)
    assert r.returncode == 1 and r.stdout.startswith("MISSING")
    r = fx.ctl(repo, "recover", JOB, check=False)
    assert r.returncode == 1 and "AMBIGUOUS" in r.stdout and "confirm" in r.stdout  # its last event was moments ago
    out = fx.ctl(repo, "recover", JOB, "--confirmed").stdout
    assert f"worktree: {wt.resolve()} (missing)" in out and "NEXT: `worktree create`" in out
    assert "(repaired;" in fx.ctl(repo, "worktree", "create", JOB).stdout
    assert "def slugify" in (wt / "textkit.py").read_text(), "committed work must survive the crash"
    (repo / "factory" / "jobs" / JOB / "build.md").write_text("# build\n")
    assert "NEXT: transition to verify" in fx.ctl(repo, "recover", JOB, "--confirmed").stdout
    assert [e for e in events(repo) if e["kind"] == "recover"]


def test_recover_points_at_the_missing_step(fx):
    repo = toy_project(fx)
    wt = building(fx, repo)
    commit_in(fx, wt, {"textkit.py": SLUGIFY, "test_textkit.py": SLUG_TEST})
    fx.ctl(repo, "transition", JOB, "verify", "--reason", "built")
    fx.ctl(repo, "verify", JOB, "--test", TEST_COMMAND)
    fx.ctl(repo, "transition", JOB, "review", "--reason", "verified")
    review_files(repo, 1, {"security": ("PASS", 0)})
    out = fx.ctl(repo, "recover", JOB, "--confirmed").stdout
    assert "verification: fresh" in out and "spawn reviewer-code (Loop step 4)" in out


def test_dirty_and_orphan_worktrees_are_reported_never_removed(fx):
    """Scenario 23."""
    repo = toy_project(fx)
    wt = building(fx, repo)
    commit_in(fx, wt, {"textkit.py": SLUGIFY})
    fx.ctl(repo, "cancel", JOB, "--reason", "superseded")
    (wt / "experiment.py").write_text("unsaved idea\n")
    out = refused(fx, repo, "worktree", "remove", JOB)
    assert "never removed automatically" in out and (wt / "experiment.py").exists()
    orphan = fx.root / ".software-factory-worktrees" / "proj" / "099-orphan"
    fx.git(repo, "worktree", "add", "-q", "-b", "factory/099-orphan", str(orphan))
    doctor = fx.ctl(repo, "doctor", check=False).stdout
    assert "uncommitted changes: experiment.py" in doctor and "never removed automatically" in doctor
    assert "099-orphan belongs to no job" in doctor
    assert wt.exists() and orphan.exists(), "doctor is read-only"


def test_cancel_deletes_nothing_and_keeps_dependents_blocked(fx):
    """Scenario 25."""
    repo = toy_project(fx)
    (repo / "factory" / "backlog.md").write_text("# Backlog\n- [ ] B1 Slugify\n- [ ] B2 Slug URLs (depends: B1)\n")
    wt = building(fx, repo)
    commit_in(fx, wt, {"textkit.py": SLUGIFY})
    out = fx.ctl(repo, "cancel", JOB, "--reason", "not needed after all").stdout
    assert "kept: branch factory/001-slugify, worktree" in out and "Nothing was deleted" in out
    j = job(repo)
    assert j["stage"] == "cancelled" and j["execution"]["completed_at"]
    assert wt.exists() and fx.git(repo, "rev-parse", "--verify", f"factory/{JOB}")
    cancel = [e for e in events(repo) if e.get("to") == "cancelled"][0]
    assert cancel["actor"] == "human" and cancel["reason"] == "not needed after all"
    rows = {line.split()[1]: line.split()[0] for line in fx.ctl(repo, "ready").stdout.splitlines()}
    assert rows == {"B1": "CANCELLED", "B2": "BLOCKED"}
    refused(fx, repo, "transition", JOB, "build", "--reason", "revive")
    refused(fx, repo, "cancel", JOB, "--reason", "again")


def test_merge_conflict_is_reworked_in_the_worktree(fx):
    """Scenario 20: a conflict with the default branch parks the job; the rework merges main in the worktree."""
    repo = toy_project(fx)
    wt = building(fx, repo)
    commit_in(fx, wt, {"textkit.py": SLUGIFY, "test_textkit.py": SLUG_TEST})
    (repo / "textkit.py").write_text((repo / "textkit.py").read_text() + "\n\ndef slugify(text):\n    return text\n")
    fx.git(repo, "commit", "-q", "-am", "teammate's slugify")
    fx.ctl(repo, "transition", JOB, "verify", "--reason", "built")
    r = fx.ctl(repo, "verify", JOB, "--test", TEST_COMMAND, check=False)
    assert r.returncode == 4 and "textkit.py" in r.stdout
    fx.ctl(repo, "transition", JOB, "needs-human", "--reason", "VERIFY FAIL: conflicts with main: textkit.py")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "rework: merge main, keep both", "--actor", "human")
    fx.ctl(repo, "worktree", "create", JOB)
    fx.ctl(repo, "snapshot", "--role", "builder", "--job", JOB)
    assert fx.run(["git", "merge", "main"], cwd=wt, check=False).returncode != 0  # the conflict
    resolved = (repo / "textkit.py").read_text().split("\n\ndef slugify")[0] + SLUGIFY
    (wt / "textkit.py").write_text(resolved)
    fx.git(wt, "add", "textkit.py")
    fx.git(wt, "commit", "-q", "--no-edit")
    fx.ctl(repo, "scope-check", "--role", "builder", "--job", JOB)
    fx.ctl(repo, "transition", JOB, "verify", "--reason", "reworked")
    assert fx.ctl(repo, "verify", JOB, "--test", TEST_COMMAND).stdout.startswith("VERIFY: PASS")
    assert "\nTested: branch\n" in (repo / "factory" / "jobs" / JOB / "verify.md").read_text()
    primary_on_main(fx, repo)


def test_scope_checks_watch_the_worktree(fx):
    repo = toy_project(fx)
    wt = building(fx, repo)
    fx.ctl(repo, "snapshot", "--role", "builder", "--job", JOB)
    commit_in(fx, wt, {"textkit.py": SLUGIFY})
    (wt / "forgot.py").write_text("never committed\n")
    out = fx.ctl(repo, "scope-check", "--role", "builder", "--job", JOB, check=False).stdout
    assert "uncommitted changes left in the job worktree: forgot.py" in out
    (wt / "forgot.py").unlink()
    fx.ctl(repo, "transition", JOB, "needs-human", "--reason", "SCOPE VIOLATION: forgot.py")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "cleaned up", "--actor", "human")
    fx.ctl(repo, "transition", JOB, "verify", "--reason", "built")
    fx.ctl(repo, "snapshot", "--role", "approver", "--job", JOB)
    (wt / "textkit.py").write_text("# an approver should never touch this\n")
    out = fx.ctl(repo, "scope-check", "--role", "approver", "--job", JOB, check=False).stdout
    assert "changed the job worktree" in out and "textkit.py" in out

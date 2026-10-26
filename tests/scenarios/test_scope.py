"""Write-scope enforcement (FR-3): `snapshot` before an agent, `scope-check` after it.

Each scenario plays one agent by writing what it would write, then checks the verdict. A violation must block the
job, be logged, and leave every file where the agent put it. Scenario numbers refer to work/upgrade_spec.md §10.
"""
from helpers.jobs import JOB, SLUGIFY, build_commit, events, job, refused, review_files, to_verified
from helpers.projects import toy_project


def snap(fx, repo, role, *extra):
    fx.ctl(repo, "snapshot", "--role", role, *(["--job", JOB] if role not in ("planner", "retro") else []), *extra)


def check(fx, repo, role, *extra, ok=True):
    args = ["scope-check", "--role", role, *(["--job", JOB] if role not in ("planner", "retro") else []), *extra]
    r = fx.ctl(repo, *args, check=False)
    assert (r.returncode == 0) == ok, f"{role}: expected {'ok' if ok else 'a violation'}\n{r.stdout}{r.stderr}"
    return r.stdout


def in_review(fx, repo):
    to_verified(fx, repo)
    fx.ctl(repo, "transition", JOB, "review", "--reason", "verified")


def test_snapshot_lives_outside_the_work_tree(fx):
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    before = fx.git(repo, "status", "--porcelain", "--ignored", "-uall")
    snap(fx, repo, "spec-writer")
    assert fx.git(repo, "status", "--porcelain", "--ignored", "-uall") == before, "a snapshot must not touch the tree"
    out = check(fx, repo, "spec-writer")
    assert "scope: ok" in out
    refused(fx, repo, "scope-check", "--role", "spec-writer", "--job", JOB)  # the snapshot is used up


def test_spec_writer_may_write_only_its_spec(fx):
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    snap(fx, repo, "spec-writer")
    (repo / "factory" / "jobs" / JOB / "spec.md").write_text("# Add slugify\nTrack: quick\n")
    check(fx, repo, "spec-writer")
    snap(fx, repo, "spec-writer")
    (repo / "factory" / "jobs" / JOB / "notes.md").write_text("scratch\n")
    out = check(fx, repo, "spec-writer", ok=False)
    assert "changed factory/jobs/001-slugify/notes.md (untracked after clean)" in out


def test_reviewer_source_edit_blocks_the_job(fx):
    """Scenario 12: a reviewer that edits source code fails the step; the job can only be parked."""
    repo = toy_project(fx)
    in_review(fx, repo)
    snap(fx, repo, "reviewers")
    review_files(repo, 1, {"security": ("PASS", 0), "code": ("PASS", 0)})
    (repo / "textkit.py").write_text((repo / "textkit.py").read_text() + "\n# reviewer fixed it\n")
    out = check(fx, repo, "reviewers", ok=False)
    assert "changed textkit.py" in out and "Nothing was deleted" in out
    assert "# reviewer fixed it" in (repo / "textkit.py").read_text(), "the validator must never revert files"
    j = job(repo)
    assert j["execution"]["blocked"].startswith("reviewers: changed textkit.py")
    refused(fx, repo, "transition", JOB, "triage", "--reason", "carry on anyway")
    fx.ctl(repo, "transition", JOB, "needs-human", "--reason", "SCOPE VIOLATION: reviewer edited textkit.py")
    scope = [e for e in events(repo) if e["kind"] == "scope"]
    assert scope[-1]["result"] == "violation" and scope[-1]["role"] == "reviewers"
    # The human looks, cleans up and sends it back: the block clears.
    fx.git(repo, "checkout", "--", "textkit.py")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "rework after scope violation", "--actor", "human")
    assert job(repo)["execution"]["blocked"] is None


def test_parallel_reviewers_are_checked_as_one_batch(fx):
    repo = toy_project(fx)
    in_review(fx, repo)  # quick track: security and code run, ux and ui are N/A
    snap(fx, repo, "reviewers")
    review_files(repo, 1, {"security": ("PASS", 0), "code": ("CHANGES", 1)})
    check(fx, repo, "reviewers")
    snap(fx, repo, "reviewers")
    review_files(repo, 1, {"ui": ("PASS", 0)})
    out = check(fx, repo, "reviewers", ok=False)
    assert "round-1/ui.md" in out, "a reviewer that does not run on this track may not write a file"


def test_board_edit_by_an_agent_is_caught(fx):
    """A non-orchestrator board edit fails the step, even on a board that was already uncommitted."""
    repo = toy_project(fx)
    in_review(fx, repo)
    assert "factory/board.json" in fx.git(repo, "status", "--porcelain")  # dirty before the agent runs
    fx.ctl(repo, "transition", JOB, "triage", "--reason", "round done")
    snap(fx, repo, "triager")
    (repo / "factory" / "jobs" / JOB / "round-1" / "triage.md").write_text("TRIAGE: CLEAN\n")
    board = (repo / "factory" / "board.json").read_text()
    (repo / "factory" / "board.json").write_text(board.replace('"triage": "pending"', '"triage": "CLEAN"', 1))
    out = check(fx, repo, "triager", ok=False)
    assert "changed factory/board.json" in out


def test_dashboard_clicks_and_ignored_files_are_not_violations(fx):
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    snap(fx, repo, "spec-writer")
    (repo / "factory" / "jobs" / JOB / "spec.md").write_text("# spec\n")
    (repo / "factory" / "actions.jsonl").write_text('{"id":"a1","type":"approve"}\n')  # server.py, not the agent
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "textkit.cpython.pyc").write_bytes(b"\0")  # ignored by .gitignore
    (repo / ".DS_Store").write_bytes(b"\0")
    check(fx, repo, "spec-writer")


def test_builder_valid_build_passes(fx):
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    snap(fx, repo, "builder")
    build_commit(fx, repo, {"textkit.py": SLUGIFY})
    (repo / "factory" / "jobs" / JOB / "build.md").write_text("# build\n")
    assert "scope: ok (builder on 001-slugify)" in check(fx, repo, "builder")


def _building(fx, repo, docs_line="none"):
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    (repo / "factory" / "jobs" / JOB / "spec.md").write_text(f"# spec\nDocs files: {docs_line}\n")
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    snap(fx, repo, "builder")


def test_builder_protected_files_on_the_branch(fx):
    """Scenario 13: factory state and undeclared docs never ride on a job branch."""
    repo = toy_project(fx)
    _building(fx, repo, docs_line="`docs/usage.md`")
    build_commit(fx, repo, {"textkit.py": SLUGIFY, "docs/usage.md": "slugify(text)\n"})
    check(fx, repo, "builder")  # docs/usage.md is on the spec's Docs files: line
    snap(fx, repo, "builder")
    build_commit(fx, repo, {"docs/architecture.md": "rewritten\n", "factory/learnings.md": "- builder note\n"},
                 message="sneak")
    out = check(fx, repo, "builder", ok=False)
    assert "changes docs/architecture.md" in out and "changes factory/learnings.md" in out


def test_builder_must_not_touch_the_default_branch(fx):
    repo = toy_project(fx)
    _building(fx, repo)
    build_commit(fx, repo, {"textkit.py": SLUGIFY})
    (repo / "README.md").write_text("edited on main\n")
    fx.git(repo, "commit", "-q", "-am", "builder committed on main")
    (repo / "scratch.txt").write_text("left behind\n")
    fx.git(repo, "tag", "v9")
    out = check(fx, repo, "builder", ok=False)
    assert "moved branch main" in out and "changed scratch.txt (untracked after clean)" in out and "created tag v9" in out


def test_builder_leaving_the_job_branch_checked_out(fx):
    repo = toy_project(fx)
    _building(fx, repo)
    build_commit(fx, repo, {"textkit.py": SLUGIFY})
    fx.git(repo, "switch", "-q", f"factory/{JOB}")
    out = check(fx, repo, "builder", ok=False)
    assert f"left factory/{JOB} checked out (was main)" in out


def test_planner_scopes_follow_the_pass(fx):
    repo = toy_project(fx)
    (repo / "docs").mkdir()
    snap(fx, repo, "planner", "--pass", "1")
    (repo / "docs" / "vision.md").write_text("# Vision\n")
    (repo / "docs" / "open-questions.md").write_text("QUESTIONS: 0\n")
    check(fx, repo, "planner", "--pass", "1")
    snap(fx, repo, "planner", "--pass", "1")
    (repo / "docs" / "epics.md").write_text("# Epics\n")
    assert "changed docs/epics.md" in check(fx, repo, "planner", "--pass", "1", ok=False)
    (repo / "docs" / "epics.md").unlink()
    snap(fx, repo, "planner", "--pass", "3", "--feature", "tags")
    (repo / "docs" / "features" / "tags").mkdir(parents=True)
    (repo / "docs" / "features" / "tags" / "claude-md-proposal.md").write_text("NONE\n")
    (repo / "factory" / "backlog.md").write_text((repo / "factory" / "backlog.md").read_text() + "- [ ] B2 Tags\n")
    check(fx, repo, "planner", "--pass", "3", "--feature", "tags")
    snap(fx, repo, "planner", "--pass", "2", "--feature", "tags")
    (repo / "docs" / "vision.md").write_text("# Vision, rewritten\n")
    assert "changed docs/vision.md" in check(fx, repo, "planner", "--pass", "2", "--feature", "tags", ok=False)
    refused(fx, repo, "snapshot", "--role", "planner", "--pass", "4")
    refused(fx, repo, "snapshot", "--role", "planner")


def test_retro_and_missing_snapshot(fx):
    repo = toy_project(fx)
    snap(fx, repo, "retro")
    (repo / "factory" / "retro-proposal.md").write_text("# Retro\n")
    check(fx, repo, "retro")
    refused(fx, repo, "scope-check", "--role", "approver", "--job", JOB)  # no such job
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    out = refused(fx, repo, "scope-check", "--role", "approver", "--job", JOB)
    assert "no snapshot" in out
    refused(fx, repo, "snapshot", "--role", "builder")  # a job role needs --job

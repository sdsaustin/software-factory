"""factory/factoryctl.py: board schema, transitions, verification bound to a commit, events, readiness, migration.

Agents are replaced by the files they would write (review, triage, a builder commit), so the job loop's state
machine runs deterministically. Scenario numbers refer to section 10 of work/upgrade_spec.md.
"""
import json
import re
import shutil

from helpers import FIXTURES, read_json, tree_digest, write_json
from helpers.projects import TEST_COMMAND, toy_project

JOB = "001-slugify"
TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
SLUGIFY = ('\n\ndef slugify(text):\n    """Lowercase words joined by single hyphens, punctuation dropped."""\n'
           '    import re\n    return "-".join(re.findall(r"[a-z0-9]+", text.lower()))\n')
SLUG_TEST = ("\n\nclass SlugifyTest(unittest.TestCase):\n    def test_slugify(self):\n"
             "        from textkit import slugify\n        self.assertEqual(slugify('Hello, World!'), 'hello-world')\n")


# ---------------------------------------------------------------- helpers

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


# ---------------------------------------------------------------- scenarios

def test_fresh_project_passes_check_and_doctor_without_writing(fx):
    """Scenario 1: what init lays out is a valid v2 project."""
    repo = toy_project(fx)
    assert fx.ctl(repo, "check").stdout.startswith("board: ok (version 2, 0 job(s))")
    before = tree_digest(repo, ["factory", "CLAUDE.md", ".gitignore"])
    r = fx.ctl(repo, "doctor", check=False)
    assert r.returncode == 0, r.stdout
    assert "FAIL" not in r.stdout
    assert tree_digest(repo, ["factory", "CLAUDE.md", ".gitignore"]) == before, "doctor must be read-only"
    assert fx.git(repo, "status", "--porcelain") == ""


def test_quick_track_job_end_to_end(fx):
    """Scenario 9: spec -> build -> verify -> review (clean) -> triage skipped -> decision -> merge -> done."""
    repo = toy_project(fx)
    to_verified(fx, repo)
    j = job(repo)
    head = fx.git(repo, "rev-parse", f"factory/{JOB}")
    assert j["stage"] == "verify" and j["evidence"]["verified_commit"] == head
    verify_md = (repo / "factory" / "jobs" / JOB / "verify.md").read_text()
    assert verify_md.startswith(f"VERIFY: PASS\nCandidate: factory/{JOB} @ {head}\nDefault: main @ ")
    assert "\nTested: branch\n" in verify_md and f"`{TEST_COMMAND}`" in verify_md and "exit 0" in verify_md
    assert "slugify" in (repo / "factory" / "jobs" / JOB / "branch.diff").read_text()

    out = fx.ctl(repo, "transition", JOB, "review", "--reason", "VERIFY: PASS").stdout
    assert "round 1: spawn reviewer-security, reviewer-code in parallel" in out
    assert job(repo)["reviews"]["1"] == {"security": "pending", "ux": "N/A", "ui": "N/A", "code": "pending",
                                         "triage": "pending"}
    review_files(repo, 1, {"security": ("PASS", 0), "code": ("PASS", 0)})
    out = fx.ctl(repo, "round", JOB).stdout
    assert out.strip().endswith("NEXT: CLEAN (triager not run)"), out
    triage = (repo / "factory" / "jobs" / JOB / "round-1" / "triage.md").read_text()
    assert triage.startswith("TRIAGE: CLEAN\nOrchestrator: 2 reviewers PASS with 0 findings; triager not run.")
    assert job(repo)["reviews"]["1"]["triage"] == "CLEAN"

    fx.ctl(repo, "transition", JOB, "triage", "--reason", "round 1 complete")
    fx.ctl(repo, "transition", JOB, "decision", "--reason", "TRIAGE: CLEAN")
    refused(fx, repo, "transition", JOB, "done", "--reason", "too early")  # no APPROVE yet
    fx.ctl(repo, "set", f"jobs.{JOB}.decision=APPROVE")
    refused(fx, repo, "transition", JOB, "done", "--reason", "not merged")  # branch not merged
    fx.git(repo, "merge", "-q", "--no-ff", f"factory/{JOB}", "-m", "merge")
    fx.ctl(repo, "transition", JOB, "done", "--reason", "merged")

    b = board(repo)
    j = b["jobs"][0]
    assert j["stage"] == "done" and j["round"] == 1 and j["execution"]["attempt"] == 1
    assert j["execution"]["completed_at"] and b["retro"]["done_since"] == 1
    kinds = [(e["kind"], e.get("to")) for e in events(repo)]
    assert kinds == [("created", "spec"), ("transition", "build"), ("transition", "verify"), ("verify", None),
                     ("transition", "review"), ("transition", "triage"), ("transition", "decision"),
                     ("transition", "done")], kinds
    for e in events(repo):
        assert TS.match(e["at"]), e
    for stamp in (j["updated"], j["execution"]["started_at"], j["evidence"]["verified_at"]):
        assert TS.match(stamp), stamp


def test_full_track_respects_skipped_reviewers(fx):
    """Scenario 10: full track runs every reviewer except an allowed ux/ui skip."""
    repo = toy_project(fx)
    to_verified(fx, repo, track="full", extra=[f'jobs.{JOB}.skip_reviewers=["ui"]'])
    out = fx.ctl(repo, "transition", JOB, "review", "--reason", "verified").stdout
    assert "spawn reviewer-security, reviewer-ux, reviewer-code" in out
    assert job(repo)["reviews"]["1"]["ui"] == "N/A"
    refused(fx, repo, "set", f'jobs.{JOB}.skip_reviewers=["security"]')  # security and code are never skipped


def test_findings_go_to_the_triager_then_a_patch_round(fx):
    """Scenarios 14 and 19: a finding means triage; a patch moves the head, so verification must run again."""
    repo = toy_project(fx)
    to_verified(fx, repo)
    fx.ctl(repo, "transition", JOB, "review", "--reason", "verified")
    review_files(repo, 1, {"security": ("PASS", 0), "code": ("CHANGES", 1)})
    assert fx.ctl(repo, "round", JOB).stdout.strip().endswith("NEXT: spawn triager")
    assert not (repo / "factory" / "jobs" / JOB / "round-1" / "triage.md").exists()
    review_files(repo, 1, {"security": ("PASS", None)})  # no FINDINGS line: never a skip
    (repo / "factory" / "jobs" / JOB / "round-1" / "triage.md").write_text("TRIAGE: PATCH\n| code | ... |\n")
    assert fx.ctl(repo, "round", JOB).stdout.strip().endswith("NEXT: PATCH")
    fx.ctl(repo, "transition", JOB, "triage", "--reason", "round 1 complete")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "TRIAGE: PATCH")
    build_commit(fx, repo, {"textkit.py": "\n# patched\n"}, message="patch")
    fx.ctl(repo, "transition", JOB, "verify", "--reason", "patched")
    out = refused(fx, repo, "transition", JOB, "review", "--reason", "skip verify")
    assert "branch moved since verification" in out
    assert fx.ctl(repo, "fresh", JOB, check=False).returncode == 1
    fx.ctl(repo, "verify", JOB, "--test", TEST_COMMAND)
    assert fx.ctl(repo, "fresh", JOB).stdout.startswith("FRESH:")
    out = fx.ctl(repo, "transition", JOB, "review", "--reason", "verified").stdout
    assert "round 2:" in out and job(repo)["execution"]["attempt"] == 1


def test_malformed_review_is_rerun_then_parked_as_agent_failure(fx):
    """Scenario 11: a malformed first line asks for one re-run; a second failure parks the job."""
    repo = toy_project(fx)
    to_verified(fx, repo)
    fx.ctl(repo, "transition", JOB, "review", "--reason", "verified")
    review_files(repo, 1, {"security": ("PASS", 0), "code": "Looks fine to me.\n"})
    r = fx.ctl(repo, "round", JOB, check=False)
    assert r.returncode == 1 and r.stdout.strip().endswith("NEXT: rerun reviewer-code"), r.stdout
    assert job(repo)["reviews"]["1"]["code"] == "pending"
    fx.ctl(repo, "event", JOB, "--kind", "agent-failure",
           "--reason", "reviewer-code wrote no verdict twice; token=abc123 leaked\x1b[31m")
    fx.ctl(repo, "transition", JOB, "needs-human", "--reason", "AGENT FAILURE: reviewer-code wrote no verdict twice")
    j = job(repo)
    assert j["stage"] == "needs-human" and j["parked_reason"].startswith("AGENT FAILURE:")
    assert "abc123" not in j["execution"]["last_error"] and "\x1b" not in j["execution"]["last_error"]
    assert "abc123" not in (repo / "factory" / "events.jsonl").read_text(), "events must not carry secrets"


def test_parking_and_human_rework_with_replayed_action(fx):
    """Scenarios 15, 16 and 24: an open decision parks; a human action resumes once, a replay does nothing."""
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    refused(fx, repo, "transition", JOB, "needs-human", "--reason", "it needs a human")  # no reason code
    fx.ctl(repo, "transition", JOB, "needs-human", "--reason", "DECISION: unicode handling in slugs")
    assert job(repo)["parked_reason"] == "DECISION: unicode handling in slugs"
    refused(fx, repo, "transition", JOB, "build", "--reason", "answered")  # no track yet
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "answered: drop non-ASCII",
           "--action-id", "a1b2", "--actor", "human")
    j = job(repo)
    assert j["stage"] == "build" and j["parked_reason"] is None and j["execution"]["attempt"] == 1
    before = state(repo)
    r = fx.ctl(repo, "transition", JOB, "build", "--reason", "again", "--action-id", "a1b2", "--actor", "human")
    assert "already applied" in r.stdout and state(repo) == before
    human = [e for e in events(repo) if e.get("actor") == "human"]
    assert len(human) == 1 and human[0]["action_id"] == "a1b2"


def test_failed_verification_blocks_review(fx):
    """Scenario 18: a red build never reaches review; the builder gets it back."""
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    build_commit(fx, repo, {"test_textkit.py": "\n\nclass Broken(unittest.TestCase):\n    def test_x(self):\n"
                                               "        self.assertEqual(1, 2)\n"})
    fx.ctl(repo, "transition", JOB, "verify", "--reason", "built")
    r = fx.ctl(repo, "verify", JOB, "--test", TEST_COMMAND, check=False)
    assert r.returncode == 1 and r.stdout.startswith("VERIFY: FAIL")
    j = job(repo)
    assert j["evidence"]["verified_commit"] is None and j["evidence"]["head_commit"]
    assert "exit 1" in (repo / "factory" / "jobs" / JOB / "verify.md").read_text()
    refused(fx, repo, "transition", JOB, "review", "--reason", "ignore the failure")
    refused(fx, repo, "transition", JOB, "decision", "--reason", "ignore the failure")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "VERIFY: FAIL, verify fix")


def test_command_that_cannot_start_is_not_a_job_failure(fx):
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    build_commit(fx, repo, {"textkit.py": SLUGIFY})
    fx.ctl(repo, "transition", JOB, "verify", "--reason", "built")
    out = refused(fx, repo, "verify", JOB, "--test", "no-such-tool-xyz --run", code=3)
    assert "could not start" in out
    refused(fx, repo, "verify", JOB)  # no test command given


def test_default_branch_code_means_merge_result_and_conflicts_are_reported(fx):
    """Stale branch: new code on the default branch is verified as the merge result; a conflict exits 4."""
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    build_commit(fx, repo, {"textkit.py": SLUGIFY, "test_textkit.py": SLUG_TEST})
    (repo / "notes.txt").write_text("unrelated code change\n")
    fx.git(repo, "add", "notes.txt")
    fx.git(repo, "commit", "-q", "-m", "teammate change")
    fx.ctl(repo, "transition", JOB, "verify", "--reason", "built")
    fx.ctl(repo, "verify", JOB, "--test", TEST_COMMAND)
    text = (repo / "factory" / "jobs" / JOB / "verify.md").read_text()
    assert "\nTested: merge result\n" in text and "notes.txt" in text
    assert fx.git(repo, "worktree", "list").count("\n") == 0, "the trial-merge worktree was left behind"

    # Factory state commits on the default branch never make evidence stale; code commits do.
    fx.ctl(repo, "set", "retro.last=2026-10-01")
    fx.git(repo, "add", "factory")
    fx.git(repo, "commit", "-q", "-m", "factory: state")
    assert fx.ctl(repo, "fresh", JOB).returncode == 0
    (repo / "textkit.py").write_text((repo / "textkit.py").read_text() + "\n\ndef slugify(text):\n    return text\n")
    fx.git(repo, "commit", "-q", "-am", "teammate slugify")
    out = fx.ctl(repo, "fresh", JOB, check=False).stdout
    assert out.startswith("STALE:") and "textkit.py" in out
    r = fx.ctl(repo, "verify", JOB, "--test", TEST_COMMAND, check=False)
    assert r.returncode == 4 and "Conflicts: textkit.py" in r.stdout
    assert job(repo)["evidence"]["verified_commit"] is None
    assert fx.git(repo, "branch", "--show-current") == "main" and fx.git(repo, "worktree", "list").count("\n") == 0


def test_invalid_transitions_change_nothing(fx):
    """FR-2: disallowed transitions fail without changing the board or the log."""
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    refused(fx, repo, "transition", JOB, "review", "--reason", "skip the build")
    refused(fx, repo, "transition", JOB, "done", "--reason", "skip everything")
    refused(fx, repo, "transition", JOB, "spec", "--reason", "same stage")
    refused(fx, repo, "transition", JOB, "shipped", "--reason", "unknown stage")
    refused(fx, repo, "transition", "999-ghost", "build", "--reason", "unknown job")
    fx.ctl(repo, "transition", JOB, "cancelled", "--reason", "no longer needed", "--actor", "human")
    out = refused(fx, repo, "transition", JOB, "build", "--reason", "revive")
    assert "terminal" in out
    refused(fx, repo, "set", f"jobs.{JOB}.stage=build")
    refused(fx, repo, "set", f"jobs.{JOB}.evidence=null")
    refused(fx, repo, "set", f"jobs.{JOB}.track=medium")
    refused(fx, repo, "job-new", JOB, "--title", "duplicate")
    refused(fx, repo, "job-new", "Bad_ID", "--title", "bad id")


def test_set_validates_top_level_fields(fx):
    repo = toy_project(fx)
    fx.ctl(repo, "set", 'run={"status":"running","started":"2026-10-26T10:00:00+00:00","max_jobs":null,'
                        '"budget_minutes":null,"jobs_done":0,"consecutive_failures":0,"stop_reason":null,"current":null}')
    fx.ctl(repo, "set", "run.current=B1", "run.jobs_done+=1", "run.jobs_done+=1")
    fx.ctl(repo, "set", 'features+={"slug":"tags","items":["B3"]}', "document=running", "default_branch=main")
    b = board(repo)
    assert b["run"]["jobs_done"] == 2 and b["run"]["current"] == "B1" and b["features"] == [{"slug": "tags", "items": ["B3"]}]
    refused(fx, repo, "set", "run.status=paused")
    refused(fx, repo, "set", "run.started=2026-10-26 10:00")  # not UTC ISO 8601 with offset
    refused(fx, repo, "set", "retro.done_since=-1")
    refused(fx, repo, "set", "version=3")
    refused(fx, repo, "set", "plan.status=running")  # plan does not exist yet
    fx.ctl(repo, "set", 'plan={"mode":"product","feature":null,"pass":1,"status":"running"}')
    refused(fx, repo, "set", "plan.pass=4")


def test_malformed_and_newer_boards_stop_every_command(fx):
    """FR-1 and scenario 5: a malformed or newer board stops with a diagnostic and is never rewritten."""
    repo = toy_project(fx)
    path = repo / "factory" / "board.json"
    path.write_text('{"version": 2, "jobs": [')
    for args in (["check"], ["status"], ["set", "document=running"], ["job-new", JOB, "--title", "x"], ["ready"],
                 ["migrate", "--apply"]):
        out = refused(fx, repo, *args)
        assert "not valid JSON" in out, (args, out)
    newer = read_json(FIXTURES / "boards" / "newer-than-plugin.json")
    newer["version"] = 3
    write_json(path, newer)
    for args in (["check"], ["set", "document=running"], ["migrate", "--apply"]):
        out = refused(fx, repo, *args)
        assert "newer" in out, (args, out)
    write_json(path, {"version": 2, "jobs": [{"id": JOB, "stage": "shipping"}], "retro": {"done_since": 0, "last": None},
                      "run": None})
    out = refused(fx, repo, "check")
    assert "bad stage" in out and "missing title" in out


def _v1_project(fx, fixture):
    repo = toy_project(fx)
    factory = repo / "factory"
    old = read_json(FIXTURES / "boards" / fixture)
    old["team_notes"] = "kept by every upgrade"
    old["jobs"][0]["custom"] = {"owner": "platform team"}
    write_json(factory / "board.json", old)
    (factory / "config.json").unlink()
    (factory / "events.jsonl").unlink()
    return repo, old


def test_migrate_pre_1_8_board(fx):
    """Scenarios 3 and 4: v1 -> v2 keeps every job's stage and decision and unknown fields; a re-run changes nothing."""
    repo, old = _v1_project(fx, "pre-1.8.json")
    before = state(repo)
    out = fx.ctl(repo, "migrate").stdout
    assert "would: version 1 -> 2" in out and "would: create config.json" in out
    assert state(repo) == before, "a dry run must not write"
    fx.ctl(repo, "migrate", "--apply")
    new = board(repo)
    assert new["version"] == 2 and new["run"] is None and new["retro"] == {"done_since": 0, "last": None}
    assert new["team_notes"] == "kept by every upgrade"
    for was, now in zip(old["jobs"], new["jobs"]):
        assert (now["id"], now["stage"], now["decision"]) == (was["id"], was["stage"], was["decision"])
        for key, value in was.items():
            assert now[key] == value, (now["id"], key)
        assert now["workspace"]["kind"] == "legacy-checkout" and now["evidence"]["verified_commit"] is None
    assert new["jobs"][0]["custom"] == {"owner": "platform team"}
    assert read_json(repo / "factory" / "config.json")["run"]["parallel_jobs"] == 1
    assert [e["kind"] for e in events(repo)] == ["migrate"]
    assert fx.ctl(repo, "check").returncode == 0
    after = state(repo)
    assert fx.ctl(repo, "migrate", "--apply").stdout.strip() == "up to date: nothing to migrate"
    assert state(repo) == after, "a second upgrade must produce no diff"


def test_migrate_in_flight_1_8_board_and_partial_config(fx):
    repo, old = _v1_project(fx, "v1.8-in-flight.json")
    write_json(repo / "factory" / "config.json", {"version": 1, "run": {"max_consecutive_failures": 3},
                                                  "team": {"channel": "#factory"}})
    fx.ctl(repo, "migrate", "--apply")
    j = board(repo)["jobs"][0]
    assert j["stage"] == "review" and j["reviews"] == old["jobs"][0]["reviews"]
    assert board(repo)["run"] == old["run"], "a run in flight is kept as it was"
    cfg = read_json(repo / "factory" / "config.json")
    assert cfg["run"] == {"max_consecutive_failures": 3, "parallel_jobs": 1}, "user values win; missing ones added"
    assert cfg["team"] == {"channel": "#factory"} and cfg["verification"]["run_test"] is True


def test_config_validation(fx):
    """FR-10: invalid values stop with a field-specific message; secrets are refused; unknown fields are fine."""
    repo = toy_project(fx)
    path = repo / "factory" / "config.json"
    good = read_json(path)
    for patch, message in [
        ({"run": {"parallel_jobs": 2, "max_consecutive_failures": 2}}, "run.parallel_jobs: must be 1"),
        ({"verification": {"run_test": "yes", "run_lint": True, "run_build": True}}, "verification.run_test"),
        ({"workspace": {"mode": "cursor", "root": "auto", "retain_completed": False}}, "workspace.mode"),
        ({"workspace": {"mode": "worktree", "root": str(repo / "wt"), "retain_completed": False}},
         "workspace.root: must not be inside the project"),
        ({"github_token": "abc"}, "secrets are not allowed"),
    ]:
        write_json(path, dict(good, **patch))
        out = refused(fx, repo, "check")
        assert message in out, (patch, out)
        refused(fx, repo, "set", "document=running")
    write_json(path, dict(good, team={"channel": "#factory"}))
    assert fx.ctl(repo, "check").returncode == 0


def test_events_survive_a_truncated_last_line(fx):
    """FR-2: a truncated final line is reported and skipped; new events start on a fresh line."""
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    log = repo / "factory" / "events.jsonl"
    log.write_text(log.read_text() + '{"at": "2026-10-26T10:00:00+00:00", "kind": "transi')
    assert "truncated last line" in fx.ctl(repo, "check").stdout
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    lines = log.read_text().splitlines()
    assert json.loads(lines[0])["kind"] == "created" and json.loads(lines[-1])["to"] == "build"
    assert "line 2 is not valid JSON" in fx.ctl(repo, "check").stdout


def test_unknown_fields_survive_writes(fx):
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    b = board(repo)
    b["future_field"] = {"x": 1}
    b["jobs"][0]["future_job_field"] = [1, 2]
    write_json(repo / "factory" / "board.json", b)
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    b = board(repo)
    assert b["future_field"] == {"x": 1} and b["jobs"][0]["future_job_field"] == [1, 2]


def test_ready_respects_dependencies(fx):
    """Scenario 21 and FR-7: a parked job blocks only its direct and transitive dependents."""
    repo = toy_project(fx)
    (repo / "factory" / "backlog.md").write_text(
        "# Backlog\n<!-- header -->\n"
        "- [x] B1 Scaffold\n"
        "- [ ] B2 Parser (depends: B1)\n"
        "- [ ] B3 Formatter (depends: B2)\n"
        "- [ ] B4 CLI (depends: B3)\n"
        "- [ ] B5 Docs page (depends: B1)\n"
        "- [ ] B6 Settings\n"
        "- [ ] B7 Loop A (depends: B8)\n"
        "- [ ] B8 Loop B (depends: B7)\n")
    fx.ctl(repo, "job-new", "002-parser", "--title", "Parser", "--item", "B2")
    fx.ctl(repo, "transition", "002-parser", "needs-human", "--reason", "DECISION: grammar choice")
    fx.ctl(repo, "job-new", "003-docs", "--title", "Docs page", "--item", "B5")
    rows = {line.split()[1]: line.split()[0] for line in fx.ctl(repo, "ready").stdout.splitlines()}
    assert rows == {"B1": "DONE", "B2": "PARKED", "B3": "BLOCKED", "B4": "BLOCKED", "B5": "ACTIVE",
                    "B6": "READY", "B7": "BLOCKED", "B8": "BLOCKED"}, rows
    assert fx.ctl(repo, "ready", "--first").stdout.strip() == "B6 B6 Settings"
    assert "dependency cycle: B7 -> B8 -> B7" in fx.ctl(repo, "ready").stdout
    (repo / "factory" / "backlog.md").write_text("# Backlog\n- [x] B1 Scaffold\n")
    r = fx.ctl(repo, "ready", "--first", check=False)
    assert r.returncode == 1 and r.stdout.strip() == "none"


def test_ready_numbers_unnumbered_lines_by_position(fx):
    repo = toy_project(fx)
    (repo / "factory" / "backlog.md").write_text("# Backlog\n- [x] Scaffold\n- [ ] Parser\n")
    assert fx.ctl(repo, "ready", "--first").stdout.strip() == "B2 Parser"


def test_doctor_reports_without_changing_anything(fx):
    """FR-11: doctor is read-only and gives a repair command per finding."""
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    fx.git(repo, "branch", "factory/099-orphan")
    (repo / "scratch.py").write_text("print('wip')\n")
    (repo / "factory" / "actions.jsonl").write_text('{"id":"x1","type":"approve","job":"001-slugify"}\n')
    before = tree_digest(repo, ["factory", "CLAUDE.md", "scratch.py"])
    head = fx.git(repo, "rev-parse", "HEAD")
    r = fx.ctl(repo, "doctor", check=False)
    assert tree_digest(repo, ["factory", "CLAUDE.md", "scratch.py"]) == before
    assert fx.git(repo, "rev-parse", "HEAD") == head
    out = r.stdout
    assert "factory/099-orphan has no job" in out
    assert "uncommitted changes outside factory/" in out and "scratch.py" in out
    assert "1 dashboard action(s) pending" in out and "fix: /software-factory:factory apply" in out
    assert f"{JOB} is mid-loop in build" in out
    fx.git(repo, "switch", "-q", "-c", "elsewhere")
    r = fx.ctl(repo, "doctor", check=False)
    assert r.returncode == 1 and "checked out elsewhere, not main" in r.stdout and "fix: git switch main" in r.stdout


def test_branch_diff_never_cuts_source(fx):
    """The approver's diff keeps every source file whole and cuts only tests and generated files."""
    repo = toy_project(fx)
    fx.ctl(repo, "job-new", JOB, "--title", "Add slugify")
    fx.ctl(repo, "set", f"jobs.{JOB}.track=quick")
    fx.ctl(repo, "transition", JOB, "build", "--reason", "spec ready")
    big = "".join(f"CASE_{i} = {i}\n" for i in range(3500))
    build_commit(fx, repo, {"textkit.py": SLUGIFY, "tests/fixtures/cases.py": big})
    fx.ctl(repo, "transition", JOB, "verify", "--reason", "built")
    fx.ctl(repo, "verify", JOB, "--test", TEST_COMMAND)
    diff = (repo / "factory" / "jobs" / JOB / "branch.diff").read_text()
    assert "def slugify" in diff and "CASE_3499" not in diff
    assert "# Cut from this file" in diff and "tests/fixtures/cases.py" in diff


def test_copy_matches_template(fx):
    """The tool a project runs is the plugin's template, byte for byte (init copies it as a plugin-owned file)."""
    repo = toy_project(fx)
    from helpers import FACTORY_TEMPLATES
    assert (repo / "factory" / "factoryctl.py").read_bytes() == (FACTORY_TEMPLATES / "factoryctl.py").read_bytes()
    shutil.rmtree(repo / "factory" / "jobs")
    assert fx.ctl(repo, "check").returncode == 0


def test_pass_with_findings_still_goes_to_the_triager(fx):
    """Item 1's rule is PASS *and* FINDINGS: 0 for every reviewer; a PASS that lists findings is triaged."""
    repo = toy_project(fx)
    to_verified(fx, repo)
    fx.ctl(repo, "transition", JOB, "review", "--reason", "verified")
    review_files(repo, 1, {"security": ("PASS", 2), "code": ("PASS", 0)})
    assert fx.ctl(repo, "round", JOB).stdout.strip().endswith("NEXT: spawn triager")
    assert not (repo / "factory" / "jobs" / JOB / "round-1" / "triage.md").exists()
    assert job(repo)["reviews"]["1"]["triage"] == "pending"


def test_migrate_refuses_a_job_without_a_stage(fx):
    """Migration never invents state: a v1 job with no stage stops the upgrade and nothing is written."""
    repo, old = _v1_project(fx, "pre-1.8.json")
    del old["jobs"][1]["stage"]
    write_json(repo / "factory" / "board.json", old)
    out = refused(fx, repo, "migrate", "--apply")
    assert "missing stage" in out

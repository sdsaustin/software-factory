"""factory/server.py: the dashboard's only write path (POST /api/action) and its guards."""
import hashlib
import json
import re

from helpers import write_json
from helpers.server import Server, copy_server, load_module

JOB = "001-health-endpoint"


def _factory(fx, stage="needs-human", built=True, run=None, inbox=None):
    factory = fx.root / "factory"
    copy_server(factory)
    write_json(factory / "board.json", {
        "version": 1, "plugin_version": "1.9.0", "default_branch": "main",
        "jobs": [{"id": JOB, "title": "Health endpoint", "stage": stage, "round": 1, "reviews": {}}],
        "retro": {"done_since": 0, "last": None}, "run": run})
    if built:
        (factory / "jobs" / JOB).mkdir(parents=True)
        (factory / "jobs" / JOB / "build.md").write_text("# build\n")
    if inbox is not None:
        write_json(factory / "inbox.json", {"entries": inbox})
    return factory


def _actions(factory):
    path = factory / "actions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _with_server(fx, factory, check):
    srv = Server(fx, factory)
    try:
        check(srv)
    finally:
        srv.stop()


def test_serves_dashboard_and_ping(fx):
    factory = _factory(fx)

    def check(srv):
        assert srv.request("GET", "/api/ping")[0] == 200
        status, body = srv.request("GET", "/dashboard.html")
        assert status == 200 and b"STAGES" in body
        assert srv.request("GET", "/board.json")[0] == 200
    _with_server(fx, factory, check)


def test_rejects_foreign_host_and_origin(fx):
    factory = _factory(fx)

    def check(srv):
        assert srv.request("GET", "/board.json", host="evil.example:80")[0] == 403
        assert srv.request("HEAD", "/board.json", host="evil.example:80")[0] == 403
        action = {"type": "approve", "job": JOB}
        assert srv.post_action(action, host="evil.example:80")[0] == 403
        assert srv.post_action(action, headers={"Origin": "http://evil.example"})[0] == 403
        assert _actions(factory) == []
    _with_server(fx, factory, check)


def test_requires_token(fx):
    factory = _factory(fx)

    def check(srv):
        action = {"type": "approve", "job": JOB}
        assert srv.post_action(action, token=False)[0] == 403
        assert srv.post_action(action, headers={"X-Factory-Token": "wrong"})[0] == 403
        assert _actions(factory) == []
    _with_server(fx, factory, check)


def test_rejects_malformed_requests(fx):
    factory = _factory(fx)

    def check(srv):
        assert srv.request("POST", "/api/other", body={"type": "approve", "job": JOB})[0] == 404
        assert srv.post_action(b"{not json")[0] == 400
        assert srv.post_action({"type": "approve", "job": JOB}, headers={"Content-Type": "text/plain"})[0] == 415
        assert srv.post_action(b"x" * (17 * 1024))[0] == 413
        assert _actions(factory) == []
    _with_server(fx, factory, check)


def test_never_serves_outside_factory(fx):
    factory = _factory(fx)
    (fx.root / "secret.txt").write_text("outside the factory folder\n")

    def check(srv):
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt"):
            status, body = srv.request("GET", path)
            assert b"outside the factory folder" not in (body if isinstance(body, bytes) else b""), path
    _with_server(fx, factory, check)


def test_approve_appends_one_clean_record_and_never_writes_board(fx):
    factory = _factory(fx)
    board_before = hashlib.sha256((factory / "board.json").read_bytes()).hexdigest()

    def check(srv):
        status, body = srv.post_action({"type": "approve", "job": JOB, "answers": {"1": "a"},
                                        "note": "ship it\x07\x1b now"})
        assert status == 201, body
        recs = _actions(factory)
        assert len(recs) == 1
        rec = recs[0]
        assert rec["id"] == body["id"] and re.fullmatch(r"[0-9a-f]{32}", rec["id"])
        assert rec["type"] == "approve" and rec["job"] == JOB and rec["answers"] == {"1": "a"}
        assert rec["note"] == "ship it now", "control characters must be stripped"
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", rec["at"]), rec["at"]
        # A repeated click is queued again with a new id; `/factory apply` keeps only the newest per job.
        status2, body2 = srv.post_action({"type": "approve", "job": JOB})
        assert status2 == 201 and body2["id"] != body["id"]
        assert len(_actions(factory)) == 2
    _with_server(fx, factory, check)
    assert hashlib.sha256((factory / "board.json").read_bytes()).hexdigest() == board_before


def test_validate_rules(fx):
    """The validator's decisions, without HTTP. Each case: (board/inbox setup, action) -> error or None."""
    factory = _factory(fx)
    srv = load_module(factory)
    board = json.loads((factory / "board.json").read_text())
    v = srv.validate

    assert v({"type": "approve", "job": JOB}, board) is None
    assert v({"type": "rework", "job": JOB, "note": "add a test"}, board) is None
    assert v({"type": "rework", "job": JOB, "answers": {"1": "b"}}, board) is None
    assert v([], board) == "body must be an object"
    assert v({"type": "merge", "job": JOB}, board) == "unknown action type"
    assert v({"type": "approve", "job": "../etc"}, board) == "bad job id"
    assert v({"type": "approve", "job": "002-other"}, board) == "job is not waiting for a human"
    assert v({"type": "rework", "job": JOB}, board) == "rework needs a note or an answer"
    assert v({"type": "rework", "job": JOB, "note": "x" * 4001}, board) == "bad note"
    assert v({"type": "approve", "job": JOB, "answers": {"one": "a"}}, board) == "bad answers"
    assert v({"type": "approve", "job": JOB, "answers": {"1": "A; rm"}}, board) == "bad answers"
    assert v({"type": "approve", "job": JOB, "answers": {str(i): "a" for i in range(11)}}, board) == "bad answers"

    running = dict(board, jobs=[dict(board["jobs"][0], stage="review")])
    assert v({"type": "approve", "job": JOB}, running) == "job is not waiting for a human"

    # resume: only a stopped run, and never with a job id
    assert v({"type": "resume"}, board) == "there is no stopped run to resume"
    stopped = dict(board, run={"status": "stopped"})
    assert v({"type": "resume"}, stopped) is None
    assert v({"type": "resume", "job": JOB}, stopped) == "resume takes no job"


def test_approve_needs_a_built_job(fx):
    factory = _factory(fx, built=False)
    srv = load_module(factory)
    board = json.loads((factory / "board.json").read_text())
    assert srv.validate({"type": "approve", "job": JOB}, board).startswith("nothing to merge yet")
    assert srv.validate({"type": "rework", "job": JOB, "note": "answer"}, board) is None


def test_signoff_needs_an_open_signoff_entry(fx):
    factory = _factory(fx, stage="build", inbox=[{"job": JOB, "reason": "SIGN-OFF"}])
    srv = load_module(factory)
    board = json.loads((factory / "board.json").read_text())
    assert srv.validate({"type": "signoff", "job": JOB}, board) is None, "an early sign-off is given mid-build"
    write_json(factory / "inbox.json", {"entries": [{"job": JOB, "reason": "ESCALATE"}]})
    assert srv.validate({"type": "signoff", "job": JOB}, board) == "this job has no open sign-off question"
    done = dict(board, jobs=[dict(board["jobs"][0], stage="done")])
    write_json(factory / "inbox.json", {"entries": [{"job": JOB, "reason": "SIGN-OFF"}]})
    assert srv.validate({"type": "signoff", "job": JOB}, done) == "this job has no open sign-off question"


def test_unreadable_board_rejects_everything(fx):
    factory = _factory(fx)
    (factory / "board.json").write_text("{ truncated")
    srv = load_module(factory)
    board = srv.load_board()
    assert board == {}
    assert srv.validate({"type": "approve", "job": JOB}, board) == "job is not waiting for a human"
    assert srv.validate({"type": "resume"}, board) == "there is no stopped run to resume"

#!/usr/bin/env python3
"""Factory state tool: the only code path that writes factory/board.json. Standard library only.

The orchestrator calls it for every state change, so the rules below are enforced by code, not by prose:
the board schema, the allowed stage transitions, verification bound to an exact commit, and an append-only
audit log (events.jsonl). It refuses rather than guesses: an invalid board, a disallowed transition or stale
evidence exits non-zero and writes nothing.

Run from the project root:  python3 factory/factoryctl.py <command> ...   (`--help` lists the commands)
Exit codes: 0 ok, 1 refused or FAIL, 2 usage error, 3 could not run (tool or environment problem), 4 merge conflict.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

VERSION = "2.0.0"           # plugin version this copy shipped with
BOARD_VERSION = 2
CONFIG_VERSION = 1

STAGES = ["spec", "build", "verify", "review", "triage", "decision", "needs-human", "done", "cancelled"]
TRANSITIONS = {
    "spec": {"build", "needs-human", "cancelled"},
    "build": {"verify", "needs-human", "cancelled"},
    "verify": {"review", "build", "decision", "needs-human", "cancelled"},
    "review": {"triage", "build", "needs-human", "cancelled"},
    "triage": {"build", "verify", "decision", "needs-human", "cancelled"},
    "decision": {"done", "verify", "build", "needs-human", "cancelled"},
    "needs-human": {"spec", "build", "verify", "decision", "done", "cancelled"},
    "done": set(),
    "cancelled": set(),
}
REASONS = ["DECISION", "ESCALATE", "SIGN-OFF", "VERIFY FAIL", "MERGE BLOCKED", "NO TEST COMMAND", "NEEDS PR",
           "AGENT FAILURE"]
REVIEWERS = ["security", "ux", "ui", "code"]
REVIEW_VERDICTS = {"PASS", "CHANGES", "pending", "N/A"}
TRIAGE_VERDICTS = {"CLEAN", "PATCH", "DECISION", "pending"}
DECISIONS = {None, "APPROVE", "APPROVE (human)", "ESCALATE"}
STATE_PATHS = ("factory/", "docs/", "CLAUDE.md")
TAIL_LINES = 40
DIFF_LIMIT = 3000

JOB_ID = re.compile(r"^\d{3,}-[a-z0-9][a-z0-9-]{0,60}$")
TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?\+00:00$")
DATE = re.compile(r"^\d{4}-\d\d-\d\d$")
CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
SECRET = re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)\b(\s*[=:]\s*|\s+)(\S+)")
SECRET_KEY = re.compile(r"(?i)(secret|token|password|passwd|api[_-]?key|credential|private[_-]?key)")

DEFAULT_CONFIG = {
    "version": CONFIG_VERSION,
    "workspace": {"mode": "worktree", "root": "auto", "retain_completed": False},
    "run": {"parallel_jobs": 1, "max_consecutive_failures": 2},
    "verification": {"run_test": True, "run_lint": True, "run_build": True},
    "audit": {"events": True},
}


class Refused(Exception):
    """A rule said no. Nothing has been written."""


class CannotRun(Exception):
    """The tool or the environment failed (not the job). Nothing has been written."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(text, limit=300):
    """One line, no control characters, obvious secrets masked, at most `limit` characters."""
    text = SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***", CTRL.sub(" ", str(text)))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def tail(output, n=TAIL_LINES):
    lines = [SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***", CTRL.sub("", ln)) for ln in output.splitlines()]
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------- files

class Factory:
    def __init__(self, directory):
        self.dir = Path(directory).resolve()
        self.root = self.dir.parent
        self.board_path = self.dir / "board.json"
        self.config_path = self.dir / "config.json"
        self.events_path = self.dir / "events.jsonl"
        self.backlog_path = self.dir / "backlog.md"

    def git(self, *args, check=True, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd or self.root, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise CannotRun(f"git {' '.join(args)} failed: {clean(r.stderr or r.stdout)}")
        return r

    def rev(self, ref):
        r = self.git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    def read_json(self, path, what):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise Refused(f"{what} not found: {path}")
        try:
            return json.loads(text)
        except ValueError as e:
            raise Refused(f"{what} is not valid JSON ({e}); fix or restore it from git, nothing was written")

    def load_board(self, require_current=True):
        board = self.read_json(self.board_path, "board.json")
        if not isinstance(board, dict):
            raise Refused("board.json must hold an object")
        version = board.get("version")
        if require_current:
            if isinstance(version, int) and version > BOARD_VERSION:
                raise Refused(f"board.json is version {version}, newer than this tool ({BOARD_VERSION}): "
                              f"update the plugin, restart Claude Code, run /software-factory:init")
            if version != BOARD_VERSION:
                raise Refused(f"board.json is version {version}: run /software-factory:init to upgrade it")
            errors = board_errors(board)
            if errors:
                raise Refused("board.json is invalid, nothing was written:\n  " + "\n  ".join(errors))
        return board

    def load_config(self):
        if not self.config_path.exists():
            return json.loads(json.dumps(DEFAULT_CONFIG))
        cfg = self.read_json(self.config_path, "config.json")
        errors = config_errors(cfg, self.root)
        if errors:
            raise Refused("config.json is invalid:\n  " + "\n  ".join(errors))
        return cfg

    def save_board(self, board):
        errors = board_errors(board)
        if errors:
            raise Refused("refusing to write an invalid board:\n  " + "\n  ".join(errors))
        write_atomic(self.board_path, json.dumps(board, indent=2, ensure_ascii=False) + "\n")

    def events(self):
        """(valid events, problem or None). A truncated last line is reported and skipped."""
        if not self.events_path.exists():
            return [], None
        out, problem = [], None
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                problem = f"events.jsonl line {i} is not valid JSON" + (" (truncated last line)" if i == len(lines) else "")
        return out, problem

    def append_event(self, event, config=None):
        if config is not None and not config.get("audit", {}).get("events", True):
            return
        event = dict({"at": now()}, **event)
        line = json.dumps(event, ensure_ascii=False) + "\n"
        prefix = ""
        if self.events_path.exists() and self.events_path.stat().st_size:
            with open(self.events_path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    prefix = "\n"  # never glue a new event onto a truncated line
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(prefix + line)
            f.flush()
            os.fsync(f.fileno())


def write_atomic(path, text):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------- schema

def new_job(job_id, title, item=None, epic=None):
    return {
        "id": job_id, "title": title, "track": None, "extra_reviewers": [], "skip_reviewers": [],
        "stage": "spec", "round": 0, "reviews": {}, "decision": None, "branch": f"factory/{job_id}",
        "updated": now(), "item": item, "epic": epic, "parked_reason": None, "assumptions": 0,
        "workspace": {"kind": "legacy-checkout", "path": None, "base_commit": None, "created_at": None},
        "execution": {"attempt": 0, "started_at": None, "completed_at": None, "last_error": None},
        "evidence": {"head_commit": None, "verified_commit": None, "verified_at": None},
    }


def _ts_ok(v, nullable=True):
    return (v is None and nullable) or (isinstance(v, str) and bool(TS.match(v)))


def job_errors(job):
    jid = job.get("id") if isinstance(job, dict) else None
    where = f"job {jid!r}"
    if not isinstance(job, dict):
        return ["a job entry is not an object"]
    e = []
    if not isinstance(jid, str) or not JOB_ID.match(jid):
        e.append(f"{where}: id must look like 001-short-slug")
    for key, ok in [
        ("title", lambda v: isinstance(v, str) and v.strip()),
        ("track", lambda v: v in (None, "quick", "full")),
        ("stage", lambda v: v in STAGES),
        ("round", lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0),
        ("branch", lambda v: isinstance(v, str) and v),
        ("item", lambda v: v is None or (isinstance(v, str) and re.fullmatch(r"B\d+", v))),
        ("epic", lambda v: v is None or (isinstance(v, str) and re.fullmatch(r"E\d+", v))),
        ("assumptions", lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0),
        ("extra_reviewers", lambda v: isinstance(v, list) and set(v) <= {"ux", "ui"}),
        ("skip_reviewers", lambda v: isinstance(v, list) and set(v) <= {"ux", "ui"}),
        ("decision", lambda v: v in DECISIONS),
        ("parked_reason", lambda v: v is None or isinstance(v, str)),
        ("updated", lambda v: _ts_ok(v, nullable=False)),
    ]:
        if key not in job:
            e.append(f"{where}: missing {key}")
        elif not ok(job[key]):
            e.append(f"{where}: bad {key}: {job[key]!r}" + (f" (stages: {', '.join(STAGES)})" if key == "stage" else ""))
    reviews = job.get("reviews")
    if not isinstance(reviews, dict):
        e.append(f"{where}: reviews must be an object")
    else:
        for rnd, verdicts in reviews.items():
            if not re.fullmatch(r"\d+", str(rnd)) or not isinstance(verdicts, dict):
                e.append(f"{where}: bad reviews round {rnd!r}")
                continue
            for k, v in verdicts.items():
                allowed = TRIAGE_VERDICTS if k == "triage" else REVIEW_VERDICTS if k in REVIEWERS else None
                if allowed is None or v not in allowed:
                    e.append(f"{where}: bad reviews[{rnd}].{k}: {v!r}")
    for block, fields in [("workspace", {"kind": None, "path": None, "base_commit": None, "created_at": "ts"}),
                          ("execution", {"attempt": "int", "started_at": "ts", "completed_at": "ts", "last_error": None}),
                          ("evidence", {"head_commit": None, "verified_commit": None, "verified_at": "ts"})]:
        b = job.get(block)
        if not isinstance(b, dict):
            e.append(f"{where}: missing {block}")
            continue
        for f, kind in fields.items():
            if f not in b:
                e.append(f"{where}: missing {block}.{f}")
            elif kind == "ts" and not _ts_ok(b[f]):
                e.append(f"{where}: {block}.{f} must be UTC ISO 8601 with +00:00")
            elif kind == "int" and not (isinstance(b[f], int) and b[f] >= 0):
                e.append(f"{where}: {block}.{f} must be a non-negative integer")
        if block == "workspace" and b.get("kind") not in ("worktree", "legacy-checkout"):
            e.append(f"{where}: workspace.kind must be worktree or legacy-checkout")
    return e


def board_errors(board):
    e = []
    if board.get("version") != BOARD_VERSION:
        e.append(f"version must be {BOARD_VERSION}")
    if not isinstance(board.get("jobs"), list):
        return e + ["jobs must be a list"]
    if board.get("default_branch") is not None and not isinstance(board.get("default_branch"), str):
        e.append("default_branch must be a branch name or null")
    retro = board.get("retro")
    if not (isinstance(retro, dict) and isinstance(retro.get("done_since"), int)
            and not isinstance(retro.get("done_since"), bool) and retro["done_since"] >= 0
            and (retro.get("last") is None or (isinstance(retro.get("last"), str) and DATE.match(retro["last"])))):
        e.append("retro must be {done_since: int, last: YYYY-MM-DD or null}")
    run = board.get("run", "missing")
    if run == "missing":
        e.append("missing run (null when no run is active)")
    elif run is not None:
        if not isinstance(run, dict) or run.get("status") not in ("running", "stopped", "done"):
            e.append("run.status must be running, stopped or done")
        else:
            if not _ts_ok(run.get("started")):
                e.append("run.started must be UTC ISO 8601 with +00:00")
            for k in ("jobs_done", "consecutive_failures"):
                if not (isinstance(run.get(k, 0), int) and not isinstance(run.get(k, 0), bool) and run.get(k, 0) >= 0):
                    e.append(f"run.{k} must be a non-negative integer")
            for k in ("max_jobs", "budget_minutes"):
                if run.get(k) is not None and not (isinstance(run[k], int) and not isinstance(run[k], bool) and run[k] > 0):
                    e.append(f"run.{k} must be a positive integer or null")
            for k in ("stop_reason", "current", "until"):
                if run.get(k) is not None and not isinstance(run[k], str):
                    e.append(f"run.{k} must be text or null")
    plan = board.get("plan")
    if plan is not None:
        if not isinstance(plan, dict):
            e.append("plan must be an object or null")
        else:
            if plan.get("mode", "product") not in ("product", "feature"):
                e.append("plan.mode must be product or feature")
            if plan.get("pass") not in (1, 2, 3, None):
                e.append("plan.pass must be 1, 2 or 3")
            if plan.get("status") not in ("running", "awaiting-questions", "awaiting-approval", "approved", "done"):
                e.append("plan.status must be running, awaiting-questions, awaiting-approval, approved or done")
    if board.get("document") not in (None, "running", "awaiting-approval", "done"):
        e.append("document must be running, awaiting-approval, done or null")
    if "features" in board and not isinstance(board["features"], list):
        e.append("features must be a list")
    seen = set()
    for job in board["jobs"]:
        e += job_errors(job)
        jid = job.get("id") if isinstance(job, dict) else None
        if jid in seen:
            e.append(f"duplicate job id {jid}")
        seen.add(jid)
    return e


def config_errors(cfg, root=None):
    e = []
    if not isinstance(cfg, dict):
        return ["config.json must hold an object"]

    def walk(obj, path):
        for k, v in obj.items():
            if SECRET_KEY.search(k):
                e.append(f"{path}{k}: secrets are not allowed in config.json")
            if isinstance(v, dict):
                walk(v, f"{path}{k}.")
    walk(cfg, "")
    if cfg.get("version") != CONFIG_VERSION:
        e.append(f"version: must be {CONFIG_VERSION}")
    checks = [
        ("workspace.mode", lambda v: v == "worktree", "must be \"worktree\""),
        ("workspace.root", lambda v: v == "auto" or (isinstance(v, str) and os.path.isabs(v)),
         "must be \"auto\" or an absolute path outside the project"),
        ("workspace.retain_completed", lambda v: isinstance(v, bool), "must be true or false"),
        ("run.parallel_jobs", lambda v: v == 1 and not isinstance(v, bool),
         "must be 1 (parallel jobs are reserved for a later version)"),
        ("run.max_consecutive_failures", lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 1,
         "must be an integer of at least 1"),
        ("verification.run_test", lambda v: isinstance(v, bool), "must be true or false"),
        ("verification.run_lint", lambda v: isinstance(v, bool), "must be true or false"),
        ("verification.run_build", lambda v: isinstance(v, bool), "must be true or false"),
        ("audit.events", lambda v: isinstance(v, bool), "must be true or false"),
    ]
    for path, ok, msg in checks:
        cur = cfg
        for part in path.split("."):
            cur = cur.get(part, KeyError) if isinstance(cur, dict) else KeyError
        if cur is KeyError:
            continue  # missing fields take their defaults; migrate adds them
        if not ok(cur):
            e.append(f"{path}: {msg} (got {cur!r})")
    if isinstance(cfg.get("workspace"), dict) and cfg["workspace"].get("root") not in (None, "auto"):
        project, root = root, cfg["workspace"]["root"]
        if isinstance(root, str) and os.path.isabs(root) and project:
            here = Path(project).resolve()
            if Path(root).resolve() == here or here in Path(root).resolve().parents:
                e.append("workspace.root: must not be inside the project")
    return e


def config_value(cfg, path):
    cur, dflt = cfg, DEFAULT_CONFIG
    for part in path.split("."):
        dflt = dflt[part]
        cur = cur.get(part) if isinstance(cur, dict) else None
    return dflt if cur is None else cur


# ---------------------------------------------------------------- helpers

def find_job(board, job_id):
    for job in board["jobs"]:
        if job.get("id") == job_id:
            return job
    raise Refused(f"no job {job_id} on the board")


def docs_files(fac, job_id):
    """Paths named on the spec's `Docs files:` line (state-like paths the job is allowed to change)."""
    spec = fac.dir / "jobs" / job_id / "spec.md"
    if not spec.exists():
        return set()
    m = re.search(r"^Docs files:\s*(.+)$", spec.read_text(encoding="utf-8"), re.M)
    if not m or m.group(1).strip().lower().startswith("none"):
        return set()
    return {p.strip("` ") for p in re.split(r"[,\s]+", m.group(1)) if p.strip("` ")}


def code_changes(fac, job_id, since, until):
    """Files changed between two default-branch commits that are not factory state (plus the job's Docs files)."""
    names = fac.git("diff", "--name-only", since, until).stdout.split("\n")
    allowed = docs_files(fac, job_id)
    return [n for n in names if n and (not n.startswith(STATE_PATHS) or n in allowed)]


def freshness(fac, board, job):
    """(fresh, reason). Fresh evidence: a PASS for the branch's current head, with no new code on the default branch."""
    ev = job["evidence"]
    if not ev.get("verified_commit"):
        return False, "no passing verification on record"
    head = fac.rev(job["branch"])
    if head is None:
        return False, f"branch {job['branch']} does not exist"
    if ev["verified_commit"] != head:
        return False, f"branch moved since verification ({ev['verified_commit'][:10]} -> {head[:10]})"
    default = board.get("default_branch")
    base = ev.get("verified_default")
    if default and base:
        tip = fac.rev(default)
        if tip and tip != base:
            moved = code_changes(fac, job["id"], base, tip)
            if moved:
                return False, f"{default} gained code since verification: {', '.join(moved[:5])}"
    verify_md = fac.dir / "jobs" / job["id"] / "verify.md"
    first = verify_md.read_text(encoding="utf-8").splitlines()[:1] if verify_md.exists() else []
    if first != ["VERIFY: PASS"]:
        return False, "verify.md does not start with VERIFY: PASS"
    return True, f"verified {head[:10]} at {ev['verified_at']}"


def expected_reviewers(job):
    track = job.get("track")
    if track == "full":
        run = [r for r in REVIEWERS if r not in job.get("skip_reviewers", [])]
    else:
        run = ["security", "code"] + [r for r in ("ux", "ui") if r in job.get("extra_reviewers", [])]
    return [r for r in REVIEWERS if r in run]


def parse_value(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


# ---------------------------------------------------------------- commands

def cmd_check(fac, args):
    board = fac.load_board()
    fac.load_config()
    _, problem = fac.events()
    print(f"board: ok (version {board['version']}, {len(board['jobs'])} job(s))")
    print("config: ok" if fac.config_path.exists() else "config: missing (defaults apply; init creates it)")
    if problem:
        print(f"events: {problem}; earlier events are intact")
    return 0


def cmd_status(fac, args):
    board = fac.load_board()
    run = board.get("run")
    if run:
        print(f"run: {run['status']} current={run.get('current')} jobs_done={run.get('jobs_done')} "
              f"stop_reason={run.get('stop_reason')}")
    for job in board["jobs"]:
        extra = f" [{job['parked_reason']}]" if job.get("parked_reason") else ""
        print(f"{job['id']}  {job['stage']:<11} round {job['round']}  {job.get('track') or '-':<5} "
              f"{job.get('decision') or '-'}{extra}")
    if not board["jobs"]:
        print("no jobs yet")
    return 0


def cmd_job_new(fac, args):
    board = fac.load_board()
    if any(j.get("id") == args.id for j in board["jobs"]):
        raise Refused(f"job {args.id} already exists")
    job = new_job(args.id, args.title, args.item, args.epic)
    errors = job_errors(job)
    if errors:
        raise Refused("; ".join(errors))
    board["jobs"].append(job)
    fac.save_board(board)
    (fac.dir / "jobs" / args.id).mkdir(parents=True, exist_ok=True)
    fac.append_event({"kind": "created", "job": args.id, "to": "spec", "reason": clean(args.title)}, fac.load_config())
    print(f"{args.id}: created in stage spec")
    return 0


SETTABLE_TOP = {
    "default_branch", "plugin_version", "run", "retro", "plan", "features", "document",
}
SETTABLE_JOB = {"title", "track", "extra_reviewers", "skip_reviewers", "assumptions", "decision", "item", "epic",
                "parked_reason"}


def cmd_set(fac, args):
    board = fac.load_board()
    touched = set()
    for assignment in args.assignments:
        m = re.fullmatch(r"([A-Za-z0-9_.-]+?)(\+?=)(.*)", assignment, re.S)
        if not m:
            raise Refused(f"expected PATH=VALUE or PATH+=VALUE, got {assignment!r}")
        path, op, value = m.group(1).split("."), m.group(2), parse_value(m.group(3))
        if path[0] == "jobs":
            if len(path) != 3 or path[2] not in SETTABLE_JOB:
                raise Refused(f"cannot set {m.group(1)}: job fields settable here are {', '.join(sorted(SETTABLE_JOB))} "
                              f"(stage changes go through `transition`, evidence through `verify`)")
            target, key = find_job(board, path[1]), path[2]
            touched.add(path[1])
        elif path[0] in SETTABLE_TOP:
            target, key = board, path[0]
            if len(path) > 1:
                target = board.get(path[0])
                for part in path[1:-1]:
                    target = target.get(part) if isinstance(target, dict) else None
                if not isinstance(target, dict):
                    raise Refused(f"cannot set {m.group(1)}: {'.'.join(path[:-1])} is not an object "
                                  f"(set {path[0]}={{...}} first)")
                key = path[-1]
        else:
            raise Refused(f"cannot set {m.group(1)}: not a settable field")
        if op == "+=":
            cur = target.get(key)
            if isinstance(cur, bool) or not (isinstance(cur, (int, list)) or cur is None):
                raise Refused(f"cannot add to {m.group(1)}: it is {cur!r}")
            if isinstance(cur, list) or (cur is None and not isinstance(value, int)):
                target[key] = (cur or []) + [value]
            elif isinstance(value, int) and not isinstance(value, bool):
                target[key] = (cur or 0) + value
            else:
                raise Refused(f"cannot add {value!r} to {m.group(1)}")
        else:
            target[key] = value
    for jid in touched:
        find_job(board, jid)["updated"] = now()
    fac.save_board(board)
    print("set: " + "; ".join(args.assignments))
    return 0


def cmd_transition(fac, args):
    board = fac.load_board()
    cfg = fac.load_config()
    job = find_job(board, args.id)
    if args.action_id:
        events, _ = fac.events()
        prior = next((e for e in events if e.get("action_id") == args.action_id), None)
        if prior:
            print(f"{args.id}: action {args.action_id} was already applied at {prior.get('at')} "
                  f"({prior.get('from')} -> {prior.get('to')}); nothing to do")
            return 0
    src, dst = job["stage"], args.stage
    if dst not in STAGES:
        raise Refused(f"unknown stage {dst!r}; stages: {', '.join(STAGES)}")
    if dst == src:
        raise Refused(f"{args.id} is already in {src}")
    if dst not in TRANSITIONS[src]:
        allowed = ", ".join(sorted(TRANSITIONS[src])) or "none (terminal)"
        raise Refused(f"{args.id}: {src} -> {dst} is not allowed (from {src}: {allowed})")
    reason = clean(args.reason)
    notes = []

    if dst == "needs-human":
        code = next((r for r in REASONS if reason.upper().startswith(r + ":")), None)
        if not code:
            raise Refused(f"a needs-human reason must start with one of {', '.join(r + ':' for r in REASONS)}")
        job["parked_reason"] = reason
    elif src == "needs-human":
        job["parked_reason"] = None

    if dst == "build":
        if job.get("track") not in ("quick", "full"):
            raise Refused(f"{args.id}: set the track from the spec before building")
        if src in ("spec", "needs-human"):
            job["execution"]["attempt"] += 1
        job["execution"]["started_at"] = job["execution"]["started_at"] or now()
        job["execution"]["completed_at"] = None
    if dst == "review":
        fresh, why = freshness(fac, board, job)
        if not fresh:
            raise Refused(f"{args.id}: cannot start a review without passing verification of the current head: {why}")
        job["round"] += 1
        run = expected_reviewers(job)
        job["reviews"][str(job["round"])] = dict({r: ("pending" if r in run else "N/A") for r in REVIEWERS},
                                                 triage="pending")
        (fac.dir / "jobs" / args.id / f"round-{job['round']}").mkdir(parents=True, exist_ok=True)
        notes.append(f"round {job['round']}: spawn {', '.join('reviewer-' + r for r in run)} in parallel")
    if dst == "decision":
        fresh, why = freshness(fac, board, job)
        if not fresh:
            raise Refused(f"{args.id}: evidence is stale ({why}); transition to verify and verify again first")
    if dst == "done":
        if job.get("decision") not in ("APPROVE", "APPROVE (human)"):
            raise Refused(f"{args.id}: done needs decision APPROVE or APPROVE (human), not {job.get('decision')!r}")
        head, default = fac.rev(job["branch"]), board.get("default_branch")
        if not head or job["evidence"].get("verified_commit") != head:
            raise Refused(f"{args.id}: the merged branch head was not the verified commit")
        if not default or fac.git("merge-base", "--is-ancestor", head, default, check=False).returncode != 0:
            raise Refused(f"{args.id}: {job['branch']} is not merged into {default}; merge first")
        board["retro"]["done_since"] += 1
        notes.append(f"retro.done_since is now {board['retro']['done_since']}")
    if dst in ("done", "cancelled"):
        job["execution"]["completed_at"] = now()

    job["stage"] = dst
    job["updated"] = now()
    fac.save_board(board)
    fac.append_event({"kind": "transition", "job": args.id, "from": src, "to": dst, "reason": reason,
                      "round": job["round"], "attempt": job["execution"]["attempt"],
                      "evidence": args.evidence, "action_id": args.action_id, "actor": args.actor}, cfg)
    print(f"{args.id}: {src} -> {dst}")
    for n in notes:
        print(f"  {n}")
    return 0


def _read_head(path, n=2):
    try:
        return (path.read_text(encoding="utf-8").splitlines() + ["", ""])[:n]
    except FileNotFoundError:
        return None


def cmd_round(fac, args):
    board = fac.load_board()
    job = find_job(board, args.id)
    if job["stage"] not in ("review", "triage"):
        raise Refused(f"{args.id} is in {job['stage']}; round results are read in review or triage")
    n = str(job["round"])
    verdicts = job["reviews"].get(n)
    if not verdicts:
        raise Refused(f"{args.id}: no review round {n} on the board")
    folder = fac.dir / "jobs" / args.id / f"round-{n}"
    rerun, findings = [], {}
    for r in REVIEWERS:
        if verdicts.get(r) == "N/A":
            continue
        head = _read_head(folder / f"{r}.md")
        m = re.fullmatch(r"VERDICT: (PASS|CHANGES)", head[0].strip()) if head else None
        if not m:
            rerun.append(r)
            print(f"{r}: {'missing' if head is None else 'malformed first line ' + repr(head[0][:60])}")
            continue
        verdicts[r] = m.group(1)
        f = re.fullmatch(r"FINDINGS: (\d+)", head[1].strip())
        findings[r] = int(f.group(1)) if f else None
        print(f"{r}: {m.group(1)} findings={findings[r] if f else 'unreadable'}")
    triage_path = folder / "triage.md"
    triage = _read_head(triage_path, 1)
    nxt = None
    if rerun:
        nxt = "rerun " + " ".join("reviewer-" + r for r in rerun)
    elif triage is not None:
        m = re.fullmatch(r"TRIAGE: (CLEAN|PATCH|DECISION)", triage[0].strip())
        if not m:
            nxt = "rerun triager (malformed triage.md)"
        else:
            verdicts["triage"] = m.group(1)
            nxt = m.group(1)
    elif findings and all(verdicts[r] == "PASS" and findings[r] == 0 for r in findings):
        write_atomic(triage_path, f"TRIAGE: CLEAN\nOrchestrator: {len(findings)} reviewers PASS with 0 findings; "
                                  f"triager not run.\n")
        verdicts["triage"] = "CLEAN"
        nxt = "CLEAN (triager not run)"
    else:
        nxt = "spawn triager"
    job["updated"] = now()
    fac.save_board(board)
    print(f"NEXT: {nxt}")
    return 1 if rerun else 0


SECONDARY = re.compile(r"(^|/)(tests?|spec|specs|__tests__|fixtures?|testdata|snapshots?|__snapshots__|dist|build|vendor)/"
                       r"|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$|\.snap$|\.min\.(js|css)$"
                       r"|(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock|go\.sum|uv\.lock)$")


def write_branch_diff(fac, job, default):
    rng = f"{default}...{job['branch']}"
    full = fac.git("diff", rng).stdout
    out = fac.dir / "jobs" / job["id"] / "branch.diff"
    if full.count("\n") <= DIFF_LIMIT:
        write_atomic(out, full)
        return
    names = [n for n in fac.git("diff", "--name-only", rng).stdout.split("\n") if n]
    primary = [n for n in names if not SECONDARY.search(n)]
    secondary = [n for n in names if SECONDARY.search(n)]
    parts, used, cut = [], 0, []
    for n in primary:  # source diffs are never cut
        d = fac.git("diff", rng, "--", n).stdout
        parts.append(d)
        used += d.count("\n")
    for n in secondary:
        d = fac.git("diff", rng, "--", n).stdout
        if used + d.count("\n") > DIFF_LIMIT:
            cut.append(n)
            continue
        parts.append(d)
        used += d.count("\n")
    if cut:
        stat = fac.git("diff", "--stat=200", rng, "--", *cut).stdout.strip()
        parts.append(f"\n# Cut from this file to stay near {DIFF_LIMIT} lines (tests, fixtures, lockfiles, generated);\n"
                     f"# read one with: git show {job['branch']}:<path>\n" +
                     "\n".join("# " + ln for ln in stat.splitlines()) + "\n")
    write_atomic(out, "".join(parts))


def run_commands(cmds, cwd, timeout):
    """[(label, command, exit code, output tail)]; raises CannotRun when a command could not start."""
    home = tempfile.mkdtemp(prefix="factory-home-")
    env = dict(os.environ, HOME=home)
    results = []
    try:
        for label, command in cmds:
            try:
                r = subprocess.run(command, shell=True, cwd=cwd, env=env, capture_output=True, text=True,
                                   timeout=timeout)
            except subprocess.TimeoutExpired as e:
                out = (e.stdout or "") if isinstance(e.stdout, str) else ""
                results.append((label, command, 124, tail(out + f"\n[timed out after {timeout}s]")))
                break
            output = (r.stdout or "") + (r.stderr or "")
            if r.returncode in (126, 127):
                raise CannotRun(f"{label} command could not start (exit {r.returncode}): {clean(tail(output, 3))}")
            results.append((label, command, r.returncode, tail(output)))
            if label == "setup" and r.returncode != 0:
                raise CannotRun(f"setup failed (exit {r.returncode}): {clean(tail(output, 3))}")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return results


def cmd_verify(fac, args):
    board = fac.load_board()
    cfg = fac.load_config()
    job = find_job(board, args.id)
    if job["stage"] != "verify":
        raise Refused(f"{args.id} is in {job['stage']}; transition it to verify first")
    default = board.get("default_branch")
    if not default:
        raise Refused("default_branch is not set on the board")
    head, tip = fac.rev(job["branch"]), fac.rev(default)
    if not head or not tip:
        raise CannotRun(f"cannot resolve {job['branch']} or {default}")
    cmds = [("setup", c) for c in args.setup]
    for label, values in (("test", args.test), ("lint", args.lint), ("build", args.build)):
        if config_value(cfg, f"verification.run_{label}"):
            cmds += [(label, c) for c in values]
    if not any(label == "test" for label, _ in cmds) and config_value(cfg, "verification.run_test"):
        raise Refused("pass the project's test command with --test (from CLAUDE.md, never from agent output)")
    if all(label == "setup" for label, _ in cmds):
        raise Refused("nothing to run: config.json turns every verification command off")
    base = fac.git("merge-base", default, job["branch"]).stdout.strip()
    moved = code_changes(fac, job["id"], base, tip)
    mode = "merge result" if moved else "branch"
    started = now()
    work = Path(tempfile.mkdtemp(prefix=f"factory-verify-{job['id']}-"))
    conflicts, results = [], []
    try:
        if mode == "branch":
            archive = subprocess.Popen(["git", "archive", "--format=tar", head], cwd=fac.root, stdout=subprocess.PIPE)
            untar = subprocess.run(["tar", "-x", "-C", str(work)], stdin=archive.stdout, capture_output=True)
            archive.stdout.close()
            if archive.wait() != 0 or untar.returncode != 0:
                raise CannotRun(f"could not export {job['branch']}: {clean(untar.stderr.decode(errors='replace'))}")
            cwd = work
        else:
            cwd = work / "wt"
            fac.git("worktree", "add", "--detach", str(cwd), tip)
            r = fac.git("merge", "--no-ff", "--no-commit", head, check=False, cwd=cwd)
            if r.returncode != 0:
                conflicts = [p for p in fac.git("diff", "--name-only", "--diff-filter=U", cwd=cwd).stdout.split("\n") if p]
                if not conflicts:
                    raise CannotRun(f"trial merge failed: {clean(r.stderr or r.stdout)}")
        if not conflicts:
            results = run_commands(cmds, cwd, args.timeout)
    finally:
        if mode == "merge result" and (work / "wt").exists():
            fac.git("worktree", "remove", "--force", str(work / "wt"), check=False)
        shutil.rmtree(work, ignore_errors=True)
    passed = not conflicts and all(code == 0 for label, _, code, _ in results if label != "setup")
    lines = [f"VERIFY: {'PASS' if passed else 'FAIL'}",
             f"Candidate: {job['branch']} @ {head}",
             f"Default: {default} @ {tip}",
             f"Tested: {mode}",
             f"Started: {started}",
             f"Finished: {now()}", ""]
    if mode == "merge result":
        lines.append(f"{default} gained code since the branch was cut ({', '.join(moved[:10])}), "
                     f"so the merge result was tested.\n")
    if conflicts:
        lines += ["## Merge conflict", "Conflicts: " + ", ".join(conflicts), ""]
    for label, command, code, out in results:
        lines += [f"## {label}: `{command}`", f"exit {code}", "```", out, "```", ""]
    stat = fac.git("diff", "--stat=200", f"{default}...{job['branch']}").stdout.rstrip()
    lines += ["## Diff stat", "```", stat, "```", ""]
    jobdir = fac.dir / "jobs" / job["id"]
    jobdir.mkdir(parents=True, exist_ok=True)
    write_atomic(jobdir / "verify.md", "\n".join(lines))
    write_branch_diff(fac, job, default)
    job["evidence"].update({"head_commit": head, "verified_commit": head if passed else None, "verified_at": now(),
                            "verified_default": tip, "tested": mode})
    job["updated"] = now()
    fac.save_board(board)
    fac.append_event({"kind": "verify", "job": job["id"], "result": "PASS" if passed else "FAIL", "commit": head,
                      "default_commit": tip, "tested": mode, "conflicts": conflicts or None,
                      "evidence": f"factory/jobs/{job['id']}/verify.md"}, cfg)
    print(lines[0])
    if conflicts:
        print("Conflicts: " + ", ".join(conflicts))
        return 4
    return 0 if passed else 1


def cmd_fresh(fac, args):
    board = fac.load_board()
    fresh, why = freshness(fac, board, find_job(board, args.id))
    print(("FRESH: " if fresh else "STALE: ") + why)
    return 0 if fresh else 1


ITEM_LINE = re.compile(r"^- \[( |x)\] (.*)$")


def backlog_items(fac):
    if not fac.backlog_path.exists():
        return []
    items, pos = [], 0
    for line in fac.backlog_path.read_text(encoding="utf-8").splitlines():
        m = ITEM_LINE.match(line)
        if not m:
            continue
        pos += 1
        text = m.group(2)
        idm = re.match(r"(B\d+) ", text)
        item_id = idm.group(1) if idm else f"B{pos}"
        dep = re.search(r"\(depends:\s*([^)]*)\)", text)
        deps = re.findall(r"B\d+", dep.group(1)) if dep else []
        items.append({"id": item_id, "checked": m.group(1) == "x", "deps": deps, "text": text})
    return items


def readiness(fac, board):
    items = backlog_items(fac)
    by_id = {i["id"]: i for i in items}
    jobs = {}
    for job in board["jobs"]:
        if job.get("item"):
            jobs[job["item"]] = job  # the latest job for an item wins
    memo = {}

    def status(item_id, trail=()):
        if item_id in memo:
            return memo[item_id]
        if item_id in trail:
            return ("CYCLE", "depends on itself through " + " -> ".join(trail + (item_id,)))
        item, job = by_id.get(item_id), jobs.get(item_id)
        if item is None:
            res = ("DONE", "") if job and job["stage"] == "done" else ("MISSING", "not in the backlog")
        elif item["checked"] or (job and job["stage"] == "done"):
            res = ("DONE", "")
        elif job and job["stage"] == "cancelled":
            res = ("CANCELLED", f"job {job['id']} was cancelled")
        elif job and job["stage"] == "needs-human":
            res = ("PARKED", f"job {job['id']}: {job.get('parked_reason') or 'waiting for a human'}")
        elif job:
            res = ("ACTIVE", f"job {job['id']} in {job['stage']}")
        else:
            res = ("READY", "")
            for d in item["deps"]:
                ds, dwhy = status(d, trail + (item_id,))
                if ds == "CYCLE":
                    res = ("BLOCKED", f"dependency cycle: {dwhy.split('through ', 1)[-1]}")
                    break
                if ds in ("PARKED", "BLOCKED", "CANCELLED", "MISSING"):
                    res = ("BLOCKED", f"by {d} ({ds.lower()})")
                    break
                if ds != "DONE" and res[0] == "READY":
                    res = ("WAITING", f"on {d}")
        memo[item_id] = res
        return res

    return [(i, *status(i["id"])) for i in items]


def cmd_ready(fac, args):
    board = fac.load_board()
    rows = readiness(fac, board)
    if args.first:
        first = next(((i, s, w) for i, s, w in rows if s == "READY"), None)
        if not first:
            print("none")
            return 1
        print(f"{first[0]['id']} {first[0]['text']}")
        return 0
    for item, st, why in rows:
        print(f"{st:<9} {item['id']:<5} {item['text'][:90]}" + (f"  [{why}]" if why else ""))
    if not rows:
        print("backlog is empty")
    return 0


def cmd_event(fac, args):
    board = fac.load_board()
    cfg = fac.load_config()
    job = find_job(board, args.id)
    reason = clean(args.reason)
    if args.kind in ("agent-failure", "dispatch-failure"):
        job["execution"]["last_error"] = reason
        job["updated"] = now()
        fac.save_board(board)
    fac.append_event({"kind": args.kind, "job": args.id, "reason": reason, "round": job["round"],
                      "attempt": job["execution"]["attempt"]}, cfg)
    print(f"{args.id}: recorded {args.kind}")
    return 0


JOB_DEFAULTS_V1 = {"title": None, "track": None, "extra_reviewers": [], "skip_reviewers": [], "stage": None,
                   "round": 0, "reviews": {}, "decision": None, "branch": None, "updated": None, "item": None,
                   "epic": None, "parked_reason": None, "assumptions": 0}


def migrate_board(board):
    """Return (new board, list of changes). Never changes a job's stage or decision."""
    version = board.get("version", 1)
    if isinstance(version, int) and version > BOARD_VERSION:
        raise Refused(f"board.json is version {version}, newer than this tool ({BOARD_VERSION}); nothing was written")
    if version == BOARD_VERSION:
        return board, []
    if version != 1:
        raise Refused(f"board.json has an unknown version {version!r}; nothing was written")
    changes = [f"version 1 -> {BOARD_VERSION}"]
    new = dict(board)
    new["version"] = BOARD_VERSION
    for key, default in (("default_branch", None), ("jobs", []), ("retro", {"done_since": 0, "last": None}),
                         ("run", None)):
        if key not in new:
            new[key] = default
            changes.append(f"added {key}")
    stamp = now()
    jobs = []
    for job in new["jobs"]:
        job = dict(job)
        added = []
        for key, default in JOB_DEFAULTS_V1.items():
            if key not in job:
                if key == "title":
                    default = job.get("id")
                elif key == "branch":
                    default = f"factory/{job.get('id')}"
                elif key == "updated":
                    default = stamp
                elif key == "stage":
                    continue
                job[key] = json.loads(json.dumps(default))
                added.append(key)
        fresh = new_job(job.get("id") or "000-x", "x")
        for block in ("workspace", "execution", "evidence"):
            if block not in job:
                job[block] = fresh[block]
                added.append(block)
        jobs.append(job)
        if added:
            changes.append(f"job {job.get('id')}: added {', '.join(added)}")
    new["jobs"] = jobs
    return new, changes


def migrate_config(cfg):
    changes = []

    def fill(dst, src, path):
        for k, v in src.items():
            if k not in dst:
                dst[k] = json.loads(json.dumps(v))
                changes.append(f"config: added {path}{k}")
            elif isinstance(v, dict) and isinstance(dst[k], dict):
                fill(dst[k], v, f"{path}{k}.")
    fill(cfg, DEFAULT_CONFIG, "")
    return cfg, changes


def cmd_migrate(fac, args):
    board = fac.load_board(require_current=False)
    new_board, changes = migrate_board(board)
    errors = board_errors(new_board)
    if errors:
        raise Refused("the migrated board would be invalid, nothing was written:\n  " + "\n  ".join(errors))
    if fac.config_path.exists():
        cfg = fac.read_json(fac.config_path, "config.json")
        cfg_errors = config_errors(cfg, fac.root)
        if cfg_errors:
            raise Refused("config.json is invalid, nothing was written:\n  " + "\n  ".join(cfg_errors))
        new_cfg, cfg_changes = migrate_config(json.loads(json.dumps(cfg)))
    else:
        new_cfg, cfg_changes = json.loads(json.dumps(DEFAULT_CONFIG)), ["create config.json"]
    events_change = [] if fac.events_path.exists() else ["create events.jsonl"]
    all_changes = changes + cfg_changes + events_change
    if not all_changes:
        print("up to date: nothing to migrate")
        return 0
    for c in all_changes:
        print(("" if args.apply else "would: ") + c)
    if not args.apply:
        return 0
    if changes:
        fac.save_board(new_board)
    if cfg_changes:
        write_atomic(fac.config_path, json.dumps(new_cfg, indent=2) + "\n")
    if events_change:
        fac.events_path.touch()
    if changes:
        fac.append_event({"kind": "migrate", "from": board.get("version", 1), "to": BOARD_VERSION,
                          "reason": f"{len(new_board['jobs'])} job(s) kept; stages and decisions unchanged"},
                         new_cfg)
    print("migrated")
    return 0


def cmd_doctor(fac, args):
    findings = []

    def add(level, what, fix=None):
        findings.append((level, what, fix))

    board = None
    try:
        board = fac.load_board()
        add("ok", f"board.json valid (version {board['version']}, {len(board['jobs'])} job(s))")
    except Refused as e:
        add("FAIL", f"board: {e}", "restore factory/board.json from git, or run /software-factory:init to upgrade it")
    try:
        fac.load_config()
        add("ok" if fac.config_path.exists() else "WARN",
            "config.json valid" if fac.config_path.exists() else "config.json missing (defaults apply)",
            None if fac.config_path.exists() else "run /software-factory:init")
    except Refused as e:
        add("FAIL", str(e), "edit factory/config.json")
    if board and board.get("plugin_version") and board["plugin_version"] != VERSION:
        add("WARN", f"board stamped by plugin {board['plugin_version']}, this tool is {VERSION}",
            "run /software-factory:init with the current plugin")
    for name in ("backlog.md", "learnings.md", "dashboard.html", "server.py", "userguide.html", "config.json",
                 "events.jsonl"):
        if not (fac.dir / name).exists():
            add("WARN", f"factory/{name} missing", "run /software-factory:init")
    _, problem = fac.events()
    if problem:
        add("WARN", f"{problem}; earlier events are intact", "leave it; new events start on a fresh line")
    inside = fac.git("rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode != 0:
        add("FAIL", "not a git repository", "git init")
    elif board:
        default = board.get("default_branch")
        current = fac.git("branch", "--show-current", check=False).stdout.strip()
        if not default:
            add("WARN", "default_branch not set on the board", "any job command detects and records it")
        elif not fac.rev(default):
            add("FAIL", f"default branch {default} does not exist", "set it with factoryctl set default_branch=<name>")
        elif current != default:
            add("FAIL", f"checked out {current or '(detached)'}, not {default}", f"git switch {default}")
        dirty = [ln[3:] for ln in fac.git("status", "--porcelain", check=False).stdout.splitlines()
                 if ln[3:] and not ln[3:].startswith(STATE_PATHS)]
        if dirty:
            add("WARN", f"uncommitted changes outside factory/, docs/, CLAUDE.md: {', '.join(dirty[:5])}",
                "commit or stash them before a job command")
        branches = set(fac.git("for-each-ref", "--format=%(refname:short)", "refs/heads/factory/",
                               check=False).stdout.split())
        jobs = {j["id"]: j for j in board["jobs"]}
        for j in board["jobs"]:
            if j["stage"] in ("verify", "review", "triage", "decision") and j["branch"] not in branches:
                add("FAIL", f"{j['id']} is in {j['stage']} but {j['branch']} does not exist",
                    f"/software-factory:factory rework {j['id']} rebuild the missing branch")
            if j["stage"] in ("spec", "build", "verify", "review", "triage", "decision") and \
                    not (board.get("run") or {}).get("status") == "running":
                add("WARN", f"{j['id']} is mid-loop in {j['stage']} with no run active",
                    f"if no session is working on it: /software-factory:factory rework {j['id']} resume after interruption")
        for b in sorted(branches):
            if b.split("/", 1)[1] not in jobs:
                add("WARN", f"branch {b} has no job on the board", f"inspect it; delete it yourself if unwanted")
        for line in fac.git("worktree", "list", "--porcelain", check=False).stdout.split("\n\n"):
            if "prunable" in line:
                path = line.split("\n")[0].replace("worktree ", "")
                add("WARN", f"stale worktree record {path}", "git worktree prune")
    if board:
        try:
            inbox = json.loads((fac.dir / "inbox.json").read_text(encoding="utf-8")).get("entries", [])
        except (OSError, ValueError, AttributeError):
            inbox = []
        jobs = {j["id"]: j for j in board["jobs"]}
        for entry in inbox:
            jid = entry.get("job") if isinstance(entry, dict) else None
            j = jobs.get(jid)
            if not j:
                add("WARN", f"inbox entry for unknown job {jid}", "remove it from inbox.json and inbox.md")
            elif entry.get("reason") != "SIGN-OFF" and j["stage"] != "needs-human":
                add("WARN", f"inbox entry for {jid} but the job is in {j['stage']}", "remove the resolved entry")
        try:
            queued = [json.loads(ln) for ln in (fac.dir / "actions.jsonl").read_text().splitlines() if ln.strip()]
        except (OSError, ValueError):
            queued = []
        try:
            applied = {a.get("id") for a in json.loads((fac.dir / "actions-applied.json").read_text()).get("applied", [])}
        except (OSError, ValueError, AttributeError):
            applied = set()
        pending = [a for a in queued if isinstance(a, dict) and a.get("id") not in applied]
        if pending:
            add("WARN", f"{len(pending)} dashboard action(s) pending", "/software-factory:factory apply")
        claude_md = fac.root / "CLAUDE.md"
        text = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
        m = re.search(r"Test command:\s*(.+)", text)
        if not m or "<TBD" in m.group(1):
            add("WARN", "CLAUDE.md has no test command", "fill in `Test command:` under Running the project")
    for level, what, fix in findings:
        print(f"{level:<4} {what}" + (f"\n     fix: {fix}" if fix else ""))
    return 1 if any(level == "FAIL" for level, _, _ in findings) else 0


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(prog="factoryctl", description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=str(Path(__file__).resolve().parent), help="the factory/ folder (default: mine)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate board.json and config.json (read-only)")
    sub.add_parser("status", help="one line per job (read-only)")
    p = sub.add_parser("job-new", help="create a job in stage spec")
    p.add_argument("id")
    p.add_argument("--title", required=True)
    p.add_argument("--item")
    p.add_argument("--epic")
    p = sub.add_parser("set", help="set allowlisted fields: PATH=VALUE or PATH+=VALUE (VALUE is JSON or text)")
    p.add_argument("assignments", nargs="+")
    p = sub.add_parser("transition", help="move a job to another stage (validated, logged)")
    p.add_argument("id")
    p.add_argument("stage")
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence")
    p.add_argument("--action-id")
    p.add_argument("--actor", choices=["orchestrator", "human"], default="orchestrator")
    p = sub.add_parser("round", help="record the current round's review and triage files; say what comes next")
    p.add_argument("id")
    p = sub.add_parser("verify", help="run the commands on an exact commit and record the evidence")
    p.add_argument("id")
    p.add_argument("--test", action="append", default=[])
    p.add_argument("--lint", action="append", default=[])
    p.add_argument("--build", action="append", default=[])
    p.add_argument("--setup", action="append", default=[])
    p.add_argument("--timeout", type=int, default=1800, help="seconds per command")
    p = sub.add_parser("fresh", help="exit 0 if the job's verification still holds")
    p.add_argument("id")
    p = sub.add_parser("ready", help="backlog items and why each is or is not ready (read-only)")
    p.add_argument("--first", action="store_true")
    p = sub.add_parser("event", help="record a failure or note for a job")
    p.add_argument("id")
    p.add_argument("--kind", required=True, choices=["agent-failure", "dispatch-failure", "note"])
    p.add_argument("--reason", required=True)
    p = sub.add_parser("migrate", help="upgrade board.json and config.json in place (dry run without --apply)")
    p.add_argument("--apply", action="store_true")
    sub.add_parser("doctor", help="read-only health check with repair commands")
    args = ap.parse_args(argv)
    fac = Factory(args.dir)
    handler = globals()["cmd_" + args.cmd.replace("-", "_")]
    try:
        if args.cmd not in ("migrate", "doctor"):
            fac.load_config()  # an invalid config stops every command with a field-specific message
        return handler(fac, args)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    except CannotRun as e:
        print(f"CANNOT RUN: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())

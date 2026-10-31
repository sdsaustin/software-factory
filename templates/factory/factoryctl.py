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
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

VERSION = "2.4.0"           # plugin version this copy shipped with
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
           "AGENT FAILURE", "SCOPE VIOLATION"]
REVIEWERS = ["security", "ux", "ui", "code", "architecture"]
REVIEW_VERDICTS = {"PASS", "CHANGES", "pending", "N/A"}
TRIAGE_VERDICTS = {"CLEAN", "PATCH", "DECISION", "pending"}
DECISIONS = {None, "APPROVE", "APPROVE (human)", "ESCALATE"}
STATE_PATHS = ("factory/", "docs/", "CLAUDE.md")
PLATFORM_ID = "_platform"
CTX_ID_RE = r"_platform|[a-z][a-z0-9-]*"
# Written while an agent runs by something other than the agent (the dashboard server); never an agent's write.
SCOPE_EXEMPT = ("factory/actions.jsonl",)
ROLES = ["spec-writer", "builder", "reviewers", "triager", "approver", "planner", "retro"]
PLANNER_PASSES = {
    "1": ["docs/vision.md", "docs/product-brief.md", "docs/open-questions.md"],
    "2A": ["docs/open-questions.md"],
    "2": ["docs/architecture.md", "docs/decisions.md", "docs/open-questions.md"],
    "3": ["docs/epics.md", "factory/backlog.md", "docs/claude-md-proposal.md", "docs/open-questions.md"],
    "DOC": ["docs/architecture.md", "docs/decisions.md", "docs/open-questions.md", "docs/claude-md-proposal.md"],
    "FEATURE 1": [],
    "FEATURE 2": ["docs/decisions.md", "docs/architecture.md"],
    "FEATURE 3": ["docs/epics.md", "factory/backlog.md"],
}
# Passes that may also write anywhere under docs/domain/ (L1, L2, glossary, ownership).
PLANNER_DOMAIN_PASSES = ("2A", "DOC", "FEATURE 2")
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
        "updated": now(), "item": item, "epic": epic, "parked_reason": None, "assumptions": 0, "contexts": [],
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
    ctxs = job.get("contexts", [])  # optional: boards from before domain contexts have none
    if not (isinstance(ctxs, list) and all(isinstance(c, str) and re.fullmatch(CTX_ID_RE, c) for c in ctxs)
            and len(set(ctxs)) == len(ctxs)):
        e.append(f"{where}: bad contexts: {ctxs!r} (a list of unique context ids, or {PLATFORM_ID})")
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
        if block == "execution" and b.get("blocked") is not None and not isinstance(b.get("blocked"), str):
            e.append(f"{where}: execution.blocked must be text or null")
        if block == "workspace" and b.get("path") is not None and not (isinstance(b["path"], str) and os.path.isabs(b["path"])):
            e.append(f"{where}: workspace.path must be an absolute path or null")
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
            if plan.get("pass") not in (1, "2A", 2, 3, None):
                e.append("plan.pass must be 1, 2A, 2 or 3")
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


def spec_contexts(fac, job_id):
    """Context ids on the spec's `Contexts:` line, or None when the spec has no such line."""
    spec = fac.dir / "jobs" / job_id / "spec.md"
    if not spec.exists():
        return None
    m = re.search(r"^Contexts:[ \t]*(.+)$", spec.read_text(encoding="utf-8"), re.M)
    if not m:
        return None
    if m.group(1).strip().lower().startswith("none"):
        return []
    return [c for c in re.split(r"[,\s]+", m.group(1).strip("` ")) if c]


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


def expected_reviewers(job, fac=None, board=None):
    """Reviewers for the job's next round. `architecture` exists only in a project with a domain model: always on
    the full track, and on quick only when architecture_triggers() finds a reason."""
    track = job.get("track")
    domain = fac is not None and has_domain(fac)
    if track == "full":
        run = [r for r in REVIEWERS if r not in job.get("skip_reviewers", []) and (r != "architecture" or domain)]
    else:
        run = ["security", "code"] + [r for r in ("ux", "ui") if r in job.get("extra_reviewers", [])]
        if domain and architecture_triggers(fac, board, job):
            run.append("architecture")
    return [r for r in REVIEWERS if r in run]


def architecture_triggers(fac, board, job):
    """Why a quick-track job still needs the architecture reviewer (empty list: skip it). Deterministic, no agent:
    a published interface, a `_shared` path or the domain docs changed, a forbidden glossary synonym was added,
    or the job does not stay inside exactly one context."""
    default = (board or {}).get("default_branch")
    if not default or not fac.rev(job["branch"]):
        return ["the diff cannot be read, so the architecture reviewer runs"]
    ownership, _prov, bad = load_ownership_full(fac)
    if bad:
        return ["the domain files are invalid: " + bad[0]]
    reasons = []
    if len(job.get("contexts") or []) != 1:
        reasons.append(f"the job declares {len(job.get('contexts') or [])} contexts, not exactly one")
    names = [n for n in fac.git("diff", "--name-only", f"{default}...{job['branch']}").stdout.split("\n") if n]
    if any(n.startswith("docs/domain/") for n in names):
        reasons.append("docs/domain/ changed")
    code = [n for n in names if not n.startswith(STATE_PATHS)]
    shared = [n for n in code if path_owner(ownership, n) == SHARED]
    if shared:
        reasons.append(f"shared path {shared[0]} changed")
    iface = published_interface_globs(fac)
    hit = next(((n, g) for n in code for g in iface if fnmatch.fnmatch(n, g) or n.startswith(g.rstrip("/*") + "/")), None)
    if hit:
        reasons.append(f"{hit[0]} implements a published interface ({hit[1]})")
    avoid = forbidden_synonyms(fac)
    if avoid and code:
        added = "\n".join(l[1:] for l in fac.git("diff", "-U0", f"{default}...{job['branch']}", "--", *code).stdout.split("\n")
                          if l.startswith("+") and not l.startswith("+++"))
        added = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", added).replace("_", " ")  # make_bill, MakeBill -> words
        word = next((w for w in avoid if re.search(rf"\b{re.escape(w)}\b", added, re.I)), None)
        if word:
            reasons.append(f"the diff adds the forbidden synonym {word!r}")
    return reasons


def published_interface_globs(fac):
    """Backticked path globs on lines of each `docs/domain/contexts/*.md` `## Published interfaces` section."""
    out = []
    for f in sorted((domain_dir(fac) / "contexts").glob("*.md")):
        body = _sections(f.read_text(encoding="utf-8"), 2).get("Published interfaces", "")
        for tok in re.findall(r"`([^`\s]+)`", body):
            if "/" in tok and not tok.startswith(("/", "GET ", "POST ")):
                out.append(tok)
    return out


def forbidden_synonyms(fac):
    gl = domain_dir(fac) / "glossary.md"
    if not gl.exists():
        return []
    words = []
    for m in re.finditer(r"Avoid:\s*(.+)$", gl.read_text(encoding="utf-8"), re.M):
        words += [w.strip(" .`*") for w in m.group(1).split(",") if w.strip(" .`*")]
    return words


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
SETTABLE_JOB = {"contexts", "title", "track", "extra_reviewers", "skip_reviewers", "assumptions", "decision", "item", "epic",
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
    known = None
    for jid in touched:
        j = find_job(board, jid)
        j["updated"] = now()
        errs = [x for x in job_errors(j) if "bad contexts" in x]
        if errs:
            raise Refused(errs[0])
        if j.get("contexts") and has_domain(fac):
            if known is None:
                known = set(parse_l1((domain_dir(fac) / "l1-context.md").read_text(encoding="utf-8"))[0]) | {PLATFORM}
            unknown = [c for c in j["contexts"] if c not in known]
            if unknown:
                raise Refused(f"{jid}: unknown context {', '.join(unknown)}; docs/domain/l1-context.md has "
                              f"{', '.join(sorted(known))}")
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
    blocked = job["execution"].get("blocked")
    if blocked and src != "needs-human" and dst not in ("needs-human", "cancelled"):
        raise Refused(f"{args.id} is blocked ({blocked}); park it: transition {args.id} needs-human "
                      f"--reason \"SCOPE VIOLATION: ...\"")
    if src == "needs-human" and blocked:
        job["execution"]["blocked"] = None

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
        run = expected_reviewers(job, fac, board)
        job["reviews"][str(job["round"])] = dict({r: ("pending" if r in run else "N/A") for r in REVIEWERS
                                                  if r != "architecture" or has_domain(fac)}, triage="pending")
        if "architecture" in run and job.get("track") != "full":
            notes.append("architecture reviewer runs on this quick job: " + "; ".join(architecture_triggers(fac, board, job)))
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
        if verdicts.get(r) in ("N/A", None):  # N/A, or a reviewer this round never had (architecture before a domain)
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


def _snapshot_path(fac, role, job_id):
    git_dir = Path(fac.git("rev-parse", "--absolute-git-dir").stdout.strip())
    return git_dir / "factory-snapshots" / f"{job_id or '-'}--{role}.json"


def _status_files(fac, cwd):
    out = fac.git("status", "--porcelain=v1", "-z", "-uall", cwd=cwd).stdout
    files, parts, i = {}, out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            files[parts[i]] = ("moved", None)  # the rename's source path
            i += 1
        p = Path(cwd) / path
        digest = None
        if p.is_file() and not p.is_symlink():
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
        files[path] = (code, digest)
    return files


def repo_state(fac, worktree=None):
    """Everything an agent could change: dirty and untracked files (by content), refs, HEAD, and the job's worktree."""
    files = _status_files(fac, fac.root)
    refs = {}
    for line in fac.git("for-each-ref", "--format=%(refname) %(objectname)").stdout.splitlines():
        name, _, sha = line.partition(" ")
        if not name.startswith("refs/remotes/"):
            refs[name] = sha
    head = fac.git("symbolic-ref", "-q", "HEAD", check=False).stdout.strip() or fac.rev("HEAD") or "(none)"
    state = {"files": files, "refs": refs, "head": head, "worktree": None}
    if worktree and Path(worktree).is_dir():
        wt_head = fac.git("symbolic-ref", "-q", "HEAD", check=False, cwd=worktree).stdout.strip() or \
            fac.git("rev-parse", "HEAD", check=False, cwd=worktree).stdout.strip()
        state["worktree"] = {"path": str(worktree), "files": _status_files(fac, worktree), "head": wt_head}
    return state


def job_worktree(job):
    ws = job.get("workspace") or {} if job else {}
    return ws.get("path") if ws.get("kind") == "worktree" else None


def scope_rules(fac, board, role, job, args):
    """(exact paths, path prefixes) the role may change in the working tree."""
    jid = job["id"] if job else None
    jobdir = f"factory/jobs/{jid}/" if jid else None
    if role == "spec-writer":
        return {jobdir + "spec.md"}, ()
    if role == "builder":
        return {jobdir + "build.md"}, ()
    if role == "reviewers":
        n = str(job["round"])
        running = [r for r, v in job["reviews"].get(n, {}).items() if r in REVIEWERS and v != "N/A"]
        return {f"{jobdir}round-{n}/{r}.md" for r in running}, ()
    if role == "triager":
        return {f"{jobdir}round-{job['round']}/triage.md"}, ()
    if role == "approver":
        return {jobdir + "decision.md"}, ()
    if role == "retro":
        return {"factory/retro-proposal.md"}, ()
    key = ("FEATURE " if args.feature else "") + str(args.pass_ or "").upper()
    if key not in PLANNER_PASSES:
        raise Refused("planner needs --pass 1|2A|2|3|DOC (plus --feature <slug> for a feature plan)")
    if args.feature and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", args.feature):
        raise Refused("--feature must be the plan's kebab-case slug")
    prefixes = (f"docs/features/{args.feature}/",) if args.feature else ()
    if key in PLANNER_DOMAIN_PASSES:
        prefixes += ("docs/domain/",)
    return set(PLANNER_PASSES[key]), prefixes


def _scope_target(fac, args):
    if args.role not in ROLES:
        raise Refused(f"unknown role {args.role!r}; roles: {', '.join(ROLES)}")
    board = fac.load_board()
    job = None
    if args.role in ("planner", "retro"):
        if args.job:
            raise Refused(f"{args.role} works on no job; drop --job")
    else:
        if not args.job:
            raise Refused(f"{args.role} needs --job <id>")
        job = find_job(board, args.job)
    return board, job


def cmd_snapshot(fac, args):
    board, job = _scope_target(fac, args)
    scope_rules(fac, board, args.role, job, args)  # validates --pass/--feature now, not after the agent ran
    path = _snapshot_path(fac, args.role, job["id"] if job else None)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = dict(repo_state(fac, job_worktree(job)), role=args.role, job=job["id"] if job else None, taken=now())
    write_atomic(path, json.dumps(state, indent=1, sort_keys=True))
    print(f"snapshot: {args.role}" + (f" for {job['id']}" if job else "") + " (stored under .git, outside the work tree)")
    return 0


def cmd_scope_check(fac, args):
    board, job = _scope_target(fac, args)
    cfg = fac.load_config()
    path = _snapshot_path(fac, args.role, job["id"] if job else None)
    if not path.exists():
        raise Refused(f"no snapshot for {args.role}" + (f" on {job['id']}" if job else "") +
                      ": take one with `snapshot` before spawning the agent")
    before = json.loads(path.read_text(encoding="utf-8"))
    after = repo_state(fac, (before.get("worktree") or {}).get("path") or job_worktree(job))
    exact, prefixes = scope_rules(fac, board, args.role, job, args)

    def allowed(p):
        return p in exact or (bool(prefixes) and p.startswith(prefixes))

    problems = []
    bfiles = {k: tuple(v) for k, v in before["files"].items()}
    for p in sorted(set(bfiles) | set(after["files"])):
        if bfiles.get(p) == after["files"].get(p) or p in SCOPE_EXEMPT or p.rsplit("/", 1)[-1] == ".DS_Store":
            continue
        if not allowed(p):
            was = bfiles.get(p, ("clean", None))[0].strip() or "clean"
            now_ = after["files"].get(p, ("clean", None))[0].strip() or "clean"
            problems.append(f"changed {p} ({'untracked' if now_ == '??' else now_} after {was})")
    if after["head"] != before["head"]:
        problems.append(f"left {after['head'].replace('refs/heads/', '')} checked out "
                        f"(was {before['head'].replace('refs/heads/', '')})")
    wb, wa = before.get("worktree"), after.get("worktree")
    if wb or wa:
        where = (wa or wb)["path"]
        if args.role == "builder":
            if wa is None:
                problems.append(f"the job worktree {where} is gone")
            else:
                if wa["head"] != f"refs/heads/{job['branch']}":
                    problems.append(f"the job worktree is on {wa['head'].replace('refs/heads/', '')}, not {job['branch']}")
                left = sorted(p for p in wa["files"] if p.rsplit("/", 1)[-1] != ".DS_Store")
                if left:
                    problems.append(f"uncommitted changes left in the job worktree: {', '.join(left[:8])} "
                                    f"(verification only sees commits)")
        else:
            wbf = {k: tuple(v) for k, v in (wb or {}).get("files", {}).items()}
            changed = sorted(p for p in set(wbf) | set((wa or {}).get("files", {}))
                             if wbf.get(p) != (wa or {}).get("files", {}).get(p))
            if changed or (wb or {}).get("head") != (wa or {}).get("head"):
                problems.append(f"changed the job worktree {where}: {', '.join(changed[:8]) or 'its checkout'}")
    own = f"refs/heads/{job['branch']}" if job and args.role == "builder" else None
    for ref in sorted(set(before["refs"]) | set(after["refs"])):
        if before["refs"].get(ref) != after["refs"].get(ref) and ref != own:
            what = "created" if ref not in before["refs"] else "deleted" if ref not in after["refs"] else "moved"
            problems.append(f"{what} {ref.replace('refs/heads/', 'branch ').replace('refs/tags/', 'tag ')}")
    if own and own in after["refs"] and board.get("default_branch"):
        allowed_docs = docs_files(fac, job["id"])
        changed = [n for n in fac.git("diff", "--name-only", f"{board['default_branch']}...{job['branch']}").stdout.split("\n") if n]
        for n in changed:
            if n.startswith(STATE_PATHS) and n not in allowed_docs:
                problems.append(f"{job['branch']} changes {n} (factory state, docs and CLAUDE.md stay off job "
                                f"branches unless named on the spec's Docs files: line)")
    notes = []
    if own and own in after["refs"] and board.get("default_branch") and has_domain(fac):
        context_scope(fac, job, board["default_branch"], problems, notes)
    label = args.role + (f" on {job['id']}" if job else "")
    if not problems:
        path.unlink()
        fac.append_event({"kind": "scope", "job": job["id"] if job else None, "role": args.role, "result": "ok"}, cfg)
        print(f"scope: ok ({label})")
        for n in notes:
            print(f"  note: {n}")
        return 0
    summary = clean(f"{args.role}: " + "; ".join(problems), 300)
    if job:
        job["execution"]["blocked"] = summary
        job["execution"]["last_error"] = summary
        job["updated"] = now()
        fac.save_board(board)
    fac.append_event({"kind": "scope", "job": job["id"] if job else None, "role": args.role, "result": "violation",
                      "problems": [clean(p, 200) for p in problems[:20]]}, cfg)
    print(f"SCOPE VIOLATION ({label}):")
    for p in problems:
        print(f"  {p}")
    print("Nothing was deleted or reverted. Leave the files as they are for the human to inspect.")
    if job:
        print(f"NEXT: transition {job['id']} needs-human --reason \"SCOPE VIOLATION: {clean(problems[0], 120)}\"")
    else:
        print("NEXT: stop and show the human these changes")
    return 1


def context_scope(fac, job, default, problems, notes):
    """A builder's branch may change only paths owned by the job's contexts (plus `_shared` and unowned paths).
    Violations in a provisional context (globs not yet confirmed by a job) are notes, not failures."""
    ownership, provisional, bad = load_ownership_full(fac)
    if bad:
        problems.extend(f"domain: {b}" for b in bad)
        return
    declared = job.get("contexts") or []
    spec = spec_contexts(fac, job["id"])
    if spec is not None and sorted(spec) != sorted(declared):
        problems.append(f"the board says contexts {declared or 'none'} but the spec's Contexts: line says "
                        f"{spec or 'none'} (set jobs.{job['id']}.contexts to match)")
    changed = [n for n in fac.git("diff", "--name-only", f"{default}...{job['branch']}").stdout.split("\n")
               if n and not n.startswith(STATE_PATHS)]
    unowned, foreign = [], {}
    for n in changed:
        owner = path_owner(ownership, n)
        if owner is None:
            unowned.append(n)
        elif owner != SHARED and owner not in declared:
            foreign.setdefault(owner, []).append(n)
    for owner, files in sorted(foreign.items()):
        msg = (f"{job['branch']} changes {', '.join(files[:4])} owned by context {owner}, "
               f"but the job declares {declared or 'no contexts'}")
        (notes if owner in provisional else problems).append(msg + (" (provisional ownership: reported only)"
                                                                   if owner in provisional else ""))
    if unowned:
        notes.append(f"not owned by any context: {', '.join(unowned[:6])}" + (" ..." if len(unowned) > 6 else ""))


def worktree_root(fac, cfg):
    root = config_value(cfg, "workspace.root")
    base = fac.root.parent / ".software-factory-worktrees" if root == "auto" else Path(root)
    return base / fac.root.name


def registered_worktrees(fac):
    out, cur = [], {}
    for line in fac.git("worktree", "list", "--porcelain").stdout.split("\n") + [""]:
        if not line:
            if cur:
                out.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value or True
    for w in out:
        w["real"] = os.path.realpath(w.get("worktree", ""))
    return out


def _find_worktree(fac, path):
    real = os.path.realpath(str(path))
    return next((w for w in registered_worktrees(fac) if w["real"] == real), None)


def _dirty(fac, path):
    return [ln[3:] for ln in fac.git("status", "--porcelain", "-uall", cwd=path).stdout.splitlines()
            if ln[3:].rsplit("/", 1)[-1] != ".DS_Store"]


def _check_base(fac, job):
    base = job["workspace"].get("base_commit")
    head = fac.rev(job["branch"])
    if base and head and fac.git("merge-base", "--is-ancestor", base, head, check=False).returncode != 0:
        raise Refused(f"{job['branch']} no longer contains its base commit {base[:10]}: it was rewritten outside "
                      f"the factory; ask the human")


def cmd_worktree(fac, args):
    board = fac.load_board()
    cfg = fac.load_config()
    job = find_job(board, args.id)
    default = board.get("default_branch")
    ws = job["workspace"]
    path = Path(ws["path"]) if ws.get("kind") == "worktree" and ws.get("path") else worktree_root(fac, cfg) / job["id"]
    if path.resolve() == fac.root or fac.root in path.resolve().parents:
        raise Refused(f"{path} is inside the project; job worktrees live outside it (config workspace.root)")
    entry = _find_worktree(fac, path)

    if args.action == "validate":
        if not entry or entry.get("prunable"):
            print(f"MISSING: {path}" + (" (registered, directory gone)" if entry else ""))
            return 1
        if entry.get("branch") != f"refs/heads/{job['branch']}":
            print(f"WRONG BRANCH: {path} has {entry.get('branch') or 'a detached HEAD'}, not {job['branch']}")
            return 1
        _check_base(fac, job)
        dirty = _dirty(fac, path)
        print(f"OK: {path} on {job['branch']}" + (f", uncommitted: {', '.join(dirty[:8])}" if dirty else ", clean"))
        return 0

    if args.action == "remove":
        if job["stage"] not in ("done", "cancelled"):
            raise Refused(f"{args.id} is in {job['stage']}: keep its worktree while rework is possible")
        if not entry or entry.get("prunable"):
            print(f"nothing to remove: {path} is not a registered worktree")
            return 0
        dirty = _dirty(fac, path)
        if dirty:
            raise Refused(f"{path} has uncommitted changes ({', '.join(dirty[:8])}); a dirty worktree is never removed "
                          f"automatically. Inspect it, then `git worktree remove {path}` yourself")
        fac.git("worktree", "remove", str(path))
        ws["path"] = None
        job["updated"] = now()
        fac.save_board(board)
        fac.append_event({"kind": "worktree", "job": job["id"], "action": "removed", "path": str(path)}, cfg)
        print(f"removed {path} (branch {job['branch']} kept)")
        return 0

    # create (idempotent; also repairs a worktree whose directory is gone)
    if job["stage"] != "build":
        raise Refused(f"{args.id} is in {job['stage']}; worktrees are created for a build (move it to build first)")
    primary = fac.git("symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
    if primary != default:
        raise Refused(f"the project checkout is on {primary or 'a detached HEAD'}, not {default}: switch back first")
    stray = [ln[3:] for ln in fac.git("status", "--porcelain").stdout.splitlines()
             if ln[3:] and not ln[3:].startswith(STATE_PATHS)]
    if stray:
        raise Refused(f"uncommitted changes outside factory/, docs/ and CLAUDE.md: {', '.join(stray[:8])}")
    action = "reused"
    if entry and entry.get("prunable"):
        fac.git("worktree", "prune")
        entry, action = None, "repaired"
    if entry:
        if entry.get("branch") != f"refs/heads/{job['branch']}":
            raise Refused(f"{path} is a worktree for {entry.get('branch') or 'a detached HEAD'}, not {job['branch']}; "
                          f"it is never reused")
    else:
        if path.exists():
            raise Refused(f"{path} exists but is not this job's worktree (another repository or job, or stray files); "
                          f"it is never reused")
        holder = next((w for w in registered_worktrees(fac) if w.get("branch") == f"refs/heads/{job['branch']}"), None)
        if holder:
            raise Refused(f"{job['branch']} is already checked out at {holder['worktree']}; free it first")
        path.parent.mkdir(parents=True, exist_ok=True)
        if fac.rev(job["branch"]):
            fac.git("worktree", "add", str(path), job["branch"])
        else:
            fac.git("worktree", "add", "-b", job["branch"], str(path), default)
        action = "created" if action == "reused" else action
    if not ws.get("base_commit"):
        ws["base_commit"] = fac.git("merge-base", default, job["branch"]).stdout.strip()
    _check_base(fac, job)
    ws.update({"kind": "worktree", "path": str(path.resolve()), "created_at": ws.get("created_at") or now()})
    job["updated"] = now()
    fac.save_board(board)
    fac.append_event({"kind": "worktree", "job": job["id"], "action": action, "path": str(path.resolve()),
                      "base_commit": ws["base_commit"]}, cfg)
    print(f"WORKTREE: {path.resolve()} ({action}; branch {job['branch']}, base {ws['base_commit'][:10]})")
    return 0


def cmd_cancel(fac, args):
    board = fac.load_board()
    job = find_job(board, args.id)
    args.stage, args.evidence, args.action_id, args.actor = "cancelled", None, args.action_id, "human"
    code = cmd_transition(fac, args)
    if code == 0:
        kept = [f"branch {job['branch']}"] if fac.rev(job["branch"]) else []
        if job_worktree(job):
            kept.append(f"worktree {job_worktree(job)}")
        print("kept: " + (", ".join(kept) if kept else "nothing to keep") +
              ". Nothing was deleted; items that depend on this one stay blocked until you change the backlog.")
    return code


def cmd_recover(fac, args):
    board = fac.load_board()
    cfg = fac.load_config()
    job = find_job(board, args.id)
    jid, stage = job["id"], job["stage"]
    if stage in ("done", "cancelled"):
        print(f"{jid} is {stage}: nothing to recover")
        return 0
    doubts = []
    run = board.get("run") or {}
    if run.get("status") == "running" and run.get("current") in (jid, job.get("item")):
        doubts.append("a run is marked running with this job in flight")
    events, _ = fac.events()
    last = next((e for e in reversed(events) if e.get("job") == jid), None)
    if last:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last["at"])
        if age.total_seconds() < 900:
            doubts.append(f"its last event was {int(age.total_seconds() // 60)} min ago ({last.get('kind')})")
    if doubts and not args.confirmed:
        print("AMBIGUOUS: " + "; ".join(doubts))
        print("NEXT: ask the human to confirm that no session is working on this job, then rerun with --confirmed")
        return 1
    folder = fac.dir / "jobs" / jid
    head = fac.rev(job["branch"])
    print(f"{jid}: stage {stage}, round {job['round']}, attempt {job['execution']['attempt']}")
    print(f"branch: {job['branch']} " + (f"at {head[:10]}" if head else "does not exist"))
    wt = job_worktree(job)
    if wt:
        entry = _find_worktree(fac, wt)
        state = "missing" if not entry or entry.get("prunable") else \
            ("dirty: " + ", ".join(_dirty(fac, wt)[:8])) if _dirty(fac, wt) else "clean"
        print(f"worktree: {wt} ({state})")
    fresh, why = freshness(fac, board, job)
    print(f"verification: {'fresh' if fresh else 'stale'} ({why})")
    if job["execution"].get("blocked"):
        nxt = f"blocked ({job['execution']['blocked']}): transition {jid} needs-human --reason \"SCOPE VIOLATION: ...\""
    elif stage == "needs-human":
        nxt = f"parked ({job.get('parked_reason')}): not an interruption; answer it with approve or rework"
    elif stage == "spec":
        nxt = "readiness gate (Loop step 2) on the existing spec.md" if (folder / "spec.md").exists() \
            else "snapshot and spawn the spec-writer (Loop step 1)"
    elif stage == "build":
        built = head and (folder / "build.md").exists() and \
            int(fac.git("rev-list", "--count", f"{board['default_branch']}..{job['branch']}").stdout.strip() or 0) > 0
        nxt = ("transition to verify and verify (Loop step 3a)" if built else
               "`worktree create` (repairs a missing worktree), then snapshot and spawn the builder with build.md "
               "and its notes (Loop step 3)")
    elif stage == "verify":
        nxt = "run factoryctl verify (Loop step 3a)"
    elif stage == "review":
        n = str(job["round"])
        missing = [r for r, v in job["reviews"].get(n, {}).items()
                   if r in REVIEWERS and v != "N/A" and not (folder / f"round-{n}" / f"{r}.md").exists()]
        nxt = (f"snapshot reviewers and spawn {', '.join('reviewer-' + r for r in missing)} (Loop step 4)" if missing
               else "factoryctl round, then triage (Loop steps 4-5)")
    elif stage == "triage":
        nxt = ("factoryctl round to record triage.md (Loop step 5)" if (folder / f"round-{job['round']}" / "triage.md").exists()
               else "factoryctl round; spawn the triager if it says so (Loop step 5)")
    else:  # decision
        nxt = ("record decision.md's first line and continue Loop step 6" if (folder / "decision.md").exists()
               else "check fresh evidence, then snapshot and spawn the approver (Loop step 6)")
    fac.append_event({"kind": "recover", "job": jid, "stage": stage, "reason": clean(nxt, 200),
                      "confirmed": bool(args.confirmed)}, cfg)
    print(f"NEXT: {nxt}")
    return 0


QUALITY = ["Security", "Privacy", "Observability", "Reliability", "Support"]


def _sections(text, level):
    """{heading text: body} for headings of exactly `level` hashes."""
    marks = [(m.start(), m.end(), m.group(1).strip()) for m in re.finditer(rf"^{'#' * level} (?!#)(.+)$", text, re.M)]
    out = {}
    for i, (start, end, title) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[end:stop]
        if level > 2:
            nxt = re.search(r"^#{2,%d} (?!#)" % (level - 1), body, re.M)
            body = body[:nxt.start()] if nxt else body
        out[title] = body
    return out


def _check_architecture(text, level, accelerator, fails, doc=False):
    heads = _sections(text, level)
    h = "#" * level
    if "Components and boundaries" not in heads and "Components and interfaces" not in heads:
        fails.append(f"no `{h} Components and boundaries` section (principal modules, what each owns, the "
                     f"interfaces between them)")
    if not doc:
        qa = heads.get("Quality attributes")
        if qa is None:
            fails.append(f"no `{h} Quality attributes` section ({', '.join(QUALITY)})")
        else:
            missing = [q for q in QUALITY if not re.search(rf"^- {q}:\s*\S", qa, re.M)]
            if missing:
                fails.append(f"Quality attributes lacks a `- <name>:` line for {', '.join(missing)} "
                             f"(write `not applicable: <why>` when it does not apply)")
        if "Pre-mortem" not in heads:
            fails.append(f"no `{h} Pre-mortem` section")
    if accelerator and "Core and extension seams" not in heads:
        fails.append(f"the brief calls this an accelerator, but there is no `{h} Core and extension seams` section "
                     f"(what is reusable core, what is per-customer configuration, and where they meet)")


def _check_commands(text, fails, allow_none=False):
    body = _sections(text, 2).get("Run and test commands")
    if body is None:
        fails.append("no `## Run and test commands` section")
        return
    for name, required in (("Test", True), ("Run", True), ("Lint", False), ("Build", False)):
        m = re.search(rf"^- {name}:\s*(.+)$", body, re.M)
        value = m.group(1).strip() if m else ""
        exact = bool(re.match(r"`[^`]+`", value))
        if not m:
            fails.append(f"Run and test commands lacks `- {name}:`")
        elif required and not exact and not (allow_none and value.lower().startswith("none")):
            fails.append(f"`- {name}:` must give the exact command in backticks, not {value[:40]!r}")
        elif not required and not exact and not value.lower().startswith("none"):
            fails.append(f"`- {name}:` must be a command in backticks or `none (<why>)`")


def _check_decisions(text, fails, only_feature=None, doc=False, contexts=None):
    blocks = re.split(r"^(?=## AD\d+)", text, flags=re.M)
    records = [b for b in blocks if re.match(r"## AD\d+", b)]
    if only_feature:
        records = [b for b in records if re.search(rf"^Feature: {re.escape(only_feature)}\s*$", b, re.M)]
    if not records and not only_feature:
        fails.append("decisions.md has no `## AD<n>` records")
    assumed = []
    for b in records:
        ad = re.match(r"## (AD\d+)(.*)", b)
        name = ad.group(1)
        status = re.search(r"Status:\s*\**\s*(accepted|assumed|inferred|superseded by AD\d+)", b, re.I)
        if not status:
            fails.append(f"{name}: no `Status: accepted|assumed|inferred` line")
        elif status.group(1).lower() == "assumed":
            assumed.append(f"{name}{ad.group(2).rstrip()}")
        if contexts:
            bc = re.search(r"^Bounded-context:\s*(\S+)\s*$", b, re.M)
            if not bc:
                fails.append(f"{name}: no `Bounded-context: <context>|global` line")
            elif bc.group(1) not in set(contexts) | {PLATFORM, "global"}:
                fails.append(f"{name}: bounded context {bc.group(1)!r} is not in l1-context.md (or {PLATFORM}, global)")
        if not re.search(r"\bConsequences\b", b):
            fails.append(f"{name}: no Consequences")
        if not re.search(r"\bContext\b", b) and not (doc and re.search(r"\bEvidence\b", b)):
            fails.append(f"{name}: no Context")
    return assumed


def _check_backlog(fac, fails):
    seen, order = {}, []
    for item in backlog_items(fac):
        if not re.match(r"B\d+ ", item["text"]):
            fails.append(f"backlog line without a B<n> id: {item['text'][:60]}")
            continue
        if item["id"] in seen:
            fails.append(f"duplicate backlog id {item['id']}")
        seen[item["id"]] = len(order)
        order.append(item)
    for item in order:
        for d in item["deps"]:
            if d not in seen:
                fails.append(f"{item['id']} depends on {d}, which is not in the backlog")
            elif seen[d] >= seen[item["id"]]:
                fails.append(f"{item['id']} depends on {d}, which is not earlier in the backlog")


# ---- Domain architecture: L1 context map, L2 context files, glossary, ownership (work/domain_architecture_spec.md) ----

SUBDOMAINS = ("core", "supporting", "generic")
RELATIONSHIPS = ("customer-supplier", "conformist", "acl", "shared-kernel", "published-language", "partnership")
PLATFORM = "_platform"
SHARED = "_shared"
PLATFORM_CAP = 0.20
CTX_ID = r"[a-z][a-z0-9-]*"


def domain_dir(fac):
    return fac.root / "docs" / "domain"


def has_domain(fac):
    return (domain_dir(fac) / "l1-context.md").exists()


def parse_l1(text):
    """({ctx id: {"type", "purpose"}}, [(downstream, upstream, relationship)], [problems]).
    Contexts: `- <id>: core|supporting|generic - <purpose>` under `## Contexts`.
    Relationships: `- <downstream> -> <upstream>: <type>` under `## Relationships`."""
    problems, contexts, rels = [], {}, []
    secs = _sections(text, 2)
    if not re.search(r"\S", secs.get("Actors and external systems", "")):
        problems.append("l1-context.md has no `## Actors and external systems` section")
    body = secs.get("Contexts")
    if body is None:
        problems.append("l1-context.md has no `## Contexts` section")
        body = ""
    for line in body.splitlines():
        if not line.startswith("- "):
            continue
        m = re.match(rf"- ({CTX_ID}):\s*(\w+)\s*(?:[-\u2013\u2014]\s*(.*))?$", line)
        if not m:
            problems.append(f"context line must be `- <id>: core|supporting|generic - <purpose>`: {line[:60]!r}")
            continue
        cid, kind, purpose = m.group(1), m.group(2).lower(), (m.group(3) or "").strip()
        if kind not in SUBDOMAINS:
            problems.append(f"context {cid}: subdomain type must be core, supporting or generic, not {kind!r}")
        if not purpose:
            problems.append(f"context {cid}: no purpose")
        if cid in contexts:
            problems.append(f"context {cid} is listed twice")
        contexts[cid] = {"type": kind, "purpose": purpose}
    if not contexts and body.strip():
        problems.append("`## Contexts` lists no contexts")
    for line in secs.get("Relationships", "").splitlines():
        if not line.startswith("- "):
            continue
        m = re.match(rf"- ({CTX_ID})\s*->\s*({CTX_ID}):\s*([A-Za-z-]+)", line)
        if not m:
            problems.append(f"relationship line must be `- <downstream> -> <upstream>: <type>`: {line[:60]!r}")
            continue
        a, b, kind = m.group(1), m.group(2), m.group(3).lower()
        if kind not in RELATIONSHIPS:
            problems.append(f"relationship {a} -> {b}: type must be one of {', '.join(RELATIONSHIPS)}, not {kind!r}")
        for c in (a, b):
            if c not in contexts:
                problems.append(f"relationship {a} -> {b} names {c}, which is not a context")
        rels.append((a, b, kind))
    return contexts, rels, problems


def load_ownership(fac):
    """({ctx or _shared: [globs]}, [problems]). Read-only parser shared by plan-check, scope-check and doctor."""
    out, _prov, problems = load_ownership_full(fac)
    return out, problems


def load_ownership_full(fac):
    """(ownership, provisional context ids, problems). `_provisional` lists contexts whose globs the planner
    proposed but no job has confirmed yet: scope-check reports their violations instead of failing the job."""
    path = domain_dir(fac) / "ownership.json"
    if not path.exists():
        return {}, set(), ["docs/domain/ownership.json is missing"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return {}, set(), [f"docs/domain/ownership.json is not valid JSON: {e}"]
    if not isinstance(data, dict):
        return {}, set(), ["docs/domain/ownership.json must be an object of {context: [globs]}"]
    problems, out, prov = [], {}, set()
    raw_prov = data.pop("_provisional", [])
    if isinstance(raw_prov, list) and all(isinstance(c, str) for c in raw_prov):
        prov = set(raw_prov)
    else:
        problems.append("ownership.json: `_provisional` must be a list of context ids")
    for ctx, globs in data.items():
        if not isinstance(globs, list) or not all(isinstance(g, str) and g.strip() for g in globs):
            problems.append(f"ownership.json: {ctx!r} must be a list of non-empty path globs")
            continue
        bad = [g for g in globs if g.startswith("/") or ".." in g.split("/")]
        if bad:
            problems.append(f"ownership.json: {ctx!r} has paths outside the project: {', '.join(bad)}")
            continue
        out[ctx] = [g.strip() for g in globs]
    for c in sorted(prov - set(out)):
        problems.append(f"ownership.json: `_provisional` names {c!r}, which owns no paths")
    return out, prov, problems


def glob_prefix(glob):
    """The static directory part of a glob: `src/billing/**` -> `src/billing`."""
    parts = []
    for part in glob.strip("/").split("/"):
        if re.search(r"[*?\[]", part):
            break
        parts.append(part)
    return "/".join(parts)


def globs_overlap(a, b):
    pa, pb = glob_prefix(a), glob_prefix(b)
    return pa == pb or pa == "" or pb == "" or pa.startswith(pb + "/") or pb.startswith(pa + "/")


def path_owner(ownership, rel):
    """Context owning a project-relative path: the longest matching static prefix, or None."""
    best, best_len = None, -1
    for ctx, globs in ownership.items():
        for g in globs:
            if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.rstrip("/") + "/*") or \
                    (g.endswith("/**") and rel.startswith(g[:-2])) or rel.startswith(g.rstrip("/") + "/"):
                n = len(glob_prefix(g))
                if n > best_len:
                    best, best_len = ctx, n
    return best


def _check_domain(fac, fails, warns, brownfield=False):
    dom = domain_dir(fac)
    try:
        l1_text = (dom / "l1-context.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        fails.append("docs/domain/l1-context.md is missing")
        return {}
    contexts, _rels, problems = parse_l1(l1_text)
    fails.extend(problems)
    ownership, problems = load_ownership(fac)
    fails.extend(problems)
    for ctx, info in contexts.items():
        if not ownership.get(ctx):
            fails.append(f"context {ctx}: no owned paths in ownership.json")
        l2 = dom / "contexts" / f"{ctx}.md"
        if not l2.exists():
            fails.append(f"context {ctx}: docs/domain/contexts/{ctx}.md is missing")
        else:
            heads = _sections(l2.read_text(encoding="utf-8"), 2)
            for tok in re.findall(r"`([^`\s]+)`", heads.get("Published interfaces", "")):
                if "/" in tok and not tok.startswith("/") and ownership.get(ctx) and \
                        not any(globs_overlap(tok, g) and (glob_prefix(tok) + "/").startswith(glob_prefix(g) + "/")
                                for g in ownership[ctx]):
                    warns.append(f"context {ctx}: published interface path `{tok}` is outside its owned paths")
        if l2.exists() and info["type"] == "core":
            if not re.search(r"^- \S", heads.get("Aggregates and invariants", ""), re.M):
                warns.append(f"core context {ctx} lists no aggregates or invariants")
    for ctx in ownership:
        if ctx not in contexts and ctx not in (SHARED, PLATFORM):
            fails.append(f"ownership.json names {ctx!r}, which is not a context in l1-context.md")
    names = [c for c in ownership if c != SHARED]
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            clash = [(x, y) for x in ownership[a] for y in ownership[b] if globs_overlap(x, y)]
            if clash:
                fails.append(f"ownership overlap: {a} `{clash[0][0]}` and {b} `{clash[0][1]}`")
    if brownfield:
        for ctx, globs in ownership.items():
            for g in globs:
                pre = glob_prefix(g)
                if pre and not (fac.root / pre).exists():
                    fails.append(f"ownership.json: {ctx} path `{g}` does not exist")
    # glossary: `- **Term** (<ctx>): definition. Avoid: a, b`
    gl = dom / "glossary.md"
    if not gl.exists():
        fails.append("docs/domain/glossary.md is missing")
    else:
        seen = set()
        for line in gl.read_text(encoding="utf-8").splitlines():
            if not line.startswith("- "):
                continue
            m = re.match(rf"- \*\*(.+?)\*\*\s*\(({CTX_ID})\):\s*\S", line)
            if not m:
                fails.append(f"glossary line must be `- **Term** (<context>): definition`: {line[:60]!r}")
                continue
            term, ctx = m.group(1).strip(), m.group(2)
            if ctx not in contexts:
                fails.append(f"glossary term {term!r} is owned by {ctx}, which is not a context")
            if (term.lower(), ctx) in seen:
                fails.append(f"glossary term {term!r} is defined twice in {ctx}")
            seen.add((term.lower(), ctx))
    if len(contexts) > 7:
        warns.append(f"{len(contexts)} contexts; more than about 7 is a lot for a first release")
    return contexts


def _check_containers(fac, arch, contexts, fails):
    """`## Containers` in architecture.md: `- <name>: hosts <ctx>[, <ctx>] ...`, every context hosted somewhere."""
    body = _sections(arch, 2).get("Containers")
    if body is None:
        fails.append("no `## Containers` section (each deployable unit and the context(s) it hosts)")
        return
    hosted = set()
    for line in body.splitlines():
        m = re.match(r"- (.+?):\s*hosts\s+(.+)$", line)
        if not m:
            if line.startswith("- "):
                fails.append(f"container line must be `- <name>: hosts <context>[, <context>]`: {line[:60]!r}")
            continue
        ids = re.findall(CTX_ID, m.group(2).split(" - ")[0].split(";")[0])
        ids = [i for i in ids if i in contexts or i in (PLATFORM, SHARED)]
        if not ids:
            fails.append(f"container {m.group(1)!r} hosts no known context")
        hosted.update(ids)
    for ctx in contexts:
        if ctx not in hosted:
            fails.append(f"context {ctx} is not hosted by any container")


def _check_context_tags(fac, contexts, fails, only_unchecked=False):
    ok = set(contexts) | {PLATFORM}
    epics = fac.root / "docs" / "epics.md"
    if epics.exists():
        for b in re.split(r"^(?=## E\d+)", epics.read_text(encoding="utf-8"), flags=re.M):
            m = re.match(r"## (E\d+)", b)
            if not m:
                continue
            c = re.search(r"^Context:\s*(\S+)\s*$", b, re.M)
            if not c:
                fails.append(f"{m.group(1)}: no `Context: <context>` line")
            elif c.group(1) not in ok:
                fails.append(f"{m.group(1)}: context {c.group(1)!r} is not in l1-context.md (or {PLATFORM})")
    items = backlog_items(fac)
    counted = [i for i in items if not (only_unchecked and i["checked"])]
    for i in counted:
        if i["ctx"] is None:
            fails.append(f"{i['id']}: no `[ctx:<context>]` tag")
        elif i["ctx"] not in ok:
            fails.append(f"{i['id']}: context {i['ctx']!r} is not in l1-context.md (or {PLATFORM})")
    tagged = [i for i in items if i["ctx"]]
    if tagged and sum(1 for i in tagged if i["ctx"] == PLATFORM) / len(tagged) > PLATFORM_CAP:
        fails.append(f"more than {int(PLATFORM_CAP * 100)}% of backlog items are {PLATFORM}; move domain work "
                     f"into its context")


def cmd_plan_check(fac, args):
    """Gate for a planner pass: the documents have the sections and labels the human approves against."""
    docs = fac.root / "docs"
    pass_ = str(args.pass_).upper()
    if pass_ not in ("2A", "2", "3", "DOC"):
        raise Refused("plan-check runs after pass 2A, 2, 3 or DOC")
    fails, assumed, warns = [], [], []
    contexts = {}
    if pass_ == "2A" or (pass_ == "DOC" and has_domain(fac)) or (has_domain(fac) and pass_ in ("2", "3")):
        contexts = _check_domain(fac, fails, warns, brownfield=pass_ == "DOC")

    def read(path):
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            fails.append(f"{path.relative_to(fac.root)} is missing")
            return ""
    brief = (docs / "features" / args.feature / "brief.md") if args.feature else (docs / "product-brief.md")
    brief_text = brief.read_text(encoding="utf-8") if brief.exists() else ""
    if not args.feature and (docs / "vision.md").exists():
        brief_text += (docs / "vision.md").read_text(encoding="utf-8")
    accelerator = bool(re.search(r"\baccelerator\b", brief_text, re.I))
    if pass_ == "2A":
        pass
    elif pass_ in ("2", "DOC"):
        arch, decisions = read(docs / "architecture.md"), read(docs / "decisions.md")
        if contexts and not args.feature:
            _check_containers(fac, arch, contexts, fails)
        if args.feature:
            section = _sections(arch, 2).get(f"Feature: {args.feature}")
            if section is None:
                fails.append(f"architecture.md has no `## Feature: {args.feature}` section")
            else:
                _check_architecture(section, 3, accelerator, fails)
            assumed = _check_decisions(decisions, fails, only_feature=args.feature, contexts=contexts)
        else:
            if arch:
                _check_architecture(arch, 2, accelerator, fails, doc=pass_ == "DOC")
                _check_commands(arch, fails, allow_none=pass_ == "DOC")
            if decisions:
                assumed = _check_decisions(decisions, fails, doc=pass_ == "DOC", contexts=contexts)
    else:
        _check_backlog(fac, fails)
        if contexts:
            _check_context_tags(fac, contexts, fails, only_unchecked=bool(args.feature))
    label = ("FEATURE " + args.feature + " " if args.feature else "") + "PASS " + pass_
    for f in fails:
        print(f"FAIL {f}")
    for w in warns:
        print(f"WARN {w}")
    if assumed:
        print("ASSUMED (show these to the human at the gate): " + "; ".join(assumed))
    print(f"{label}: {'ok' if not fails else str(len(fails)) + ' problem(s)'}")
    return 1 if fails else 0


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
        ctx = re.search(r"\[ctx:([A-Za-z0-9_-]+)\]", text)
        items.append({"id": item_id, "checked": m.group(1) == "x", "deps": deps, "text": text,
                      "ctx": ctx.group(1) if ctx else None})
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



REVIEW_DRIFT_FILES = 10
UNOWNED_EXEMPT = re.compile(r"(^|/)\.[^/]+|^[^/]+$")  # dotfiles and files at the project root


def domain_health(fac):
    """Read-only freshness report for docs/domain/: [(level, what, fix)]. Never fails a project."""
    out, fails, warns = [], [], []
    _check_domain(fac, fails, warns)
    for f in fails:
        out.append(("WARN", f"domain model: {f}", "fix the file, or re-run /software-factory:factory plan or document"))
    ownership, provisional, bad = load_ownership_full(fac)
    if bad:
        return out
    tracked = [n for n in fac.git("ls-files", check=False).stdout.split("\n") if n]
    for ctx, globs in sorted(ownership.items()):
        if ctx in provisional:
            continue  # proposed paths that no job has confirmed yet are expected to be missing
        for g in globs:
            if not any(fnmatch.fnmatch(n, g) or n.startswith(g.rstrip("/*") + "/") for n in tracked):
                out.append(("WARN", f"{ctx} owns `{g}` but no tracked file matches it",
                            "correct docs/domain/ownership.json, or list the context under `_provisional`"))
    code = [n for n in tracked if not n.startswith(STATE_PATHS) and not UNOWNED_EXEMPT.search(n)
            and not SECONDARY.search(n) and path_owner(ownership, n) is None]
    if code:
        out.append(("WARN", f"{len(code)} tracked file(s) belong to no context, e.g. {', '.join(code[:4])}",
                    "add their paths to a context (or `_shared`) in docs/domain/ownership.json"))
    for f in sorted((domain_dir(fac) / "contexts").glob("*.md")):
        ctx = f.stem
        m = re.search(r"^Reviewed-at:\s*([0-9a-f]{7,40})\s*$", f.read_text(encoding="utf-8"), re.M)
        if not m or not ownership.get(ctx):
            continue
        if not fac.rev(m.group(1)):
            out.append(("WARN", f"{ctx}: Reviewed-at {m.group(1)} is not a commit in this repository",
                        f"re-read docs/domain/contexts/{ctx}.md and update its Reviewed-at line"))
            continue
        pathspecs = [g.rstrip("/*") or "." for g in ownership[ctx]]
        changed = [n for n in fac.git("diff", "--name-only", f"{m.group(1)}..HEAD", "--", *pathspecs,
                                      check=False).stdout.split("\n") if n]
        if len(changed) >= REVIEW_DRIFT_FILES:
            out.append(("WARN", f"{ctx}: {len(changed)} files it owns changed since its context file was reviewed "
                                f"({m.group(1)[:8]})",
                        f"re-read docs/domain/contexts/{ctx}.md against the code and update its Reviewed-at line"))
    return out


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
                    f"if no session is working on it: /software-factory:factory recover {j['id']}")
        for b in sorted(branches):
            if b.split("/", 1)[1] not in jobs:
                add("WARN", f"branch {b} has no job on the board", f"inspect it; delete it yourself if unwanted")
        try:
            cfg = fac.load_config()
        except Refused:
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # already reported above; keep checking with defaults
        root = os.path.realpath(str(worktree_root(fac, cfg)))
        retain = config_value(cfg, "workspace.retain_completed")
        wts = registered_worktrees(fac)
        owned = {}
        for j in board["jobs"]:
            wt = job_worktree(j)
            if wt:
                owned[os.path.realpath(wt)] = j
                entry = _find_worktree(fac, wt)
                if j["stage"] not in ("done", "cancelled") and (not entry or entry.get("prunable")):
                    add("WARN", f"{j['id']}'s worktree {wt} is missing", f"/software-factory:factory recover {j['id']}")
                elif j["stage"] in ("done", "cancelled") and entry and not entry.get("prunable"):
                    dirty = _dirty(fac, wt)
                    if dirty:
                        add("WARN", f"{j['id']} is {j['stage']} but its worktree has uncommitted changes: "
                                    f"{', '.join(dirty[:5])}", f"inspect {wt}; it is never removed automatically")
                    elif not retain:
                        add("WARN", f"{j['id']} is {j['stage']} and its clean worktree is still there",
                            f"python3 factory/factoryctl.py worktree remove {j['id']}")
        for w in wts:
            if w.get("prunable"):
                add("WARN", f"stale worktree record {w.get('worktree')} (directory gone)", "git worktree prune")
            elif w["real"].startswith(root + os.sep) and w["real"] not in owned:
                add("WARN", f"worktree {w.get('worktree')} belongs to no job on the board",
                    f"inspect it, then git worktree remove {w.get('worktree')} if it is unneeded")
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
        if has_domain(fac):
            for level, what, fix in domain_health(fac):
                add(level, what, fix)
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
    for name, text in (("snapshot", "record the repository before an agent runs (stored under .git)"),
                       ("scope-check", "after the agent: fail if it changed anything its role may not")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--role", required=True, choices=ROLES)
        p.add_argument("--job")
        p.add_argument("--pass", dest="pass_", help="planner pass: 1, 2A, 2, 3 or DOC")
        p.add_argument("--feature", help="planner: the feature plan's slug")
    p = sub.add_parser("worktree", help="the job's own worktree outside the project: create (idempotent, repairs), "
                                        "validate, remove (clean and finished only)")
    p.add_argument("action", choices=["create", "validate", "remove"])
    p.add_argument("id")
    p = sub.add_parser("recover", help="after an interruption: report the job's state and the step to resume from")
    p.add_argument("id")
    p.add_argument("--confirmed", action="store_true", help="the human confirmed no session is working on it")
    p = sub.add_parser("cancel", help="move a job to cancelled; deletes nothing")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p.add_argument("--action-id")
    p = sub.add_parser("plan-check", help="after a planner pass: required sections, exact commands, decision labels, "
                                          "backlog ids (read-only)")
    p.add_argument("--pass", dest="pass_", required=True, help="2A (domain), 2, 3 or DOC")
    p.add_argument("--feature", help="the feature plan's slug")
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

# Claude-Native Software Factory Specification

Status: Proposed development specification  
Target repository: `/Users/sanjaysankolli/Documents/GitHub/software-factory`  
Baseline inspected: v1.8.0, commit `117b7a6`  
Date: 2026-10-26

## 1. Purpose

Evolve the existing Claude Code plugin into a reliable, self-contained software factory that takes an idea through shaping, design, planning, implementation, review, approval, and learning without requiring external orchestration or project-management products.

The factory remains centered on:

- Claude Code;
- Claude plugin skills and agents;
- Git branches and native Git worktrees;
- Markdown and JSON state committed with the project;
- deterministic project commands for verification;
- human approval at product, architecture, exception, and merge boundaries.

This is an incremental hardening of the current factory, not a replacement platform.

## 2. Explicit exclusions

The following are out of scope:

- Cursor and Cursor CLI;
- Orca;
- Herdr;
- Linear;
- GitHub Issues as a required backlog;
- multiple coding-agent providers;
- dynamic model/provider routing;
- cloud agent services;
- databases for factory state;
- vector memory, Mem0, Graphify, or OpenDeepWiki;
- automatic production deployment;
- unattended changes to governance rules;
- autonomous waiver of failed tests, security findings, or human sign-off;
- mandatory pull requests for local operation.

GitHub may continue to host the repository. Pull requests and protected branches may be supported later as optional delivery mechanisms, but the factory must not depend on them.

## 3. Product definition

### 3.1 Product statement

The software factory is a Claude Code plugin that turns a human idea into reviewed, verified code using an inspectable, resumable, file-based workflow.

### 3.2 Primary users

- A technical product owner shaping an idea into an implementable product.
- An architect or senior developer approving design decisions.
- A developer supervising autonomous implementation.
- A maintainer operating and upgrading the factory plugin.

### 3.3 Core outcome

A user can begin with either a new idea or an existing codebase and produce a sequence of small, approved, tested changes while retaining complete visibility into decisions, specifications, agent findings, verification evidence, and factory state.

### 3.4 Success measures

- Every completed job is traceable from intent to merge evidence.
- An interrupted run can be resumed without reconstructing state from conversation history.
- Invalid state transitions and unauthorized agent writes are detected before merge.
- Plugin upgrades preserve user-owned state.
- Core workflows are covered by repeatable automated regression scenarios.
- A project can operate the full factory without any service other than Claude Code and Git.

## 4. Design principles

1. **Files are durable truth.** Conversation context is transient; approved artifacts live in the repository.
2. **One state writer.** Only the factory orchestrator writes workflow state.
3. **Humans own intent.** Claude may recommend, but product scope and material design decisions require human approval.
4. **Agents work from explicit contracts.** Every role has defined inputs, outputs, and write scope.
5. **Verification outranks assertion.** Agent statements never replace test results or repository evidence.
6. **Autonomy does not lower gates.** Headless execution changes waiting behavior, not quality requirements.
7. **Small vertical slices.** Backlog items should produce observable outcomes and remain independently verifiable.
8. **Safe failure.** Ambiguity, invalid state, missing evidence, or tool failure parks the job rather than guessing.
9. **Native dependencies first.** Prefer Claude Code, Git, Python standard library, and project-native commands.
10. **Migration is part of the feature.** Every schema or template change includes an idempotent upgrade path.

## 5. Existing capabilities to preserve

The following v1.8.0 behavior is required and must not regress:

- idempotent initialization and upgrades;
- preservation of jobs, backlog entries, docs, learnings, inbox state, and user-authored context;
- `brainstorm` divergence and MoSCoW convergence;
- `grill` decision-frontier interviews and resumable logs;
- three-pass product planning with approval after each pass;
- three-pass feature planning against recorded architecture;
- existing-code documentation before feature planning;
- scaffold-first product backlog;
- explicit backlog dependencies;
- quick and full job tracks;
- specification readiness gate;
- headless run, parking, inbox, and resume behavior;
- human sign-off criteria;
- parallel reviewers within a review round;
- triage before rework or approval;
- verification from an isolated snapshot;
- approval based on specifications, reviews, and verification evidence;
- human approve and rework actions;
- merge-conflict recovery;
- recurring retrospectives and follow-up tracking;
- local dashboard with untrusted action validation;
- default-branch detection rather than assuming `main`.

## 6. Target workflow

```text
                           ┌──────── human notes/rework ────────┐
                           │                                    │
Idea → Shape → Design → Plan → Spec → Build → Review → Triage → Verify → Approve → Merge
          ↑          human gates         ↑          │                        │
          └──── questions/decisions ─────┘          └── patch/rework ───────┘
                                                                                │
                                                                                v
                                                                              Retro
```

### 6.1 New product

```text
/software-factory:init
/software-factory:factory plan <vision>
/software-factory:factory run
```

Planning runs:

1. Shape and clarify.
2. Design architecture and decisions.
3. Produce epics and dependency-ordered backlog.

### 6.2 Existing product

```text
/software-factory:init
/software-factory:factory document
/software-factory:factory plan <capability>
/software-factory:factory run
```

### 6.3 One-off change

```text
/software-factory:factory <feature>
```

The one-off path still requires a ready specification, verification, applicable reviews, triage, and approval.

## 7. Functional requirements

## FR-1: Canonical workflow state

`factory/board.json` remains the canonical workflow snapshot. Only the orchestrator may write it.

The board schema version must be incremented from 1 to 2 when worktree and execution-evidence fields are introduced.

Required top-level structure:

```json
{
  "version": 2,
  "plugin_version": "<semver>",
  "default_branch": "<branch>",
  "jobs": [],
  "retro": {
    "done_since": 0,
    "last": null
  },
  "run": null
}
```

Each job must contain:

```json
{
  "id": "001-short-slug",
  "title": "<title>",
  "track": "quick|full|null",
  "stage": "spec|build|review|triage|verify|decision|needs-human|done",
  "round": 0,
  "branch": "factory/001-short-slug",
  "item": "B1|null",
  "epic": "E1|null",
  "assumptions": 0,
  "extra_reviewers": [],
  "skip_reviewers": [],
  "reviews": {},
  "decision": null,
  "parked_reason": null,
  "workspace": {
    "kind": "worktree|legacy-checkout",
    "path": null,
    "base_commit": null,
    "created_at": null
  },
  "execution": {
    "attempt": 0,
    "started_at": null,
    "completed_at": null,
    "last_error": null
  },
  "evidence": {
    "head_commit": null,
    "verified_commit": null,
    "verified_at": null
  },
  "updated": "<UTC ISO 8601>"
}
```

### Acceptance criteria

- Invalid stages or missing required fields are rejected before a command mutates state.
- Existing v1 boards migrate without changing semantic job state.
- Unknown future fields are preserved during read-modify-write.
- All timestamps use UTC ISO 8601 with an offset.
- A malformed board stops the command with a diagnostic and does not rewrite the file.

## FR-2: Explicit state-transition validation

All transitions must pass through one orchestrator-owned transition procedure.

Allowed primary transitions:

```text
spec → build
spec → needs-human
build → review
build → needs-human
review → triage
triage → build
triage → verify
triage → needs-human
verify → decision
verify → build
verify → needs-human
decision → done
decision → build
decision → needs-human
needs-human → spec|build|done
```

Transition records must capture:

- previous and next stage;
- timestamp;
- reason;
- current job attempt and round;
- relevant evidence path;
- orchestrator action or human action ID.

Write transition events to `factory/events.jsonl`. This is an append-only audit log; `board.json` remains the current snapshot.

### Acceptance criteria

- Disallowed transitions fail without changing the board.
- Replaying the same action ID does not apply it twice.
- The dashboard never writes events or board state.
- A truncated final event line is reported and ignored; prior valid events remain readable.
- Sensitive prompt or command content is not copied into events.

## FR-3: Agent write-scope enforcement

Prompt instructions are insufficient as the only boundary. The orchestrator must validate repository changes after every agent invocation.

Role scopes:

| Role | Permitted writes |
|---|---|
| Planner | Files explicitly assigned for the current planning pass |
| Spec writer | `factory/jobs/<id>/spec.md` |
| Builder | Product files, tests, allowed `Docs files`, and `factory/jobs/<id>/build.md` on the job branch |
| Reviewer | Its assigned `round-<N>/<reviewer>.md` only |
| Triager | `round-<N>/triage.md` only |
| Approver | `factory/jobs/<id>/decision.md` only |
| Retro | `factory/retro-proposal.md` only |
| Orchestrator | Board, inbox, events, verification evidence, backlog status, and other declared state files |

Validation must inspect staged, unstaged, committed, and untracked changes created during the agent step.

### Acceptance criteria

- A reviewer source-code edit fails the step.
- A non-orchestrator board edit fails the step.
- An unexpected untracked file fails the step unless explicitly permitted.
- A builder may change only documentation named by `Docs files`.
- A scope violation is recorded, the job is parked or retried according to policy, and state does not advance.
- The validator never deletes unexpected files automatically.

## FR-4: Native Git worktree isolation

Each build job must run in a native Git worktree owned by that job. No external worktree manager is required.

Default location:

```text
<project-parent>/.software-factory-worktrees/<project-name>/<job-id>/
```

The path may be configurable, but it must not live inside the tracked project directory.

Lifecycle:

1. Confirm the primary checkout is on the default branch.
2. Confirm there are no prohibited primary-checkout changes.
3. Record the default branch commit as `base_commit`.
4. Create or recover `factory/<id>`.
5. Create the job worktree.
6. Invoke the builder with the worktree as its working directory.
7. Validate branch, changes, commit, and evidence.
8. Retain the worktree while rework is possible.
9. Remove it only after merge or explicit cancellation and only when clean.

### Acceptance criteria

- The primary checkout remains on the default branch during build and review.
- Worktree creation is idempotent.
- A path belonging to another repository or job is never reused.
- Dirty worktrees are never removed automatically.
- A crashed job can recover its existing worktree.
- Branch identity and base commit are validated before every builder run.
- Sequential operation remains the default.

## FR-5: Claude-native agent dispatch

All implementation and review agents are Claude Code plugin agents declared under `agents/`.

No provider or runner selection is exposed. Agent model selection remains in agent frontmatter.

The orchestrator must:

- spawn the correct namespaced agent;
- provide job ID, worktree, branch, default branch, round, and relevant paths;
- validate the required output grammar;
- retry one malformed agent output;
- park after repeated malformed output or tool failure;
- record sanitized failure evidence.

### Acceptance criteria

- The factory contains no Cursor, Orca, Herdr, Linear, or multi-provider configuration.
- Agent dispatch fails clearly when the plugin agent cannot be loaded.
- A failed agent cannot advance the job.
- A replacement builder can resume entirely from Git and job files.

## FR-6: Shaping and design contracts

Preserve the existing brainstorm, grill, and planner behavior, with these additions to architecture planning:

- Identify principal module boundaries and public interfaces.
- Identify reusable core versus project/customer-specific configuration when the product is an accelerator.
- State security, privacy, observability, reliability, and support implications when applicable.
- Label every design statement as accepted, assumed, or inferred where the distinction matters.

These additions belong in existing artifacts; do not create mandatory new documents unless the project requires them.

### Acceptance criteria

- Product PASS 2 cannot complete without exact test and run commands.
- Architecture decisions cite their context and consequences.
- Feature planning does not silently supersede an earlier decision.
- Assumed decisions remain visible at the human approval gate.
- Accelerator plans name core and extension seams.

## FR-7: Dependency-aware autonomous run

`/software-factory:factory run` remains a sequential autonomous outer loop.

Ready means:

- backlog item is unchecked;
- every `depends:` item is complete;
- item is not already represented by an active or parked job;
- no conflicting plan is running;
- default-branch safety check passes;
- budget and job limits permit another job.

The loop must stop for:

- no ready work;
- configured job or time budget;
- explicit `--until` target;
- repeated parked/failing jobs under the existing policy;
- red default branch;
- merge permission failure;
- invalid factory state.

### Acceptance criteria

- A parked job blocks only direct and transitive dependents.
- Independent ready work may continue according to the existing stop policy.
- Backlog items are checked only after merge.
- Resume never duplicates an already merged or active job.
- Sequential behavior is deterministic for the same backlog state.

## FR-8: Deterministic verification

Verification must run against the exact candidate commit or exact prospective merge result.

Evidence in `factory/jobs/<id>/verify.md` must include:

- `VERIFY: PASS|FAIL` first line;
- job branch and candidate commit;
- default branch and commit;
- whether branch or merge-result content was tested;
- commands executed;
- exit codes;
- sanitized output tail;
- diff statistics;
- start/end timestamps.

`evidence.verified_commit` must equal the candidate commit considered by the approver. Any later commit invalidates verification and approval.

### Acceptance criteria

- Missing verification evidence forces escalation.
- Failed test, lint, or required build prevents approval.
- Changing the job head invalidates prior verification.
- Commands run with a throwaway home and documented telemetry opt-outs.
- Verification never runs from an accidentally switched primary checkout.

## FR-9: Review and approval

Preserve quick/full track behavior:

- quick: security and code, plus requested UI/UX reviewers;
- full: security, code, UI, and UX, except explicitly permitted UI/UX skips;
- security and code cannot be skipped on full track.

The approver must approve only when:

- all acceptance criteria have evidence;
- final required reviews pass or have sound triage dispositions;
- verification passes for the current candidate;
- no decision item remains;
- required human sign-off is recorded.

### Acceptance criteria

- Review outputs use their required first-line grammar.
- Findings cite concrete locations and consequences.
- Triage verifies each finding before routing it.
- Human exceptions are recorded as amendments, never hidden.
- The approver cannot merge.
- Merge occurs only through the orchestrator after approval.

## FR-10: Configuration

Keep configuration minimal and Claude-specific.

Committed factory configuration: `factory/config.json`

```json
{
  "version": 1,
  "workspace": {
    "mode": "worktree",
    "root": "auto",
    "retain_completed": false
  },
  "run": {
    "parallel_jobs": 1,
    "max_consecutive_failures": 2
  },
  "verification": {
    "run_test": true,
    "run_lint": true,
    "run_build": true
  },
  "audit": {
    "events": true
  }
}
```

For this specification, `parallel_jobs` must equal `1`. Values greater than one are reserved for a future version and must fail validation rather than be silently ignored.

### Acceptance criteria

- `init` creates the file only when absent.
- Upgrades add missing fields without replacing user values.
- Unknown fields are preserved.
- Secrets are forbidden in the configuration.
- Invalid values stop execution with a field-specific message.

## FR-11: Commands

Preserve all current commands and add:

```text
/software-factory:factory doctor
/software-factory:factory recover <id>
/software-factory:factory cancel <id> [reason]
```

### `doctor`

Read-only checks:

- plugin/project version compatibility;
- board and config schema validity;
- Git repository and default branch;
- test/lint/build commands;
- required factory files;
- job branch/worktree consistency;
- stale or orphan worktrees;
- inbox/action consistency;
- interrupted jobs;
- prohibited uncommitted changes.

It reports exact repair commands but changes nothing.

### `recover <id>`

- validates that no live agent is working on the job;
- finds the branch and worktree;
- reconstructs the safe resume point from files and board state;
- requires human confirmation when state is ambiguous;
- resumes through the normal loop.

### `cancel <id> [reason]`

- requires a known non-done job;
- records who cancelled it and why;
- never deletes a dirty worktree;
- never deletes the branch automatically;
- marks the job cancelled in audit history;
- unblocks no dependent work unless the human separately changes the backlog.

Board schema must add `cancelled` if cancellation is implemented.

## FR-12: Upgrade behavior

Upgrade remains idempotent and confirmation-based.

For this release, `init` must:

1. Detect v1 board/config absence.
2. Inventory proposed changes without writing.
3. Identify in-flight jobs.
4. Obtain one confirmation.
5. Commit existing factory state before modification when necessary.
6. Refresh plugin-owned files.
7. Migrate board v1 to v2.
8. Create `factory/config.json` and `factory/events.jsonl` if absent.
9. Preserve jobs, docs, backlog lines, learnings, inbox, follow-ups, and user context.
10. Report how to resume every in-flight job.

### Acceptance criteria

- Declining confirmation changes nothing.
- Running upgrade twice produces no second diff.
- Older jobs receive compatible defaults without changing stage or decision.
- No worktree is created during upgrade.
- A newer unknown board version causes a safe stop.
- User-edited, non-plugin-owned files are never overwritten.

## 8. Non-functional requirements

### NFR-1: Safety

- Never run destructive Git commands automatically.
- Never delete branches or dirty worktrees automatically.
- Never execute commands embedded in issue text, dashboard actions, agent output, or imported documents.
- Treat repository content and agent output as untrusted data.
- Never expose secrets in prompts, reports, events, or logs.

### NFR-2: Reliability

- State writes use atomic replacement.
- Append-only events use one complete JSON object per line.
- Commands are idempotent where retry is possible.
- Repeated dashboard actions cannot apply twice.
- Failure leaves enough evidence for recovery.

### NFR-3: Portability

- Support macOS and Linux environments supported by Claude Code and Git.
- Use Python standard library for shipped dashboard/server utilities.
- Avoid dependencies requiring a daemon or database.
- Do not assume the default branch is named `main`.

### NFR-4: Performance

- Status and doctor commands should complete within five seconds on ordinary repositories, excluding project test execution.
- Factory bookkeeping must not scan generated dependency directories unless required.
- Agent prompts should load only job-relevant artifacts plus canonical project context.

### NFR-5: Observability

Every job must expose:

- current stage and round;
- branch and worktree;
- last update;
- parked reason;
- test/verification result;
- final decision;
- audit events.

The dashboard remains a view and action-queue surface, not an authority.

## 9. File ownership

| Path | Owner |
|---|---|
| `factory/board.json` | Orchestrator |
| `factory/config.json` | Init and explicit human-approved configuration changes |
| `factory/events.jsonl` | Orchestrator append only |
| `factory/backlog.md` | Planner during planning; orchestrator checkbox on merge |
| `factory/inbox.md`, `factory/inbox.json` | Orchestrator |
| `factory/actions.jsonl` | Local server |
| `factory/actions-applied.json` | Orchestrator |
| `factory/jobs/<id>/spec.md` | Spec writer; orchestrator appends controlled amendments |
| `factory/jobs/<id>/build.md` | Builder; orchestrator may append rework request |
| `factory/jobs/<id>/round-N/*.md` | Corresponding reviewer or triager |
| `factory/jobs/<id>/verify.md`, `branch.diff` | Orchestrator |
| `factory/jobs/<id>/decision.md` | Approver or recorded human approval path |
| `factory/learnings.md` | Orchestrator from approved retro output |
| `docs/` planning files | Planner according to pass-specific rules |
| `CLAUDE.md` project block | Init/planner only after human approval |

## 10. Automated test specification

Create a committed regression harness under:

```text
tests/
  fixtures/
  scenarios/
  helpers/
  run.py
```

Use temporary Git repositories and a throwaway home. Agent-dependent scenarios may use deterministic fake-agent outputs for state-machine testing, plus a smaller opt-in live-Claude suite.

### Required deterministic scenarios

1. Initialize an empty repository.
2. Initialize an existing repository and detect commands.
3. Re-run init with no changes.
4. Upgrade representative pre-1.8 and 1.8 projects.
5. Reject a newer unknown schema.
6. Product-plan state transitions and approval gates.
7. Feature-plan append-only behavior and ID continuation.
8. Document-existing-code flow.
9. Quick-track successful job.
10. Full-track successful job.
11. Malformed agent output and one retry.
12. Reviewer scope violation.
13. Builder protected-file violation.
14. Patch round.
15. Human rework.
16. Open decision parking.
17. Early sign-off and final sign-off escalation.
18. Failed verification.
19. Candidate commit changes after verification.
20. Merge conflict and rework.
21. Dependency blocking and independent readiness.
22. Interrupted build recovery.
23. Stale and dirty worktree handling.
24. Duplicate dashboard action.
25. Cancellation without data deletion.
26. Retro threshold and proposal approval.

### Test acceptance criteria

- `python3 tests/run.py` runs the deterministic suite.
- Tests require no network access.
- Tests do not require a Claude subscription.
- Tests never modify the developer's global Git configuration.
- Every scenario runs in a unique temporary directory.
- Failed scenarios print the retained fixture path or sufficient diagnostics.
- CI runs the deterministic suite on every pull request.

## 11. Implementation phases

## Phase 1: Regression foundation

Deliver:

- `tests/` harness;
- deterministic fake-agent fixtures;
- initialization, upgrade, planning, and core job scenarios;
- CI workflow.

Exit criteria:

- Existing documented behavior has executable coverage.
- Current tests pass before architecture changes begin.

## Phase 2: Schema and transition hardening

Deliver:

- board schema v2;
- `factory/config.json`;
- centralized validation and transitions;
- `factory/events.jsonl`;
- upgrade migration;
- `doctor` command.

Exit criteria:

- Invalid transitions cannot mutate state.
- Upgrade tests prove state preservation and idempotency.

## Phase 3: Write-scope enforcement

Deliver:

- role scope definitions;
- pre/post-agent repository snapshots;
- scope violation reports;
- state-machine integration.

Exit criteria:

- Unauthorized writes are blocked in deterministic tests.
- Existing valid agents still complete their workflows.

## Phase 4: Native worktrees

Deliver:

- worktree create/recover/validate/cleanup procedures;
- worktree fields in job state;
- builder dispatch into the worktree;
- updated verification and rework paths;
- `recover` and `cancel` commands.

Exit criteria:

- Primary checkout remains on the default branch.
- Crash/recovery and dirty-worktree scenarios pass.
- Sequential end-to-end jobs pass.

## Phase 5: Dashboard and documentation

Deliver:

- dashboard display of worktree, verification commit, and audit events;
- doctor output in the dashboard where safe;
- updated README, user guide, architecture guide, and upgrade guide;
- removal of all Cursor/Orca/Herdr/Linear concepts from the target design.

Exit criteria:

- Guides describe the actual implemented behavior.
- Dashboard actions remain untrusted and orchestrator-validated.
- A fresh user can complete new-product and existing-product walkthroughs.

## 12. Backlog

| ID | Work item | Depends on |
|---|---|---|
| SF-201 | Create fixture-based regression harness | — |
| SF-202 | Add init and upgrade scenarios | SF-201 |
| SF-203 | Add planning and document scenarios | SF-201 |
| SF-204 | Add job-loop and dashboard-action scenarios | SF-201 |
| SF-205 | Define board v2 and config schemas | SF-202, SF-204 |
| SF-206 | Centralize validated state transitions | SF-205 |
| SF-207 | Add append-only event audit | SF-206 |
| SF-208 | Implement v1-to-v2 migration | SF-205, SF-202 |
| SF-209 | Add read-only doctor command | SF-205, SF-206 |
| SF-210 | Define role write scopes | SF-204 |
| SF-211 | Implement post-agent scope validation | SF-210, SF-206 |
| SF-212 | Add scope-violation scenarios | SF-211 |
| SF-213 | Implement native worktree lifecycle | SF-205, SF-209 |
| SF-214 | Dispatch builder in job worktree | SF-213, SF-211 |
| SF-215 | Adapt verification and rework to worktrees | SF-214 |
| SF-216 | Implement recover command | SF-213, SF-206 |
| SF-217 | Implement safe cancel command | SF-213, SF-207 |
| SF-218 | Add crash, stale, and dirty-worktree scenarios | SF-215, SF-216, SF-217 |
| SF-219 | Update dashboard for worktree and audit state | SF-207, SF-215 |
| SF-220 | Update all user and maintainer documentation | SF-219 |

## 13. Release acceptance criteria

The release is acceptable when:

- all deterministic regression scenarios pass;
- a fresh project can initialize, plan, and complete its first job;
- an existing project can document, plan a capability, and complete a job;
- the primary checkout never leaves the default branch during a job;
- interrupted jobs recover without duplicate merge or lost work;
- unauthorized agent writes stop the workflow;
- approval is bound to the currently verified candidate commit;
- upgrade from v1.8.0 preserves all user state;
- a second upgrade produces no changes;
- no external service is required beyond Claude Code and Git;
- the repository contains no active Cursor, Orca, Herdr, Linear, or multi-runner integration;
- README and guides match the released behavior.

## 14. Open decisions for implementation kickoff

These decisions should be made before Phase 2:

1. Whether `factory/events.jsonl` is committed or treated as local operational history. Recommendation: commit lifecycle events but keep command output in job evidence files.
2. Exact default worktree root on macOS and Linux. Recommendation: sibling directory outside the repository, derived deterministically from the project path.
3. Whether completed clean worktrees are removed immediately or retained briefly. Recommendation: remove after merge by default, with a config option to retain.
4. Whether cancellation is a new board stage or a terminal decision field. Recommendation: terminal `cancelled` stage for clarity.
5. Whether schema validation is implemented directly in the skill instructions or by a shipped Python utility. Recommendation: a small standard-library utility with unit tests, invoked by the orchestrator.

## 15. Definition of done

Development is done when implementation, automated tests, migration, dashboard changes, README, user guide, architecture guide, and upgrade guide are complete and consistent. Manual notes that a behavior was tested are not a substitute for the committed deterministic regression suite.

# Upgrade workplan: v1.8.0 → Claude-native factory (v2)

Status: approved 2026-10-26 (D1–D6 accepted as recommended). v1.9.0 implemented on branch `release/v1.9.0`; later releases not started.
Inputs: `work/upgrade_spec.md`, the v1.8.0 repo (`117b7a6`), and the subagent-folding review (items 1 and 2).
Date: 2026-10-26

## 1. Summary

The work ships as six releases. Each one is small enough to review, upgrade through `init` and try in a sandbox.

| Release | Content | Kind of change |
|---|---|---|
| **v1.9.0** | Item 1 (skip the triager on clean rounds), item 2 (verify once, before review) | Prompt-only |
| **v1.10.0** | Phase 1: regression harness, CI, opt-in live smoke suite | Tests only, no behaviour change |
| **v2.0.0** | Phase 2: `factoryctl.py`, board v2, `config.json`, transitions, `events.jsonl`, migration, `doctor`, commit-bound verification | Schema change, one migration |
| **v2.1.0** | Phase 3: write-scope enforcement | Code + orchestrator |
| **v2.2.0** | Phase 4: native worktrees, `recover`, `cancel` | Code + orchestrator + builder |
| **v2.3.0** | Phase 5: dashboard, planner contracts (FR-6), full docs pass | UI + prompts + docs |

The board v2 schema is defined in full in v2.0.0, including the worktree fields and the `verify` and `cancelled` stages. So only one migration exists, and v2.1 and v2.2 start using fields that are already there.

## 2. Findings from reading the spec against the repo

These change the plan. Each is either handled below or listed under section 3.

**F1. The deterministic suite cannot test v1.8.0's job loop.** All workflow logic in v1.8.0 is prose in `skills/factory/SKILL.md` that the model follows. The only shipped code is `templates/factory/server.py`. Scenarios 6–26 in section 10 of the spec (transitions, patch rounds, parking, recovery) can be deterministic only after that logic moves into code the tests can call. So the Phase 1 exit criterion "existing documented behaviour has executable coverage" can't be met as written. Plan: Phase 1 covers what is code today, plus contract-consistency checks and a small opt-in live suite (`claude -p`). Each later phase writes its own deterministic scenarios alongside the code it adds. The scenario list in section 10 stays the same; it is just spread across phases.

**F2. Item 2 changes where verification sits (FR-2 and FR-8).** The spec's transitions put `verify` after `triage`. Item 2 moves it to right after `build`, so reviewers read real test evidence and a red build never reaches four reviewers. Before the approver, verification is re-run only if the job head or the default branch has moved, which is the commit binding FR-8 asks for. This needs your call (D1).

**F3. The spec's backlog leaves out some of its own requirements.** SF-201..220 cover none of these:
- FR-6 (planner contracts);
- FR-8's evidence fields (`verified_commit`, commit lines in `verify.md`);
- FR-5's dispatch-failure evidence;
- FR-7's ready computation.

They are added below as SF-221..226.

**F4. The scope check cannot attribute writes to individual parallel reviewers.** FR-3's per-agent snapshot can't tell which reviewer wrote which file when four of them run at once. Plan: one snapshot around the whole parallel batch, checked against the union of the reviewers' allowed files (`round-N/{security,code,ui,ux}.md`), plus a check that each expected file exists. Any write outside the union fails the step.

**F5. Worktrees: use native `git worktree`, not the Agent tool's `isolation: "worktree"`.** Claude Code places and cleans up its own worktrees, and auto-cleans unchanged ones. That conflicts with FR-4's fixed location, retention for rework, and recovery after a crash. The builder gets the worktree's absolute path and works only there. Write-scope enforcement (v2.1) catches any write to the primary checkout.

Worktrees also remove two fragile rules from SKILL.md step 3: "commit factory state before every builder run" and "`git switch` back after every agent". The primary checkout never leaves the default branch.

**F6. There is nothing to remove for Cursor, Orca, Herdr or Linear.** The repo has no such references (the only grep hits are CSS `cursor:`). The Phase 5 removal task becomes a CI grep guard. The untracked `.cursor/` folder in this working copy is yours and will be left alone; I'll stage explicit paths so it is never committed.

**F7. The dashboard has to change in v2.0.0, not Phase 5.** `templates/factory/dashboard.html:115` hard-codes `STAGES`. A job in `verify` or `cancelled` would drop off the board view. The one-line stage update ships with the schema; the richer display (worktree, verified commit, events) stays in Phase 5.

**F8. FR-7 is mostly in place already.** v1.8.0's Pick rule already blocks dependents through `depends:`, and the run already has `--max-jobs`, `--budget`, `--until`, the consecutive-failure stop, the safety check and the merge-blocked stop. The remaining work is to compute readiness in code (`factoryctl ready`, deterministic for scenario 21) and to make "blocked transitively" explicit.

## 3. Decisions for you (needed before the work they gate)

| # | Decision | Recommendation | Gates |
|---|---|---|---|
| D1 | Where verification runs | **After build, before review**, re-run before the approver only if the job head or default-branch commit changed. Transitions become `build → verify → review → triage → decision`, with `verify → build` on FAIL and `decision → verify` when the commits are stale. | v1.9.0 |
| D2 | Where the state utility lives | **`factory/factoryctl.py`**, copied by `init` as a plugin-owned file like `server.py`. Its version then always matches the project's board schema, it works when `${CLAUDE_PLUGIN_ROOT}` doesn't expand (a known `init` problem), and `server.py` can import its validators. | v2.0.0 |
| D3 | Release cadence | **One release per phase**, as in section 1. The alternative is a single v2.0.0 from a long-lived branch. | all |
| D4 | Spec section 14, items 1–5 | **Accept all five recommendations**: commit lifecycle events; worktree root `<project-parent>/.software-factory-worktrees/<project-name>/<job-id>/`; remove clean worktrees after merge (config can retain them); terminal `cancelled` stage; a standard-library Python utility (D2). | v2.0.0 |
| D5 | Phase 1 scope (F1) | **Accept the reordering**: deterministic coverage grows with each phase's code; Phase 1 adds contract checks and 2–3 live smoke scenarios. | v1.10.0 |
| D6 | Verification commands | **The orchestrator reads them from `CLAUDE.md` "Running the project" and passes them to `factoryctl verify` as arguments.** `config.json` keeps the spec's `run_test/run_lint/run_build` booleans. Commands never come from agent output (NFR-1). | v2.0.0 |

## 4. v1.9.0: review-round savings (items 1 and 2)

Prompt-only. No schema change, so the upgrade just refreshes plugin-owned files.

### SF-200a: Skip the triager on clean rounds (item 1)
- `agents/reviewer-{code,security,ui,ux}.md`: the second line of every review becomes `FINDINGS: <n>`. It counts findings in the verdict, not follow-ups.
- `skills/factory/SKILL.md` step 5: if every reviewer that ran wrote `VERDICT: PASS` and `FINDINGS: 0`, the orchestrator writes `round-N/triage.md` itself. Its first line is `TRIAGE: CLEAN`, followed by `Orchestrator: <k> reviewers PASS with 0 findings; triager not run.` The board records `CLEAN`. Otherwise, or if any `FINDINGS:` line is missing or unreadable, spawn the triager as today.
- The approver, dashboard and follow-up collection stay unchanged. The triage file still exists, and follow-ups come from the reviewer files.

### SF-200b: Verify once, before review (item 2, if D1 is accepted)
- `SKILL.md`: move the verify mechanics from step 6 into a new step between Build and Review. They run after every builder run: round 0, patch rounds and rework. The mechanics stay the same: `git archive`, a throwaway `HOME`, the stale-branch merge-result check, and the `AGENT FAILURE` vs `VERIFY: FAIL` rule.
- `verify.md` gains two lines: `Candidate: factory/<id> @ <sha>` and `Default: <branch> @ <sha>`.
- **FAIL**: don't spawn reviewers. Resume the builder with `verify.md`, then re-verify. A second consecutive FAIL in the same round parks the job as `VERIFY FAIL`. This doesn't use up a review round.
- **Step 6 (decision)**: if both SHAs still match, reuse `verify.md`. Otherwise re-verify before spawning the approver.
- `agents/reviewer-code.md`: replace "Run the test command yourself" with "read `verify.md`". It may still run one targeted test to check a specific claim, such as whether a test fails without the change, from a `git archive` copy with a throwaway home.
- `agents/triager.md`: targeted runs only, never the full suite.
- `agents/approver.md`: drop "you may re-run the tests"; `verify.md` is the evidence.
- The run loop's `APPROVED` check (`verify.md` starts with `VERIFY: PASS`) stays as it is.

### Release chores
- Bump `.claude-plugin/plugin.json` and `marketplace.json` to 1.9.0.
- Update the step descriptions in `docs/factoryarchitecture.html`, `docs/userguide.html` and `templates/factory/userguide.html`. README only if needed.

### Validation
A targeted sandbox run of two jobs only: one clean job, which exercises the triager skip, and one job seeded with a failing test, which exercises verify-before-review and the builder fix. Per your earlier guidance, it stops once both paths are seen. I'll ask before starting it, since it uses your subscription.

## 5. v1.10.0: Phase 1, regression foundation

| ID | Work | Notes |
|---|---|---|
| SF-201 | `tests/run.py`, `tests/helpers/`, `tests/fixtures/`, `tests/scenarios/` | Standard library only. Each scenario gets its own temp directory and a temp git repo. It sets `GIT_CONFIG_GLOBAL` to a temp file and `GIT_CONFIG_NOSYSTEM=1`, with a per-repo `user.name` and `user.email`, so your global git config is never touched. Each scenario runs with a throwaway `HOME`. A failed scenario prints its retained fixture path. |
| SF-202 | Template and fixture scenarios | Templates parse. Fixture boards cover pre-1.8, 1.8 with in-flight jobs, and a newer unknown version; Phase 2 migration tests reuse them. |
| SF-204 | `server.py` scenarios | Token check, host check, action validation, malformed input. |
| SF-226 | Contract-consistency checks | The stage list in SKILL.md matches the dashboard's `STAGES` and the template board. The first-line grammar (`VERDICT:`, `FINDINGS:`, `TRIAGE:`, `VERIFY:`, `APPROVE|ESCALATE`) is consistent across agents and SKILL.md. Every agent named in SKILL.md exists in `agents/`. Plus a grep guard for excluded integrations (F6). |
| SF-227 | Opt-in live suite: `tests/run.py --live` | Two or three `claude -p` scenarios: `init` on an empty repo, `init` upgrade, and one quick-track job on a toy repo. It uses your normal login; it never changes `HOME` or `CLAUDE_CONFIG_DIR` for `claude` itself. Transcripts are copied to `tests/out/` (gitignored), and the folders the suite creates under `~/.claude/projects/` are deleted. |
| — | `.github/workflows/test.yml` | Runs `python3 tests/run.py` on every PR, no network. |

SF-203 (planning and document scenarios) moves to the live suite. Planning stays prompt-driven, and only its file outputs can be checked.

**Exit criterion:** the deterministic suite and contract checks pass on main, and CI is green.

## 6. v2.0.0: Phase 2, schema, transitions, evidence

`factory/factoryctl.py` (standard library, plugin-owned, D2) has these subcommands. The orchestrator calls them through Bash, and every write is atomic (temp file, then rename).

| Subcommand | Does | Covers |
|---|---|---|
| `check` | Validates `board.json` and `config.json`; prints field-specific errors; exits non-zero without writing | FR-1, FR-10 |
| `transition <id> <to> --reason --evidence --action-id` | Validates against the allowed table (D1 version), updates the job, appends an event; a repeated `action-id` is a no-op | FR-2 |
| `migrate [--apply]` | v1 → v2. Dry run by default. Jobs get defaults (`workspace.kind: legacy-checkout`, empty `execution`/`evidence`) without changing stage or decision. Unknown fields are kept. A newer version stops. | FR-12 |
| `verify <id> --cmd ... [--setup ...]` | Archive or merge-result copy, throwaway `HOME`, writes `verify.md` with every FR-8 field, sets `evidence.*` | FR-8, SF-222 |
| `ready` | Prints READY backlog items and transitively blocked ones | FR-7, SF-223 |
| `doctor` | Read-only checks from FR-11; prints repair commands | FR-11 |
| `event` | Appends a sanitized failure or dispatch record | FR-5, SF-224 |

| ID | Work |
|---|---|
| SF-205 | Board v2 and config schemas, including the `verify` and `cancelled` stages and the `workspace`, `execution` and `evidence` blocks. `parallel_jobs` other than 1 is rejected. Secret-looking keys are rejected. |
| SF-206 | `transition`, and SKILL.md rewritten so every board write goes through `factoryctl`. Freehand board edits are no longer allowed. |
| SF-207 | `events.jsonl`: one object per line; a truncated last line is reported and skipped; no prompt or command text. |
| SF-208 | `migrate`, plus `init` upgrade steps U3–U5 calling it and creating `config.json`/`events.jsonl` if absent. |
| SF-209 | `doctor`, and the `/factory doctor` command. |
| SF-222 | Commit-bound verification (FR-8). |
| SF-223 | `ready` (FR-7). |
| SF-224 | Dispatch failures: retry malformed output once, then park as `AGENT FAILURE` with a sanitized event (FR-5). |
| — | One-line dashboard `STAGES` update (F7). |
| — | Deterministic scenarios 1–5, 9–11, 14–19, 21 and 24, driven through `factoryctl` with fake agent output files. |

**Exit criteria:**
- Invalid transitions can't mutate state.
- Migrating twice gives no second diff.
- Declining the upgrade changes nothing.
- A v1.8.0 sandbox project upgrades with every job intact.

## 7. v2.1.0: Phase 3, write-scope enforcement

| ID | Work |
|---|---|
| SF-210 | Role scope table, kept in `factoryctl` as data (spec section 9). |
| SF-211 | `factoryctl snapshot` before an agent step and `factoryctl scope-check <role>` after it. Covers staged, unstaged, committed (HEAD and branch tips) and untracked changes. Parallel reviewers are checked as one batch (F4). A violation is recorded and parks the job, and files are never deleted. |
| SF-212 | Scenarios 12 and 13, plus a builder writing a docs file not named on its `Docs files:` line. |

**Exit criterion:** every existing role still completes a job in the live suite.

## 8. v2.2.0: Phase 4, native worktrees, `recover`, `cancel`

| ID | Work |
|---|---|
| SF-213 | `factoryctl worktree create|recover|validate|remove`. Creation is idempotent. A path owned by another repo or job is refused. A dirty worktree is never removed. |
| SF-214 | The builder runs in the worktree. `builder.md` gets the absolute path and the rule to work only there. SKILL.md drops the commit-before-build and switch-back rules (F5). |
| SF-215 | Verification and rework read from the worktree's commit; the merge-result check is unchanged. |
| SF-216 | `/factory recover <id>`: confirms no live agent, rebuilds the resume point from files and the board, and asks when it's ambiguous. |
| SF-217 | `/factory cancel <id> [reason]`: moves the job to stage `cancelled` with an event. Never deletes a branch or a dirty worktree, and unblocks nothing. |
| SF-218 | Scenarios 20, 22, 23 and 25. |

**Exit criterion:** the primary checkout stays on the default branch through a full live job, a patch round and a rework.

## 9. v2.3.0: Phase 5, dashboard, planner contracts, docs

| ID | Work |
|---|---|
| SF-219 | Dashboard shows the worktree, verified commit, last events and the `doctor` summary. The server stays read-only plus the action queue. |
| SF-221 | FR-6 planner contracts in `agents/planner.md` PASS 2 and FEATURE PASS 2: module boundaries and interfaces, core vs extension seams for accelerators, security/privacy/observability/reliability notes, and accepted/assumed/inferred labels. PASS 2 can't finish without exact test and run commands. |
| SF-220 | README, both user guides, the architecture guide and the upgrade guide checked against the released behaviour. |
| — | Scenarios 6–8 and 26 (live suite), and the remaining deterministic gaps. |

## 10. Risks

- **SKILL.md is already about 54 KB.** Moving rules into `factoryctl` should shrink it. Every phase must delete the prose it replaces, not add on top of it.
- **Model drift on the new grammar.** A missing `FINDINGS:` line falls back to running the triager, so it fails safe. `factoryctl check` and the contract tests catch drift in the files.
- **In-flight jobs across the v2.0.0 migration.** Migration never touches stage or decision. The `init` report says how to resume each job, and `recover` arrives in v2.2.
- **Live-suite cost.** It stays opt-in and at most three scenarios.

## 11. Next step after your review

Once D1–D6 are answered, I start with v1.9.0 (SF-200a and SF-200b) on a branch, show you the diff, and ask before the sandbox validation run.

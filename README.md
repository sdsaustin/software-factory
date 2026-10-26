# software-factory

A Claude Code plugin that turns any project into a small "software factory": a vision becomes a plan, and each feature flows through spec, build, parallel review, triage and approval. All state is plain files, so every step is inspectable.

Based on the zazencodes software-factory loop, with practices borrowed from BMAD-METHOD: right-sized tracks, a project-context block, a readiness gate, review triage, and retros.

## Install
```
/plugin marketplace add <path-or-github-repo-of-this-plugin>
/plugin install software-factory@software-factory
```
For local development: `claude --plugin-dir /path/to/software-factory`.

## Use
In any project:
```
/software-factory:init                                  # one-time setup; after a plugin update, run it again to upgrade the project
/software-factory:factory plan <your vision>            # no code yet: brief, architecture, epics, backlog
/software-factory:factory <feature>                     # run one feature through the loop
/software-factory:factory document                      # existing code: draft architecture + decisions from the repo, confirm with you
/software-factory:factory plan <capability>             # existing, documented product: plan one capability into epics + backlog (feature mode)
/software-factory:brainstorm [topic]                    # vague idea? you generate options (the coach adds none), then narrow with MoSCoW
/software-factory:grill [topic]                         # have a direction? rounds of numbered questions until every decision is made
/software-factory:factory next | status | retro
/software-factory:factory run [--max-jobs N]             # drain the backlog autonomously; parks jobs that need a human, batches the questions in factory/inbox.md
/software-factory:factory apply                             # carry out the approve / rework / resume actions you clicked on the dashboard (python3 factory/server.py)
/software-factory:factory approve <id> | rework <id> <note>
/software-factory:factory signoff <id> [note]               # sign off a job's [sign-off] criteria early, while it is still being built
```
`init` creates `factory/` (board, backlog, learnings, `config.json`, `events.jsonl`, the state tool `factoryctl.py`, dashboard, `server.py`, `userguide.html`), appends a project-context block to `CLAUDE.md`, and asks whether the factory may merge approved jobs (on a yes it adds `Bash(git merge --no-ff factory/*)` to `.claude/settings.local.json`). It is safe to re-run. On a project that is already initialized it runs as an upgrade: after one confirmation it refreshes the plugin-owned files (`factoryctl.py`, `dashboard.html`, `server.py`, `userguide.html`, the backlog header and the `## Software factory` section of `CLAUDE.md`), migrates the board to the current version (jobs keep their stage and decision), numbers an unnumbered backlog, and stamps `plugin_version` in `board.json`; your state (jobs, backlog items, docs, learnings) is never rewritten. Every factory command warns in one line when the project and the plugin versions differ. See [`docs/upgradepath.html`](docs/upgradepath.html).

## The skills
| Skill | Use it when |
|---|---|
| `init` | Once per project: sets up `factory/` and `CLAUDE.md`, offers the merge permission. Again after each plugin update: upgrades the project |
| `factory` | Everything else: `plan` (a product, or one capability on existing code), `document`, `<feature>`, `next`, `run`, `apply`, `approve`, `rework`, `signoff`, `recover`, `cancel`, `status`, `doctor`, `retro` |
| `brainstorm` | The idea is vague. It widens: a coach draws out many options from you, one prompt at a time, then you narrow them with MoSCoW into `docs/intent.md` |
| `grill` | You have a direction. It narrows: numbered rounds of questions (`[recommend]` ones carry a suggested answer, `[open]` product calls do not) until nothing is assumed; decisions go to `docs/grill-log.md` and `docs/intent.md` |

When the idea is vague, run brainstorm and then grill. `factory plan` runs both for you (brainstorm only when the vision is thin), and the factory grills you whenever a spec or `document` pass has open decisions. The user guide's "The four skills" section has the full comparison.

## What ships
| Path | Purpose |
|---|---|
| `skills/factory/SKILL.md` | Orchestrator (sole writer of `factory/board.json`) |
| `skills/init/SKILL.md` | Project initializer |
| `skills/grill/SKILL.md` | "Grill me" interviewer, adapted from mattpocock/skills (MIT, see `NOTICE`); keeps a resumable decision log, ends with an intent doc, supports headless runs |
| `skills/brainstorm/SKILL.md` | Facilitator-style brainstorming and MoSCoW narrowing, condensed from BMAD's brainstorming |
| `agents/` | planner, spec-writer, builder, reviewer-security/ux/ui/code, triager, approver, retro |
| `templates/` | Files copied into the project by `init`, including `factory/factoryctl.py`, the state tool every board change goes through |
| `docs/userguide.html` | User guide: the skills, walkthroughs, commands, files, troubleshooting |
| `docs/factoryarchitecture.html` | Maintainer reference: architecture, file contracts, flows, invariants |
| `docs/upgradepath.html` | Upgrading a part-built project from an older plugin version (update, restart, `init`) and resuming |
| `tests/` | Regression suite for maintainers (not used by projects) |

Read [`docs/userguide.html`](docs/userguide.html) for the skills and for walkthroughs: a vague idea through to a shipped product, adding features to existing code (`init` → `document` → `plan <capability>` → `run`), a single small change, and a large capability split into jobs. `init` also copies a shorter guide to `factory/userguide.html` in the project.

Maintainers: see [`docs/factoryarchitecture.html`](docs/factoryarchitecture.html) for the architecture, file contracts, flows, invariants, maintenance playbooks and known limitations. Run `python3 tests/run.py` before every change (no network, no Claude login; CI runs it on pull requests) and `python3 tests/run.py --live` before a release (drives `claude -p` on your normal login).

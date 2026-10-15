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
/software-factory:init                                  # one-time setup
/software-factory:factory plan <your vision>            # no code yet: brief, architecture, epics, backlog
/software-factory:factory <feature>                     # run one feature through the loop
/software-factory:brainstorm [topic]                    # vague idea? generate options, then narrow with MoSCoW
/software-factory:grill [topic]                         # stress-test any idea, plan or spec (bundled, no other plugin needed)
/software-factory:factory next | status | retro
/software-factory:factory approve <id> | rework <id> <note>
```
`init` creates `factory/` (board, backlog, learnings, dashboard, `userguide.html`) and appends a project-context block to `CLAUDE.md`. It never overwrites existing files and is safe to re-run.

## What ships
| Path | Purpose |
|---|---|
| `skills/factory/SKILL.md` | Orchestrator (sole writer of `factory/board.json`) |
| `skills/init/SKILL.md` | Project initializer |
| `skills/grill/SKILL.md` | "Grill me" interviewer, adapted from mattpocock/skills (MIT, see `NOTICE`); keeps a resumable decision log, ends with an intent doc, supports headless runs |
| `skills/brainstorm/SKILL.md` | Facilitator-style brainstorming and MoSCoW narrowing, condensed from BMAD's brainstorming |
| `agents/` | planner, spec-writer, builder, reviewer-security/ux/ui/code, triager, approver, retro |
| `templates/` | Files copied into the project by `init` |

Open `factory/userguide.html` in an initialized project for the full walkthrough and examples.

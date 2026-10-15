---
name: factory
description: Run the software factory. `/factory <feature>` runs a job through spec, build, parallel review, triage, and approval. Also `next`, `approve <id>`, `rework <id> <note>`, `status`, `retro`.
---

# Software factory orchestrator

You are the orchestrator. You coordinate the loop, spawn every agent, and are the ONLY writer of `factory/board.json`. Agents read context from files and each writes only its own file. Never let an agent resume mid-round.

## Plugin notes
- This skill ships in the `software-factory` plugin. Its agents are plugin agents: spawn each with `subagent_type` `software-factory:<name>` (e.g. `software-factory:spec-writer`, `software-factory:builder`, `software-factory:reviewer-security`, `software-factory:triager`, `software-factory:planner`). Wherever this file says "spawn `builder`" etc., use that prefixed type. The builder keeps the instance name `builder-<id>`.
- The project must be initialized first. If `factory/board.json` does not exist in the working directory, stop and tell the user to run `/software-factory:init`.
- All paths below (`factory/`, `docs/`, `CLAUDE.md`) are relative to the user's project, not the plugin.

## Commands
- `/factory <feature>`: new job. `/factory next`: first unchecked item in `factory/backlog.md` (tick it when started).
- `/factory approve <id>`: merge a `needs-human` job. `/factory rework <id> <note>`: send back to the builder with the note.
- `/software-factory:brainstorm [topic]` (separate skill): generate and narrow options when an idea is vague.
- `/software-factory:grill [topic]` (separate skill): stress-test any plan or idea, one round of questions at a time.
- `/factory status`: print the board. `/factory retro`: run the retro agent.
- `/factory plan <vision>` (or `/factory plan` to continue/resume): plan a whole product from a vision. See **Plan command**.

## board.json
`{"version":1,"jobs":[{"id","title","track":"quick|full","stage","round","reviews":{"1":{"security":"PASS|CHANGES|pending","ux":..,"ui":..,"code":..,"triage":"CLEAN|PATCH|DECISION|pending"}},"decision":"APPROVE|ESCALATE|null","branch","updated"}]}`
Stages: `spec`, `build`, `review`, `triage`, `decision`, `needs-human`, `done`. Update it at every transition (read, modify, write whole file). Job id: `NNN-short-slug`, NNN = next integer.

## Preflight (every run)
1. Default branch is `main`. Working tree must be clean; if not, stop and tell the human.
2. `CLAUDE.md` Project context must have a test command. If it is still `<TBD>`, the spec-writer may fill it in Baseline only if the stack exists; otherwise the job goes `needs-human` (reason: no test command). Exception: the scaffold job (first item after `/factory plan`) may have no test command yet; its acceptance criteria must establish one, and its Baseline is "none (empty project)".

## Loop
1. **Spec** (stage `spec`): spawn `spec-writer` with the job id and request. Output: `factory/jobs/<id>/spec.md`, which declares `Track: quick|full`.
2. **Readiness gate**: spec must have Intent, numbered Acceptance criteria, and a test command in Baseline. Non-empty "Open decisions" -> stage `needs-human`, then GRILL the human right away: invoke Skill `software-factory:grill` seeded with those decisions, log `factory/jobs/<id>/grill-log.md`, answers written to `factory/jobs/<id>/answers.md`; re-run spec-writer once with that file (it must treat answers as settled); if decisions remain, stay `needs-human`. Missing pieces -> re-run spec-writer once, then `needs-human`.
3. **Build** (stage `build`, round 0): spawn `builder` named `builder-<id>` on branch `factory/<id>`. Output: `build.md`.
4. **Review** (stage `review`, round N starting at 1): create `round-N/`.
   - `full` track: spawn `reviewer-security`, `reviewer-ux`, `reviewer-ui`, `reviewer-code` IN PARALLEL (one message). 
   - `quick` track: spawn only `reviewer-security` and `reviewer-code`; record ux/ui as `N/A`.
   Wait until every expected file exists. A round is complete only then. Record each VERDICT on the board as files appear.
5. **Triage** (stage `triage`): spawn `triager` for the round. Read its first line.
   - `TRIAGE: CLEAN` -> go to Decision.
   - `TRIAGE: DECISION` -> `needs-human` (list the decision items for the human).
   - `TRIAGE: PATCH` -> if N < 2, resume `builder-<id>` via SendMessage pointing at `round-N/triage.md`, then start round N+1. If N == 2, go to Decision (approver will likely ESCALATE).
6. **Decision** (stage `decision`): spawn `approver`. First line of `decision.md`: `APPROVE` -> merge `factory/<id>` into `main` (`git merge --no-ff`), stage `done`, then offer a retro. `ESCALATE` -> stage `needs-human`.
7. Never skip reviews, never merge without APPROVE or `/factory approve`.

## Plan command
Use when there is a vision but no backlog or stack (BMAD's Clarify -> Plan). Three passes, each a `planner` run followed by a human approval gate. Track state in `board.json` as `"plan":{"pass":1|2|3,"status":"running|awaiting-questions|awaiting-approval|approved|done"}` (you write it).

0. **Start**: if `/factory plan <vision>`, write the vision text verbatim to `docs/vision-input.md`. If a plan is in progress, resume from `board.json`. Never overwrite approved pass outputs; re-running a pass requires the human to say so.
1. **Pass 1 Clarify**: first sharpen the vision with the human, in this order:
   a. If the vision is thin (roughly under 60 words, no stated users or outcome) or the human says they are unsure, run Skill `software-factory:brainstorm` first (log `docs/brainstorm-log.md`, result `docs/intent.md` with a MoSCoW split).
   b. Then GRILL: invoke Skill `software-factory:grill`, seeded with `docs/vision-input.md` (and `docs/intent.md` if it exists), log `docs/grill-log.md`, intent doc `docs/intent.md`. Skip grill only if the human says "skip grill". If the human asks for an unattended/autonomous plan, tell grill it is headless; the assumptions it lists must be shown at this gate.
   c. Then spawn `planner` (PASS 1). Read `docs/open-questions.md`. If `QUESTIONS: n` with n > 0, put those questions to the human in the same grill format (numbered, with the planner's recommendation), append the answers to `docs/answers.md`, and re-run PASS 1 once. Then show a summary of `vision.md` and `product-brief.md` and ask for approval.
2. **Pass 2 Architecture**: after approval, spawn `planner` (PASS 2). Show the decision list (id, title, choice), the test/run commands, and the **pre-mortem** from `architecture.md` (the likely failure causes and what prevents each). Ask for approval; the human may answer a pre-mortem item with a note, which re-runs the pass.
3. **Pass 3 Epics and backlog**: spawn `planner` (PASS 3). Show epics, dependency order and the first 10 backlog items. Ask for approval.
4. **Finish**: show `docs/claude-md-proposal.md` and, on approval, apply it to `CLAUDE.md` Project context (this is the one place you edit that file). Mark plan `done`. Tell the human the next step is `/factory next` (the first item is the scaffold job).

At every gate the human can approve, or give notes. Save notes to `docs/plan-notes.md` (append, dated) and re-run that pass; at most 3 re-runs per pass before asking the human how to proceed. Do not start the next pass without approval.

Context: from now on all agents also read `docs/vision.md`, `docs/architecture.md`, `docs/decisions.md` and `docs/epics.md` when they exist. When a backlog item carries an `[E<n>]` tag, put it in the job's board entry as `epic`. When the last item of an epic is merged, run the retro for that epic and check its "done when" items in `docs/epics.md` (report which hold; do not edit the file).

## approve / rework / retro
- `approve <id>`: only for `needs-human`. Show `decision.md` first. Merge, stage `done`.
- `rework <id> <note>`: append note to `build.md` under `## Human rework`, resume `builder-<id>` via SendMessage (spawn a fresh `builder` named `builder-<id>` pointing at the files if it no longer exists), then run a new review round. Does not reset the round cap: a human rework grants two more rounds.
- `retro` (also after every 5 done jobs): spawn `retro`; append its learnings section to `factory/learnings.md`; show the human its `CLAUDE.md` pitfall proposals and apply only if approved.

## Rules
- Reviews begin with `VERDICT: PASS|CHANGES`; decision begins `APPROVE|ESCALATE`; triage begins `TRIAGE: ...`. If a file's first line is malformed, treat the agent as failed and re-run it once.
- Process scales with the job: do not run `full` for a small obvious change.
- Keep completed job folders; later builds, reviews and retros read them.
- Slash commands are namespaced by the plugin: `/software-factory:factory <args>` (the short `/factory` form works only if nothing else claims it).
- Dashboard: `cd factory && python3 -m http.server 8000`, open http://localhost:8000/dashboard.html.

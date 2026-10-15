## Project context
<!-- software-factory:project-context -->
<!-- BMAD-style: only what agents can't learn from the code. No directory trees, no tech-stack lists.
     Add a pitfall only after someone has actually seen the mistake. Never delete a rule just because
     nothing broke recently. -->

### Policy
- Never commit secrets, real customer data, or credentials.
- <frozen paths / generated files / branch rules: TBD>

### Running the project
- Test command: <TBD - the factory's readiness gate requires this>
- Lint/typecheck/build command: <TBD>

### Non-default conventions
- <TBD>

### Observed pitfalls
- <none yet - the factory `retro` agent proposes entries; a human approves them>

## Software factory
Work flows through `/software-factory:factory` (installed plugin `software-factory`). State lives in `factory/board.json`
(only the orchestrator writes it). Agents read context from files, never the conversation, and write only their own file.
Every agent reads this file and `factory/learnings.md` before starting, plus `docs/vision.md`, `docs/architecture.md`,
`docs/decisions.md` and `docs/epics.md` when they exist (produced by `/software-factory:factory plan`). Specs and builds must
not contradict `docs/decisions.md`; if one must change, say so in `spec.md` under Open decisions.
User guide: `factory/userguide.html`.

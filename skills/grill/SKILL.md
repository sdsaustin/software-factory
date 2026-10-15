---
name: grill
description: Grill the user relentlessly about a plan, decision, or idea until nothing is silently assumed. Use to stress-test a vision, spec or design, and when the factory needs decisions from a human. Trigger phrases - grill me, stress-test this, poke holes.
---

<!-- Adapted from the "grilling" skill in mattpocock/skills (MIT, (c) 2026 Matt Pocock). Log, mirror, intent-doc and headless ideas
     follow BMAD-METHOD's brainstorming skill. See NOTICE. -->

Interview the user relentlessly until you reach a shared understanding. Map this as a **design tree**: every decision branches into the decisions that hang off it.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled: the questions you can ask _now_ without guessing at answers you haven't heard yet. Ask the whole frontier in one round: number each question, then wait for the user's answers before the next round.

Tag each question by how much you should steer:
- `[recommend]` - mechanical or well-trodden choices. Give your recommended answer.
- `[open]` - product-defining choices: who the users and buyers are, what is in and out of scope, which regulations or frameworks matter, what success means. Give NO recommendation, so the answer is the user's own. Offer options only if the user asks.

```
❓ **Q1** [recommend] - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>

---

❓ **Q2** [open] - **<question title>**: <question body>
```

Each round the user answers reshapes the tree: settled decisions push the frontier outward and unblock questions that depended on them. Recompute the frontier and ask the next round. A question whose answer depends on another question still open in this round belongs to a _later_ round, not this one.

Finding _facts_ is your job, never the user's. When a frontier question needs a fact from the environment (filesystem, tools, docs, the web), dispatch a sub-agent to find it; don't ask the user for anything you could look up yourself. Don't block on it: a running exploration is an unsettled prerequisite, so only the questions downstream of it wait for the sub-agent to report; ask the rest of the frontier now. The _decisions_ are the user's: put each to them and wait.

## Decision log (survives interruption)
Keep an append-only log at the path the caller names (default `docs/grill-log.md`). Create it with frontmatter `topic`, `status: open`, `mode: interactive|headless`. After EVERY round, append one line per settled decision: `D<n> Q: <question> -> A: <answer> (user|assumed)`, plus any `RISK:` or `OPEN:` lines you noticed. Never edit or reorder earlier lines. On start, if the log exists with `status: open`, read it, summarize where things stand in two lines, and continue from its frontier instead of starting over. Set `status: complete` only at wrap-up.

## Wrap-up: mirror, then connect
The session is done when the frontier is empty: every branch of the design tree visited, nothing left silently assumed. Then:
1. **Mirror first.** Reflect the settled decisions back in a short list and ask what the user sees now, what feels wrong, what is missing. Let them react before you add anything.
2. **Then connect.** Point out contradictions or tensions between answers (for example single-tenant against sold to many customers), decisions that quietly constrain others, and anything still assumed.
3. **Write the intent doc** at the path the caller names (default `docs/intent.md`): a succinct brief, no report bloat, with sections Goal, Decisions (grouped, each with its D-number), Out of scope, Risks, Still open. It must be usable as direct input to the next stage (the factory planner or a spec-writer).
4. Set the log `status: complete`. Do not act on the result until the user confirms the shared understanding.

## Headless mode
If the caller says the run is headless or non-interactive (no human to answer, for example an unattended factory run), do not wait: answer every `[recommend]` question with your own recommendation and mark it `(assumed)` in the log. For `[open]` questions that you cannot settle from provided files, record `OPEN: <question>` and do not invent an answer. Finish with the intent doc, and add an **Assumptions** section listing every `(assumed)` decision so a human can review them. Return `status: complete` and set the log `status: complete` only if no `OPEN` lines remain; otherwise set both to `partial`, and the orchestrator must put the OPEN questions to a human before the plan is approved.

## Inside the factory
- **Standalone**: `/software-factory:grill <topic>` grills on any plan or idea. With no topic, grill on the current conversation or the most recent factory artifact (`docs/vision.md`, a job's `spec.md`).
- **When called by `/software-factory:factory`** (plan Pass 1, or a spec with Open decisions): seed the tree from the given input, use the log and intent paths the orchestrator names (`docs/grill-log.md` + `docs/intent.md` for planning; `factory/jobs/<id>/grill-log.md` + `factory/jobs/<id>/answers.md` for a spec, where `answers.md` is the intent doc in short form). Do not edit other files.
- For a GRC-style product, make sure the tree covers: who the users and buyers are, which frameworks or regulations are in scope, how data and tenancy are separated, what evidence or audit trail must be kept, and what is explicitly out of scope.

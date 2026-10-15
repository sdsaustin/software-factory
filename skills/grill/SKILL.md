---
name: grill
description: Grill the user relentlessly about a plan, decision, or idea until nothing is silently assumed. Use to stress-test a vision, spec or design, and when the factory needs decisions from a human. Trigger phrases - grill me, stress-test this, poke holes.
---

<!-- Adapted from the "grilling" skill in mattpocock/skills (MIT, (c) 2026 Matt Pocock). See NOTICE. -->

Interview the user relentlessly until you reach a shared understanding. Map this as a **design tree**: every decision branches into the decisions that hang off it.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled: the questions you can ask _now_ without guessing at answers you haven't heard yet. Ask the whole frontier in one round: number each question and give your recommended answer. Then wait for the user's answers before the next round.

Format a round like so:

```
❓ **Q1** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>

---

❓ **Q2** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>
```

Each round the user answers reshapes the tree: settled decisions push the frontier outward and unblock questions that depended on them. Recompute the frontier and ask the next round. A question whose answer depends on another question still open in this round belongs to a _later_ round, not this one.

Finding _facts_ is your job, never the user's. When a frontier question needs a fact from the environment (filesystem, tools, docs, the web), dispatch a sub-agent to find it; don't ask the user for anything you could look up yourself. Don't block on it: a running exploration is an unsettled prerequisite, so only the questions downstream of it wait for the sub-agent to report; ask the rest of the frontier now. The _decisions_ are the user's: put each to them and wait.

The session is done when the frontier is empty: every branch of the design tree visited, nothing left silently assumed. Do not act on it until the user confirms you have reached a shared understanding.

## Inside the factory
- **Standalone**: `/software-factory:grill <topic>` grills on any plan or idea. With no topic, grill on the current conversation or the most recent factory artifact (`docs/vision.md`, a job's `spec.md`).
- **When called by `/software-factory:factory`** (plan Pass 1, or a spec with Open decisions): seed the design tree from the given input (the vision, or the spec's Open decisions plus any assumption that looks risky), run the rounds above, and finish by writing the settled decisions verbatim, one `Q -> answer` per line, to the file the orchestrator names (`docs/answers.md` or `factory/jobs/<id>/answers.md`). Do not edit other files.
- For a GRC-style product, make sure the tree covers: who the users and buyers are, which frameworks or regulations are in scope, how data and tenancy are separated, what evidence or audit trail must be kept, and what is explicitly out of scope.

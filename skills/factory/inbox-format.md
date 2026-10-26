<!-- Part of the factory skill (skills/factory/SKILL.md). Read in full when SKILL.md sends you here; every rule in SKILL.md still applies. -->

# Inbox: parking a job for a human

Used whenever a job parks in headless mode (Loop), by `run`, and by `apply`.

**Inbox entry format** (append to `factory/inbox.md`, one per parked job; also add its entry to `inbox.json`, below):
```
## <job id> - <reason: DECISION | ESCALATE | SIGN-OFF | VERIFY FAIL | MERGE BLOCKED | NO TEST COMMAND | NEEDS PR | AGENT FAILURE | SCOPE VIOLATION>
Blocks: <backlog ids whose depends: name this job's item directly, or none (the dashboard shows the items blocked further down)>
Why: <1-3 sentences>
Questions:
1. <question> - options: a) ... b) ... - recommended: <x>
Actions: (only for entries with no questions, e.g. ESCALATE or SIGN-OFF)
- approve: <label> [recommended]
- rework: <label>
- signoff: <label> (SIGN-OFF entries only, and then the only action)
Next: <exact command to resume, e.g. answer here, then `/factory rework <id> <note>` or `/factory approve <id>`>
```
For `DECISION`, copy the spec's Open decisions and write `Questions:`; for `ESCALATE`, the approver's reasons from `decision.md`, with `Questions: none` and the `Actions:` list matching `inbox.json`; for `VERIFY FAIL`, the failing command and last lines; for `MERGE BLOCKED`, the refused command and the refusal, with one recommended `approve` action; for `SCOPE VIOLATION`, the agent and each path it should not have changed, with one `rework` action (never a recommended `approve`: the human inspects the stray changes first); for a merge conflict (stale branch), the conflicting paths and one `rework` action labelled "merge <default branch> into the branch, keep both sides, re-run tests" (an earlier human sign-off stays valid unless the rework changes the signed-off behaviour).

**inbox.json** (you own it; rewrite the whole file whenever an entry is added or resolved, and keep it in step with `inbox.md`): the dashboard renders it as clickable options.
```
{"entries":[{"job":"<id>","reason":"DECISION|ESCALATE|SIGN-OFF|VERIFY FAIL|MERGE BLOCKED|NO TEST COMMAND|NEEDS PR|AGENT FAILURE|SCOPE VIOLATION","conflicts":[],"blocks":["B6"],"why":"1-3 sentences",
  "questions":[{"id":1,"text":"...","options":[{"key":"a","label":"...","recommended":true,"then":"approve"},{"key":"b","label":"...","then":"rework"}]}],
  "actions":[{"type":"approve|rework|signoff","label":"...","recommended":false}]}]}
```
Rules: for a `DECISION` entry, copy every question and option from the spec's Open decisions VERBATIM: the option `key` is the spec's own letter (`a)` becomes key `a`), the `label` its text, and `recommended` goes on the letter the spec recommends. Never re-letter, merge or reorder options: the human's answer is recorded as a letter, and a letter that means one thing in the inbox and another in the spec silently changes the scope (seen in a sandbox run). Question `id` is a small integer; option `key` is one lowercase letter or digit; mark at most one option per question `recommended` and only if you have a view (the planner/triager/approver recommendation, if any). `then` says what happens when the human picks it: `approve` = the answer settles the question, merge as is; `rework` = send back to the builder (or spec-writer) with the answer. If answers disagree, `rework` wins. Entries with no questions (for example an approver ESCALATE) list `actions` instead: `approve` and/or `rework`, with `recommended` copied from the approver's `Recommended:` line in `decision.md` (`approve` or `rework`; `none` marks nothing), and only when that line names it. Never infer a recommendation: if the approver lays out conditional paths ("if yes, approve; if no, rework") or calls it the human's choice, mark none. Omit actions that do not apply (for `NO TEST COMMAND` and `NEEDS PR` there is nothing to click). The human acts on these from the dashboard; see `apply.md`.

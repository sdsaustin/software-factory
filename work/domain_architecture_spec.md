# Domain Architecture Specification (L1 / L2 + Bounded Contexts)

Status: Proposed
Applies to: `plan` (new product), `plan` feature mode, `document` (brownfield), job specs, scope enforcement, review
Origin: critic review of the factory against DDD strategic design. C4 is a non-goal as vocabulary.

## 1. Problem

The factory structures work by feature priority (MoSCoW capabilities -> epics -> backlog). Architecture is one flat `architecture.md` listing "components". There is no subdomain classification, bounded context, ubiquitous language or context map. Consequences: cross-context coupling hides in `depends:` chains, agents name concepts inconsistently, scope enforcement guards roles but not domain boundaries, and no reviewer checks boundary erosion.

## 2. Levels

- **L1 System context** (`docs/domain/l1-context.md`): actors, external systems, bounded contexts as a map, each tagged core / supporting / generic, with relationships typed (customer-supplier, ACL, conformist, shared kernel, published language, partnership). Includes one Mermaid diagram.
- **L2 Context design** (`docs/domain/contexts/<ctx>.md`, one per context): purpose; glossary terms owned; aggregates and invariants (core subdomains only); published interfaces (endpoints, events, functions); consumed interfaces (with the upstream context and relationship type); deployable units; owned paths.
- **Glossary** (`docs/domain/glossary.md`): the ubiquitous language. Each term: definition, owning context, forbidden synonyms. Terms that mean different things in two contexts appear once per context.
- **Ownership map** (`docs/domain/ownership.json`): `{ "<ctx>": ["path/glob", ...] }`. Machine-readable; the only file the tooling parses.

Existing `architecture.md` keeps technology, quality attributes, run/test commands and pre-mortem. Its `## Components and boundaries` section is replaced by a pointer to L1/L2 and a container (deployable unit) table that names the context(s) each hosts.

## 3. Planning changes

### New product

- **PASS 2a Domain** (after Clarify approval, before Architecture): planner writes L1, L2 files, glossary, ownership map (paths are proposed targets). Human gate shows: contexts with subdomain type, context map, glossary conflicts, contexts with no owner of a Must capability.
- PASS 2 Architecture then must place every container in a context; ADs carry `Context:` (a context id or `global`).
- PASS 3: every epic names exactly one owning context (`Context:` line); every backlog line carries `[ctx:<id>]`. Work spanning contexts is split into one item per context plus an explicit integration item that names the relationship from the map.

### Feature mode

- FEATURE PASS 2 appends to L1/L2 only when the feature adds or changes a context, relationship, published interface or glossary term. Otherwise it states "No domain change; touches <ctx>". The brief's **Touches** table lists contexts, not just files.
- Modifying a context relationship requires a human decision, same rule as superseding an AD.

### Brownfield (`document`)

- PASS DOC gains an **as-is domain step**: infer candidate contexts from top-level packages, import graph and data ownership; write L1/L2 with `Status: inferred`; record `ownership.json` from actual paths; list **tangles** (a path imported by two contexts' code, tables written by several contexts, shared models) as observations, not violations.
- Inferred contexts are binding for jobs like inferred decisions: conflict is a question for the human. A to-be map is optional (`docs/domain/target.md`); when present, the tangles list becomes the seed for refactoring backlog items.

## 4. Job-level changes

- Spec template gains `Contexts:` (the contexts the job may change, normally one) and `Glossary terms used`. Spec-writer derives them from the backlog tag and ownership map; a job with more than one context must cite the integration relationship.
- `factoryctl scope-check` additionally fails a builder diff that touches paths owned by a context not listed in the job's `contexts`. Unowned paths are allowed but reported. A context whose globs are still provisional is reported, not failed (Decision 2). Shared/generated paths are listed under `"_shared"` in the ownership map.
- Board job entry gains `contexts: []`; `job_errors` validates ids against `ownership.json`.
- **New agent `reviewer-architecture`**: checks the diff against the context map (dependency direction, access only through published interfaces, no foreign-table writes), glossary terms and forbidden synonyms, and invariants of touched aggregates. Writes `round-N/architecture.md` with the usual `VERDICT:` / `FINDINGS:` header. Always runs on `standard` and `full`; on `light` it runs only when the diff touches a published interface, a `_shared` path or a glossary term (Decision 3). Triager treats it like the others.

## 5. Gates (`factoryctl plan-check`)

Fail when: a context has no purpose, subdomain type or owner path; a relationship has no type; a glossary term has no owning context; a container hosts no context; an epic or backlog line has no/unknown context; an `ownership.json` glob overlaps another context's (except `_shared`); in brownfield, an owned path does not exist. Fail also when `_platform` exceeds about 20% of backlog items. Warn (not fail) on: a core context with no invariants listed; more than ~7 contexts for a first release.

## 6. Freshness

`ownership.json` globs are checked by `doctor`: paths with no files, and tracked source paths owned by no context, are reported. L2 files record `Reviewed-at: <commit>`; `doctor` warns when owned paths changed substantially since then (reuse the existing freshness mechanism).

## 7. Non-goals

- Tactical DDD (repositories, factories, domain events) beyond aggregates and invariants for core contexts.
- C4 notation, auto-generated diagrams from code, or enforcing import rules through language-specific linters (may be suggested as an AD).
- Forcing microservices: a context is a model boundary and may live in a monolith.

## 8. Compatibility and upgrade

- Board `version` and plugin minor bump; `init` migrates: adds `contexts: []` to jobs, creates empty `docs/domain/` only on request.
- Projects without `docs/domain/` keep working: context checks are skipped, `doctor` suggests running `document` or `plan`.
- Backlog lines without `[ctx:]` remain valid; the tag is required only for items created after the domain gate is approved.

## 9. Tests (extend `tests/scenarios`)

- plan-check: each failure in section 5 has a fixture; a clean domain passes.
- scope-check: builder touching a foreign context's path fails; `_shared` and unowned paths pass/report.
- migrate: pre-change board upgrades with no job loss.
- contracts: `reviewer-architecture` header format; spec template contains `Contexts:`.
- fixture project for brownfield with two tangled packages: `document` output lists the tangle.

## 10. Delivery order

1. Artifact formats + `plan-check` rules + `ownership.json` parser (no agent changes).
2. Job `contexts` field, spec template, `scope-check` enforcement.
3. PASS 2a and feature-mode changes in `planner.md` / `plan.md`.
4. Brownfield as-is step in PASS DOC.
5. `reviewer-architecture` and track wiring.
6. `doctor` freshness, dashboard grouping by context.

## 11. Decisions

1. **Platform work uses a `_platform` pseudo-context.** Every epic names exactly one owning context; enablers (scaffold, CI, auth plumbing, logging, shared UI kit) use `Context: _platform`. `plan-check` fails when `_platform` exceeds about 20% of backlog items. `_platform` code may not hold domain logic and may not import from domain contexts. Multi-context epics are not allowed.
2. **Ownership globs are proposed, then confirmed.** The planner proposes `ownership.json` at plan time and the scaffold job confirms or corrects it. A context's enforcement is *provisional* (scope-check reports only) until its globs are confirmed, then it becomes a hard failure. In brownfield the globs come from real paths and are confirmed at the `document` gate.
3. **The architecture reviewer runs conditionally on `light` jobs.** It always runs on `standard` and `full`. On `light`, `factoryctl` runs it only if the diff touches a published interface, a `_shared` path, or a glossary term; the check is deterministic and costs no agent run when it does not trigger.

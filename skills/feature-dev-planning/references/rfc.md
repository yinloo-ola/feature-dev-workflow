# Phase 1 — PRD → RFC

## Procedure

1. **Transcribe the PRD.** If the user gave the PRD inline or as a link/attachment, save it as
   `.specs/<feature>/01-prd.md` verbatim (add nothing). The feature name is kebab-case, derived
   from the PRD title.

2. **Audit the codebase.** Before writing a single design line, establish:
   - The modules, packages, and files the feature touches (by search, not assumption).
   - Existing schemas/migrations and the migration conventions (tool, naming, direction).
   - Existing patterns for the closest analogous feature already in the repo — the RFC extends
   the codebase's real conventions, never invents parallel ones.
   - Anything the PRD assumes exists but doesn't.

3. **Interrogate the PRD.** Choose at most 5 questions, asked in one batched message, covering:
   edge cases the PRD is silent on, expected scale/limits, failure and rollback expectations,
   and anything that contradicts what you found in the audit. Everything else becomes an
   `## Open questions` entry in the RFC. Do not ask more than 5; do not hold a multi-round
   interview.

4. **Draft the RFC** as `.specs/<feature>/02-rfc.md` using the template below. Keep it additive
   in spirit: the design should be shippable in stages, each stage leaving the system working.

5. **Present and stop.** Show the user the RFC path and the open questions. Do not begin Phase 2
   until the user approves.

## RFC template

```markdown
# RFC: <feature>

- Stack: <language / framework / datastore — pins which idioms apply>
- Status: draft | approved
- PRD: [01-prd.md](01-prd.md)

## Summary
Two to four sentences. What changes and why.

## Current state
What the audited codebase does today, with file paths. Written from the
audit, not from the PRD's assumptions.

## Design
The chosen approach. For each significant change: what is added, what is
extended, what is eventually removed. Include schema/API sketches where
concrete. Link idioms from references/idioms.md that apply.

## Alternatives considered
Brief. One alternative minimum, with the reason it lost.

## Staging
How the change ships in stages (expand → migrate → contract), each stage
leaving the system working. This feeds Phase 2 slicing.

## Open questions
- [ ] <question> — <why it matters / default if unanswered>
```

Rules for the template:

- Every open question carries a **default answer**. Unanswered questions must not block
  slicing; the default is used and noted in the plan.
- `Staging` is the bridge to Phase 2: each stage that can stand alone becomes one or more
  slices; stages that can't stand alone are sequencing constraints, not parallel work.
- `Stack` is mandatory. The executor reads it to select idioms; a missing stack pin is an
  RFC defect.

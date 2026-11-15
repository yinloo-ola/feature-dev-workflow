# Phase 2 — RFC → Slice plan

`03-plan.md` is the contract between this skill and `slice-executor`. Sub-agents executing
slices read **only their slice card** (plus the files it references) — so each card must be
complete on its own.

## Slice card format

```markdown
# Plan: <feature>

Base branch: main
Stack: <pinned from 02-rfc.md>

### Slice 1: <imperative title, one reviewable decision>
- mode: additive
- depends-on: []
- files:
  - internal/payments/processor.go
  - migrations/0007_add_payments.sql
- tests:
  - internal/payments/processor_test.go
- card: |
    <Self-contained description. Goal, approach, acceptance criteria,
    and pointers to the exact 02-rfc.md sections that matter.>

### Slice 2: ...
```

Field rules (enforced by `plan_lint.py`):

- `mode`: `additive` (no deletions of existing behavior), `migrate`, or `contract`
  (deletions/cleanup). Slice 1 must be `additive`. Only `contract` slices may delete.
- `depends-on`: slice numbers that must be **merged** before this slice starts. Empty `[]`
  means parallelizable immediately. Add an edge for semantic dependencies (this slice needs
  that slice's behavior to exist), not just file overlap. When in doubt, add the edge.
- `files`: every non-test file the slice may create or modify. The executor's diff must touch
  nothing outside `files` + `tests`. This is a hard scope gate — declare honestly and completely.
- `tests`: test files that must exist and cover the slice's behavior changes. `tests: none` is
  allowed only with a one-line justification in the card.

## Slicing rules

1. **One reviewable decision per PR.** If a card needs an "and also" sentence, it's two slices.
   ~350 changed lines is the backstop for a card's total file set, not a target to fill.
2. **Expand → migrate → contract, never mixed.** No slice mixes additions and deletions of the
   same behavior. Deletions of old paths are the final `contract` slice, reviewable with
   near-zero scrutiny.
3. **Every slice independently green.** Each slice builds, passes its tests, and is mergeable
   alone. A slice that only makes sense once a later slice lands is a sequencing bug — re-slice.
4. **Disjoint file sets across parallel slices.** Two slices with overlapping `files` and no
   dependency edge between them fail the linter. Resolve by adding an edge or re-slicing.
5. **Foundation first.** Slice 1 is the additive base (shared schema/endpoints/interfaces) that
   all parallel branches cut from. Fan-out begins only after slice 1 lands on the base branch.
6. **Cold-start test.** Before writing the plan off, read each card as if you've never seen the
   feature. If a competent dev would need to ask a question to ship it, the card is incomplete —
   fold the answer in.

## Slicing procedure

1. Take the RFC's `Staging` section as the skeleton; break each stage into cards.
2. Assign `mode`, `files`, `tests`, `depends-on` for every card.
3. Order cards: foundation (1), then parallelizable additions, then `contract` last.
4. Run the linter and fix every failure in the plan:

   ```bash
   python3 <skill-dir>/scripts/plan_lint.py .specs/<feature>/03-plan.md
   ```

5. Present the plan with the dependency graph in one line, e.g.
   `1 → {2, 3} → 4 → 5(contract)`, and the wall-clock implication
   ("slices 2 and 3 can run in parallel; 4 waits for both").

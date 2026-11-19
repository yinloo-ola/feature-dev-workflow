# Phase 2 — RFC → Slice plan

`03-plan.md` is the contract between this skill and `slice-executor`. Sub-agents executing
slices read **only their slice card** (plus the files it references) — so each card must be
complete on its own.

## Slice card format

```markdown
# Plan: <feature>

Base branch: main
deliver: parallel
shape-ack: <only to accept a pipeline shape — see field rules>
Stack: <from 02-rfc.md, only when stated — polyglot/greenfield cases>

### Slice 1: <imperative title, one reviewable decision>
- deletes-existing: no
- depends-on: []
- files:
  - internal/payments/processor.go
  - migrations/0007_add_payments.sql
- tests:
  - internal/payments/processor_test.go
- card: |
    <Self-contained description: goal, approach, exact identifiers/signatures/data shapes,
    error behavior, and an acceptance-criteria checklist citing the RFC `## Behaviors`
    entries this slice implements (table-driven test rows map 1:1 to those entries).
    Pointers to the exact 02-rfc.md sections that matter.>

### Slice 2: ...
```

Field rules (enforced by `plan_lint.py`):

Structural rules are enforced mechanically by the linter; the zero-ambiguity rule below is
procedural — enforced by the cold-start test and by slice-executor's stop-and-report rule.

- Plan header `deliver`: `parallel` | `stacked` (default `parallel` when omitted).
- Plan header `shape-ack`: `<reason>` — the recorded decision to ship a pipeline shape. The
  linter warns when the fan-out ratio is below 30% or the longest chain exceeds 5 cards. That
  WARN is a question with three answers: collapse sequential cards, declare `deliver: stacked`,
  or — when the chain is genuinely right — record why with `shape-ack`. An acknowledgment
  without reason text is not an acknowledgment. Ignoring the WARN is the one answer that
  leaves no trace.
- `mode`: LEGACY, advisory — still accepted (the linter reports it once for the plan), never
  gating. The v2 field is `deletes-existing`: `no` (default, may be omitted) | `yes — <what and
  why>` (must carry reason text after `yes —`). In a multi-slice plan slice 1 must be `no`; a
  one-card plan has no foundation to protect and may declare deletions. Any card whose diff
  deletes existing lines declares `deletes-existing: yes`.
- `depends-on`: slice numbers that must be **merged** before this slice starts. Empty `[]`
  means parallelizable immediately. Add an edge for semantic dependencies (this slice needs
  that slice's behavior to exist), not just file overlap. When in doubt, add the edge.
- `files`: every non-test file the slice may create or modify. The executor's diff must touch
  nothing outside `files` + `tests`. This is a hard scope gate — declare honestly and completely.
- `tests`: test files that must exist and cover the slice's behavior changes. `tests: none` is
  allowed only with a one-line justification in the card.
  Changing behavior an existing test asserts obligates declaring that test file under
  `tests:`, with the note "update existing expectations".

## Slicing rules

1. **One reviewable decision per PR.** If a card needs an "and also" sentence, it's two slices.
   ~350 changed lines is slicing-time guidance for a card's total file set — not a target to
   fill, and no longer a gate.
2. **Expand → migrate → contract, never mixed.** No slice mixes additions and deletions of the
   same behavior. Deletions of old paths are the final `contract` slice, reviewable with
   near-zero scrutiny.
3. **Every slice independently green.** Each slice builds, passes its tests, and is mergeable
   alone. A slice that only makes sense once a later slice lands is a sequencing bug — re-slice.
4. **Disjoint file sets across parallel slices.** Two slices with overlapping `files` and no
   dependency edge between them fail the linter. Resolve by adding an edge or re-slicing.
5. **Foundation first.** Slice 1 is the additive base (shared schema/endpoints/interfaces) that
   all parallel branches cut from. Fan-out begins only after slice 1 lands on the base branch.
   This rule exists to protect a fan-out, so a **one-card plan is exempt** from the slice-1
   `deletes-existing: no` requirement: with no other slices to break, the single PR may
   declare deletions like any other card. See the single-card plan under "Slice plan shapes".
6. **Cold-start test.** Before writing the plan off, read each card as if you've never seen the
   feature. If a competent dev would need to ask a question to ship it, the card is incomplete —
   fold the answer in.
7. **Zero ambiguity.** If two competent developers could implement a card differently, the
   card is ambiguous — a plan defect. Pin everything interpretive in the card: exact
   identifiers, signatures/routes, data shapes, error behavior, and acceptance criteria.
   "Sensible" is not a spec.
8. **Chains collapse.** A slice that only continues the previous slice's files/behavior was
   split for line count, not review; a PR boundary must be a reviewable decision.

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

## Slice plan shapes

Two shapes share one grammar, and both are linted and gated the same way:

**Multi-slice plan** — the default. Phase 1 produces an RFC, Phase 2 slices it. Slice 1 is the
foundation and must be additive; the rest fan out from it.

**Single-card plan** — one slice, one PR, for a change that Step 0 of the skill sized as
no / no / yes. It skips Phase 1: there is no RFC and no audit document. The card is the whole
spec, so the cold-start test carries the weight that the RFC would otherwise carry.

```markdown
# Plan: <fix-name>

Base branch: main
deliver: parallel

### Slice 1: <imperative title, one reviewable decision>
- deletes-existing: no                 # or "yes — <what and why>"; allowed here
- depends-on: []
- files:
  - <the one or few product files this changes>
- tests:
  - <the test file, or "none" with a justification in the card>
- card: |
    Goal, exact identifiers/signatures, error behavior, and acceptance criteria.
```

The plan is committed to the base branch before the slice executes, and is never edited on the
slice branch — the card defines scope, so an editable card would defeat the scope gate. Amend
on base, then rebase.

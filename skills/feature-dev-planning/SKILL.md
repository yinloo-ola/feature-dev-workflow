---
name: feature-dev-planning
description: "Turn a PRD into an RFC and a parallel-safe PR slice plan under .specs/{feature}/ (01-prd.md, 02-rfc.md, 03-plan.md). Use when the user provides a PRD, feature request, or spec and wants it planned, broken into small reviewable PRs, or prepared for parallel slice execution by sub-agents. Covers Step 0 sizing (a small change gets a single-card plan with no RFC), Phase 1 (PRD interrogation + RFC draft) and Phase 2 (slicing with dependency edges). Execution of individual slices is handled by the slice-executor skill."
---

# Feature Dev Planning

Plan a feature as a sequence of small, independently reviewable, independently executable PRs.
All state lives in `.specs/<feature>/` in the working checkout — ignored by git, local to your
machine — and the highest-numbered doc that exists tells you where you are. No other progress
tracking exists or is needed.

```
.specs/<feature>/
├── 01-prd.md    # the input (user-provided or transcribed by you)
├── 02-rfc.md    # the design — audited against the real codebase
├── 03-plan.md   # the slice checklist — the contract for slice-executor
└── 04-contract-<consumer>.md   # verbatim copy of each external contract the design consumes
```

Never start Phase 2 before the user approves the RFC — and approval is only valid with
**zero open questions**: each one answered, explicitly accepted as an assumption, or
deferred-as-scope with the user's sign-off of its recorded consequence (see the dispositions
in [references/rfc.md](references/rfc.md)). Never fan out before PR 1 (the foundation) has landed on the base branch.

## Step 0 — Size the change before planning it

Not every request is a feature. Ask these three questions about the request; the answers pick
the path, and the user does not have to know which one to choose:

1. Does it delete or change behavior something else already relies on?
2. Does it cross an external contract, a schema, or a migration?
3. Can it ship as one PR?

**No / no / yes → single-card plan.** Skip Phase 1 entirely. Write one card — a one-slice
`03-plan.md` (see [references/slices.md](references/slices.md) for the card format) — and hand
it to `slice-executor`. The cold-start test still applies: the card is the whole spec, so it
must be complete. This path is for a bug fix with a test, a small refactor, a doc correction,
a log line.

**Any yes → full RFC + slice plan.** Run Phase 1 and Phase 2 below.

Mandatory on every path: the `check_slice.py` gates (scope, deletions, tests) and the rule
that changing behavior an existing test asserts obligates declaring that test file. What
scales down is the ceremony — the audit narrative, the coverage ledger, the stress test, the
sectioned walkthrough, the approval gate — not the verification.

A single-card plan is still a plan: write it to `.specs/<feature>/03-plan.md` before executing
the slice, and never edit it while the slice is in flight (the card defines scope, so
allowlisting it would defeat the scope gate). Amend it there, then re-dispatch or rebase.
`.specs/` is gitignored, so "before executing" means on disk, not committed — see
"Tracking the plan" below for what that means for durability.

## Phase 1 — PRD → RFC

Read [references/rfc.md](references/rfc.md) for the full procedure and the RFC template. Summary:

1. Audit the codebase first: find the modules, tables, and conventions the PRD touches. The RFC
   must describe the change against the *actual* code, not a generic architecture.
2. Interrogate the PRD until nothing material is unknown: requirements, flows, edge cases,
   failure modes, and scale; alternative designs where the choice is non-obvious. Exhaust the
   codebase audit and sensible
   defaults first; ask the human only what genuinely remains, in batched rounds where every
   question carries a proposed default. Full protocol: [references/rfc.md](references/rfc.md).
3. Draft `02-rfc.md` using the template in the reference.
4. Design stress test: walk the draft against the fixed seven-question adversarial list in
   rfc.md; each item is answered in Design or given a Coverage disposition.
5. Reviewer pass: before presenting, fresh-context reviewers read the RFC against the same
   checklist; their findings join the interrogation queue. The EGM goldfish wiring is deferred
   pending a trial — until then the fallback is fresh subagents prompted with the checklist.
6. Present the RFC and stop. Wait for user approval. A long RFC is presented as a guided
   walkthrough (agenda, then one section per turn), a short one whole — rfc.md has the rule.

   The Design section must record the concrete patterns derived from the repo's closest
   analogous code — or a recorded deviation with its reason. The executor implements what
   the RFC records, so a pattern-free Design is an RFC defect. The stack itself needs no
   pin in an established repo (the executor works inside it); state it only when the repo
   is polyglot or greenfield and the choice isn't obvious.

## Phase 2 — RFC → Slice plan

Read [references/slices.md](references/slices.md) for the slice-card format and the full slicing
rules. Summary of the rules that are enforced, not advisory:

- **One reviewable decision per PR.** ~350 changed lines is the backstop, not the goal.
- **Expand → migrate → contract, never mixed.** Slice 1 is the strictly additive foundation
  (new columns/tables/endpoints, no deletions); destructive cleanup is the last slice.
- **Declare deletions and delivery shape.** Each card declares `deletes-existing: no` (default)
  or `yes — <what and why>`; the legacy `mode:` field is advisory only. The plan header declares
  `deliver: parallel` (default; PRs target the base branch) or `deliver: stacked` (PR n targets
  slice n-1's branch). The linter prints a shape warning when the plan is a pipeline; answer it
  by collapsing sequential cards or declaring `deliver: stacked`.
- **Chains collapse.** A slice that only continues the previous slice's files or behavior was
  split for line count, not review — merge it into its predecessor.
- **Existing tests are declared.** Changing behavior an existing test asserts obligates listing
  that test file under the card's `tests:`.
- **Every slice independently green and mergeable.** If slice N only makes sense with slice N+1
  applied, re-slice.
- **Disjoint file sets across parallel slices.** Two slices that need the same file are not
  independent — sequence them with a `depends-on` edge, or re-slice until they are disjoint.
  When in doubt, add the edge: a false dependency costs wall-clock time; a missing one costs a
  broken parallel build.
- **Slice cards must pass the cold-start test:** a competent dev (or sub-agent) who has never
  seen the feature could ship the slice from the card alone, without asking questions.
- **Freeze external contracts before slicing.** An interface another team owns is transcribed
  verbatim (dated) to `.specs/<feature>/04-contract-<consumer>.md`, and recorded in the RFC's
  Coverage with owner, artifact and version/hash. A contract change after slicing is a material
  amendment whose blast radius is the cards citing it (see Amendments after dispatch).

Write `03-plan.md`, then run:

```bash
python3 <skill-dir>/scripts/plan_lint.py .specs/<feature>/03-plan.md
```

The linter enforces: required card fields, the `Base branch:` header, file overlap without a
dependency edge, backward-only dependencies (which makes cycles structurally impossible),
`deletes-existing` grammar, and that slice 1 declares `deletes-existing: no` (multi-slice plans
only). It also prints WARNs (never failures): legacy `mode:` (once for the plan, not per card)
and plan shape (fan-out ratio, longest chain). A shape WARN is a question — answer it by
collapsing cards, declaring `deliver: stacked`, or recording the decision in the plan header as
`shape-ack: <reason>`, which silences the WARN and keeps the reason in the plan. Fix failures in
the plan — never waive the linter. For
idioms to follow in cards (additive-first, one path per behavior, test-covered,
at-least-once execution), read [references/idioms.md](references/idioms.md).

## Fan-out — handoff to slice-executor

After `03-plan.md` lints clean and the foundation slice (slice 1) has landed on the base branch:

- **Assign, don't let agents self-claim.** You (the orchestrator) decide which agent gets which
  slice. Assignment needs no locking.
- Only assign ready slices: with `deliver: parallel`, every `depends-on` target is merged into
  the base branch; with `deliver: stacked`, each `depends-on` branch exists, is pushed, and
  descends from its parent's tip. Re-evaluate after each merge. Executors open PRs against the
  base branch (parallel) or the parent slice's branch (stacked).
- Run `scripts/slice_status.py` to see the whole plan at once instead of re-deriving state per
  slice: one line per slice with its state, a detail column (PR number, the worktree holding
  the claim, what a blocked slice waits on, or a stale claim and the `rmdir` that clears it),
  and a summary line last, so `tail -1` answers "what can I dispatch now". It derives state
  from git, `claims/` and gh at read time and writes nothing — re-running it is always safe.
  `--no-pr` skips gh entirely; without gh it prints one line saying PR state is unavailable
  and judges `merged` by the local branch alone.
- Spawn one sub-agent per ready slice, each instructed to run the `slice-executor` skill against
  `.specs/<feature>/` and its slice number. You may pre-create worktrees and spawn agents inside
  them, or let each executor create its own — its Step 3 handles both. Either way, never spawn
  two agents into the same checkout. Agents work on branches named `slice/<feature>-<n>` —
  the branch name is a contract: downstream readiness checks depend on it. Hand over the plan
  path **absolute**: `.specs/` is gitignored, so a fresh worktree does not contain it and the
  relative path does not resolve there.
- After each slice's PR merges, rebase the remaining worktrees onto the updated base branch and
  have each re-run its verification before continuing (slice-executor documents the
  mid-flight rebase procedure for the agents). Default posture: merge each green, reviewed slice
  promptly, so any stack that must be rebased stays short. **Keeping a slice branch after merge
  is a convenience, not a requirement** — `check_slice.py ready` treats a dependency as merged
  when its PR is merged even if the local branch is gone (it asks gh when the branch is
  missing). Keep the local `slice/<feature>-<n>` branches if you want `stack.py` output and
  `merge-base` checks to stay complete; letting GitHub delete the remote copy is safe either
  way.
- If an executor reports a card as ambiguous or blocking: fix the card in `03-plan.md`, re-run
  `plan_lint.py`, then re-dispatch. Never resolve ambiguity with verbal instructions to one
  agent — parallel agents won't hear them, and the plan stops matching the code.

### Tracking the plan

`.specs/<feature>/` is **local working state, not a committed record**: `/.specs/` is in
`.gitignore`, so nothing in it is tracked by git, pushed, or reviewable in a PR. It is kept for
the life of the feature and cleared once the feature completes — scratch space that happens to
be long-lived. Because it is gitignored it is also absent from every fresh worktree, so anything
run from a worktree must be handed the plan path **absolute**. Anything that must outlive the
feature belongs in the PR description (and the merged code): the goal, the decisions, the
Behavior→test mapping.
A card fix appends a line to that card, `amended: <date> — <reason>` (append-only, like the RFC
change log; never rewrite history). After any card fix, either re-dispatch or explicitly confirm
reconciliation with every affected executor — an executor holding the old card is working to a
stale contract.

### Amendments after dispatch

When an external change lands during execution (a changed contract, a new requirement), pick the
response by blast radius and by whether any open slice is behavior-wrong:

```
- only not-yet-dispatched slices affected -> amend the cards (zero git cost)
- open slices affected, none behavior-wrong -> merge the stack as-is; ship the change as a new change request (new slices on top)
- open slices affected and behavior-wrong -> incorporate at the lowest affected slice k; rebase the N-k descendants; state the cost before executing
- mixed (some slices already merged) -> never retro-fit merged code; forward slices for merged parts, amend only unmerged wrong slices
```

Cost first: before any restack-or-fold, state its cost and the alternative, and let the human
pick. A merged slice is never rewritten; a card amendment follows the tracking rules above.

### Restacking with stack.py

For `deliver: stacked` plans, and for any amendment-driven restack, use
`scripts/stack.py` — never hand-rolled shell loops. Run it from inside the repo checkout:

```bash
python3 <skill-dir>/scripts/stack.py status .specs/<feature>/03-plan.md
python3 <skill-dir>/scripts/stack.py restack .specs/<feature>/03-plan.md --from <n> --dry-run
python3 <skill-dir>/scripts/stack.py restack .specs/<feature>/03-plan.md --from <n>
python3 <skill-dir>/scripts/stack.py continue .specs/<feature>/03-plan.md   # after resolving a conflict
python3 <skill-dir>/scripts/stack.py abort .specs/<feature>/03-plan.md      # restore recorded SHAs
python3 <skill-dir>/scripts/stack.py push .specs/<feature>/03-plan.md       # cost report, then lease push
```

`--dry-run` mutates nothing and prints the plan and cost; `push --yes` skips the cost-report
prompt, and `push --from <n>` restacks first. The tool refuses a dirty worktree, an
`index.lock`, or an unfinished rebase; pushes with force-with-lease against remote SHAs captured
at restack time; skips branches whose PR is merged; and never auto-resolves a conflict. On a
conflict it stops, writes `conflict-report.md` beside the plan, and leaves the rebase in
progress: a mechanical conflict is reconciled keeping both intents (note it against the card); a
semantic one is not resolved — escalate it through the amendment table above.

### Review bundle

When the fan-out opens **more than 3 PRs**, produce a review bundle during wrap-up, before the
human asks: the PRs in merge order (stack order for `deliver: stacked`, dependency order
otherwise); a risk class per PR (skim / read closely / medium) from the `check_slice.py verify`
size report (code / tests / data-generated lines) plus card semantics; what to check per PR; and
the Behavior→test map (each RFC `## Behaviors` entry to the test rows that cover it). At 3 PRs
or fewer, a review note in each PR body is enough.

Fallback if agents must self-claim (e.g., user fires off independent sessions): the cheapest
atomic lock is `mkdir .specs/<feature>/claims/slice-<n>` — exactly one agent succeeds; all
others move to the next unclaimed slice.

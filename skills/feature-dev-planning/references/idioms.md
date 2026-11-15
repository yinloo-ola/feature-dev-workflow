# Idioms

Process-level principles every slice follows, regardless of stack. Concrete language and
database patterns are deliberately **not** listed here — they are derived from the target
repo (see *Deriving stack patterns*) so they never compete with the codebase's own
conventions.

## Precedence

When guidance conflicts, resolve in this order:

1. **Repo convention** — the closest analogous code already in the repo wins.
2. **These principles** — where the repo is silent.
3. **Agent preference** — never. Where 1 and 2 are both silent, the RFC records an
   assumption instead of an agent silently choosing.

A principle that contradicts repo convention loses, and the conflict is recorded in the
RFC's Design section so it's a visible decision, not a silent one.

## Principles

1. **Additive changes first.** New columns, tables, endpoints, and flags are added alongside
   the old path. Nothing existing is deleted until the contract slice. Merges of additive
   changes carry near-zero regression risk.
2. **Explicit over implicit.** Name what a change touches: explicit column lists in queries,
   explicit parameter groups, explicit return shapes. Never implicit-expansion (`SELECT *`),
   never option bags that hide required state, never reflection where a literal will do.
3. **One path per behavior.** When extending behavior, prefer adding a new named path
   (function, endpoint, column) over branching inside the existing one. Reviewers see the
   addition; they don't have to re-verify the old path.
4. **Behavior changes are test-covered.** Every slice that changes observable behavior ships
   tests in the same diff. Tests are table-driven where the input space is combinatorial:
   one case per row, edge cases as rows — rows map to the RFC `## Behaviors` entries the
   card cites.
5. **Migrations never rewrite history.** Forward-only, additive migrations. Data backfills
   are separate slices from schema changes when the backfill is non-trivial.
6. **Design for at-least-once execution.** Anything that can be re-run — backfills, background
   jobs, endpoints under client retry — must be idempotent: running it twice leaves the same
   state as running it once. Guard writes with natural keys or existence checks, not with
   "this will only be called once" hope.
7. **New paths are observable.** Every new endpoint, job, or migration state emits enough
   signal (structured logs with context, metrics on outcome) that success and failure are
   distinguishable in production without reproducing them locally. The RFC's observability
   coverage item is implemented here.

## Deriving stack patterns

Concrete patterns — naming, error-handling style, test framework and structure, migration
style — come from the repo, not from this file:

- At planning time, derive them from the closest analogous feature (found in the codebase
  audit) and record them in the RFC's Design section. The executor implements what is
  written there; it does not guess and does not import generic language fashion.
- House rules that apply across a team's repos belong in each repo's `AGENTS.md` /
  `CLAUDE.md`, versioned next to the code they govern — not in this skill, which travels
  to repos those rules would contradict.
- When the repo is genuinely inconsistent (two competing patterns for the same thing),
  pick the one the RFC can defend and record the choice as an assumption.

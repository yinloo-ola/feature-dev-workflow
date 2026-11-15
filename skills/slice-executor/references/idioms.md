# Idioms

> Mirrored from feature-dev-planning. If a conflict is ever found, fix both.

Process-level principles every slice follows, regardless of stack. Concrete language and
database patterns are deliberately **not** listed here — they are derived from the target
repo and recorded in the RFC's Design section (see the planning skill's RFC reference).

## Precedence

When guidance conflicts, resolve in this order:

1. **A recorded RFC decision** — a deliberate deviation from repo convention, with its
   reason written in the RFC, overrides everything below. Improving on a bad pattern is
   allowed; doing it silently is not.
2. **Repo convention** — the default, where the repo speaks and no recorded decision says
   otherwise.
3. **These principles** — where the repo is silent.
4. **Agent preference** — never. Where everything above is silent, the RFC records an
   assumption instead of an agent silently choosing.

Unrecorded deviation is the defect to watch for: code that departs from repo convention
without the RFC saying so is an agent silently choosing — reviewers see it as noise.

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

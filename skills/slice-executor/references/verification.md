# Verification loop

Run every step from inside your worktree. Use the repo's own commands —
discover them, don't invent them:

- `Makefile` / `justfile` targets (`make test`, `make lint`)
- `package.json` scripts (`npm test`, `npm run lint`)
- CI config (`.github/workflows/*.yml`) — the commands CI runs are the ones to run
- Language defaults (`go vet ./... && go test ./...`, `pytest`, `cargo test`)

## The loop

1. **Build** — the project compiles. A slice that doesn't build is never "almost done".
2. **Lint / vet** — zero new warnings. You didn't write them; you don't ship them.
3. **Targeted tests** — the test files your card declares, plus tests for any file you
   touched. Fast feedback while iterating.
4. **Full suite** — the entire test suite, before the PR. Every slice is independently
   green; "green once my sibling slices land" is a plan defect, not an excuse.
5. **Prove the behaviors** — demonstrate each Behavior your card cites by exercising the
   real artifact, not a proxy.

## Prove the behaviors

A green suite is evidence, not proof. Before the PR, show the behaviors working:

- **The strongest proof is scripted.** A table-driven test whose rows map to the card's
  cited Behavior entries is a re-runnable proof — cite it (file + row names) in the PR.
- **What tests can't reach gets demonstrated.** Integration points, real IO, runtime
  behavior: run the real thing (start the server and curl it, execute the CLI with real
  input, query the actual table) and capture the **actual output** into the PR.
- **Never substitute a proxy** — "it compiles", "the suite is green", or your own summary
  of what happened. The reviewer re-runs your commands or your tests instead of trusting
  your word; that is the bar.
- **If a demonstration fails while tests pass**, suspect the observation method first
  (wrong port, stale build, wrong environment), then the system. Either way: report it —
  never tune the demo until it looks right.

Proof scales with the card automatically: a card citing two Behaviors owes two proofs,
small or scripted. If proving a behavior honestly is impossible in your environment
 (no database available, no deploy target), say so explicitly in the PR — an honest
 "unproven, here's why" beats a fabricated demonstration.

## Rules

- If the full suite fails on tests unrelated to your slice, confirm the failure exists on
  the base branch too — run the suite in another checkout at base (the main checkout your
  worktree was created from, or a temporary `git worktree add` at `origin/<base>`), or
  note it in the PR. Pre-existing failures get reported in the PR, not fixed by you — fixing
  them is out of your slice's scope and the scope gate will reject it.
- No skips, no `t.Skip`, no commented-out assertions, no weakened test to make it pass.
  If a test you wrote can't pass, the card's assumptions are wrong — report and stop.
- After the loop is green, run the mechanical gates (`check_slice.py verify`) — the loop
  checks *behavior*, the gates check *shape*. Both must pass. The gates are scope
  (production files outside the card fail; undeclared test files warn), deletions
  (non-test deletions need `deletes-existing: yes — <what and why>`), and tests (declared
  test files must change); the report adds a code / tests / data-generated size breakdown
  that feeds the review bundle's risk class. There is no size cap, and `mode:` is advisory.

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

## Rules

- If the full suite fails on tests unrelated to your slice, confirm the failure exists on
  the base branch too — run the suite in another checkout at base (the main checkout your
  worktree was created from, or a temporary `git worktree add` at `origin/<base>`), or
  note it in the PR. Pre-existing failures get reported in the PR, not fixed by you — fixing
  them is out of your slice's scope and the scope gate will reject it.
- No skips, no `t.Skip`, no commented-out assertions, no weakened test to make it pass.
  If a test you wrote can't pass, the card's assumptions are wrong — report and stop.
- After the loop is green, run the mechanical gates (`check_slice.py verify`) — the loop
  checks *behavior*, the gates check *shape*. Both must pass.

# feature-dev-workflow

Skills for AI-assisted feature development: plan a PRD into reviewable PR slices, then execute
each slice in isolation.

## Skills

- **feature-dev-planning** — turn a PRD into an RFC (`.specs/{feature}/02-rfc.md`) and a slice
  plan (`03-plan.md`). Interactive. `plan_lint.py` runs the plan-time checks:
  - field validation (v2 card format below);
  - a shape report (fan-out ratio and longest dependency chain), with a WARN when the plan is
    a pipeline rather than a parallel plan and does not declare `deliver: stacked`;
  - an undeclared-test check: a backticked identifier in a card that appears in a test file
    the card does not declare is reported as a WARN.

  It also ships `stack.py` for restacking slice branches, and the planning skill writes a
  review bundle when a fan-out opens more than 3 PRs.
- **slice-executor** — execute one planned slice in an isolated git worktree and open a small,
  reviewable PR. Stateless; designed to be run by parallel sub-agents. `check_slice.py` runs
  three always-wrong gates and a size report:
  - **scope** — a changed production file outside the card's `files` + `tests` fails; an
    undeclared test file only warns;
  - **deletions** — non-test deleted lines fail unless the card declares
    `deletes-existing: yes — <what and why>`;
  - **tests** — every declared test file must change (`tests: none` is honored);
  - **size report** — code / tests / data-generated line counts. There is no size cap.

## Delivery shapes

The plan header sets `deliver: parallel | stacked` (default `parallel`).

- **parallel** — each slice branches from the plan's base branch; `ready` requires every
  `depends-on` slice to be merged into the base, and the PR targets the base.
- **stacked** — each slice branches from its parent slice. `ready` requires each `depends-on`
  branch to exist locally, be pushed, and descend from its own parent's pushed tip. `verify`
  diffs against the parent slice's branch (`slice/<feature>-<highest depends-on>`), not the
  base, so the gates judge only that slice's own change. The PR base is the parent slice's
  branch.

## Card format v2

```markdown
# Plan: payments

Base branch: main
deliver: parallel

### Slice 1: Add payments table and processor interface
- deletes-existing: no
- depends-on: []
- files:
  - internal/payments/processor.go
  - migrations/0007_add_payments.sql
- tests:
  - internal/payments/processor_test.go
- card: |
    Goal, exact identifiers/signatures, error behavior, and acceptance criteria citing the
    RFC `## Behaviors` entries this slice implements.

### Slice 2: Remove the legacy charge path
- deletes-existing: yes — drops the legacy charge function; replaced by slice 1's processor
- depends-on: [1]
- files:
  - internal/payments/legacy.go
- tests: none
- card: |
    Tests: none — pure deletion, covered by slice 1's suite.
    Goal, identifiers, and acceptance criteria as above.
```

- `deletes-existing` is `no` (default, may be omitted) or `yes — <what and why>`; the reason
  text is required. Slice 1 is the foundation and must be `no`.
- `mode:` is legacy and advisory: still accepted (the linter prints a WARN per card, the
  executor prints it as context), never gating. Legacy plans with `mode:` fields and no
  `deliver:` header keep linting.
- `tests: none` needs a one-line justification in the card.

```bash
python3 skills/feature-dev-planning/scripts/plan_lint.py .specs/<feature>/03-plan.md
python3 skills/slice-executor/scripts/check_slice.py ready  .specs/<feature>/03-plan.md <n>
python3 skills/slice-executor/scripts/check_slice.py verify .specs/<feature>/03-plan.md <n>
```

## Stacked branches: stack.py

`stack.py` inspects and restacks the `slice/<feature>-<n>` branches of a plan. The stack is
the plan's slices in ascending number order, each on top of the previous. Run it from inside
the repo checkout; it refuses on a dirty worktree, an `index.lock`, or an unfinished rebase.

```bash
S=skills/feature-dev-planning/scripts/stack.py
PLAN=.specs/<feature>/03-plan.md

python3 $S status   $PLAN                                # order, local/remote SHAs, PR state (PRs need gh)
python3 $S restack  $PLAN --from <n> [--dry-run]         # replay slices n..N onto their parents
python3 $S continue $PLAN [--dry-run]                    # after resolving a conflict: tree check + tests, then carry on
python3 $S abort    $PLAN [--dry-run]                    # restore every unpushed branch to its recorded SHA
python3 $S push     $PLAN [--from <n>] [--yes] [--dry-run]  # cost report, confirm, force-push with lease
```

- State lives in `.git/stack-state.json`, written before any mutation, so `continue` and
  `abort` work across invocations.
- On a restack conflict it stops, leaves the rebase in progress, and writes
  `conflict-report.md` beside the plan.
- `push` prints a cost report, asks to proceed (`--yes` skips the prompt), then pushes with
  `--force-with-lease` against the remote SHA captured at restack time. It skips branches
  whose PR is merged. `push --from <n>` restacks first.
- `--dry-run` prints what would happen and changes nothing.

## Review bundle

When a fan-out opens more than 3 PRs, the planning skill produces a review bundle during
wrap-up: the PRs in merge order, a risk class per PR (skim / read closely / medium) from the
`check_slice.py` size report plus card semantics, what to check per PR, and the
Behavior-to-test map. At 3 PRs or fewer, a review note in each PR body is enough.

## Install

```bash
./install.sh
```

Symlinks each skill from `skills/` into `~/.agents/skills/`, so edits in this repo are live
for the symlinked entries. `install.sh` skips (with a message) any entry that exists and is
not a symlink; remove that directory first and re-run.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s skills/feature-dev-planning/scripts -p "test_*.py"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s skills/slice-executor/scripts -p "test_*.py"
```

Requires python3 and git; `gh` is optional (PR state in `stack.py status` and `push`).

## Layout

```
skills/
├── feature-dev-planning/
│   └── scripts/   plan_lint.py, stack.py (+ tests)
└── slice-executor/
    └── scripts/   check_slice.py (+ tests)
```

Each skill folder follows the standard structure: `SKILL.md` (required), `references/`
(load-on-demand docs), `scripts/` (deterministic gates), `agents/openai.yaml` (UI metadata).

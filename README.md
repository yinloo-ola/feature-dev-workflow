# feature-dev-workflow

Skills for AI-assisted feature development: plan a PRD into parallel-safe, reviewable PR
slices, then execute each slice in isolation.

## Skills

- **feature-dev-planning** — turn a PRD into an RFC (`.specs/{feature}/02-rfc.md`) and a
  parallel-safe PR slice plan (`03-plan.md`). Interactive; covers planning only.
- **slice-executor** — execute one planned slice in an isolated git worktree and open a small,
  reviewable PR. Stateless; designed to be run by parallel sub-agents.

## Install

```bash
./install.sh
```

Symlinks each skill from `skills/` into `~/.agents/skills/`, so edits in this repo are live
immediately.

## Layout

```
skills/
├── feature-dev-planning/
└── slice-executor/
```

Each skill folder follows the standard structure: `SKILL.md` (required), `references/`
(load-on-demand docs), `scripts/` (deterministic gates), `agents/openai.yaml` (UI metadata).

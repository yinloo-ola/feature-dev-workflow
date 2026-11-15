#!/usr/bin/env bash
# Install skills from this repo into ~/.agents/skills via symlinks.
# Re-running is safe: existing symlinks are refreshed, foreign directories abort.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="${HOME}/.agents/skills"

mkdir -p "$SKILLS_DIR"

for skill_dir in "$REPO_DIR"/skills/*/; do
  name="$(basename "$skill_dir")"
  target="$SKILLS_DIR/$name"
  if [ -L "$target" ]; then
    rm "$target"
  elif [ -e "$target" ]; then
    echo "SKIP: $SKILLS_DIR/$name exists and is not a symlink (remove it first)" >&2
    continue
  fi
  ln -s "$skill_dir" "$target"
  echo "installed: $name -> $target"
done

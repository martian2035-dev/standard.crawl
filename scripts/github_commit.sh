#!/usr/bin/env bash
# Commit only the requested data files; preserve remote commits on push failure.
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == "true" ]] || { echo "For GitHub Actions only" >&2; exit 1; }
git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
paths=()
for path in "$@"; do
  if [[ -e "$path" ]]; then paths+=("$path"); fi
done
if [[ ${#paths[@]} == 0 ]]; then exit 0; fi
git add -- "${paths[@]}"
if git diff --cached --quiet; then exit 0; fi
git commit -m "chore(data): update monthly standards data $(date '+%Y-%m-%d %H:%M') [skip ci]"
if ! git push origin HEAD:main; then
  git pull --rebase origin main
  git push origin HEAD:main
fi

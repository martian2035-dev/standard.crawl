#!/usr/bin/env bash
# 仅提交调用方指定的数据文件，推送失败时不覆盖远端提交。
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == "true" ]] || { echo "仅供 GitHub Actions 使用" >&2; exit 1; }
git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
paths=()
for path in "$@"; do
  if [[ -e "$path" ]]; then paths+=("$path"); fi
done
if [[ ${#paths[@]} == 0 ]]; then exit 0; fi
git add -- "${paths[@]}"
if git diff --cached --quiet; then exit 0; fi
git commit -m "chore(data): 月度标准数据更新 $(date '+%Y-%m-%d %H:%M') [skip ci]"
if ! git push origin HEAD:main; then
  git pull --rebase origin main
  git push origin HEAD:main
fi

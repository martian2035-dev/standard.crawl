#!/usr/bin/env bash
# ============================================================================
# commit_back.sh —— 把本片采集成果回写本仓库
#
# 采集的「进度」就是仓库里的这两样东西：
#   1. 三个 CSV          —— 已抓到的数据（同时充当标准号去重基线）
#   2. .crawl_state/     —— 每个任务的页面游标与统计
# 只有推送到远端，下一片流水线才能接上，所以这一步是断点续爬的关键。
#
# 提交信息带 [ci skip]，避免这次回写再次触发 push 流水线。
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

BRANCH="${CNB_BRANCH:-main}"
HOST="${CNB_WEB_HOST:-cnb.cool}"
SLUG="${CNB_REPO_SLUG:-martianw_tx/standard_crawl}"
TOKEN="${CNB_TOKEN:-}"

info() { printf '\033[1;34m[commit-back]\033[0m %s\n' "$*"; }

# 流水线镜像必须带 git（python:3.12-slim 不含 git，python:3.12 才有）
if ! command -v git >/dev/null 2>&1; then
  echo "[commit-back] ❌ 容器内没有 git，无法回写采集进度。" >&2
  echo "              请把 .cnb.yml 里的 python:3.12-slim 改成 python:3.12。" >&2
  exit 1
fi

git config user.name  "${GIT_AUTHOR_NAME:-standard-crawl-bot}"
git config user.email "${GIT_AUTHOR_EMAIL:-bot@cnb.cool}"
git config --global --add safe.directory "${ROOT_DIR}" 2>/dev/null || true

if [ -n "${TOKEN}" ]; then
  export DEPLOY_PASSWORD="${TOKEN}"
  export GIT_ASKPASS="${SCRIPT_DIR}/git-askpass.sh"
  export GIT_TERMINAL_PROMPT=0
  chmod +x "${GIT_ASKPASS}"
  git remote set-url origin "https://${HOST}/${SLUG}.git"
fi

# 只回写采集状态，不碰 standard_db（那是构建管道的产物）
git add -f -- "国家公开标准信息.csv" "行业公开标准信息.csv" "地方公开标准信息.csv" ".crawl_state" 2>/dev/null || true

if git diff --cached --quiet; then
  info "采集状态无变化，跳过回写。"
  exit 0
fi

CAMP="$(python3 -c "import json,pathlib;p=pathlib.Path('.crawl_state/state.json');print(json.loads(p.read_text())['campaign'] if p.exists() else 'unknown')" 2>/dev/null || echo unknown)"

info "本次变更：$(git diff --cached --shortstat || true)"
git commit --quiet -m "chore(data): 采集进度 ${CAMP} 分片回写 $(date '+%Y-%m-%d %H:%M') [ci skip]"

info "推送 → ${BRANCH}"
if ! git push --quiet origin "HEAD:${BRANCH}"; then
  info "推送被拒（远端可能已更新），rebase 后重试"
  git pull --quiet --rebase origin "${BRANCH}"
  git push --quiet origin "HEAD:${BRANCH}"
fi
info "✅ 采集进度已回写远端。"

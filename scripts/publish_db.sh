#!/usr/bin/env bash
# ============================================================================
# publish_db.sh —— 把构建好的 standard_db 归档到本仓库
#
# 说明：数据库只保留在本代码库内，不再推送到其他仓库。
#       如需分发，请在此基础上自行编辑传递逻辑。
#
# 环境变量：
#   DB_COMPRESS=1   改为提交 standards.db.gz（约 27MB，而不是 113MB），
#                   可显著减缓仓库历史增长速度
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

BRANCH="${CNB_BRANCH:-main}"
HOST="${CNB_WEB_HOST:-cnb.cool}"
SLUG="${CNB_REPO_SLUG:-martianw_tx/standard_crawl}"
TOKEN="${CNB_TOKEN:-}"
DB_DIR="${DB_DIR:-standard_db}"
DB_NAME="${DB_NAME:-standards.db}"
DB_COMPRESS="${DB_COMPRESS:-0}"

info() { printf '\033[1;34m[publish-db]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[publish-db]\033[0m %s\n' "$*" >&2; exit 1; }

# 流水线镜像必须带 git（python:3.12-slim 不含 git，python:3.12 才有）
command -v git >/dev/null 2>&1 \
  || die "容器内没有 git，无法归档 standard_db。请把 .cnb.yml 里的 python:3.12-slim 改成 python:3.12。"

[ -f "${DB_DIR}/${DB_NAME}" ] || die "找不到 ${DB_DIR}/${DB_NAME}，请先执行 python build_db.py"

git config user.name  "${GIT_AUTHOR_NAME:-standard-crawl-bot}"
git config user.email "${GIT_AUTHOR_EMAIL:-bot@cnb.cool}"
git config --global --add safe.directory "${ROOT_DIR}" 2>/dev/null || true
# 大文件推送兜底，避免 http.postBuffer 默认 1MB 导致的 411/RPC 失败
git config http.postBuffer 524288000

if [ -n "${TOKEN}" ]; then
  export DEPLOY_PASSWORD="${TOKEN}"
  export GIT_ASKPASS="${SCRIPT_DIR}/git-askpass.sh"
  export GIT_TERMINAL_PROMPT=0
  chmod +x "${GIT_ASKPASS}"
  git remote set-url origin "https://${HOST}/${SLUG}.git"
fi

if [ "${DB_COMPRESS}" = "1" ]; then
  info "压缩数据库 → ${DB_NAME}.gz"
  gzip -9 -c "${DB_DIR}/${DB_NAME}" > "${DB_DIR}/${DB_NAME}.gz"
  # 本地保留未压缩的 .db（方便直接使用），但只把 .gz 纳入版本控制
  git add -f "${DB_DIR}/${DB_NAME}.gz" "${DB_DIR}/manifest.json"
  git rm -q --cached --ignore-unmatch "${DB_DIR}/${DB_NAME}" 2>/dev/null || true
  ARTIFACT="${DB_NAME}.gz"
else
  git add -f "${DB_DIR}/${DB_NAME}" "${DB_DIR}/manifest.json"
  git rm -q --cached --ignore-unmatch "${DB_DIR}/${DB_NAME}.gz" 2>/dev/null || true
  ARTIFACT="${DB_NAME}"
fi

if git diff --cached --quiet; then
  info "standard_db 无变化，跳过归档。"
  exit 0
fi

SIZE="$(du -h "${DB_DIR}/${ARTIFACT}" | cut -f1)"
info "提交 ${ARTIFACT}（${SIZE}）..."
git commit --quiet -m "chore(db): 更新 standard_db $(date '+%Y-%m-%d') [ci skip]"

info "推送 → ${BRANCH}"
git push --quiet origin "HEAD:${BRANCH}" || {
  info "推送被拒，rebase 后重试"
  git pull --quiet --rebase origin "${BRANCH}"
  git push --quiet origin "HEAD:${BRANCH}"
}
info "✅ standard_db 已归档到 ${SLUG}:${BRANCH}"

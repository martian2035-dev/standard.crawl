#!/usr/bin/env bash
# ============================================================================
# sync_latest.sh —— 把工作区同步到远端最新提交
#
# 为什么需要它：
#   cnb:apply 触发的子流水线可能基于「父流水线的起始 commit」签出代码，
#   而父流水线刚刚推送了新的抓取进度。这里显式拉取最新提交，
#   保证每一片采集都能看到上一片落盘的游标与 CSV。
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

BRANCH="${CNB_BRANCH:-main}"
HOST="${CNB_WEB_HOST:-cnb.cool}"
SLUG="${CNB_REPO_SLUG:-martianw_tx/standard_crawl}"

info() { printf '\033[1;34m[sync]\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 前置检查：流水线镜像必须带 git
#   python:3.12-slim 基于 debian-slim，**不含 git**；python:3.12 基于
#   buildpack-deps，自带 git。镜像换错时这里给出明确提示，而不是
#   报一句莫名其妙的 "git: command not found"。
# ---------------------------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
  cat >&2 <<'EOF'
[sync] ❌ 当前流水线容器里没有 git 命令，无法回写/同步代码。

  原因：docker.image 用了 python:3.12-slim —— slim 变体基于 debian-slim，
        不包含 git。请改用 python:3.12（基于 buildpack-deps，自带 git），
        或在自己构建的镜像里安装 git。

  修复：编辑仓库根目录 .cnb.yml，把所有
          image: python:3.12-slim
        改成
          image: python:3.12
EOF
  exit 1
fi

# 容器内 UID 与仓库属主不一致时 git 会拒绝操作
git config --global --add safe.directory "${ROOT_DIR}" 2>/dev/null || true

info "拉取 origin/${BRANCH} ..."
if ! git fetch --quiet origin "${BRANCH}" 2>/tmp/git-fetch.err; then
  info "内置凭证不可用，改用流水线临时令牌重试"
  if [ -z "${CNB_TOKEN:-}" ]; then
    cat >&2 <<EOF
[sync] ❌ 无法拉取代码，且没有 CNB_TOKEN 可用。

  git fetch 的原始错误：
$(sed 's/^/    /' /tmp/git-fetch.err 2>/dev/null || echo '    (无)')
EOF
    exit 1
  fi
  export DEPLOY_PASSWORD="${CNB_TOKEN}"
  export GIT_ASKPASS="${SCRIPT_DIR}/git-askpass.sh"
  export GIT_TERMINAL_PROMPT=0
  chmod +x "${GIT_ASKPASS}"
  git remote set-url origin "https://${HOST}/${SLUG}.git"
  if ! git fetch --quiet origin "${BRANCH}"; then
    cat >&2 <<EOF
[sync] ❌ 使用流水线临时令牌拉取仍然失败，请检查：
    1) CNB_TOKEN 对该仓库是否有 repo-code:r 权限；
    2) 仓库地址是否正确：https://${HOST}/${SLUG}.git
EOF
    exit 1
  fi
fi

BEFORE="$(git rev-parse --short HEAD 2>/dev/null || echo none)"
git reset --hard --quiet FETCH_HEAD
AFTER="$(git rev-parse --short HEAD)"
info "工作区已同步：${BEFORE} → ${AFTER}"

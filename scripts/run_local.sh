#!/usr/bin/env bash
# ============================================================================
# run_local.sh —— 本地跑流水线（与 CNB 上执行的是同一套脚本）
#
#   bash scripts/run_local.sh                     # 抓 1 个分片 → 构建 → 校验
#   bash scripts/run_local.sh --no-crawl          # 跳过抓取，只用现有 CSV 重建数据库
#   bash scripts/run_local.sh --crawl-until-done  # 本地循环跑分片直到抓完，再构建
#   bash scripts/run_local.sh --status            # 只看采集进度
#   bash scripts/run_local.sh --new-campaign      # 开启新的采集行动（游标归零）
#
# 环境变量：
#   CRAWL_BUDGET_SECONDS   分片时间预算（秒），默认 4200
#   CRAWL_MAX_PAGES        本片最多抓多少页（0=不限），快速试跑用
#
# 首次使用请先准备虚拟环境：
#   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

DO_CRAWL=1
UNTIL_DONE=0
NEW_CAMPAIGN=0
STATUS_ONLY=0
for arg in "$@"; do
  case "${arg}" in
    --no-crawl)         DO_CRAWL=0 ;;
    --crawl-until-done) UNTIL_DONE=1 ;;
    --new-campaign)     NEW_CAMPAIGN=1 ;;
    --status)           STATUS_ONLY=1 ;;
    -h|--help)          sed -n '2,17p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数：${arg}" >&2; exit 2 ;;
  esac
done

# 选择解释器：优先 .venv，其次 python3
if [ -x "${ROOT_DIR}/.venv/bin/python" ]; then
  PY="${ROOT_DIR}/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

step "使用解释器：${PY}  ($(${PY} -V 2>&1))"

if [ "${STATUS_ONLY}" = "1" ]; then
  "${PY}" standard_crawl.py --status
  exit 0
fi

step "检查依赖"
"${PY}" - <<'EOF'
import importlib.util
import sys

missing = [m for m in ("pandas", "requests", "bs4") if importlib.util.find_spec(m) is None]
if missing:
    sys.exit("缺少依赖：%s\n请执行  %s -m pip install -r requirements.txt" % (", ".join(missing), sys.executable))
print("依赖 OK")
EOF

# ---------------------------------------------------------------- 采集
CRAWL_ARGS=(--budget-seconds "${CRAWL_BUDGET_SECONDS:-4200}")
[ "${NEW_CAMPAIGN}" = "1" ] && CRAWL_ARGS+=(--new-campaign)
[ -n "${CRAWL_MAX_PAGES:-}" ] && CRAWL_ARGS+=(--max-pages "${CRAWL_MAX_PAGES}")

if [ "${DO_CRAWL}" = "1" ]; then
  if [ "${UNTIL_DONE}" = "1" ]; then
    step "循环采集分片，直到全部完成（Ctrl-C 可随时中断，进度已落盘）"
    round=0
    while :; do
      round=$((round + 1))
      step "采集分片 #${round}"
      if ! "${PY}" standard_crawl.py "${CRAWL_ARGS[@]}"; then
        echo "采集分片失败，已停止。" >&2
        exit 1
      fi
      NEXT="$("${PY}" standard_crawl.py --emit-next | sed -E 's/.*crawl_next=([^]]*).*/\1/')"
      if [ "${NEXT}" = "build" ]; then
        break
      elif [ "${NEXT}" != "crawl" ]; then
        echo "采集无法继续（next=${NEXT}）。" >&2
        exit 1
      fi
      CRAWL_ARGS=(--budget-seconds "${CRAWL_BUDGET_SECONDS:-4200}")   # 续片不再重置游标
    done
  else
    step "采集 1 个分片（断点续爬；想一次跑完用 --crawl-until-done）"
    "${PY}" standard_crawl.py "${CRAWL_ARGS[@]}"
  fi
else
  step "跳过采集（--no-crawl）"
fi

"${PY}" standard_crawl.py --status

# ---------------------------------------------------------------- 构建
step "构建 standard_db（只消费已产出的 CSV）"
"${PY}" build_db.py

step "校验 standard_db"
"${PY}" build_db.py --verify

step "🎉 完成"

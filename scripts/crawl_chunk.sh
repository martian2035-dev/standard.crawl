#!/usr/bin/env bash
# ============================================================================
# crawl_chunk.sh —— 跑一个采集分片
#
# 由 api_trigger_crawl（首片，游标归零）或 api_trigger_crawl_continue
# （续片）触发。两条流水线共用同一段脚本，区别只在是否重置游标。
#
# 相关环境变量：
#   CRAWL_BUDGET_SECONDS     本片时间预算（秒），默认 4200（70 分钟）
#   CRAWL_CHECKPOINT_EVERY   每多少页落盘一次，默认 25
#   CRAWL_DELAY              每页间隔秒数，默认 0.4
#   CRAWL_MAX_PAGES          本片最多抓多少页（0=不限），调试用
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

PY="${PYTHON_BIN:-python3}"
[ -x "${ROOT_DIR}/.venv/bin/python" ] && PY="${ROOT_DIR}/.venv/bin/python"

ARGS=(
  --budget-seconds "${CRAWL_BUDGET_SECONDS:-4200}"
  --checkpoint-every "${CRAWL_CHECKPOINT_EVERY:-25}"
  --delay "${CRAWL_DELAY:-0.4}"
)

# 只有「首片」才把游标归零；续片必须沿用已有进度
if [ "${CNB_EVENT:-}" = "api_trigger_crawl" ]; then
  ARGS+=(--new-campaign)
  echo "[crawl] 首片：开启新的采集行动"
else
  echo "[crawl] 续片：沿用已有游标"
fi

if [ -n "${CRAWL_MAX_PAGES:-}" ] && [ "${CRAWL_MAX_PAGES}" != "0" ]; then
  ARGS+=(--max-pages "${CRAWL_MAX_PAGES}")
fi

# 仅用于「有界链式测试」：限制一次采集行动最多跑几片（到上限会主动报错停止）
if [ -n "${CRAWL_MAX_CHUNKS:-}" ] && [ "${CRAWL_MAX_CHUNKS}" != "0" ]; then
  ARGS+=(--max-chunks "${CRAWL_MAX_CHUNKS}")
fi

if [ -n "${CRAWL_ONLY:-}" ]; then
  # 逗号分隔，例如 CRAWL_ONLY=industry,local
  IFS=',' read -ra parts <<< "${CRAWL_ONLY}"
  for p in "${parts[@]}"; do ARGS+=(--only "$p"); done
fi

echo "[crawl] budget=${CRAWL_BUDGET_SECONDS:-4200}s checkpoint=${CRAWL_CHECKPOINT_EVERY:-25}页"
exec "${PY}" standard_crawl.py "${ARGS[@]}"

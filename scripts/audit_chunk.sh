#!/usr/bin/env bash
# 固定输入、固定历史基线；先离线校验，再低频请求。不回写 Git、不部署。
set -euo pipefail
rm -f local_audit/cnb/本片完成.flag
audit_batch="${AUDIT_BATCH_SIZE:-50}"
# 推送的首片快速验证恢复和交付链路，后续自动分片按正常批量处理。
if [[ "${CNB_EVENT:-}" == "push" ]]; then
  audit_batch="${AUDIT_INITIAL_BATCH_SIZE:-3}"
fi

args=(
  audit_pipeline.py run audit_inputs/standards.csv
  --output-dir local_audit/cnb
  --batch-size "$audit_batch"
  --budget-seconds "${AUDIT_BUDGET_SECONDS:-1800}"
  --max-requests "${AUDIT_MAX_REQUESTS:-250}"
  --delay "${AUDIT_DELAY:-5}"
  --sources "${AUDIT_SOURCES:-samr,national,industry,local}"
)
if [[ "${AUDIT_RETRY_FAILED:-0}" == "1" ]]; then
  args+=(--retry-failed)
fi
result=0
python "${args[@]}" || result=$?
if [[ "$result" != "0" && "$result" != "2" ]]; then
  exit "$result"
fi
# 未确认的标准保留待核查，仍继续处理其它标准；输入/运行错误则停止。
touch local_audit/cnb/本片完成.flag

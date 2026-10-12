#!/usr/bin/env bash
# 在本地一条命令获取最新成果，自动合并原表并生成 Excel。
set -euo pipefail
cd "$(dirname "$0")/.."
audit_python=".venv/bin/python"
if [[ ! -x "$audit_python" ]]; then
  audit_python="python3"
fi
exec "$audit_python" audit_pipeline.py pull "$@"

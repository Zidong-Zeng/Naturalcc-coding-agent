#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
# 激活 conda 环境 agent（兼容新旧版 conda）
eval "$(conda shell.bash hook 2>/dev/null)" && conda activate agent 2>/dev/null || \
  source activate agent 2>/dev/null || true
export PYTHONNOUSERSITE=1
# 代理设置（外接 API 需要）
export http_proxy=http://127.0.0.1:10809
export https_proxy=http://127.0.0.1:10809
export all_proxy=socks5://127.0.0.1:10809
echo "Starting B1 Agent API on http://localhost:8000 ..."
exec python3 agent_api.py

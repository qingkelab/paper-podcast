#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QINGKE_DIR="$ROOT/integrations/qingke-jiedu"
PIDS=()

cleanup() {
  trap - INT TERM EXIT
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "缺少项目 Python 环境：请先按 AGENTS.md 创建 .venv 并安装 backend/requirements.txt。" >&2
  exit 1
fi

if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
  pnpm --dir "$ROOT/frontend" install
fi
if [[ ! -d "$QINGKE_DIR/node_modules" ]]; then
  npm ci --prefix "$QINGKE_DIR"
fi

(
  cd "$QINGKE_DIR"
  QINGKE_SHARED_ENV="$ROOT/.env" PORT="${QINGKE_JIEDU_PORT:-4781}" exec node server.js
) &
PIDS+=("$!")

(
  cd "$ROOT/backend"
  exec "$ROOT/.venv/bin/python" -m uvicorn app.main:app --reload \
    --port "${PAPER_PODCAST_PORT:-8000}"
) &
PIDS+=("$!")

(
  cd "$ROOT/frontend"
  export VITE_QINGKE_JIEDU_URL="${VITE_QINGKE_JIEDU_URL:-http://127.0.0.1:${QINGKE_JIEDU_PORT:-4781}/}"
  exec ./node_modules/.bin/vite --host 127.0.0.1 \
    --port "${PAPER_PODCAST_FRONTEND_PORT:-5173}" --strictPort
) &
PIDS+=("$!")

echo "paper-podcast: http://127.0.0.1:${PAPER_PODCAST_FRONTEND_PORT:-5173}"
echo "快速图文服务: http://127.0.0.1:${QINGKE_JIEDU_PORT:-4781}"
echo "按 Ctrl+C 停止全部服务。"

while true; do
  for pid in "${PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid"
      exit $?
    fi
  done
  sleep 1
done

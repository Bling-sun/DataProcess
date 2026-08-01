#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${PROJECT_DIR}/runtime/server.pid"

if [[ ! -f "${PID_FILE}" ]]; then
  echo "DataProcess is not running (PID file missing)."
  exit 0
fi
server_pid="$(cat "${PID_FILE}")"
if [[ ! "${server_pid}" =~ ^[0-9]+$ ]]; then
  echo "Invalid PID file: ${PID_FILE}" >&2
  exit 1
fi
if kill -0 "${server_pid}" 2>/dev/null; then
  server_pgid="$(ps -o pgid= -p "${server_pid}" | tr -d ' ')"
  signal_target="${server_pid}"
  if [[ "${server_pgid}" == "${server_pid}" ]]; then
    signal_target="-${server_pgid}"
  fi
  kill -TERM -- "${signal_target}"
  for _attempt in {1..30}; do
    kill -0 -- "${signal_target}" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 -- "${signal_target}" 2>/dev/null; then
    kill -KILL -- "${signal_target}" 2>/dev/null || true
  fi
fi
rm -f "${PID_FILE}"
echo "DataProcess stopped (PID ${server_pid})."

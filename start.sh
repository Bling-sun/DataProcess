#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${PROJECT_DIR}/runtime"
PID_FILE="${RUNTIME_DIR}/server.pid"
LOG_FILE="${RUNTIME_DIR}/server.log"

mkdir -p "${RUNTIME_DIR}"
if [[ -f "${PID_FILE}" ]]; then
  running_pid="$(cat "${PID_FILE}")"
  if [[ "${running_pid}" =~ ^[0-9]+$ ]] && kill -0 "${running_pid}" 2>/dev/null; then
    echo "DataProcess is already running (PID ${running_pid})."
    exit 0
  fi
fi

# A dedicated process group lets stop.sh terminate the HTTP server and any
# ffmpeg preview workers together without touching unrelated user processes.
nohup setsid "${PROJECT_DIR}/run.sh" </dev/null >>"${LOG_FILE}" 2>&1 &
server_pid=$!
echo "${server_pid}" >"${PID_FILE}"
disown "${server_pid}" 2>/dev/null || true
sleep 0.8
if ! kill -0 "${server_pid}" 2>/dev/null; then
  echo "DataProcess failed to start. Check ${LOG_FILE}." >&2
  exit 1
fi
echo "DataProcess started (PID ${server_pid}, port ${DATAPROCESS_PORT:-8088})."

#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
DEPS_DIR="${PROJECT_DIR}/.deps"
PROJECT_TMP_DIR="${DATAPROCESS_TMP_DIR:-${PROJECT_DIR}/runtime/tmp}"

if [[ -f "${PROJECT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${PROJECT_DIR}/.env"
  set +a
fi

export PATH="${PROJECT_DIR}/.venv/bin:${DEPS_DIR}/bin:${PATH}"

mkdir -p "${PROJECT_TMP_DIR}"
export TMPDIR="${PROJECT_TMP_DIR}"
export TMP="${PROJECT_TMP_DIR}"
export TEMP="${PROJECT_TMP_DIR}"

if [[ -x "${PYTHON_BIN}" ]] && "${PYTHON_BIN}" -c "import pyarrow" >/dev/null 2>&1; then
  :
elif [[ -d "${DEPS_DIR}/pyarrow" ]]; then
  PYTHON_BIN="python3"
  export PYTHONPATH="${DEPS_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
else
  PYTHON_BIN="python3"
  echo "Warning: PyArrow is not installed; review/replay works, but export requires ./setup.sh." >&2
fi

exec "${PYTHON_BIN}" "${PROJECT_DIR}/server.py" \
  --host "${DATAPROCESS_HOST:-0.0.0.0}" \
  --port "${DATAPROCESS_PORT:-8088}" \
  --raw-root "${DATAPROCESS_RAW_ROOT:-/mnt/pangyunyi/figure/raw/20260730}"

#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DIR="${PROJECT_DIR}/.venv"
DEPS_DIR="${PROJECT_DIR}/.deps"
PROJECT_TMP_DIR="${PROJECT_DIR}/runtime/tmp"
PROJECT_PIP_CACHE_DIR="${PROJECT_DIR}/runtime/pip-cache"

mkdir -p "${PROJECT_TMP_DIR}" "${PROJECT_PIP_CACHE_DIR}"
export TMPDIR="${PROJECT_TMP_DIR}"
export TMP="${PROJECT_TMP_DIR}"
export TEMP="${PROJECT_TMP_DIR}"
export PIP_CACHE_DIR="${PROJECT_PIP_CACHE_DIR}"

if python3 -m venv "${ENV_DIR}" 2>/dev/null && "${ENV_DIR}/bin/python" -m pip --version >/dev/null 2>&1; then
  "${ENV_DIR}/bin/python" -m pip install --upgrade pip
  "${ENV_DIR}/bin/python" -m pip install -r "${PROJECT_DIR}/requirements.txt"
  echo "Virtual environment ready: ${ENV_DIR}"
else
  echo "python3-venv is unavailable; installing isolated packages into ${DEPS_DIR}."
  mkdir -p "${DEPS_DIR}"
  python3 -m pip install --upgrade --target "${DEPS_DIR}" -r "${PROJECT_DIR}/requirements.txt"
  echo "Project-local dependencies ready: ${DEPS_DIR}"
fi

echo "Start with: ${PROJECT_DIR}/run.sh"

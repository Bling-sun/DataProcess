#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
One-command Figure raw -> GR00T-compatible LeRobot v2.1 conversion.

Usage:
  start_lerobot_v21_conversion.sh \
    --source /absolute/path/to/raw/date \
    --output /absolute/path/to/output_name \
    --task "language instruction" [options]

Required:
  --source DIR                Raw dataset containing episode_XXXXXX directories
  --output DIR                Final dataset directory (must not already exist)
  --task TEXT                 Language instruction stored in every episode

Options:
  --fps NUMBER                Output FPS (default: 20)
  --gpus LIST                 Comma-separated GPU indices (default: all detected)
  --parquet-workers NUMBER    Parquet worker count (default: 8)
  --video-quality NUMBER      NVENC constant quality (default: 20)
  --watchdog-minutes NUMBER   Watchdog interval in minutes (default: 30)
  --python BIN                Python with numpy and pyarrow (auto-detected)
  --skip-invalid              Skip and record invalid episodes (default)
  --strict                    Abort if any source episode is invalid
  --dry-run                   Run preflight checks without starting processes
  -h, --help                  Show this help

The converter is resumable. It writes to .OUTPUT_NAME.inprogress and publishes
the final directory only after all Parquet and decoded video checks pass.
EOF
}

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONVERTER="${PROJECT_DIR}/scripts/batch_convert_lerobot_v21.py"
WATCHDOG="${PROJECT_DIR}/scripts/monitor_lerobot_conversion.sh"
STATUS_SCRIPT="${PROJECT_DIR}/scripts/lerobot_v21_status.sh"
SOURCE=""
OUTPUT=""
TASK=""
FPS=20
GPUS=""
PARQUET_WORKERS=8
VIDEO_QUALITY=20
WATCHDOG_MINUTES=30
PYTHON_BIN="${LEROBOT_PYTHON:-}"
SKIP_INVALID=1
DRY_RUN=0

while (($#)); do
  case "$1" in
    --source) SOURCE="${2:-}"; shift 2 ;;
    --output) OUTPUT="${2:-}"; shift 2 ;;
    --task) TASK="${2:-}"; shift 2 ;;
    --fps) FPS="${2:-}"; shift 2 ;;
    --gpus) GPUS="${2:-}"; shift 2 ;;
    --parquet-workers) PARQUET_WORKERS="${2:-}"; shift 2 ;;
    --video-quality) VIDEO_QUALITY="${2:-}"; shift 2 ;;
    --watchdog-minutes) WATCHDOG_MINUTES="${2:-}"; shift 2 ;;
    --python) PYTHON_BIN="${2:-}"; shift 2 ;;
    --skip-invalid) SKIP_INVALID=1; shift ;;
    --strict) SKIP_INVALID=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ -n "${SOURCE}" ]] || fail "--source is required."
[[ -n "${OUTPUT}" ]] || fail "--output is required."
[[ -n "${TASK//[[:space:]]/}" ]] || fail "--task is required and must not be empty."
[[ "${SOURCE}" == /* ]] || fail "--source must be an absolute path."
[[ "${OUTPUT}" == /* ]] || fail "--output must be an absolute path."
[[ "${PARQUET_WORKERS}" =~ ^[1-9][0-9]*$ ]] || fail "--parquet-workers must be a positive integer."
[[ "${VIDEO_QUALITY}" =~ ^[0-9]+$ ]] || fail "--video-quality must be a non-negative integer."
[[ "${WATCHDOG_MINUTES}" =~ ^[1-9][0-9]*$ ]] || fail "--watchdog-minutes must be a positive integer."

SOURCE="$(readlink -m -- "${SOURCE}")"
OUTPUT="$(readlink -m -- "${OUTPUT}")"
[[ -d "${SOURCE}" ]] || fail "source directory does not exist: ${SOURCE}"
[[ "${OUTPUT}" != "${SOURCE}" ]] || fail "output cannot equal source."
[[ "${OUTPUT}" != "${SOURCE}/"* ]] || fail "output cannot be inside source."
[[ -f "${CONVERTER}" ]] || fail "converter is missing: ${CONVERTER}"
[[ -x "${WATCHDOG}" ]] || fail "watchdog is missing or not executable: ${WATCHDOG}"
[[ -x "${STATUS_SCRIPT}" ]] || fail "status script is missing or not executable: ${STATUS_SCRIPT}"

for command_name in ffmpeg ffprobe nvidia-smi; do
  command -v "${command_name}" >/dev/null 2>&1 || fail "required command is missing: ${command_name}"
done
ffmpeg_encoders=$(ffmpeg -hide_banner -encoders 2>/dev/null)
[[ "${ffmpeg_encoders}" == *h264_nvenc* ]] \
  || fail "FFmpeg does not provide the h264_nvenc encoder."

python_works() {
  [[ -n "${1:-}" && -x "${1}" ]] || return 1
  "${1}" -c 'import numpy, pyarrow' >/dev/null 2>&1
}

if [[ -n "${PYTHON_BIN}" && "${PYTHON_BIN}" != */* ]]; then
  PYTHON_BIN="$(command -v "${PYTHON_BIN}" 2>/dev/null || true)"
fi
if ! python_works "${PYTHON_BIN}"; then
  PYTHON_BIN=""
  for candidate in \
    "${PROJECT_DIR}/.venv/bin/python" \
    /mnt/sunbing/projects/FigureDemo/envs/lerobot-validator/bin/python \
    "$(command -v python3 2>/dev/null || true)"; do
    if python_works "${candidate}"; then
      PYTHON_BIN="${candidate}"
      break
    fi
  done
fi
if [[ -z "${PYTHON_BIN}" && -d "${PROJECT_DIR}/.deps/pyarrow" ]]; then
  export PYTHONPATH="${PROJECT_DIR}/.deps${PYTHONPATH:+:${PYTHONPATH}}"
  candidate="$(command -v python3 2>/dev/null || true)"
  if python_works "${candidate}"; then
    PYTHON_BIN="${candidate}"
  fi
fi
[[ -n "${PYTHON_BIN}" ]] || fail "no Python with numpy and pyarrow; run ./setup.sh first."

if [[ -z "${GPUS}" ]]; then
  GPUS=$(nvidia-smi --query-gpu=index --format=csv,noheader,nounits | tr -d ' ' | paste -sd, -)
fi
[[ -n "${GPUS}" ]] || fail "no GPU was detected."
present_gpus=",$(nvidia-smi --query-gpu=index --format=csv,noheader,nounits | tr -d ' ' | paste -sd, -),"
IFS=',' read -r -a requested_gpus <<<"${GPUS}"
for gpu in "${requested_gpus[@]}"; do
  [[ "${gpu}" =~ ^[0-9]+$ ]] || fail "invalid GPU list: ${GPUS}"
  [[ "${present_gpus}" == *",${gpu},"* ]] || fail "GPU ${gpu} does not exist on this host."
done

episode_count=$(find "${SOURCE}" -mindepth 1 -maxdepth 1 -type d -name 'episode_*' | wc -l)
((episode_count > 0)) || fail "no episode_XXXXXX directories found under ${SOURCE}."

OUTPUT_PARENT="$(dirname "${OUTPUT}")"
OUTPUT_NAME="$(basename "${OUTPUT}")"
STAGING="${OUTPUT_PARENT}/.${OUTPUT_NAME}.inprogress"
CONVERSION_LOG="${OUTPUT_PARENT}/${OUTPUT_NAME}_conversion.log"
LAUNCHER_LOG="${OUTPUT_PARENT}/${OUTPUT_NAME}_launcher.log"
PID_FILE="${OUTPUT_PARENT}/${OUTPUT_NAME}_conversion.pid"
WATCHDOG_LOG="${OUTPUT_PARENT}/${OUTPUT_NAME}_watchdog.log"
WATCHDOG_LAUNCHER_LOG="${OUTPUT_PARENT}/${OUTPUT_NAME}_watchdog_launcher.log"
WATCHDOG_PID_FILE="${OUTPUT_PARENT}/${OUTPUT_NAME}_watchdog.pid"
INTERVAL_SECONDS=$((WATCHDOG_MINUTES * 60))

if [[ -f "${OUTPUT}/_SUCCESS" ]]; then
  echo "Already complete: ${OUTPUT}"
  "${STATUS_SCRIPT}" --output "${OUTPUT}"
  exit 0
fi
if [[ -e "${OUTPUT}" ]]; then
  fail "final output exists without _SUCCESS; move it aside before retrying: ${OUTPUT}"
fi

echo "Preflight passed"
echo "  source:              ${SOURCE} (${episode_count} source episodes)"
echo "  output:              ${OUTPUT}"
echo "  resumable staging:   ${STAGING}"
echo "  task:                ${TASK}"
echo "  fps:                 ${FPS}"
echo "  GPUs:                ${GPUS}"
echo "  parquet workers:     ${PARQUET_WORKERS}"
echo "  skip invalid:        ${SKIP_INVALID}"
echo "  watchdog interval:   ${WATCHDOG_MINUTES} minutes"
echo "  Python:              ${PYTHON_BIN}"

if ((DRY_RUN)); then
  echo "Dry run only; no process was started."
  exit 0
fi

mkdir -p "${OUTPUT_PARENT}"

main_is_running() {
  local pid command_line
  pid=$(cat "${PID_FILE}" 2>/dev/null || true)
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  command_line=$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)
  [[ "${command_line}" == *batch_convert_lerobot_v21.py* ]] || return 1
  [[ "${command_line}" == *"${SOURCE}"* ]] || return 1
  [[ "${command_line}" == *"${OUTPUT}"* ]]
}

watchdog_is_running() {
  local pid command_line
  pid=$(cat "${WATCHDOG_PID_FILE}" 2>/dev/null || true)
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  command_line=$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)
  [[ "${command_line}" == *monitor_lerobot_conversion.sh* ]] || return 1
  [[ "${command_line}" == *"${OUTPUT}"* ]]
}

common_args=(
  --source "${SOURCE}"
  --output "${OUTPUT}"
  --task "${TASK}"
  --fps "${FPS}"
  --gpus "${GPUS}"
  --parquet-workers "${PARQUET_WORKERS}"
  --video-quality "${VIDEO_QUALITY}"
)
converter_mode_args=()
watchdog_mode_arg=--strict
if ((SKIP_INVALID)); then
  converter_mode_args=(--skip-invalid)
  watchdog_mode_arg=--skip-invalid
fi

if main_is_running; then
  conversion_pid=$(cat "${PID_FILE}")
  echo "Converter is already running (PID ${conversion_pid})."
else
  cd "${PROJECT_DIR}"
  nohup "${PYTHON_BIN}" "${CONVERTER}" "${common_args[@]}" "${converter_mode_args[@]}" \
    --log-file "${CONVERSION_LOG}" </dev/null >>"${LAUNCHER_LOG}" 2>&1 &
  conversion_pid=$!
  printf '%s\n' "${conversion_pid}" >"${PID_FILE}"
  sleep 0.5
  kill -0 "${conversion_pid}" 2>/dev/null \
    || fail "converter failed to start; inspect ${LAUNCHER_LOG}"
  echo "Converter started (PID ${conversion_pid})."
fi

if watchdog_is_running; then
  watchdog_pid=$(cat "${WATCHDOG_PID_FILE}")
  echo "Watchdog is already running (PID ${watchdog_pid})."
else
  nohup "${WATCHDOG}" "${common_args[@]}" "${watchdog_mode_arg}" \
    --python "${PYTHON_BIN}" \
    --converter "${CONVERTER}" \
    --interval-seconds "${INTERVAL_SECONDS}" \
    </dev/null >>"${WATCHDOG_LAUNCHER_LOG}" 2>&1 &
  watchdog_pid=$!
  sleep 0.5
  kill -0 "${watchdog_pid}" 2>/dev/null \
    || fail "watchdog failed to start; inspect ${WATCHDOG_LAUNCHER_LOG}"
  echo "Watchdog started (PID ${watchdog_pid})."
fi

echo
echo "Status: ${STATUS_SCRIPT} --output ${OUTPUT}"
echo "Progress log: tail -f ${CONVERSION_LOG}"
echo "Watchdog log: tail -f ${WATCHDOG_LOG}"

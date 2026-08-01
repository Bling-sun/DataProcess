#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Usage: monitor_lerobot_conversion.sh --source DIR --output DIR --python BIN [options]

Internal watchdog for start_lerobot_v21_conversion.sh. It checks the conversion
process at a fixed interval and resumes from the .inprogress directory if needed.

Required:
  --source DIR              Figure raw dataset root
  --output DIR              Final LeRobot v2.1 dataset root
  --python BIN              Python interpreter with numpy and pyarrow

Options:
  --converter FILE          Converter script path
  --task TEXT               Language instruction
  --fps NUMBER              Output FPS (default: 20)
  --gpus LIST               Comma-separated GPU indices
  --parquet-workers NUMBER  Parquet worker count (default: 8)
  --video-quality NUMBER    NVENC constant quality (default: 20)
  --interval-seconds N      Check interval (default: 1800)
  --skip-invalid            Skip invalid source episodes (default)
  --strict                  Abort on the first invalid episode
  -h, --help                Show this help
EOF
}

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE=""
OUTPUT=""
PYTHON_BIN=""
CONVERTER="${PROJECT_DIR}/scripts/batch_convert_lerobot_v21.py"
TASK="pick up the packaged item with both hands"
FPS=20
GPUS=""
PARQUET_WORKERS=8
VIDEO_QUALITY=20
INTERVAL_SECONDS=1800
SKIP_INVALID=1

while (($#)); do
  case "$1" in
    --source) SOURCE="${2:-}"; shift 2 ;;
    --output) OUTPUT="${2:-}"; shift 2 ;;
    --python) PYTHON_BIN="${2:-}"; shift 2 ;;
    --converter) CONVERTER="${2:-}"; shift 2 ;;
    --task) TASK="${2:-}"; shift 2 ;;
    --fps) FPS="${2:-}"; shift 2 ;;
    --gpus) GPUS="${2:-}"; shift 2 ;;
    --parquet-workers) PARQUET_WORKERS="${2:-}"; shift 2 ;;
    --video-quality) VIDEO_QUALITY="${2:-}"; shift 2 ;;
    --interval-seconds) INTERVAL_SECONDS="${2:-}"; shift 2 ;;
    --skip-invalid) SKIP_INVALID=1; shift ;;
    --strict) SKIP_INVALID=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "${SOURCE}" || -z "${OUTPUT}" || -z "${PYTHON_BIN}" ]]; then
  echo "--source, --output and --python are required." >&2
  usage >&2
  exit 2
fi
if [[ ! "${INTERVAL_SECONDS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "--interval-seconds must be a positive integer." >&2
  exit 2
fi

OUTPUT_PARENT="$(dirname "${OUTPUT}")"
OUTPUT_NAME="$(basename "${OUTPUT}")"
STAGING="${OUTPUT_PARENT}/.${OUTPUT_NAME}.inprogress"
CONVERSION_LOG="${OUTPUT_PARENT}/${OUTPUT_NAME}_conversion.log"
LAUNCHER_LOG="${OUTPUT_PARENT}/${OUTPUT_NAME}_launcher.log"
PID_FILE="${OUTPUT_PARENT}/${OUTPUT_NAME}_conversion.pid"
WATCHDOG_LOG="${OUTPUT_PARENT}/${OUTPUT_NAME}_watchdog.log"
WATCHDOG_PID_FILE="${OUTPUT_PARENT}/${OUTPUT_NAME}_watchdog.pid"

converter_args() {
  CONVERTER_ARGS=(
    "${PYTHON_BIN}" "${CONVERTER}"
    --source "${SOURCE}"
    --output "${OUTPUT}"
    --fps "${FPS}"
    --task "${TASK}"
    --gpus "${GPUS}"
    --parquet-workers "${PARQUET_WORKERS}"
    --video-quality "${VIDEO_QUALITY}"
    --log-file "${CONVERSION_LOG}"
  )
  if ((SKIP_INVALID)); then
    CONVERTER_ARGS+=(--skip-invalid)
  fi
}

expected_episodes() {
  local value
  value=""
  if [[ -f "${STAGING}/meta/info.json" ]]; then
    value=$("${PYTHON_BIN}" -c 'import json,sys; print(json.load(open(sys.argv[1]))["total_episodes"])' \
      "${STAGING}/meta/info.json" 2>/dev/null || true)
  elif [[ -f "${OUTPUT}/meta/info.json" ]]; then
    value=$("${PYTHON_BIN}" -c 'import json,sys; print(json.load(open(sys.argv[1]))["total_episodes"])' \
      "${OUTPUT}/meta/info.json" 2>/dev/null || true)
  fi
  if [[ ! "${value}" =~ ^[0-9]+$ ]] && [[ -f "${CONVERSION_LOG}" ]]; then
    value=$(sed -n 's/.*validated source: \([0-9][0-9]*\) usable\/.*/\1/p' \
      "${CONVERSION_LOG}" | tail -n 1)
  fi
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    value=$(find "${SOURCE}" -mindepth 1 -maxdepth 1 -type d -name 'episode_*' 2>/dev/null | wc -l)
  fi
  printf '%s' "${value}"
}

log_status() {
  local now main_pid main_state parquet_count video_count temporary_count encoders episodes videos
  now=$(date --iso-8601=seconds)
  main_pid=$(cat "${PID_FILE}" 2>/dev/null || true)
  main_state=missing
  if [[ "${main_pid}" =~ ^[0-9]+$ ]] && kill -0 "${main_pid}" 2>/dev/null; then
    main_state=$(ps -p "${main_pid}" -o stat= 2>/dev/null | tr -d ' ')
  fi
  parquet_count=$(find "${STAGING}/data" -type f -name '*.parquet' 2>/dev/null | wc -l)
  video_count=$(find "${STAGING}/videos" -type f -name '*.mp4' 2>/dev/null | wc -l)
  temporary_count=$(find "${STAGING}/videos" -type f -name '*.tmp' 2>/dev/null | wc -l)
  encoders=$(ps -eo args= | awk -v needle="${STAGING}" \
    'index($0,"ffmpeg") && index($0,needle) {count++} END {print count+0}')
  episodes=$(expected_episodes)
  videos=$((episodes * 3))
  printf '%s main_pid=%s main_state=%s parquet=%s/%s video=%s/%s temp=%s encoders=%s\n' \
    "${now}" "${main_pid:-none}" "${main_state}" "${parquet_count}" "${episodes}" \
    "${video_count}" "${videos}" "${temporary_count}" "${encoders}" >>"${WATCHDOG_LOG}"
}

main_is_running() {
  local main_pid command_line
  main_pid=$(cat "${PID_FILE}" 2>/dev/null || true)
  [[ "${main_pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${main_pid}" 2>/dev/null || return 1
  command_line=$(tr '\0' ' ' <"/proc/${main_pid}/cmdline" 2>/dev/null || true)
  [[ "${command_line}" == *batch_convert_lerobot_v21.py* ]] || return 1
  [[ "${command_line}" == *"${SOURCE}"* ]] || return 1
  [[ "${command_line}" == *"${OUTPUT}"* ]]
}

start_conversion() {
  local conversion_pid
  cd "${PROJECT_DIR}" || return 1
  converter_args
  nohup "${CONVERTER_ARGS[@]}" </dev/null >>"${LAUNCHER_LOG}" 2>&1 &
  conversion_pid=$!
  printf '%s\n' "${conversion_pid}" >"${PID_FILE}"
  printf '%s restart pid=%s\n' "$(date --iso-8601=seconds)" "${conversion_pid}" >>"${WATCHDOG_LOG}"
}

mkdir -p "${OUTPUT_PARENT}"
printf '%s\n' "$$" >"${WATCHDOG_PID_FILE}"
printf '%s watchdog_started pid=%s interval=%ss\n' \
  "$(date --iso-8601=seconds)" "$$" "${INTERVAL_SECONDS}" >>"${WATCHDOG_LOG}"

while true; do
  if [[ -f "${OUTPUT}/_SUCCESS" ]]; then
    printf '%s completed output=%s\n' "$(date --iso-8601=seconds)" "${OUTPUT}" >>"${WATCHDOG_LOG}"
    exit 0
  fi

  log_status
  if ! main_is_running; then
    printf '%s main_process_missing; restarting from checkpoint\n' \
      "$(date --iso-8601=seconds)" >>"${WATCHDOG_LOG}"
    start_conversion
  fi
  sleep "${INTERVAL_SECONDS}"
done

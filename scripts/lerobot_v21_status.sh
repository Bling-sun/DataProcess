#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Usage: lerobot_v21_status.sh --output /absolute/path/to/final_dataset

Shows converter/watchdog state, output counts, latest progress, and final
validation summary for a batch_convert_lerobot_v21.py job.
EOF
}

OUTPUT=""
while (($#)); do
  case "$1" in
    --output) OUTPUT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "${OUTPUT}" || "${OUTPUT}" != /* ]]; then
  echo "--output must be an absolute path." >&2
  usage >&2
  exit 2
fi

OUTPUT="$(readlink -m -- "${OUTPUT}")"
OUTPUT_PARENT="$(dirname "${OUTPUT}")"
OUTPUT_NAME="$(basename "${OUTPUT}")"
STAGING="${OUTPUT_PARENT}/.${OUTPUT_NAME}.inprogress"
CONVERSION_LOG="${OUTPUT_PARENT}/${OUTPUT_NAME}_conversion.log"
PID_FILE="${OUTPUT_PARENT}/${OUTPUT_NAME}_conversion.pid"
WATCHDOG_LOG="${OUTPUT_PARENT}/${OUTPUT_NAME}_watchdog.log"
WATCHDOG_PID_FILE="${OUTPUT_PARENT}/${OUTPUT_NAME}_watchdog.pid"

process_state() {
  local pid_file="$1" expected="$2" pid command_line state
  pid=$(cat "${pid_file}" 2>/dev/null || true)
  if [[ ! "${pid}" =~ ^[0-9]+$ ]] || ! kill -0 "${pid}" 2>/dev/null; then
    printf 'stopped'
    return
  fi
  state=$(ps -p "${pid}" -o stat= 2>/dev/null | tr -d ' ')
  if [[ "${state}" == Z* ]]; then
    printf 'stopped'
    return
  fi
  command_line=$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)
  if [[ "${command_line}" != *"${expected}"* || "${command_line}" != *"${OUTPUT}"* ]]; then
    printf 'stale-pid-file(pid=%s)' "${pid}"
    return
  fi
  printf 'running(pid=%s,state=%s)' "${pid}" "${state:-unknown}"
}

expected_episodes="?"
if [[ -f "${CONVERSION_LOG}" ]]; then
  parsed=$(sed -n 's/.*validated source: \([0-9][0-9]*\) usable\/.*/\1/p' \
    "${CONVERSION_LOG}" | tail -n 1)
  if [[ "${parsed}" =~ ^[0-9]+$ ]]; then
    expected_episodes="${parsed}"
  fi
fi

parquet_count=$(find "${STAGING}/data" -type f -name '*.parquet' 2>/dev/null | wc -l)
video_count=$(find "${STAGING}/videos" -type f -name '*.mp4' 2>/dev/null | wc -l)
temporary_count=$(find "${STAGING}/videos" -type f -name '*.tmp' 2>/dev/null | wc -l)
expected_videos="?"
if [[ "${expected_episodes}" =~ ^[0-9]+$ ]]; then
  expected_videos=$((expected_episodes * 3))
fi

echo "time:       $(date --iso-8601=seconds)"
echo "output:     ${OUTPUT}"
echo "converter:  $(process_state "${PID_FILE}" batch_convert_lerobot_v21.py)"
echo "watchdog:   $(process_state "${WATCHDOG_PID_FILE}" monitor_lerobot_conversion.sh)"

if [[ -f "${OUTPUT}/_SUCCESS" && -f "${OUTPUT}/meta/validation.json" ]]; then
  python_bin=""
  for candidate in \
    "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.venv/bin/python" \
    /mnt/sunbing/projects/FigureDemo/envs/lerobot-validator/bin/python \
    "$(command -v python3 2>/dev/null || true)"; do
    if [[ -x "${candidate}" ]]; then
      python_bin="${candidate}"
      break
    fi
  done
  echo "stage:      complete"
  echo "success:    $(cat "${OUTPUT}/_SUCCESS")"
  if [[ -n "${python_bin}" ]]; then
    "${python_bin}" - "${OUTPUT}/meta/validation.json" "${OUTPUT}/meta/info.json" <<'PY'
import json
import sys

validation = json.load(open(sys.argv[1], encoding="utf-8"))
info = json.load(open(sys.argv[2], encoding="utf-8"))
print(f"validation: {validation.get('status')}")
print(f"version:    {info.get('codebase_version')}")
print(f"episodes:   {validation.get('episodes')}")
print(f"frames:     {validation.get('frames')}")
print(f"videos:     {validation.get('videos')}")
print(f"skipped:    {info.get('source_episodes_skipped', 0)}")
PY
  fi
  exit 0
fi

echo "stage:      in-progress"
echo "parquet:    ${parquet_count}/${expected_episodes}"
echo "videos:     ${video_count}/${expected_videos}"
echo "temporary:  ${temporary_count}"

if [[ -f "${CONVERSION_LOG}" ]]; then
  echo
  echo "Latest conversion log:"
  tail -n 8 "${CONVERSION_LOG}"
fi
if [[ -f "${WATCHDOG_LOG}" ]]; then
  echo
  echo "Latest watchdog log:"
  tail -n 5 "${WATCHDOG_LOG}"
fi

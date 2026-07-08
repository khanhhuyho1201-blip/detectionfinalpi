#!/bin/bash
set -euo pipefail

LOG_FILE="${1:-/tmp/wifi_ap_bench.log}"
SCRIPT_DIR="/home/bbsw/workspace/device_service"
AP_SCRIPT="$SCRIPT_DIR/wifi_ap.sh"

ts_ms() {
  python3 - <<'PY'
import time
print(f"{time.monotonic():.6f}")
PY
}

now_iso() {
  date --iso-8601=seconds
}

start_ts="$(ts_ms)"
log() {
  local now delta
  now="$(ts_ms)"
  delta="$(python3 - <<PY
start=float("$start_ts")
now=float("$now")
print(f"{now-start:.3f}")
PY
)"
  echo "[$(now_iso)] +${delta}s $*" | tee -a "$LOG_FILE"
}

wait_for() {
  local label="$1"
  local timeout_s="$2"
  shift 2
  local deadline
  deadline="$(python3 - <<PY
import time
print(time.monotonic() + float("$timeout_s"))
PY
)"
  while true; do
    if "$@" >/dev/null 2>&1; then
      log "$label"
      return 0
    fi
    python3 - <<PY
import time,sys
sys.exit(0 if time.monotonic() < float("$deadline") else 1)
PY
    sleep 0.1
  done
}

log "begin benchmark"
bash "$AP_SCRIPT" down >/dev/null 2>&1 || true
sleep 1
log "calling wifi_ap.sh up"
bash "$AP_SCRIPT" up >>"$LOG_FILE" 2>&1 &
up_pid=$!

wait_for "AP connection active" 20 bash -lc "nmcli -t -f NAME con show --active | grep -qx CardFeederAP"
wait_for "wlan0 has 10.42.0.1" 20 bash -lc "ip -4 addr show wlan0 | grep -q '10\\.42\\.0\\.1/'"
wait_for "HTTP 127.0.0.1:80 ready" 20 bash -lc "curl -fsS --max-time 1 http://127.0.0.1/ >/dev/null"
wait_for "HTTP 10.42.0.1:80 ready" 20 bash -lc "curl -fsS --max-time 1 http://10.42.0.1/ >/dev/null"
wait "$up_pid"
log "benchmark done"

#!/bin/bash
# Web-kiosk launcher: local server + Chromium fullscreen, with crash auto-recovery.
cd "$(dirname "$0")"
export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
# 3.5" MPI3508 panel: force 3:2 native each launch (else EDID picks 1280x720 -> stretched).
# Try both ports — the panel enumerates as HDMI-A-1 or HDMI-A-2 depending on which
# micro-HDMI it's plugged into (it moved ports after the power-loss repair).
for HDMI_OUT in HDMI-A-1 HDMI-A-2; do
  wlr-randr --output "$HDMI_OUT" --mode 720x480 --scale 1.5 2>/dev/null && break
done
PORT="${CARD_WEB_PORT:-8800}"
PROFILE="$HOME/.card_kiosk_profile"

# start the UI/control server — RELAUNCH nếu nó chết (trước đây chạy 1 lần: server
# crash thì chỉ còn Chromium nháy vào server chết mãi, systemd không restart unit).
# Use the project venv (flask is installed there, not on system python3)
PYTHON="${VENV_PYTHON:-/home/bbsw/workspace/venv/bin/python}"
(
  while true; do
    "$PYTHON" -u server.py
    echo "[kiosk] server.py thoát (code $?) — relaunch sau 2s" >&2
    sleep 2
  done
) &
SERVER=$!
# EXIT: dừng cả vòng relaunch server + tiến trình python con + Chromium
trap 'kill $SERVER 2>/dev/null; pkill -f "[s]erver.py" 2>/dev/null; pkill -f "card_kiosk_profile" 2>/dev/null' EXIT

# wait until it answers
for i in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1 && break
  sleep 0.3
done

# Fresh profile each launch — the SD card is flaky and a corrupt Chromium
# GPU/shader cache causes "Aw Snap (Error 11)" renderer crashes.
rm -rf "$PROFILE" 2>/dev/null

# Watchdog: if the page stops polling /api/state (renderer "Aw Snap" crash) or
# the server dies, /api/_alive fails → kill Chromium. The relaunch loop below
# then brings it straight back (self-healing) instead of leaving a dead screen.
(
  sleep 25                       # grace for first load
  while sleep 5; do
    curl -sf --max-time 2 "http://127.0.0.1:$PORT/api/_alive" >/dev/null 2>&1 \
      || pkill -f "card_kiosk_profile"
  done
) &

# Chromium kiosk. Two hard-won fixes for this Pi 5 (labwc/Wayland) + chromium 147:
#   1) Call the chromium BINARY directly, NOT the /usr/bin/chromium wrapper — the
#      RPi wrapper injects --js-flags=--no-decommit-pooled-pages (removed in v147
#      → "unrecognized flag", chromium won't start) plus --use-angle=gles /
#      --enable-gpu-rasterization that made the renderer SIGSEGV.
#   2) Run NATIVE Wayland (--ozone-platform=wayland), not XWayland (x11): XWayland
#      fullscreen on labwc was the source of "Aw, Snap! Error code 11".
# The whole launch is wrapped in a relaunch loop so any crash self-heals.
CHROME_BIN="/usr/lib/chromium/chromium"
[ -x "$CHROME_BIN" ] || CHROME_BIN="chromium"
while true; do
  "$CHROME_BIN" \
    --kiosk --ozone-platform=wayland \
    --disable-dev-shm-usage \
    --force-device-scale-factor=1 \
    --app="http://127.0.0.1:$PORT/" \
    --password-store=basic --use-mock-keychain \
    --no-first-run --no-default-browser-check \
    --noerrdialogs --disable-infobars --disable-translate \
    --disable-features=Translate,TranslateUI --lang=vi-VN \
    --check-for-update-interval=31536000 --disable-component-update \
    --overscroll-history-navigation=0 --disable-pinch \
    --autoplay-policy=no-user-gesture-required \
    --user-data-dir="$PROFILE"
  sleep 1   # chromium exited/crashed → relaunch
done

# (loop never returns; trap stops the server if the unit is stopped)

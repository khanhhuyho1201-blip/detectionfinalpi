#!/bin/bash
# stop_kiosk.sh — dừng toàn bộ kiosk service (Chromium + server.py).
# Được gọi từ labwc keybind (Alt+F4) — phải tự set env vì labwc Execute
# không kế thừa DBUS_SESSION_BUS_ADDRESS / XDG_RUNTIME_DIR từ user session.

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

echo "$(date): stop_kiosk called, stopping service..." >> /tmp/stop_kiosk.log
systemctl --user stop card-device.service
echo "$(date): systemctl stop exit=$?, service now: $(systemctl --user is-active card-device.service)" >> /tmp/stop_kiosk.log

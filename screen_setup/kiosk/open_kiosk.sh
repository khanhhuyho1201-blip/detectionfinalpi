#!/bin/bash
# open_kiosk.sh — mở/đưa màn hình Card Feeder lên trước mặt khi bấm icon desktop.
#
# Vì sao cần script này: khi double-click icon trên desktop (labwc/Wayland), lệnh
# chạy KHÔNG có XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS, nên `systemctl --user`
# thất bại im lặng -> "bấm không lên". Script tự set các biến đó.
#
# Hành vi: nếu kiosk CHƯA chạy -> start. Nếu service đang chạy nhưng cửa sổ
# Chromium đã chết -> restart. Nếu đang chạy bình thường -> chỉ đảm bảo nó ở đó
# (kiosk fullscreen tự chiếm màn hình), KHÔNG restart để tránh nháy đen.

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
export DISPLAY="${DISPLAY:-:0}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"

SC="/usr/bin/systemctl --user"
UNIT="card-device.service"

active() { $SC is-active "$UNIT" >/dev/null 2>&1; }
kiosk_window() { pgrep -f card_kiosk_profile >/dev/null 2>&1; }

if ! active; then
    # service chưa chạy -> bật lên
    $SC start "$UNIT"
elif ! kiosk_window; then
    # service "active" nhưng Chromium kiosk đã chết -> làm mới
    $SC restart "$UNIT"
fi
# nếu cả service lẫn cửa sổ đều đang chạy: không làm gì (kiosk fullscreen đã hiển thị)

exit 0

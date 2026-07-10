#!/bin/bash
# restart_card.sh — bấm icon "Card Feeder" trên desktop => mở / khởi động lại
# màn hình Card Feeder (card-device.service).
#
# Vì sao cần script (giống open_kiosk.sh): khi double-click icon trên desktop
# (labwc/Wayland), lệnh chạy KHÔNG có XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS,
# nên `systemctl --user` thất bại im lặng -> "bấm không lên". Script tự set biến.
#
# Lưu ý: card-device.service là USER service (~/.config/systemd/user/), KHÔNG phải
# system service => dùng `systemctl --user`, KHÔNG dùng `sudo systemctl`.

LOG="$HOME/cardfeeder_click.log"
echo "===== $(date '+%F %T') click =====" >>"$LOG"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
export DISPLAY="${DISPLAY:-:0}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"

{
  echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
  echo "DBUS=$DBUS_SESSION_BUS_ADDRESS"
  echo "USER=$(id -un) UID=$(id -u)"
} >>"$LOG"

cd /home/bbsw/workspace/device_service || exit 1

SC="/usr/bin/systemctl --user"
UNIT="card-device.service"

if $SC is-active "$UNIT" >/dev/null 2>&1 && pgrep -f card_kiosk_profile >/dev/null 2>&1; then
  # đang chạy và cửa sổ còn sống -> restart để làm mới theo yêu cầu
  echo "action: restart (đang chạy)" >>"$LOG"
  $SC restart "$UNIT" >>"$LOG" 2>&1
else
  # chưa chạy hoặc cửa sổ đã chết -> bật lên
  echo "action: start (chưa chạy / cửa sổ chết)" >>"$LOG"
  $SC start "$UNIT" >>"$LOG" 2>&1
fi

echo "exit systemctl rc=$? ; is-active=$($SC is-active "$UNIT" 2>&1)" >>"$LOG"
exit 0

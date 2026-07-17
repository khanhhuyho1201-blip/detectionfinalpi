#!/bin/bash
# Launcher for Card Feeder kiosk on THIS Pi (X11 / LXDE / pcmanfm desktop).
# The repo's kiosk.sh targets Wayland/labwc (wlr-randr, --ozone-platform=wayland),
# which does NOT work here — this machine is X11 (DISPLAY=:0, no wayland socket).
# So we run the server + Chromium ourselves with X11 flags.

REPO="/home/bbsw/workspace/detectionfinalpi"
PYTHON="$REPO/.venv/bin/python"
PORT="${CARD_WEB_PORT:-8800}"
PROFILE="$HOME/.card_kiosk_profile"
LOG="/tmp/card-feeder.log"

export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# ── Chống chạy trùng: nếu đã có instance đang chạy thì thôi (khỏi loop chồng). ──
LOCK="/tmp/card-feeder.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[launch] Card Feeder đã đang chạy — không mở thêm." >> "$LOG"
  exit 0
fi

# Chọn cổng Arduino: ưu tiên ttyUSB (Nano FTDI/CH340) rồi ttyACM (Uno native-USB).
# No board -> fallback REAL port /dev/ttyACM0 (NOT sim): connected=False -> UI shows Arduino/Motor missing + gates START truthfully. Force sim ONLY via explicit CARD_SERIAL_PORT=sim (tests/demo).
# (Env CARD_SERIAL_PORT đặt sẵn từ ngoài vẫn được tôn trọng — vd ép "sim" để test.)
DEV="$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -1)"
export CARD_SERIAL_PORT="${CARD_SERIAL_PORT:-${DEV:-/dev/ttyACM0}}"
echo "[launch] CARD_SERIAL_PORT=$CARD_SERIAL_PORT" >> "$LOG"

cd "$REPO/device_service" || exit 1

echo "===== $(date) launch =====" >> "$LOG"

# Dừng mọi tàn dư cũ để khỏi tranh cổng 8800.
pkill -f "[s]erver.py" 2>/dev/null
sleep 0.5

# Server Flask — relaunch nếu chết.
(
  while true; do
    "$PYTHON" -u server.py >> "$LOG" 2>&1
    echo "[launch] server.py thoát (code $?) — relaunch sau 2s" >> "$LOG"
    sleep 2
  done
) &
SERVER=$!

# Khi thoát: dừng server + chromium của kiosk này.
trap 'kill $SERVER 2>/dev/null; pkill -f "[s]erver.py" 2>/dev/null; pkill -f "card_kiosk_profile" 2>/dev/null' EXIT

# Chờ server trả lời.
for i in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1 && break
  sleep 0.3
done

# Profile mới mỗi lần (SD hay lỗi cache GPU).
rm -rf "$PROFILE" 2>/dev/null

CHROME_BIN="/usr/lib/chromium/chromium"
[ -x "$CHROME_BIN" ] || CHROME_BIN="chromium"

# Chromium kiosk trên X11. KHÔNG dùng wayland ở máy này.
"$CHROME_BIN" \
  --kiosk --ozone-platform=x11 \
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
  --user-data-dir="$PROFILE" >> "$LOG" 2>&1

# Chromium đóng -> trap dừng server. (Không loop để tránh nháy vô tận.)

#!/bin/bash
###############################################################################
# flash.sh — Nạp production.ino vào Arduino Nano (board v2), TỰ ĐỘNG hết:
#   compile -> dừng app (nhả cổng) -> upload (tự dò bootloader mới/cũ)
#   -> reset feed-forward EEPROM (F0) -> khởi động lại app.
#
# Dùng:   ./flash.sh              (cổng mặc định /dev/ttyACM0)
#         ./flash.sh /dev/ttyACM1 (nếu cổng khác)
#
# Nano & Uno cùng chip ATmega328P -> CODE Y HỆT. Script tự thử bootloader mới
# (115200) trước, lỗi thì fallback bootloader cũ (57600) — khỏi lo Nano đời nào.
###############################################################################
set -u
export PATH="$HOME/bin:$PATH"

DIR="$(cd "$(dirname "$0")" && pwd)"
SKETCH="$DIR/production.ino"
STAGE="/tmp/production"
PORT="${1:-/dev/ttyACM0}"
LAUNCHER="$HOME/.local/bin/card-feeder-launch.sh"
LOCK="/tmp/card-feeder.lock"

echo "=== NẠP FIRMWARE: $SKETCH -> $PORT ==="

# ---- 1) COMPILE (không cần board) ----
echo "[1/6] Compile (arduino:avr:nano)..."
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp "$SKETCH" "$STAGE/production.ino"
if ! arduino-cli compile --fqbn arduino:avr:nano "$STAGE"; then
    echo "❌ COMPILE LỖI — sửa code rồi chạy lại. (Không đụng tới board/app.)"; exit 1
fi
echo "  ✓ compile OK"

# ---- 2) Kiểm tra board đã cắm ----
if [ ! -e "$PORT" ]; then
    echo "❌ Không thấy $PORT. Cắm Arduino Nano bằng cáp USB rồi chạy lại."
    echo "   (Cổng khác thì:  ./flash.sh /dev/ttyACM1  — xem: ls /dev/ttyACM* /dev/ttyUSB*)"
    exit 1
fi

# ---- 3) DỪNG app để nhả cổng serial ----
echo "[2/6] Dừng app (nhả cổng $PORT)..."
systemctl --user stop 'app-cardfeeder@autostart.service' 2>/dev/null || true
pkill -f "card-feeder-launch.sh" 2>/dev/null || true
sleep 1
pkill -f "[s]erver.py" 2>/dev/null || true
rm -f "$LOCK"
sleep 2
if command -v fuser >/dev/null && fuser "$PORT" >/dev/null 2>&1; then
    echo "  ! Cổng còn bị giữ — chờ thêm 3s..."; sleep 3
fi
echo "  ✓ cổng đã nhả"

# ---- 4) UPLOAD (bootloader mới 115200 -> fallback cũ 57600) ----
restart_app() {
    echo "[6/6] Khởi động lại app..."
    setsid "$LAUNCHER" >/dev/null 2>&1 &
    echo "  ✓ app đang khởi động lại (kiosk + server)"
}

# Thứ tự thử bootloader: Nano trên ttyUSB (FTDI/CH340) hầu hết là bootloader CŨ
# (57600) -> thử cũ trước cho trúng ngay, khỏi treo ~30s ở 115200. ttyACM (Uno /
# Nano native-USB) thì thử MỚI trước. Dù sao vẫn fallback cái còn lại.
case "$PORT" in
    *ttyUSB*) ORDER="atmega328old atmega328"; echo "[3/6] Upload — cổng $PORT (USB-serial) -> thử bootloader CŨ (57600) trước..." ;;
    *)        ORDER="atmega328 atmega328old"; echo "[3/6] Upload — thử bootloader MỚI (115200) trước..." ;;
esac
UPOK=0
for cpu in $ORDER; do
    baud=$([ "$cpu" = atmega328old ] && echo 57600 || echo 115200)
    echo "  -> arduino:avr:nano:cpu=$cpu (baud $baud)..."
    if arduino-cli upload -p "$PORT" --fqbn "arduino:avr:nano:cpu=$cpu" "$STAGE" 2>&1; then
        echo "  ✓ NẠP OK ($cpu / $baud)"; UPOK=1; break
    fi
    echo "  ! $cpu lỗi -> thử cái còn lại..."
done
if [ "$UPOK" != 1 ]; then
    echo "❌ NẠP THẤT BẠI cả 2 bootloader. Kiểm tra: cáp USB / cổng $PORT / driver."
    restart_app
    exit 1
fi

# ---- 5) Reset feed-forward EEPROM (chỉ cần khi đổi tốc/pin; vô hại nếu không) ----
echo "[4/6] Reset feed-forward EEPROM (lệnh F0)..."
python3 - "$PORT" <<'PY' 2>/dev/null && echo "  ✓ F0 xong" || echo "  (bỏ qua F0 — không sao)"
import serial, time, sys
s = serial.Serial(sys.argv[1], 115200, timeout=1)
time.sleep(2.8); s.reset_input_buffer()
s.write(b"F0\n"); time.sleep(1.0); s.close()
PY

echo "[5/6] (xong nạp)"
restart_app
echo ""
echo "✅ HOÀN TẤT. Firmware v2 đã nạp vào Nano. App đang chạy lại."
echo "   Kiểm tra: bấm nút trên màn hình -> máy chạy; chạm công tắc D7 -> stepper dừng ngay."

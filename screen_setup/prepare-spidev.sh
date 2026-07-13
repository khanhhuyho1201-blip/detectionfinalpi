#!/bin/bash
# Chuẩn bị chip cảm ứng ADS7846 (spi0.1) cho driver userspace:
# tách nó khỏi driver kernel "ads7846" và gắn "spidev" để đọc SPI thô.
# Idempotent: chạy lại nhiều lần không sao.
set -u

SPIDEV="/sys/bus/spi/devices/spi0.1"
DRV_ADS="/sys/bus/spi/drivers/ads7846"
DRV_SPIDEV="/sys/bus/spi/drivers/spidev"

echo "[prepare-spidev] Bắt đầu..."

# Đảm bảo module spidev + uinput có mặt
modprobe spidev 2>/dev/null || true
modprobe uinput 2>/dev/null || true

# Chờ spi0.1 xuất hiện (tối đa ~10s sau boot)
for i in $(seq 1 50); do
    [ -e "$SPIDEV" ] && break
    sleep 0.2
done
if [ ! -e "$SPIDEV" ]; then
    echo "[prepare-spidev] LỖI: không thấy $SPIDEV" >&2
    exit 1
fi

# Nếu ads7846 đang giữ spi0.1 -> unbind
if [ -e "$DRV_ADS/spi0.1" ]; then
    echo "[prepare-spidev] Unbind ads7846 khỏi spi0.1"
    echo "spi0.1" > "$DRV_ADS/unbind" 2>/dev/null || true
    sleep 0.3
fi

# Đặt driver_override = spidev rồi bind (nếu chưa có /dev/spidev0.1)
if [ ! -e /dev/spidev0.1 ]; then
    echo "[prepare-spidev] Gắn spidev vào spi0.1"
    echo "spidev" > "$SPIDEV/driver_override" 2>/dev/null || true
    echo "spi0.1" > "$DRV_SPIDEV/bind" 2>/dev/null || true
    sleep 0.3
fi

if [ -e /dev/spidev0.1 ]; then
    echo "[prepare-spidev] OK: /dev/spidev0.1 sẵn sàng"
    exit 0
else
    echo "[prepare-spidev] LỖI: không tạo được /dev/spidev0.1" >&2
    exit 1
fi

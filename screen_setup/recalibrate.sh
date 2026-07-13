#!/bin/bash
###############################################################################
# HIỆU CHỈNH CẢM ỨNG TỰ PHỤC VỤ
# Anh chạy:  sudo /opt/ads7846-touch/recalibrate.sh   (hoặc đường dẫn tương ứng)
# Rồi làm theo trên MÀN HÌNH LCD: chạm 9 dấu +, tự áp dụng, khỏi cần thao tác gì thêm.
#
# MẸO QUAN TRỌNG để chính xác:
#   - Ấn CHẮC (ép hẳn ngón tay xuống), GIỮ YÊN ~1.5 giây tới khi chấm vàng to lên.
#   - Chạm đúng TÂM dấu +. Đừng chạm nhẹ/lướt.
###############################################################################
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$DIR/touch.env"
[ -f "$ENV_FILE" ] || ENV_FILE="/opt/ads7846-touch/touch.env"

# tìm DISPLAY của phiên X đang chạy
export DISPLAY="${DISPLAY:-:0}"
GUI_USER="$(who | awk '/tty|:0/{print $1; exit}')"
[ -z "$GUI_USER" ] && GUI_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
GUI_HOME="$(getent passwd "$GUI_USER" | cut -d: -f6)"
export XAUTHORITY="${XAUTHORITY:-$GUI_HOME/.Xauthority}"

echo "=== Hiệu chỉnh cảm ứng ==="
echo "User X: $GUI_USER  DISPLAY=$DISPLAY"

echo "[1/4] Dừng service cảm ứng..."
systemctl stop ads7846-touch.service 2>/dev/null || true
sleep 1

echo "[2/4] Chuẩn bị SPI..."
"$DIR/prepare-spidev.sh" >/dev/null 2>&1 || true

echo "[3/4] Mở bảng hiệu chỉnh trên màn hình LCD."
echo "      >>> Chạm CHẮC 9 dấu + (giữ ~1.5s mỗi cái), rồi xem chế độ kiểm tra."
rm -f "$DIR/affine_result.txt" "$DIR/calib_points.json" "$DIR/best_model.json"
# chạy GUI với quyền user X (để mở được cửa sổ)
sudo -u "$GUI_USER" DISPLAY="$DISPLAY" XAUTHORITY="$XAUTHORITY" \
    python3 "$DIR/affine_calib.py" || true

echo "[4/4] Chọn model tốt nhất & áp dụng..."
if [ -f "$DIR/calib_points.json" ]; then
    # fit affine/bilinear/biquadratic, chọn theo kiểm định chéo LOO
    python3 "$DIR/fit_models.py" || true
    if [ -f "$DIR/best_model.json" ]; then
        KIND="$(python3 -c 'import json;print(json.load(open("'"$DIR"'/best_model.json"))["kind"])' 2>/dev/null)"
        VAL="$(python3 -c 'import json;print(json.load(open("'"$DIR"'/best_model.json"))["value"])' 2>/dev/null)"
        # gỡ mọi dòng hiệu chỉnh cũ
        sed -i 's|^TS_AFFINE=|#TS_AFFINE=|; s|^TS_POLY=|#TS_POLY=|' "$ENV_FILE"
        if [ "$KIND" = "poly" ]; then
            echo "TS_POLY=$VAL" >> "$ENV_FILE"
            echo "  ✓ Áp dụng model phi tuyến (TS_POLY)"
        else
            echo "TS_AFFINE=$VAL" >> "$ENV_FILE"
            echo "  ✓ Áp dụng model affine (TS_AFFINE)"
        fi
    else
        echo "  ! Không tính được model — giữ nguyên hiệu chỉnh cũ."
    fi
else
    echo "  ! Chưa hoàn tất 9 điểm — giữ nguyên hiệu chỉnh cũ."
fi

echo "Khởi động lại service..."
systemctl start ads7846-touch.service
sleep 2
if systemctl is-active --quiet ads7846-touch.service; then
    echo "✅ XONG. Cảm ứng đã chạy lại với hiệu chỉnh mới. Anh chạm thử nhé."
else
    echo "⚠ Service chưa chạy. Kiểm tra: journalctl -u ads7846-touch -n 20"
fi

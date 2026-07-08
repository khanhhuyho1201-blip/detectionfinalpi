#!/bin/bash
# Cài thư viện cho button_start_stop trên Raspberry Pi OS.
# Trên Pi hiện tại (bbswcmd) MỌI thư viện đã có sẵn — script này dành cho máy mới.
set -e
cd "$(dirname "$0")"

echo "=== button_start_stop — Install ==="

# Gói hệ thống (tkinter, Pillow+ImageTk, pyserial, requests, ffmpeg, v4l-utils)
sudo apt-get update -q
sudo apt-get install -y \
    python3-tk python3-pil python3-pil.imagetk \
    python3-serial python3-requests \
    ffmpeg v4l-utils

# (Tuỳ chọn) qua pip nếu thiếu trên distro:
# pip3 install --break-system-packages pyserial Pillow requests

echo ""
echo "=== Kiểm tra ==="
python3 - <<'PY'
import importlib
for m in ("tkinter","PIL","PIL.ImageTk","serial","requests"):
    try:
        importlib.import_module(m); print(f"  OK  {m}")
    except Exception as e:
        print(f"  THIẾU {m}: {e}")
PY

echo ""
echo "Xong. Chạy thử (không cần phần cứng):  ./run.sh sim"

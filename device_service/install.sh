#!/bin/bash
set -e

echo "=== Card Device — Install ==="

cd "$(dirname "$0")"

# System packages
sudo apt-get update -q
sudo apt-get install -y python3-pip python3-tk python3-pil python3-pil.imagetk \
    libzbar0 libgl1 libglib2.0-0 ffmpeg

# Python packages
pip3 install --break-system-packages requests Pillow pyzbar

# Try picamera2 (only on real Pi with camera)
pip3 install --break-system-packages picamera2 || echo "picamera2 not available — camera will use dummy mode"

echo ""
echo "=== Install xong ==="
echo "Chạy ứng dụng:"
echo "  python3 app.py"

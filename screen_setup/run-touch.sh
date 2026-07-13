#!/bin/bash
# Wrapper: chuẩn bị SPI (tách ads7846, gắn spidev) rồi chạy driver userspace.
set -e
DIR="/home/bbsw/ads7846-userspace"
"$DIR/prepare-spidev.sh"
exec python3 "$DIR/ads7846_touch.py"

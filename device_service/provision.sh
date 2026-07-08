#!/bin/bash
# provision.sh — nạp credential (do server cấp lúc tạo máy) vào Pi.
#
# Dùng lúc SẢN XUẤT: admin tạo máy trên server -> nhận device_id + device_key,
# rồi chạy script này trên Pi để ghi ~/.card_device/credentials.json. Sau đó máy
# nói chuyện được với server ngay (không cần enroll qua mạng).
#
#   ./provision.sh <server_url> <device_id> <device_key>
#
# Ví dụ:
#   ./provision.sh http://100.110.72.1:8040 2bc6437b-... Ys24OumQ...
#
# Tùy chọn: kiểm tra heartbeat ngay sau khi ghi (cần đã có mạng tới server).
set -euo pipefail

CRED_DIR="$HOME/.card_device"
CRED_FILE="$CRED_DIR/credentials.json"

usage() {
  echo "Cách dùng: $0 <server_url> <device_id> <device_key>" >&2
  echo "  vd: $0 http://100.110.72.1:8040 <device_id> <device_key>" >&2
  exit 1
}

[ $# -eq 3 ] || usage
SERVER_URL="$1"; DEVICE_ID="$2"; DEVICE_KEY="$3"

# strip trailing slash on server_url
SERVER_URL="${SERVER_URL%/}"

mkdir -p "$CRED_DIR"
# write JSON atomically (matches config.py: server_url/device_id/device_key)
python3 - "$SERVER_URL" "$DEVICE_ID" "$DEVICE_KEY" "$CRED_FILE" <<'PY'
import json, os, sys
server_url, device_id, device_key, path = sys.argv[1:5]
data = {"server_url": server_url, "device_id": device_id, "device_key": device_key}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, path)
os.chmod(path, 0o600)
print("Đã ghi", path)
PY

echo "=== credentials.json ==="
cat "$CRED_FILE"
echo

# optional connectivity check — đổi device_key lấy access token. Đây là endpoint
# DUY NHẤT nhận device_key (heartbeat/runs đều yêu cầu Bearer token), nên token
# 200 = key hợp lệ + kết nối được server.
echo "=== Kiểm tra kết nối server (đổi key → token) ==="
HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 \
  -X POST "$SERVER_URL/api/device/token" \
  -H "X-Device-Id: $DEVICE_ID" \
  -H "X-Device-Key: $DEVICE_KEY" || echo "000")
if [ "$HTTP" = "200" ]; then
  echo "OK — máy active, key hợp lệ, kết nối được server (token 200)."
elif [ "$HTTP" = "401" ] || [ "$HTTP" = "403" ]; then
  echo "LỖI — server từ chối key (HTTP=$HTTP). Kiểm tra lại device_id/device_key."
else
  echo "Chưa kiểm tra được (HTTP=$HTTP) — có thể chưa có mạng tới server."
  echo "Credential vẫn đã được nạp; máy sẽ kết nối khi có mạng."
fi

echo
echo "Xong. Khởi động lại app: systemctl --user restart card-device.service"

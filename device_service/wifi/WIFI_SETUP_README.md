# WiFi setup qua QR (AP mode) — TẤT CẢ trong folder `wifi/`

> **Đây là 1 cửa để debug toàn bộ luồng WiFi.** Mọi file liên quan việc Pi phát AP
> cho điện thoại quét QR cài WiFi đều nằm trong `device_service/wifi/`.
> Cập nhật: 2026-07-13 (gom folder + hardening).

---

## 0. Bản đồ file trong folder này

| File | Vai trò |
|---|---|
| **`wifi_ap.sh`** | Bật/tắt AP bằng nmcli + captive (nft DNAT :80, DNS hijack). `up`/`down`/`status`. |
| **`wifi_portal.py`** | Captive portal Flask (cổng 80). HTML nhúng sẵn trong file (không CDN). Điện thoại chọn WiFi + nhập pass. |
| **`wifi_watchdog.py`** | Tự bật AP khi mất WiFi nhà > GRACE; tự hạ AP sau `CARD_AP_TIMEOUT`. |
| **`wifi_ap_bench.sh`** | Script đo thời gian AP lên (benchmark, chạy tại máy). |
| **`card-wifi-portal.service`** | systemd **system** unit cho portal (mẫu — bản thật ở `/etc/systemd/system/`). |
| **`card-wifi-watchdog.service`** | systemd **system** unit cho watchdog (mẫu). |
| **`wifi_qr_print.png`** | Ảnh QR để in dán lên máy (nối AP). |
| **`WIFI_SETUP_README.md`** | File này. |

**Đường dẫn thật khi chạy:** `/home/bbsw/workspace/detectionfinalpi/device_service/wifi/`
Service chạy bằng venv: `/home/bbsw/workspace/detectionfinalpi/.venv/bin/python`.

> ⚠️ `wifi_portal.py` và `wifi_watchdog.py` `from settings import settings` — file
> nằm trong `wifi/` nên mỗi file có **sys.path shim** ở đầu (thêm `device_service/`
> vào path) để import settings được. WorkingDirectory của service vẫn là `device_service/`.

---

## 1. Luồng hoạt động

1. Máy bật → NetworkManager thử nối WiFi nhà đã lưu.
2. Không có WiFi nhà > **40s** (`CARD_WIFI_GRACE`) → `wifi_watchdog` bật AP.
3. **SSID = `CMD X BBSW`**, pass = **`cardfeeder`** (ghim 1 nguồn, xem §4).
4. Điện thoại nối AP → iOS/Android **tự bật captive portal** (mọi URL http → 302 về
   `http://10.42.0.1`). Hoặc quét QR / mở `http://10.42.0.1`.
5. Trang liệt kê WiFi → chọn mạng + nhập mật khẩu → Kết nối.
6. Pi hạ AP, nối WiFi nhà. Watchdog thấy có mạng → im (không bật AP nữa).

---

## 2. Cách chạy / cài (tại máy)

```bash
cd /home/bbsw/workspace/detectionfinalpi/device_service/wifi
chmod +x wifi_ap.sh

# cài 2 system service (cần sudo: cổng 80 + nmcli)
sudo cp card-wifi-portal.service   /etc/systemd/system/
sudo cp card-wifi-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now card-wifi-portal.service card-wifi-watchdog.service
```

## 3. Test an toàn tại máy

```bash
cd /home/bbsw/workspace/detectionfinalpi/device_service/wifi
sudo bash wifi_ap.sh up      # -> SSID "CMD X BBSW", http://10.42.0.1  (NGẮT WiFi/SSH!)
sudo bash wifi_ap.sh status  # xem AP active chưa
sudo bash wifi_ap.sh down    # hạ AP, NM nối lại WiFi nhà
```

> ⚠️ Pi chỉ 1 card WiFi → **bật AP là mất SSH**. Chỉ `up` khi ngồi tại máy.
> Portal/watchdog test không đụng mạng có thể chạy từ xa (chỉ HTTP local).

---

## 4. Hardening đã áp dụng (2026-07-13) — đọc trước khi sửa

1. **AP lên chắc chắn** (`wifi_ap.sh up`): retry+verify 3 lần, MỖI lần xác minh AP
   thực sự ACTIVE; chỉ bật captive/DNS-hijack SAU khi verified; fail thì DỌN SẠCH
   nft+DNS rồi thoát 1 (không để luật chặn treo trên wlan0).
2. **Nhiều điện thoại cùng lúc — "ai gửi pass trước thắng"**: portal có lock; điện
   thoại đầu tiên POST `/connect` thắng (nhận `connect_id`), số còn lại nhận `busy`
   và chỉ *chờ* (không hiện "thành công" giả). `/status` trả `id` để mỗi máy chỉ
   nhận kết quả của phiên mình.
3. **SSID hiển thị = SSID thật**: portal đọc SSID từ `nmcli con show CardFeederAP`
   (không đoán tên) → luôn khớp cái AP đang phát.
4. **Captive nổ ngay mọi OS**: route cho iOS/macOS (`hotspot-detect.html`), Android
   (`generate_204`, `gen_204`), Windows (`ncsi.txt`, `connecttest.txt`), Firefox,
   Kindle, Xiaomi… tất cả 302 → `http://10.42.0.1`. Cổng 80 ghim (`CARD_PORTAL_PORT=80`).
5. **Mật khẩu AP ghim `cardfeeder`** trong `wifi_ap.sh` (không cho env đổi lệch QR).
   Đổi pass phải sửa CẢ QR (web/index.html) LẪN `wifi_ap.sh`.
6. **`check_every=2s`** (watchdog) thay vì 0.2s — bớt tải CPU.

## 5. Cấu hình quan trọng

- `CARD_AP_TIMEOUT` (watchdog.service, hiện **600**): AP tự hạ sau 600s nếu không ai
  cấu hình — an toàn khi test từ xa. **Sản xuất thực thụ**: đổi `0` để AP giữ mãi.
- `CARD_WIFI_GRACE=40`: mất WiFi nhà bao lâu thì bật AP.
- Cổng portal **80** (ghim). QR = `http://10.42.0.1` phải khớp gateway AP (`ipv4.method shared`).

## 6. Gỡ

```bash
sudo systemctl disable --now card-wifi-portal.service card-wifi-watchdog.service
sudo rm /etc/systemd/system/card-wifi-portal.service /etc/systemd/system/card-wifi-watchdog.service
sudo systemctl daemon-reload
sudo bash /home/bbsw/workspace/detectionfinalpi/device_service/wifi/wifi_ap.sh down
```

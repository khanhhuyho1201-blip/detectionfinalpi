# Cài WiFi cho máy qua QR (AP mode) — Hướng dẫn

Các file (đặt trong `~/workspace/device_service/`):
- `wifi_ap.sh` — bật/tắt WiFi AP bằng nmcli.
- `wifi_portal.py` — trang web cho điện thoại người mua chọn WiFi (cổng 80).
- `wifi_watchdog.py` — tự bật AP khi mất WiFi nhà; tự hạ AP sau timeout (an toàn).
- `card-wifi-portal.service`, `card-wifi-watchdog.service` — systemd **system** unit.

> ⚠️ CHƯA KÍCH HOẠT TỪ XA. Bật AP sẽ ngắt WiFi/SSH của Pi (Pi chỉ 1 card WiFi).
> Chỉ chạy các bước dưới khi bạn **ngồi tại máy** (hoặc đã cắm dây mạng eth0).

## Luồng hoạt động (sau khi cài)
1. Máy bật, NetworkManager thử nối WiFi nhà đã lưu.
2. Nếu sau ~40s vẫn không có WiFi nhà → `wifi_watchdog` bật AP **CardFeeder-XXXX**
   (XXXX = 4 ký tự cuối device_id), mật khẩu mặc định `cardfeeder`.
3. Người mua nối điện thoại vào AP đó, **quét QR** (hoặc mở `http://10.42.0.1`).
4. Trang hiện danh sách WiFi → chọn mạng nhà + nhập mật khẩu → bấm Kết nối.
5. Pi hạ AP, nối WiFi nhà. Watchdog thấy đã có mạng → giữ nguyên (không bật AP nữa).
   Máy dùng credential nạp sẵn để nói chuyện server ngay.

## Cài đặt (chạy tại máy)
```bash
cd ~/workspace/device_service
chmod +x wifi_ap.sh

# cài 2 system service (cần sudo vì cổng 80 + nmcli)
sudo cp card-wifi-portal.service   /etc/systemd/system/
sudo cp card-wifi-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now card-wifi-portal.service
sudo systemctl enable --now card-wifi-watchdog.service
```

## Test an toàn tại máy
```bash
# 1) Bật AP thủ công để xem trang portal
sudo bash wifi_ap.sh up           # -> SSID CardFeeder-XXXX, http://10.42.0.1
#    (điện thoại nối AP, quét QR / mở http://10.42.0.1, thử chọn WiFi)

# 2) Hạ AP, nối lại WiFi nhà
sudo bash wifi_ap.sh down

# 3) Hoặc để watchdog tự lo: tắt WiFi nhà, đợi ~40s -> AP tự bật.
#    Nếu không cấu hình, sau 180s (CARD_AP_TIMEOUT) AP tự hạ -> Pi nối lại WiFi cũ.
```

## Ghi chú cấu hình
- `CARD_AP_TIMEOUT=180` (trong watchdog.service): AP tự hạ sau 180s nếu không ai
  cấu hình — để không mất Pi khi test. **Sản xuất thực thụ**: đổi thành `0` để AP
  giữ mãi tới khi người mua cấu hình xong.
- `CARD_AP_PASS` (env, mặc định `cardfeeder`): mật khẩu vào AP. In kèm QR nếu muốn.
- `CARD_WIFI_PORTAL_URL` ở server (mặc định `http://10.42.0.1`) phải khớp gateway AP.
- Cổng 80: nếu vướng dịch vụ khác, đổi `CARD_PORTAL_PORT` (nhưng QR là `http://10.42.0.1`
  → nên giữ 80).

## Gỡ
```bash
sudo systemctl disable --now card-wifi-portal.service card-wifi-watchdog.service
sudo rm /etc/systemd/system/card-wifi-portal.service /etc/systemd/system/card-wifi-watchdog.service
sudo systemctl daemon-reload
sudo bash wifi_ap.sh down
```

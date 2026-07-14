# Kiosk launcher + autostart — cài cho máy mới

Các file trong folder này là **bản gốc đã chép từ máy đang chạy** (`bbswcmd`, X11).
Cài lên Pi mới bằng đúng các lệnh sau (chạy dưới user thường, KHÔNG sudo cho phần user):

```bash
cd screen_setup/kiosk

# 1) Launcher THẬT (X11): Flask :8800 + Chromium kiosk, tự dò cổng Arduino
mkdir -p ~/.local/bin
cp card-feeder-launch.sh ~/.local/bin/
chmod +x ~/.local/bin/card-feeder-launch.sh

# 2) Autostart: mở app khi boot + ẩn con trỏ chuột (touch thuần)
mkdir -p ~/.config/autostart
cp cardfeeder.desktop  ~/.config/autostart/
cp unclutter.desktop   ~/.config/autostart/

# 3) (tuỳ chọn) unit systemd --user — nếu muốn chạy bằng systemctl thay autostart
mkdir -p ~/.config/systemd/user
cp app-cardfeeder@autostart.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now app-cardfeeder@autostart.service

# 4) reboot -> app tự lên toàn màn hình
```

## Ghi chú
- **`card-feeder-launch.sh`** là launcher đang dùng. Nó `cd` vào
  `<repo>/device_service` chạy `server.py` bằng `<repo>/.venv/bin/python`, mở Chromium
  `--kiosk --ozone-platform=x11`. Sửa biến `REPO=` trong file nếu repo đặt chỗ khác.
- Cần màn hình + cảm ứng đã setup trước (xem `../SETUP_GUIDE.md` mục 1–2).
- Xem app từ máy khác: `http://<IP-Pi>:8800/` (đặt `CARD_WEB_HOST=0.0.0.0`).
- **`kiosk-wayland.sh`, `open_kiosk.sh`, `stop_kiosk.sh`**: model cũ (Wayland/labwc +
  `card-device.service`), KHÔNG dùng trên máy X11 hiện tại — giữ để tham khảo.
- **`99-calibration.conf`**: chỉ áp cho driver cảm ứng KERNEL. Máy này dùng driver
  userspace (`ads7846-touch`) nên file này bị bỏ qua — hiệu chỉnh bằng `sudo fix-touch`.

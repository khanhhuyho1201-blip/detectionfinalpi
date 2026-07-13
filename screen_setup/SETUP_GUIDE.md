# Screen Setup — Waveshare 3.5" RPi LCD (C) trên Raspberry Pi 5

Toàn bộ cấu hình màn hình + cảm ứng + kiosk cho Card Feeder.
Folder này là kết quả của quá trình chẩn đoán/fix đầy đủ trên máy `bbswcmd`.

## Vấn đề phần cứng của màn hình này

- Màn dùng chip cảm ứng **XPT2046** (tương thích ADS7846) qua SPI0 CS1.
- **Mạch PENIRQ (chân ngắt cảm ứng) hỏng** → driver kernel chuẩn (chờ ngắt)
  không bao giờ nhận chạm. Kênh đo tọa độ X/Y vẫn tốt.
- → Giải pháp: **driver userspace polling** (`ads7846_touch.py`) đọc chip trực
  tiếp, phát sự kiện touchscreen chuẩn qua uinput.

## Cài nhanh cho máy MỚI (1 lệnh)

```bash
sudo bash install-ads7846-touch.sh   # cài driver + service + ẩn con trỏ + fix-touch
sudo reboot                          # kích hoạt ẩn con trỏ
sudo fix-touch                       # hiệu chỉnh 9 điểm cho tấm màn của máy đó
```

## Các cấu hình hệ thống đã áp dụng (làm tay nếu không dùng installer)

### 1. /boot/firmware/config.txt — hiển thị
```
dtparam=spi=on
dtoverlay=waveshare35c,speed=20000000,fps=20
```
- **20MHz** vì màn nối bằng dây jumper dài 30cm — 115MHz mặc định gây sọc
  trắng/tím/vàng (hỏng tín hiệu). Dây ngắn/cắm trực tiếp có thể nâng 32-64MHz.

### 2. Cảm ứng — service `ads7846-touch`
- File: `ads7846_touch.py` + `prepare-spidev.sh` + `run-touch.sh` + `touch.env`
- Cài tại `/opt/ads7846-touch/`, systemd unit `ads7846-touch.service`
- **Điểm chết người từng gặp**: thiết bị uinput khai báo thêm `BTN_LEFT` làm udev
  phân loại nhầm MOUSE → X nuốt hết sự kiện bấm (chạm di được mà không click).
  Driver hiện tại chỉ dùng `BTN_TOUCH` + `INPUT_PROP_DIRECT` → touchscreen chuẩn.
- Hiệu chỉnh: `sudo fix-touch` (9 dấu +, ấn CHẮC ~1.5s/dấu; tự fit affine/
  bilinear/biquadratic, chọn model tốt nhất theo kiểm định chéo).

### 3. Ẩn con trỏ chuột (kiosk cảm ứng thuần)
- `/etc/X11/xinit/xserverrc`: `exec /usr/bin/X -nolisten tcp -nocursor "$@"`
- Tắt unclutter package cũ: `/etc/default/unclutter` → `START_UNCLUTTER="false"`
  (bản `-idle 1` làm con trỏ HIỆN khi chạm).

### 4. Diệt popup desktop đè lên kiosk
- `~/.config/lxsession/rpd-x/autostart` (override — KHÔNG chạy lxpanel/pcmanfm/
  xscreensaver, chống tắt màn):
```
@xset s off
@xset -dpms
@xset s noblank
```
- `~/.config/autostart/*.desktop` với `Hidden=true` cho: pprompt, squeekboard,
  lxpolkit, polkit-mate-authentication-agent-1, pwrkey.

### 5. WiFi setup flow (QR + captive portal)
- 2 service PHẢI cài (repo có sẵn file nhưng chú ý **đường dẫn đúng**):
  - `card-wifi-portal.service` → chạy `wifi_portal.py` bằng **venv** (cần flask),
    cổng 80, root.
  - `card-wifi-watchdog.service` → `wifi_watchdog.py`, `CARD_AP_TIMEOUT=600`.
- Phụ thuộc: `dnsmasq-base`, `nftables` (captive redirect).
- Luồng: kiosk Setup → `wifi_ap.sh up` (AP `CMD X BBSW`/`cardfeeder`, 10.42.0.1)
  → điện thoại quét QR → captive portal tự bật → chọn mạng + mật khẩu → xong.
- Nhiều điện thoại cùng vào được; **ai bấm Connect trước thắng** (máy sau nhận
  "Another phone is configuring").

### 6. App tự mở khi boot
- `~/.config/autostart/cardfeeder.desktop` → `card-feeder-launch.sh`
  (Flask :8800 + Chromium kiosk).
- Mở từ máy khác: `http://<IP-của-Pi>:8800/` (cần `CARD_WEB_HOST=0.0.0.0`
  trong `/home/bbsw/workspace/.env`).

## File trong folder này

| File | Vai trò |
|---|---|
| `install-ads7846-touch.sh` | **Installer 1 file** — cài toàn bộ cho máy mới |
| `ads7846_touch.py` | Driver cảm ứng polling (SPI→uinput) |
| `prepare-spidev.sh` | Tách chip khỏi driver kernel, gắn spidev |
| `run-touch.sh` | Wrapper khởi động driver |
| `touch.env` | Cấu hình hướng + hiệu chỉnh (TS_AFFINE/TS_POLY...) |
| `ads7846-touch.service` | systemd unit |
| `recalibrate.sh` | Lệnh `fix-touch` (hiệu chỉnh tự phục vụ) |
| `affine_calib.py` | GUI hiệu chỉnh 9 điểm + chế độ kiểm tra |
| `fit_models.py` | Fit affine/bilinear/biquadratic, chọn tốt nhất |
| `drawpad.py`, `touchtest_gui.py` | Công cụ test hướng/độ chính xác |
| `README.md` | Chi tiết driver cảm ứng |

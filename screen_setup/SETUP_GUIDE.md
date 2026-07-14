# Screen Setup — Waveshare 3.5" RPi LCD (C) trên Raspberry Pi 5

Toàn bộ cấu hình màn hình + cảm ứng + kiosk cho Card Feeder.
Folder này là kết quả của quá trình chẩn đoán/fix đầy đủ trên máy `bbswcmd`.
**Đây là 1 folder DUY NHẤT để setup phần màn hình/cảm ứng cho mọi máy sau.**

## Cấu trúc folder (gom 2026-07-14)

```
screen_setup/
├─ SETUP_GUIDE.md                 ← FILE NÀY (thứ tự setup máy mới)
├─ display/
│   └─ config.txt.waveshare35c    ← các dòng cần thêm vào /boot/firmware/config.txt
├─ (driver cảm ứng — ở gốc folder)
│   install-ads7846-touch.sh  ads7846_touch.py  prepare-spidev.sh  run-touch.sh
│   touch.env  ads7846-touch.service  recalibrate.sh  affine_calib.py
│   fit_models.py  drawpad.py  touchtest_gui.py  README.md
└─ kiosk/                         ← launcher + autostart + cursor + calib (bản LIVE đã chép)
    card-feeder-launch.sh         ← LAUNCHER THẬT (X11) — cài vào ~/.local/bin/
    cardfeeder.desktop            ← autostart mở app khi boot -> ~/.config/autostart/
    unclutter.desktop             ← ẩn con trỏ (autostart) -> ~/.config/autostart/
    app-cardfeeder@autostart.service  ← unit systemd --user (tham khảo)
    99-calibration.conf           ← X11 calib (chỉ hợp driver kernel; userspace bỏ qua)
    kiosk-wayland.sh              ← launcher Wayland/labwc CŨ (KHÔNG dùng trên máy X11)
    open_kiosk.sh / stop_kiosk.sh ← helper mở/dừng kiosk (model card-device.service cũ)
```

## Thứ tự setup 1 máy MỚI (tóm tắt)

1. **Màn hình**: chép `display/config.txt.waveshare35c` vào `/boot/firmware/config.txt` → reboot.
2. **Cảm ứng**: `sudo bash install-ads7846-touch.sh` → reboot → `sudo fix-touch` (hiệu chỉnh 9 điểm).
3. **Kiosk app**: chép `kiosk/card-feeder-launch.sh` → `~/.local/bin/` (chmod +x);
   `kiosk/cardfeeder.desktop` + `kiosk/unclutter.desktop` → `~/.config/autostart/`.
4. Reboot → app tự mở toàn màn hình, cảm ứng + ẩn con trỏ hoạt động.

*(Cuộn cảm ứng "kéo-để-cuộn" và ẩn con trỏ ở tầng trang nằm trong app UI
`device_service/web/index.html`, KHÔNG phải ở folder này — đi theo repo sẵn.)*

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
- **TẤT CẢ file WiFi ở `device_service/wifi/`** (gom 1 chỗ 2026-07 — xem
  `device_service/wifi/WIFI_SETUP_README.md` để debug). Service trỏ
  `.../device_service/wifi/wifi_portal.py` và `.../wifi/wifi_watchdog.py`.
- 2 service PHẢI cài (repo có sẵn file nhưng chú ý **đường dẫn đúng**):
  - `card-wifi-portal.service` → chạy `wifi_portal.py` bằng **venv** (cần flask),
    cổng 80, root.
  - `card-wifi-watchdog.service` → `wifi_watchdog.py`, `CARD_AP_TIMEOUT=600`.
- Phụ thuộc: `dnsmasq-base`, `nftables` (captive redirect).
- Luồng: kiosk Setup → `wifi_ap.sh up` (AP `CMD X BBSW`/`cardfeeder`, 10.42.0.1)
  → điện thoại quét QR → captive portal tự bật → chọn mạng + mật khẩu → xong.
- Nhiều điện thoại cùng vào được; **ai bấm Connect trước thắng** (máy sau nhận
  "Another phone is configuring").

### 6. App tự mở khi boot  (bản gốc các file: `kiosk/`)
- Chép `kiosk/card-feeder-launch.sh` → `~/.local/bin/` (`chmod +x`). Đây là launcher
  THẬT trên máy X11: Flask :8800 + Chromium `--kiosk --ozone-platform=x11`, tự dò
  cổng serial Arduino (`ttyUSB0`>`ttyACM0`>`sim`), relaunch nếu server chết.
- Chép `kiosk/cardfeeder.desktop` → `~/.config/autostart/` (autostart mở app khi boot).
- Chép `kiosk/unclutter.desktop` → `~/.config/autostart/` (ẩn con trỏ chuột).
- Mở từ máy khác: `http://<IP-của-Pi>:8800/` (cần `CARD_WEB_HOST=0.0.0.0`
  trong `/home/bbsw/workspace/.env`).
- Wayland/labwc cũ: `kiosk/kiosk-wayland.sh` (không dùng trên máy X11 này — giữ tham khảo).

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
| `display/config.txt.waveshare35c` | Dòng cần thêm vào `/boot/firmware/config.txt` |
| `kiosk/card-feeder-launch.sh` | **Launcher THẬT** (X11) → `~/.local/bin/` |
| `kiosk/cardfeeder.desktop` | Autostart app → `~/.config/autostart/` |
| `kiosk/unclutter.desktop` | Autostart ẩn con trỏ → `~/.config/autostart/` |
| `kiosk/kiosk-wayland.sh` | Launcher Wayland/labwc cũ (tham khảo) |
| `kiosk/open_kiosk.sh`, `kiosk/stop_kiosk.sh` | Helper mở/dừng kiosk (model cũ) |
| `kiosk/99-calibration.conf` | X11 calib (driver kernel; userspace bỏ qua) |

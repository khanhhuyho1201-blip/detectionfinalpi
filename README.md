# CMD Card Feeder — Raspberry Pi 5 (workspace)

Toàn bộ phần mềm chạy TRÊN MÁY (Raspberry Pi 5) của máy phát/đếm bài:
- Điều khiển motor + đếm lá qua **Arduino Uno** (firmware C++).
- App web kiosk (Flask + Chromium toàn màn hình) trên màn cảm ứng.
- Quay video mẻ chạy → upload lên server để YOLO nhận diện lá.

> Máy khác chỉ cần **clone/pull repo này → tạo venv → cài requirements → nạp
> firmware → bật service**. Xem mục "Cài từ đầu".

---

## Yêu cầu môi trường (đã kiểm chứng trên máy đang chạy)

| Thành phần | Version | Ghi chú |
|---|---|---|
| OS | **Debian 13 (trixie)** — Raspberry Pi OS, aarch64 | Pi 5 |
| Python | **3.13.5** | tạo venv bằng `python3 -m venv` |
| Thư viện Python | **28 gói** trong `requirements.txt` gốc | Flask 3.1.3, pyserial 3.5, pillow 12.3.0, requests 2.34.2, qrcode 8.2, python-escpos 3.1… (pin version chính xác) |
| arduino-cli | **1.5.1** + core `arduino:avr` 1.8.8 | chỉ cần khi NẠP firmware — xem `button_start_stop/arduino/README.md` §6 |
| System (apt) | `ffmpeg`, `v4l-utils`, `chromium`, `wlr-randr` | cho quay video + kiosk; cài bằng `apt` |

**CHỈ CÓ 1 FILE `requirements.txt` DUY NHẤT** = ở thư mục gốc `~/workspace/requirements.txt`.
Các file `requirements.txt` trong `button_start_stop/` và `device_service/` chỉ là
**con trỏ** ghi "dùng file cha" — ĐỪNG cài từ chúng.

Gói apt cho hệ thống (nếu Pi mới):
```bash
sudo apt update
sudo apt install -y ffmpeg v4l-utils chromium wlr-randr python3-venv
```

---

## Bản đồ thư mục

| Thư mục / file | Vai trò | README chi tiết |
|---|---|---|
| `device_service/` | **App chính**: Flask server (`server.py`), điều khiển (`controller.py`), serial (`serial_link.py`), parse (`parser.py`), camera, upload, in QR | `device_service/WIFI_SETUP_README.md`, `FE_STATUS_SPEC.md`, `BA_error_catalog.md` |
| `button_start_stop/` | Bản trước của app + **firmware Arduino** trong `arduino/Test.ino` | `button_start_stop/README.md`, **`arduino/README.md`** (nạp firmware + tốc độ), `PROTOCOL.md`, `ARDUINO_CHANGES.md` |
| `code/` | Script phụ (detect camera…) | — |
| `weight/` | Model YOLO / trọng số dùng cục bộ | — |
| `card_device/` | File runtime của thiết bị (credentials, speed_model.json, printer.json…) — **KHÔNG commit dữ liệu nhạy cảm** | — |
| `deploy/` | File systemd unit `card-device.service` để cài service kiosk | — |
| `requirements.txt` | Thư viện Python — **sinh từ `.venv/bin/pip freeze`** (28 gói, gồm Flask, pyserial, pillow, requests, qrcode, python-escpos) | — |
| **`ARCHITECTURE.md`** | **Thành phần quan trọng trong code + tác động khi sửa** (đọc trước khi sửa) | — |
| `AUDIT_REPORT.md` | Báo cáo rà soát code | — |

> ⚠️ **Trước khi sửa code:** đọc **`ARCHITECTURE.md`** — nó chỉ rõ file nào là bộ
> não, điểm nào nhạy cảm (đếm-lá-theo-encoder, tầng sàn tốc, EEPROM qua flash,
> import lazy…), và "muốn đổi X thì sửa ở đâu".

**KHÔNG commit** (đã để trong `.gitignore`): `.venv/`, `venv`, `*.log`,
`__pycache__/`, `.pytest_cache/`, `*.bak.*`, `backup_before_refactor/`,
`card_device/credentials.json`.

---

## Cài từ đầu trên một Pi mới

```bash
cd ~/workspace                     # thư mục repo sau khi pull

# 1) Môi trường Python
python3 -m venv .venv
ln -sf .venv venv                  # kiosk.sh trỏ tới ./venv/bin/python
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt   # ← FILE DUY NHẤT, đừng dùng file con
./.venv/bin/python -c "import flask, serial, requests, PIL, qrcode; print('OK, đủ thư viện')"

# 2) Nạp firmware Arduino  →  xem button_start_stop/arduino/README.md
#    (cài arduino-cli nếu Pi mới; stage /tmp/Test; stop service; upload; F0; restart)

# 3) Cài + bật service kiosk (systemd USER — KHÔNG dùng sudo)
#    unit đi kèm repo ở deploy/card-device.service (giả định repo ở ~/workspace,
#    user tên bbsw, uid 1000 — sửa lại các đường dẫn/uid trong unit nếu khác)
mkdir -p ~/.config/systemd/user
cp deploy/card-device.service ~/.config/systemd/user/card-device.service
systemctl --user daemon-reload
systemctl --user enable --now card-device.service
```

> Nếu username/đường dẫn khác `bbsw`/`~/workspace`, sửa `WorkingDirectory`,
> `ExecStart`, và `XDG_RUNTIME_DIR=/run/user/<uid>` trong file unit trước khi copy.

---

## Chạy & vận hành

**Service kiosk** (server Flask + Chromium toàn màn hình, tự relaunch nếu crash):
```bash
systemctl --user status  card-device.service
systemctl --user restart card-device.service     # KHÔNG restart giữa lúc đang chạy 1 mẻ
systemctl --user stop    card-device.service      # cần khi nạp firmware (nhả cổng serial)
```
- Server chạy `device_service/server.py` (WorkingDirectory), lắng ở cổng `8800`.
- Chromium mở `http://127.0.0.1:8800/` ở chế độ `--kiosk`.
- Launcher: `device_service/kiosk.sh` (ép mode màn 720x480, dùng `./venv/bin/python`).

**Test app không cần phần cứng** (simulator):
```bash
cd device_service && BSS_SERIAL_PORT=sim python3 server.py
# hoặc bản button_start_stop: cd button_start_stop && ./run.sh sim
```

---

## Firmware Arduino (motor + đếm lá)

**Đọc `button_start_stop/arduino/README.md`** — quy trình nạp đầy đủ + giải thích
tham số tốc độ (nhiều tầng sàn governor). Tóm tắt:

- Board: **Arduino Uno**, `/dev/ttyACM0`, 115200 baud, FQBN `arduino:avr:uno`.
- Nạp: stage sang `/tmp/Test/Test.ino` → compile → **stop service** → upload →
  **`F0` reset FF/EEPROM khi đổi tốc** → restart service.
- Tốc độ: hạ ĐỒNG BỘ `STEADY_SPEED` + `CAD_SPD_LO` + `CAD_LO_LIGHT` (sàn) +
  `CAD_HI_HEAVY`/`CAD_SPD_HI` (trần) + `SPEED_MIN`. Chỉ hạ `STEADY_SPEED` thì
  governor vẫn kéo lên sàn → "vẫn nhanh".

---

## Giao thức Pi ⇄ Arduino

Xem `button_start_stop/PROTOCOL.md`. Gọn: Pi gửi `B1`/`B0`/`N<n>`/`S`; Arduino
báo `ST st=RUN n=<đếm> tot=<target> err=<NONE|CLUMP|STALL|LIMIT> spd=<pwm>`.

---

## Debug

- Log serial LIVE: `tail -f /tmp/serial_live.log` (dòng `[STAT]`: CARD/SEN/ENC/meas/PWM).
- `ENC=` đứng cục + `meas=0` dù motor quay → **dây encoder D2/D3 lỏng**, không phải lỗi code.
- Lỗi service: `journalctl --user -u card-device.service -n 50`.

---

## Xử lý sự cố "thiếu thư viện / thiếu thành phần"

1. **`ModuleNotFoundError`** (vd `No module named 'serial'`) → chưa cài đúng venv.
   Kiểm tra đang chạy đúng python của venv:
   ```bash
   which python           # phải là .../workspace/.venv/bin/python
   ./.venv/bin/pip install -r requirements.txt   # cài LẠI từ file gốc
   ```
2. **Cài đúng file requirements nào?** → **CHỈ** `~/workspace/requirements.txt`.
   File trong `button_start_stop/` và `device_service/` là con trỏ, không cài.
3. **Kiểm tra nhanh đủ thư viện chưa:**
   ```bash
   ./.venv/bin/python -c "import flask, serial, requests, PIL, qrcode; print('OK')"
   ```
4. **`arduino-cli: command not found`** khi nạp firmware → chưa cài toolchain.
   Xem `button_start_stop/arduino/README.md` §6 (cài arduino-cli + core AVR).
5. **Service không lên / màn đen** → thiếu gói apt (`chromium`, `wlr-randr`, `ffmpeg`).
   Cài: `sudo apt install -y ffmpeg v4l-utils chromium wlr-randr`.
6. **Cổng serial busy khi nạp** → `systemctl --user stop card-device.service` trước.

> Nếu venv thật đổi (thêm/bớt gói), **cập nhật lại requirements gốc**:
> `./.venv/bin/pip freeze > requirements.txt` rồi commit — để file luôn khớp.

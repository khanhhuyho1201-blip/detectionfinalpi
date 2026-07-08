# README — Firmware Motor máy phát bài (Arduino Uno) trên Pi

> Ghi chú vận hành cho phần điều khiển motor. Đọc file này để **nạp firmware đúng
> quy trình** và **hiểu các tham số tốc độ**. Cập nhật lần cuối: 2026-07-08.

---

## 0. TL;DR — nạp firmware đầy đủ trong 1 lần

```bash
export PATH=$HOME/bin:$PATH
cd ~/workspace/button_start_stop/arduino

# 1) (khuyến nghị) backup trước khi sửa
cp Test.ino Test.ino.bak.$(date +%s)

# 2) sửa Test.ino nếu cần (xem mục THAM SỐ TỐC bên dưới)

# 3) stage sang thư mục có TÊN TRÙNG file rồi compile
mkdir -p /tmp/Test && cp Test.ino /tmp/Test/Test.ino
arduino-cli compile --fqbn arduino:avr:uno /tmp/Test

# 4) DỪNG service để nhả cổng /dev/ttyACM0 (BẮT BUỘC)
systemctl --user stop card-device.service
sleep 2
fuser /dev/ttyACM0 || echo PORT_FREE      # phải thấy PORT_FREE

# 5) nạp
arduino-cli upload -p /dev/ttyACM0 --fqbn arduino:avr:uno /tmp/Test

# 6) (khi đổi TỐC hoặc trần/sàn) reset đường cong FF đã học trong EEPROM
python3 - <<'PY'
import serial, time
s = serial.Serial("/dev/ttyACM0", 115200, timeout=1)
time.sleep(2.8); s.reset_input_buffer()
s.write(b"F0\n"); time.sleep(1.0)
print(s.read(4000).decode(errors="replace"))
s.close()
PY

# 7) khởi động lại service
systemctl --user restart card-device.service
sleep 5
systemctl --user is-active card-device.service          # phải "active"
grep -E "SMART CARD FEEDER|SPEED  :" /tmp/serial_live.log | tail -2   # xác nhận tốc mới
```

---

## 1. Bản đồ file & vị trí

| Thứ | Đường dẫn |
|---|---|
| **Firmware nguồn** (đang dùng) | `~/workspace/button_start_stop/arduino/Test.ino` |
| Backup v6.9 (STEADY=450) | `~/workspace/button_start_stop/arduino/Test_v6.9_backup_20260630.ino` |
| Các backup tự tạo | `~/workspace/button_start_stop/arduino/Test.ino.bak.*` |
| **Device service (Python)** | `~/workspace/device_service/` (server.py, controller.py, serial_link.py, parser.py…) |
| Service unit | `card-device.service` (user systemd) — chạy `kiosk.sh` → `server.py` + Chromium kiosk |
| Log serial LIVE | `/tmp/serial_live.log` ← đọc file này để debug |
| Sketch staged để compile | `/tmp/Test/Test.ino` |
| **arduino-cli** | `~/bin/arduino-cli` (v1.5.1) — KHÔNG có sẵn khi cài mới, xem mục 6 |

**Phần cứng:** Arduino **Uno** trên `/dev/ttyACM0`, baud 115200.
FQBN = `arduino:avr:uno`. Chân: SENSOR=D4, ENC_A=D2, ENC_B=D3, DIR=D8.

---

## 2. QUY TẮC VÀNG khi nạp

1. **PHẢI dừng `card-device.service` trước khi nạp** — service giữ cổng
   `/dev/ttyACM0`. Không dừng → upload lỗi "port busy".
2. **arduino-cli cần tên sketch TRÙNG tên thư mục.** File tên `Test.ino` phải
   nằm trong thư mục tên `Test/`. Vì thư mục gốc tên `arduino/` nên luôn
   **stage sang `/tmp/Test/Test.ino`** rồi compile/upload thư mục đó.
3. **Nạp firmware KHÔNG xóa EEPROM.** Đường cong feed-forward (FF) + startSpeed
   đã học vẫn còn sau khi flash. Khi **đổi tốc độ/trần/sàn**, phải gửi lệnh
   **`F0`** qua serial để reset FF, nếu không startSpeed cũ (tốc cao) sẽ ghi đè
   trần mới → chạy sai tốc.
4. **Restart service sau khi nạp** để Pi mở lại cổng và kết nối firmware.

---

## 3. THAM SỐ TỐC ĐỘ (rất quan trọng — đọc kỹ)

Tốc độ **không chỉ** nằm ở `STEADY_SPEED`. Có **nhiều tầng SÀN/TRẦN** trong
cadence governor. Nếu chỉ hạ `STEADY_SPEED` mà không hạ sàn `CAD_SPD_LO` thì
governor vẫn kéo tốc lên tới sàn → "hạ tốc mà vẫn nhanh".

**Muốn hạ tốc THẬT: hạ ĐỒNG BỘ các hằng số này (dòng trong Test.ino):**

| Hằng số | Dòng | Ý nghĩa | Giá trị hiện tại (2026-07-08) |
|---|---|---|---|
| `STEADY_SPEED` | 52 | Tốc danh định (nominal) | **143** |
| `CAD_SPD_LO` | 67 | **SÀN governor pha nặng/giữa** (thủ phạm "vẫn nhanh") | **143** |
| `CAD_LO_LIGHT` | 71 | Sàn pha nhẹ (cuối mẻ) | **143** |
| `CAD_HI_HEAVY` | 63 | Trần tốc pha nặng | **180** |
| `CAD_SPD_HI` | 68 | Mốc clamp startSpeed (khởi động) | **180** |
| `SPEED_MIN` | 38 | Sàn tốc tuyệt đối | **120** |

Quy tắc: **sàn (`CAD_SPD_LO`, `CAD_LO_LIGHT`) = tốc mong muốn**;
**trần (`CAD_HI_HEAVY`, `CAD_SPD_HI`) cao hơn sàn ~25-40** để governor còn biên
điều chỉnh; **`SPEED_MIN` thấp hơn sàn** để không chặn.

> Đơn vị: **c/s** (counts/giây của encoder), KHÔNG phải lá/giây.

### Chống STALL sai ("operation error" dù lá vẫn qua)
| Hằng số | Dòng | Ý nghĩa | Hiện tại |
|---|---|---|---|
| `JAM_RECOVER_MS` | 152 | Lá che sensor > ms này → coi là kẹt, thử gỡ | **3000** |
| `JAM_RECOVER_MAX` | 153 | Số lần thử gỡ trước khi bỏ cuộc → STALL | 3 |

Tốc càng chậm → lá che sensor càng lâu. Nếu `JAM_RECOVER_MS` quá thấp so với
thời gian 1 lá đi qua, firmware **tưởng nhầm lá bình thường là kẹt** → STALL
sớm với `CARD=0`. Đã nâng 1500 → 3000 để hợp với tốc chậm.

---

## 4. LỊCH SỬ thay đổi đã áp dụng (2026-07-08)

1. **Cài arduino-cli + core AVR** lên Pi (trước đó KHÔNG có toolchain nào).
2. **Sửa lỗi "operation error" / STALL sớm:** `JAM_RECOVER_MS` 1500 → **3000**.
   Nguyên nhân: tốc chậm 320 → lá che sensor ~1.6s > ngưỡng jam 1.5s → tưởng kẹt.
3. **Giảm tốc theo yêu cầu, nhiều đợt:** 320 → 224 (−30%) → 179 (−20%) →
   **143** (−20%). Ở bước cuối phát hiện SÀN governor 300 chặn, nên hạ đồng bộ
   `CAD_SPD_LO/CAD_LO_LIGHT` 300→143, `CAD_HI_HEAVY/CAD_SPD_HI` 360/340→180,
   `SPEED_MIN` 150→120. Reset FF (`F0`) sau mỗi lần đổi tốc.
4. **Reset FF/EEPROM (`F0`)** để xóa đường cong học từ phần cứng/tốc cũ
   (sau khi thay thẻ SD, hoặc đổi tốc). Curve về default `120,116,…,85`.

> Ghi chú: **encoder** là nguồn để đếm lá (đo quãng đường lúc lá che sensor).
> Nếu count không tăng / `meas=0 c/s` dù motor quay / `ENC=` đứng cục → **nghi
> dây encoder (D2/D3) lỏng**, KHÔNG phải lỗi code. Kiểm tra phần cứng trước.

---

## 5. DEBUG nhanh qua log serial

```bash
tail -f /tmp/serial_live.log
```
Đọc dòng `[STAT]`:
- `CARD=n` : số lá đã đếm. Kẹt ở 0 = không đếm được.
- `SEN=LOW` : sensor thấy lá che. `SEN=HIGH` : trống.
- `ENC=` : giá trị encoder. Phải **tăng/giảm đều** khi motor quay. Đứng cục = encoder/dây hỏng.
- `meas=... c/s` : tốc đo được. =0 liên tục dù PWM cao = encoder không ra xung.
- `PWM=` : lực đẩy motor.

Sự kiện: `[JAMFIX]`=đang thử gỡ kẹt, `[STALL]`=dừng do kẹt/hết lá,
`[DONE]`=đủ target, `[REGRIP]`=lùi đề-ba bám lại lá.

Lệnh serial thủ công (khi service đã DỪNG): `B1`=chạy, `B0`=dừng, `S`=status,
`G`=diag, `N<n>`=đặt số lá, `F`=xem FF, `F0`=reset FF, `R`=test quay ngược.

---

## 6. Nếu Pi MỚI (chưa có arduino-cli)

```bash
mkdir -p ~/bin
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=~/bin sh
export PATH=$HOME/bin:$PATH
arduino-cli config init
arduino-cli core update-index
arduino-cli core install arduino:avr        # kéo gcc + avrdude (~1-2 phút)
```
Rồi làm theo mục 0.

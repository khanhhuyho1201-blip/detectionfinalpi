# README — Firmware Motor máy phát bài (Arduino Uno) trên Pi

> Ghi chú vận hành cho phần điều khiển motor. Đọc file này để **nạp firmware đúng
> quy trình** và **hiểu các tham số tốc độ**. Cập nhật lần cuối: 2026-07-08.

---

## 0. TL;DR — nạp firmware bằng 1 lệnh (KHUYẾN NGHỊ)

Đã có script tự động `flash.sh`: compile → dừng app (nhả cổng) → upload (tự dò
bootloader Nano mới/cũ) → reset FF (F0) → khởi động lại app. Chỉ cần:

```bash
cd ~/workspace/button_start_stop/arduino
./flash.sh                 # cổng mặc định /dev/ttyACM0
# ./flash.sh /dev/ttyACM1  # nếu Arduino ở cổng khác
```

- **Chưa cắm board** → script chỉ compile để kiểm tra rồi dừng, KHÔNG đụng app.
- **Cắm rồi** → nạp full. Nano đời mới/cũ đều tự xử (thử 115200, lỗi thì 57600).

### Nạp thủ công (nếu cần từng bước)

```bash
export PATH=$HOME/bin:$PATH
cd ~/workspace/button_start_stop/arduino

cp production.ino production.ino.bak.$(date +%s)      # backup (tùy chọn)
mkdir -p /tmp/production && cp production.ino /tmp/production/production.ino
arduino-cli compile --fqbn arduino:avr:nano /tmp/production

# DỪNG app để nhả cổng (server.py giữ /dev/ttyACM0):
pkill -f card-feeder-launch.sh; sleep 1; pkill -f "[s]erver.py"; rm -f /tmp/card-feeder.lock
sleep 2; fuser /dev/ttyACM0 || echo PORT_FREE          # phải thấy PORT_FREE

# NẠP — Nano mới (115200); nếu lỗi 'not in sync' đổi cpu=atmega328old (57600):
arduino-cli upload -p /dev/ttyACM0 --fqbn arduino:avr:nano:cpu=atmega328 /tmp/production
# arduino-cli upload -p /dev/ttyACM0 --fqbn arduino:avr:nano:cpu=atmega328old /tmp/production

# reset FF EEPROM khi đổi tốc/pin:
python3 -c 'import serial,time; s=serial.Serial("/dev/ttyACM0",115200,timeout=1); time.sleep(2.8); s.write(b"F0\n"); time.sleep(1); s.close()'

# khởi động lại app:
setsid ~/.local/bin/card-feeder-launch.sh >/dev/null 2>&1 &
```

---

## 1. Bản đồ file & vị trí

| Thứ | Đường dẫn |
|---|---|
| **Firmware nguồn** (production, board v2) | `~/workspace/button_start_stop/arduino/production.ino` |
| Bản OLD-board (pin cũ + A0, không endstop) | git history: `git log --follow -- button_start_stop/arduino/production.ino` → mở commit TRƯỚC khi chuyển v2 |
| Các backup tự tạo | `~/workspace/button_start_stop/arduino/production.ino.bak.*` (nếu có) |
| **Device service (Python)** | `~/workspace/device_service/` (server.py, controller.py, serial_link.py, parser.py…) |
| Service unit | `card-device.service` (user systemd) — chạy `kiosk.sh` → `server.py` + Chromium kiosk |
| Log serial LIVE | `/tmp/serial_live.log` ← đọc file này để debug |
| Sketch staged để compile | `/tmp/production/production.ino` |
| **arduino-cli** | `~/bin/arduino-cli` (v1.5.1) — KHÔNG có sẵn khi cài mới, xem mục 6 |

**Phần cứng:** Arduino **Nano** (ATmega328P, PCB v2) trên `/dev/ttyACM0`, baud 115200.
Giao tiếp Pi ↔ Arduino = **cáp USB** (Serial cứng D0/D1 = cổng USB của Nano).
Chân (PCB v2): SENSOR=D4, ENC_A=D2(C1), ENC_B=D3(C2), MOTOR IN1=D5/IN2=D6,
STEP=D9, DIR=D10, MS=A3/A2/A1, ENDSTOP=D7. (Công tắc A0 đã bỏ; J10/D11-D12 UART chưa dùng.)

**FQBN nạp:** Nano & Uno cùng chip ATmega328P nên **code y hệt, không cần sửa** —
chỉ khác bootloader:
- Uno / Nano đời mới (115200): `--fqbn arduino:avr:uno`  (mặc định các lệnh dưới)
- Nano đời CŨ (57600) — nếu nạp lỗi `not in sync`/timeout: đổi thành
  `--fqbn arduino:avr:nano:cpu=atmega328old`

---

## 2. QUY TẮC VÀNG khi nạp

1. **PHẢI dừng `card-device.service` trước khi nạp** — service giữ cổng
   `/dev/ttyACM0`. Không dừng → upload lỗi "port busy".
2. **arduino-cli cần tên sketch TRÙNG tên thư mục.** File `production.ino` phải
   nằm trong thư mục tên `production/`. Vì thư mục gốc tên `arduino/` nên luôn
   **stage sang `/tmp/production/production.ino`** rồi compile/upload thư mục đó.
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

**Muốn hạ tốc THẬT: hạ ĐỒNG BỘ các hằng số này (dòng trong production.ino):**

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

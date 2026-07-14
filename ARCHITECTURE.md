# Thành phần quan trọng & Tác động khi sửa

> Bản đồ "đụng vào đây thì ảnh hưởng gì". Đọc trước khi sửa để biết một thay đổi
> lan tới đâu. Đường dẫn tính từ `~/workspace/`.

---

## 1. Luồng tổng thể (một mẻ chạy)

```
Người bấm START (web/index.html)
  → server.py /api/... → controller.py
      → PRE-FLIGHT: server(api_client) → camera(camera.py) → motor(serial_link.py)
      → xin target từ server (start_run) ; bật camera + gửi "B1"/"N<target>" xuống Arduino
      → Arduino chạy motor, đếm lá bằng ENCODER, gửi "ST st=RUN n=.. tot=.." lên
      → serial_link.py đọc dòng → parser.py → MachineStatus → controller cập nhật count
      → Arduino gửi "[DONE]" hoặc "[STALL]" → controller auto-stop
      → camera dừng, video → api_client.py upload lên server → in QR (printer.py)
```

**Nguyên tắc:** `controller.py` là bộ điều phối trung tâm. Mọi module khác là
"tay chân" nó gọi. Sửa logic vận hành → sửa ở `controller.py`.

---

## 2. Các file cốt lõi — vai trò & TÁC ĐỘNG khi sửa

| File | Vai trò | Sửa vào đây ẢNH HƯỞNG gì |
|---|---|---|
| **`device_service/controller.py`** (1300 dòng) | **Bộ não** — pre-flight, đếm, auto-stop, upload, gate mẻ kế | Thay đổi hành vi vận hành, trình tự chạy, điều kiện dừng/lỗi. Rủi ro CAO — 1 lỗi làm hỏng cả luồng chạy. |
| **`device_service/serial_link.py`** | Cầu nối serial ⇄ Arduino, thread đọc, auto-reconnect | Đổi baud/port/parse-byte → mất kết nối Arduino. Có chế độ `sim`. |
| **`device_service/parser.py`** | Dịch dòng serial thô → `MachineStatus` + sự kiện | Đổi format dòng Arduino gửi → PHẢI sửa đây, nếu không count/trạng thái sai. Hiểu cả 2 format. |
| **`device_service/server.py`** | Web server Flask: phục vụ UI + API + MJPEG camera | Đổi endpoint/cổng → FE (`web/index.html`) gọi API hỏng. Cổng mặc định 8800. |
| **`device_service/settings.py`** | **Nguồn cấu hình DUY NHẤT** — đọc env → .env → default | Đổi default ở đây ảnh hưởng TOÀN BỘ. Ưu tiên: env(systemd) > `.env` > default. |
| **`device_service/camera.py`** | Bật/tắt camera, quay video, MJPEG preview | Đổi device/size/exposure → preview/video lỗi. `TMP_DIR` module khác import. |
| **`device_service/api_client.py`** | HTTP tới server (start_run, upload, result) | **Import `requests` LAZY + retry** (thẻ SD lỗi làm import fail). Đừng đổi thành import top-level. |
| **`device_service/printer.py`** | In QR (chỉ QR = run_id UUID, không chữ) | Đổi backend máy in (ESC/POS↔Brother) chỉ thêm class, không đụng logic. |
| **`device_service/errors.py`** | **Nguồn DUY NHẤT** mọi mã lỗi/cảnh báo | Thêm/sửa mã lỗi ở đây. Mỗi mã (SRV-/CAM-/MCU-/UPL-/SYS-) map sang 1 group hiện trên FE. |
| **`device_service/web/index.html`** | Giao diện kiosk (1 file, ~105KB) | Đổi UI/JS. Gọi các API `/api/state`, `/api/print`, `/api/history`… phải khớp `server.py`. |
| **`button_start_stop/arduino/production.ino`** | **Firmware** motor + đếm lá | Xem README riêng trong thư mục đó. Rủi ro CAO — sửa sai làm kẹt/không đếm. |

---

## 3. Điểm NHẠY CẢM — dễ sai, tác động lớn

### 3.1 Đếm lá phụ thuộc ENCODER (không phải sensor)
- Arduino đếm 1 lá = đo **quãng đường encoder** lúc lá che sensor (`len`).
- **Nếu encoder không ra xung** (dây D2/D3 lỏng) → `len=0` → KHÔNG đếm dù sensor
  vẫn nhấp nháy. Triệu chứng: `CARD=0`, `meas=0`, `ENC=` đứng cục → STALL.
- ⇒ "count không tăng" thường là **encoder**, không phải code/sensor.

### 3.2 Tốc độ có NHIỀU tầng sàn (firmware)
- Chỉ hạ `STEADY_SPEED` KHÔNG đủ — sàn governor `CAD_SPD_LO` vẫn kéo tốc lên.
- Hạ tốc thật: hạ đồng bộ `STEADY_SPEED`+`CAD_SPD_LO`+`CAD_LO_LIGHT` (sàn) +
  `CAD_HI_HEAVY`/`CAD_SPD_HI` (trần) + `SPEED_MIN`. Chi tiết ở README firmware.

### 3.3 EEPROM sống qua flash (firmware)
- Nạp firmware KHÔNG xóa EEPROM. Đường cong FF + startSpeed đã học vẫn còn.
- Đổi tốc/trần mà không gửi `F0` → startSpeed cũ ghi đè → chạy sai tốc.
- ⇒ Sau khi đổi tốc: LUÔN gửi `F0` reset FF.

### 3.4 `JAM_RECOVER_MS` vs tốc độ (firmware)
- Lá che sensor lâu hơn `JAM_RECOVER_MS` → firmware tưởng KẸT → STALL sai
  ("operation error") dù lá vẫn qua. Tốc càng chậm càng dễ dính.
- Đã nâng 1500→3000 cho tốc chậm. Nếu hạ tốc nữa, cân nhắc nâng tiếp.

### 3.5 `settings.py` là điểm cấu hình DUY NHẤT
- Thứ tự: **biến môi trường (systemd `Environment=`)** > `.env` > default trong code.
- Các cờ `CARD_FAKE_*` bật chế độ giả lập (test không cần phần cứng):
  `CARD_FAKE_SERVER`, `CARD_FAKE_CAMERA`, `CARD_SERIAL_PORT=sim`…
- Đổi hành vi runtime nên qua env/.env, KHÔNG hardcode.

### 3.6 `api_client.py` import LAZY (đừng "dọn dẹp" nhầm)
- Thẻ SD lỗi ext4 làm `import requests` fail lúc khởi động. File cố tình import
  `requests` **lazy + retry mỗi lần gọi**. Đổi thành import top-level → 1 lỗi SD
  làm CHẾT HTTP cả tiến trình. Giữ nguyên.

### 3.7 Serial protocol là HỢP ĐỒNG 2 phía
- `button_start_stop/PROTOCOL.md` = ranh giới Pi ⇄ Arduino.
- Đổi format 1 bên (firmware `emitStatus()` hoặc `parser.py`) mà không đổi bên kia
  → count/trạng thái sai. Sửa PHẢI đồng bộ cả 2 + cập nhật PROTOCOL.md.

---

## 4. Model tốc độ ML (tùy chọn — làm mượt tốc theo tải)

- `card_device/speed_model.json` = model nhỏ (MLP) train từ log serial trên server.
- Pi **inference closed-form** trong `controller._maybe_send_model_speed()` → gửi
  lệnh `V<c/s>` xuống Arduino để vi chỉnh tốc theo số lá còn lại.
- Thiếu file này → chạy bình thường bằng governor firmware (không bắt buộc).
- Đổi `CARD_SPEED_DT_TARGET` đổi mục tiêu nhịp của model.

---

## 5. Tóm tắt "muốn đổi X thì sửa đâu"

| Muốn đổi… | Sửa ở |
|---|---|
| Tốc độ motor | `production.ino` (nhiều hằng số tốc) + `F0` + nạp lại — xem README firmware |
| Số lá mục tiêu | Server (`start_run` trả `target`) — KHÔNG hardcode ở Pi |
| Ngưỡng lỗi/cảnh báo hiển thị | `errors.py` |
| Giao diện / nút | `device_service/web/index.html` (+ endpoint `server.py`) |
| Cấu hình runtime (port, camera, timeout) | biến môi trường / `.env` (đọc bởi `settings.py`) |
| Logic vận hành (trình tự, dừng, retry) | `controller.py` |
| Cách nói chuyện với Arduino | `serial_link.py` + `parser.py` (đồng bộ `PROTOCOL.md`) |
| Máy in | thêm backend trong `printer.py` |

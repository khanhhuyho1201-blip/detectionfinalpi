# button_start_stop — App điều khiển máy đếm lá (Raspberry Pi 5)

Màn hình cảm ứng 1 trang: nút **BẮT ĐẦU / DỪNG** vừa điều khiển máy (Arduino qua
serial) vừa (tuỳ chọn) quay video và gửi lên server. Đếm lá hiển thị realtime,
đèn trạng thái xanh/vàng/đỏ, có nhật ký cuộn.

Là phần **B** trong kế hoạch. Phần **A** (firmware Arduino) xem `ARDUINO_CHANGES.md`,
giao thức chung xem `PROTOCOL.md`.

## Chạy nhanh

```bash
# Không cần phần cứng — dùng simulator có sẵn (test UI ngay):
./run.sh sim
# hoặc: BSS_SERIAL_PORT=sim python3 app.py

# Với Arduino thật cắm ở /dev/ttyACM0:
./run.sh
```

Phím tắt: `Esc` thoát toàn màn hình, `F11` bật/tắt toàn màn hình.

## Module

| File            | Vai trò                                                        |
|-----------------|----------------------------------------------------------------|
| `app.py`        | Điểm vào — ghép mọi thứ lại, chạy vòng lặp UI                  |
| `ui.py`         | Giao diện cảm ứng (nút, số đếm, đèn, log, preview)             |
| `session.py`    | Điều phối 1 mẻ: START→N→camera→B1; DONE/STALL/STOP→B0→upload   |
| `serial_link.py`| Mở cổng serial, đọc nền, gửi lệnh, **tự reconnect**           |
| `simulator.py`  | Arduino giả — test toàn bộ app không cần board                 |
| `parser.py`     | Phân tích dòng serial → `MachineStatus` (đọc cả `ST` lẫn log)  |
| `camera.py`     | Quay USB webcam bằng ffmpeg (tuỳ chọn, M5)                     |
| `uploader.py`   | Gửi clip + metadata lên server (tuỳ chọn, M6)                  |
| `config.py`     | Toàn bộ cấu hình + override qua biến môi trường `BSS_*`        |

## Cấu hình (biến môi trường `BSS_*`)

| Biến                  | Mặc định        | Ý nghĩa                                       |
|-----------------------|-----------------|-----------------------------------------------|
| `BSS_SERIAL_PORT`     | `/dev/ttyACM0`  | Cổng Arduino, hoặc `sim` để dùng simulator    |
| `BSS_SERIAL_BAUD`     | `115200`        | Baud                                          |
| `BSS_DEFAULT_TOTAL`   | `412`           | Số lá / mẻ mặc định (chỉnh được trên màn hình)|
| `BSS_TOTAL_STEP`      | `10`            | Bước tăng/giảm của nút − / +                  |
| `BSS_CAMERA`          | `0`             | `1` để bật quay video (M5)                    |
| `BSS_VIDEO_DEVICE`    | `/dev/video0`   | Webcam                                        |
| `BSS_VIDEO_DIR`       | `~/bss_videos`  | Nơi lưu clip                                  |
| `BSS_UPLOAD`          | `0`             | `1` để bật upload (M6) — cần `BSS_UPLOAD_URL` |
| `BSS_UPLOAD_URL`      | (trống)         | Endpoint nhận clip                            |
| `BSS_FULLSCREEN`      | `1`             | `0` để chạy cửa sổ (tiện debug)               |
| `BSS_AUTOSTART`       | `0`             | `1` tự bấm START sau khi mở (demo/chụp hình)  |

Knob cho simulator: `BSS_SIM_LEAF_MS` (ms/lá), `BSS_SIM_CLUMP_PCT` (% dính lá),
`BSS_SIM_STALL_AT` (giả lập hết lá tại số đếm này).

## Bám theo mốc test trong kế hoạch

| Mốc | Bật gì                                   | Kiểm chứng                                  |
|-----|------------------------------------------|---------------------------------------------|
| M2  | `./run.sh sim`                           | Số đếm + log chạy realtime, không cần board |
| M3  | `./run.sh` (Arduino thật, sau M1)        | Bấm nút trên màn hình → máy chạy/dừng       |
| M4  | thêm dòng `ST` ở firmware (A5)           | Số X/412 + đèn lỗi đúng                      |
| M5  | `BSS_CAMERA=1 ./run.sh`                  | START tự quay; mẻ xong tự dừng quay         |
| M6  | `BSS_UPLOAD=1 BSS_UPLOAD_URL=... ./run.sh` | Mẻ xong → clip lên server                  |

> Server chưa chốt (Phụ lục mục 5): khi `BSS_UPLOAD=0`, clip được giữ ở
> `~/bss_videos` và in đường dẫn ra log. Khi có server, sửa `_post()` trong
> `uploader.py` cho khớp API là xong.

## Phụ thuộc

Đã có sẵn trên Pi này (python3 hệ thống). Máy mới xem `requirements.txt`.

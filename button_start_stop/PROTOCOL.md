# Giao thức Pi5 ⇄ Arduino (Part C)

Ranh giới giữa hai phía. Cả firmware Arduino và app Pi5 phải tuân theo bảng này.
Đường truyền: USB CDC serial, **115200 baud**, mỗi lệnh/dòng kết thúc bằng `\n`.

## Pi → Arduino (lệnh)

| Lệnh    | Ý nghĩa                                            |
|---------|----------------------------------------------------|
| `B1`    | Bắt đầu chạy (ON)                                  |
| `B0`    | Dừng + home (OFF)                                  |
| `N<n>`  | Đặt số lá mẻ này (vd `N412`). `N0` = kéo đến hết   |
| `S`     | Hỏi trạng thái ngay (Arduino trả 1 dòng `ST`)      |

## Arduino → Pi (báo cáo)

Pi parse được **cả hai** kiểu dưới đây, nên firmware gửi kiểu nào cũng chạy.

### Kiểu gọn, máy đọc — khuyến nghị (A5)
```
ST st=RUN n=137 tot=412 err=NONE spd=130
```
- `st`  ∈ `RUN | IDLE | OFF | DONE | ERROR`
- `n`   = số lá đã đếm trong mẻ (`cardCount`)
- `tot` = mục tiêu mẻ (`batchTarget`, 0 = kéo hết)
- `err` ∈ `NONE | CLUMP | STALL | LIMIT`
- `spd` = **PWM động cơ 0–255** (`motorPWM`, KHÔNG phải lá/giây). Khớp với số
  sau `PWM=` trong `[CARD]`.

Firmware gửi định kỳ ~250 ms khi chạy, ngay khi nhận `S`, và tại các mốc
ON/OFF/DONE/STALL.

> **Lưu ý đồng bộ:** ngay sau `B0`, firmware (`doMachineOff`) phát
> `ST st=OFF err=NONE` (đã reset lỗi). Pi **chốt (latch)** kết quả mẻ
> (DONE/STALL) trước khi dòng OFF này tới, nên màn hình vẫn giữ kết quả đúng
> đến lần START kế. Xem `session.py: outcome()`.

### Kiểu log cũ (con người đọc) — Pi vẫn hiểu (định dạng THẬT của firmware)
| Dòng (thực tế)                                          | Pi hiểu là                          |
|---------------------------------------------------------|-------------------------------------|
| `[CARD] #137 | REM=275 | dt=137ms | PWM=130`            | đếm = 137 (lấy số sau `#`, PWM=)    |
| `[CLUMP] 2 la 1 luc len=820 ratio=1.9`                  | cảnh báo dính lá (đèn vàng)         |
| `[STALL] khong co la 1500ms -> DA DUNG MOTOR`           | hết lá / kẹt → kết thúc mẻ          |
| `[DONE] da dem 412 la (hoan tat me)`                    | xong mẻ (Pi bắt token, không cần `total=`) |
| `[MACHINE] ON` / `OFF` / `READY`                        | đồng bộ trạng thái nút              |
| `[STATUS]` / `[STAT]` / `[HEALTH]` / `[WAIT]` / `[REGRIP]` | dòng thông tin (chỉ hiện ở log)  |

> **`[LIMIT]` gần như không xuất hiện:** máy KHÔNG có công tắc giới hạn vật lý;
> `[LIMIT]` chỉ là cảnh báo platform chạm trần khi `STEPPER_ENABLED=true` (đang
> tắt) và không dừng máy. Pi có xử lý nhưng KHÔNG phụ thuộc vào nó.

> **STALL = hết lá:** firmware báo `err=STALL` cho cả khi hết deck. Pi diễn giải
> theo ngữ cảnh: chế độ kéo-hết (`tot=0`) → coi là **xong bình thường** (xanh);
> có mục tiêu mà dừng sớm → **cảnh báo** thiếu lá (vàng). Đạt mục tiêu → `[DONE]`
> (`st=DONE`, xanh).

## Luồng 1 mẻ (do Pi điều phối — `session.py`)

```
Bấm BẮT ĐẦU →  N<total>  →  (camera.start nếu bật)  →  B1
Máy chạy    →  [CARD]/ST cập nhật số đếm trên màn hình
Kết thúc    →  do [DONE]/[STALL]  HOẶC bấm DỪNG
            →  B0  →  camera.stop+lưu  →  upload (nếu bật)
```

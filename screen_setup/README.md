# Cảm ứng userspace — Waveshare 3.5" (C) / XPT2046 trên Raspberry Pi 5

## Vấn đề & giải pháp

Màn hình hiển thị tốt nhưng **cảm ứng không chạy** vì **mạch PENIRQ hỏng phần cứng**
(chip XPT2046/ADS7846 không phát ngắt khi chạm). Driver kernel chuẩn phụ thuộc
PENIRQ nên không đọc được. Tuy nhiên kênh đo tọa độ X/Y vẫn tốt qua SPI.

Driver này đọc chip kiểu **polling** (bỏ qua PENIRQ) tại 125kHz PD=00, phát hiện
chạm qua độ ổn định của X/Y, hiệu chỉnh **affine/đa thức** rồi bơm sự kiện qua
`uinput`. Chạy như systemd service, tự khởi động khi boot. Kèm ẩn con trỏ chuột.

## Lệnh thường dùng

```bash
sudo fix-touch                       # HIỆU CHỈNH điểm chạm (chạm 9 dấu + trên màn hình)
sudo systemctl status ads7846-touch  # trạng thái
sudo systemctl restart ads7846-touch # khởi động lại (sau khi sửa touch.env)
journalctl -u ads7846-touch -f       # xem log
```

## Hiệu chỉnh cho chính xác (`sudo fix-touch`)

Chạy `sudo fix-touch`, trên màn hình hiện 9 dấu **+**. Với mỗi dấu:
- **Ấn CHẮC** (ép hẳn ngón tay xuống — đừng chạm nhẹ/lướt).
- **Giữ yên ~1.5 giây** tới khi chấm vàng to lên → tự nhảy dấu kế.
- Chạm đúng **tâm** dấu +.

Xong 9 dấu, máy vào **chế độ kiểm tra** (vòng đỏ theo ngón tay) rồi tự áp dụng.
Ấn chắc + chạm đúng tâm = dữ liệu sạch = con trỏ nằm đúng ngón tay.

## Chỉnh hướng (hiếm khi cần)

Sửa `/opt/ads7846-touch/touch.env` rồi `sudo systemctl restart ads7846-touch`:
- Ngược trái/phải → đổi `TS_INVERT_X` (0↔1)
- Ngược trên/dưới → đổi `TS_INVERT_Y` (0↔1)
- Di ngang chạy dọc → đổi `TS_SWAP_XY` (0↔1)

(Khi có `TS_AFFINE` hoặc `TS_POLY`, các cờ trên bị bỏ qua — hiệu chỉnh đã gồm hướng.)

## Độ chính xác & phần cứng (QUAN TRỌNG)

- Tấm cảm ứng **điện trở** XPT2046 có giới hạn vật lý ~1–1.5mm. Không đạt "0 tuyệt đối".
- **Điểm chạm bị TRÔI theo thời gian** thường do **dây tín hiệu cảm ứng tiếp xúc
  chập chờn** (jumper lỏng). Điện áp đọc trôi → hiệu chỉnh hôm nay đúng mai lại lệch.
  → **Cắm chắc/hàn lại 4 dây cảm ứng (X+,X−,Y+,Y−) + PENIRQ**, rồi `sudo fix-touch`.
- Ấn nhẹ/nhanh khi hiệu chỉnh cũng cho dữ liệu rác → luôn ấn CHẮC.

## Các file (`/opt/ads7846-touch/`)

| File | Vai trò |
|------|---------|
| `ads7846_touch.py` | Driver polling SPI→uinput (hỗ trợ affine + đa thức) |
| `touch.env` | Cấu hình hướng + hiệu chỉnh (TS_AFFINE/TS_POLY/...) |
| `run-touch.sh`, `prepare-spidev.sh` | Khởi động: tách chip khỏi driver kernel, gắn spidev |
| `recalibrate.sh` | Lệnh `fix-touch` (hiệu chỉnh tự phục vụ) |
| `affine_calib.py` | GUI hiệu chỉnh 9 điểm + kiểm tra trực tiếp |
| `fit_models.py` | Backend: fit affine vs đa thức, chọn tốt nhất |
| `drawpad.py`, `touchtest_gui.py` | Công cụ test hướng/độ chính xác |

## Gỡ cài đặt

```bash
sudo systemctl disable --now ads7846-touch
sudo rm /etc/systemd/system/ads7846-touch.service /usr/local/bin/fix-touch
sudo rm /etc/modules-load.d/uinput.conf
sudo rm -rf /opt/ads7846-touch && sudo systemctl daemon-reload
```

#!/usr/bin/env python3
"""
Driver cảm ứng userspace cho ADS7846/XPT2046 trên màn hình Waveshare 3.5" (C).

Lý do tồn tại: trên tấm cảm ứng cụ thể này, mạch PENIRQ và kênh đo áp lực (Z)
bị hở phần cứng, nên driver kernel ads7846 (vốn phụ thuộc ngắt PENIRQ) không
bao giờ đọc. TUY NHIÊN kênh đo tọa độ X/Y vẫn hoạt động tốt qua SPI.

Driver này đọc ADS7846 qua SPI theo kiểu POLLING (không cần PENIRQ), phát hiện
"đang chạm" dựa vào việc X/Y rời khỏi vị trí floating (X=0, Y=max) và độ ổn định
giữa các mẫu, rồi bơm sự kiện cảm ứng tuyệt đối vào hệ thống qua uinput.

Chạy: sudo python3 ads7846_touch.py
Cấu hình qua biến môi trường (systemd truyền vào) — xem phần CONFIG.
"""
import os
import sys
import time
import signal

import spidev
from evdev import UInput, ecodes as e, AbsInfo

# ----------------------------- CONFIG ------------------------------
SPI_BUS      = int(os.environ.get("TS_SPI_BUS", "0"))
SPI_CS       = int(os.environ.get("TS_SPI_CS", "1"))       # chip cảm ứng ở CS1
# 125kHz + PD=00: cấu hình đọc SẠCH nhất, giống driver kernel. Tốc độ cao (500k) +
# PD=11 gây nhiễu chéo kênh -> tọa độ rác. ĐỪNG tăng tốc độ này nếu chưa test kỹ.
SPI_HZ       = int(os.environ.get("TS_SPI_HZ", "125000"))

# Độ phân giải màn hình (điểm ảnh) — dùng cho hệ tọa độ uinput
SCREEN_W     = int(os.environ.get("TS_SCREEN_W", "480"))
SCREEN_H     = int(os.environ.get("TS_SCREEN_H", "320"))

# Hiệu chỉnh: vùng giá trị ADC thô tương ứng mép màn hình.
# Giá trị đo thực tế từ record.py (90 điểm quét khắp màn hình, phân vị 2%-98%).
CAL_X_MIN    = int(os.environ.get("TS_CAL_X_MIN", "360"))
CAL_X_MAX    = int(os.environ.get("TS_CAL_X_MAX", "3050"))
CAL_Y_MIN    = int(os.environ.get("TS_CAL_Y_MIN", "285"))
CAL_Y_MAX    = int(os.environ.get("TS_CAL_Y_MAX", "3700"))

# Hướng màn hình (landscape). Các cờ này ánh xạ trục ADC -> trục màn hình.
# Mặc định SWAP=0: test cho thấy raw_X<->screen_X, raw_Y<->screen_Y (correlation +1.00),
# phủ gần hết 480x320. Nếu hướng sai khi dùng thật, chỉ cần lật các cờ này qua env.
SWAP_XY      = os.environ.get("TS_SWAP_XY", "0") == "1"
INVERT_X     = os.environ.get("TS_INVERT_X", "0") == "1"
INVERT_Y     = os.environ.get("TS_INVERT_Y", "0") == "1"

# --- Hiệu chỉnh ĐA THỨC phi tuyến (ưu tiên CAO NHẤT) ---
# Bắt độ méo phi tuyến của tấm điện trở (góc hụt, rìa cong) mà affine không bù được.
# u=rawX/4095, v=rawY/4095.
#   8 số  -> BILINEAR:    cơ sở [1,u,v,u*v]        (4/trục) — kéo đúng 4 góc (keystone).
#   12 số -> BIQUADRATIC: cơ sở [1,u,v,u*v,u^2,v^2] (6/trục) — thêm độ cong.
# sx = a·cơ_sở ; sy = b·cơ_sở. Đặt qua TS_POLY = "a...,b...". GHI ĐÈ mọi cách khác.
_POLY = None
_POLY_N = 0   # số hệ số mỗi trục (4=bilinear, 6=biquadratic)
_poly_raw = os.environ.get("TS_POLY", "").strip()
if _poly_raw:
    try:
        _pv = [float(v) for v in _poly_raw.replace(";", ",").split(",")]
        if len(_pv) == 8:
            _POLY = _pv; _POLY_N = 4
        elif len(_pv) == 12:
            _POLY = _pv; _POLY_N = 6
    except Exception:
        _POLY = None

# --- Hiệu chỉnh AFFINE 6 tham số (ưu tiên sau đa thức) ---
# sx = A*rawX + B*rawY + C ; sy = D*rawX + E*rawY + F.
_AFFINE = None
_affine_raw = os.environ.get("TS_AFFINE", "").strip()
if _affine_raw:
    try:
        _vals = [float(v) for v in _affine_raw.replace(";", ",").split(",")]
        if len(_vals) == 6:
            _AFFINE = _vals
    except Exception:
        _AFFINE = None

# Tốc độ quét và lọc
POLL_HZ      = int(os.environ.get("TS_POLL_HZ", "100"))    # số lần quét/giây
SAMPLES      = int(os.environ.get("TS_SAMPLES", "12"))     # số mẫu/lần đọc (khử nhiễu, lấy trung vị)
# Phát hiện "đang chạm": khi nhấc tay -> X=0, Y=4095 (floating). Khi chạm thật ->
# X,Y trong dải hợp lệ VÀ ổn định (độ lệch chuẩn nhỏ).
TOUCH_X_LO   = int(os.environ.get("TS_TOUCH_X_LO", "60"))
TOUCH_X_HI   = int(os.environ.get("TS_TOUCH_X_HI", "4030"))
TOUCH_Y_LO   = int(os.environ.get("TS_TOUCH_Y_LO", "60"))
TOUCH_Y_HI   = int(os.environ.get("TS_TOUCH_Y_HI", "4030"))
# Độ lệch chuẩn tối đa cho phép của phần lõi mẫu (đo bằng σ). Chạm thật: σ<40.
# Nhiễu khi nhấc/chạm hụt: σ lớn -> loại.
MAX_STD      = int(os.environ.get("TS_MAX_STD", "60"))
# Số lần đọc "không chạm" liên tiếp trước khi phát sự kiện nhấc tay (chống rung).
RELEASE_DEBOUNCE = int(os.environ.get("TS_RELEASE_DEBOUNCE", "4"))
# Làm mượt vị trí bằng trung bình động có trọng số (0=tắt, 0..1 càng lớn càng mượt)
SMOOTHING    = float(os.environ.get("TS_SMOOTHING", "0.30"))
# Số điểm "ổn định" đầu tiên bỏ qua trước khi ĐẶT BÚT tại vị trí chạm mới.
# Tránh điểm nhiễu lúc ngón tay vừa tiếp xúc -> chống hiện tượng con trỏ lệch/nhảy
# ở đầu mỗi lần chạm (đặc biệt sau khi vừa kéo một đoạn). 2-3 là hợp lý.
SETTLE_SAMPLES = int(os.environ.get("TS_SETTLE_SAMPLES", "2"))
DEBUG        = os.environ.get("TS_DEBUG", "0") == "1"

# Control byte differential, PD=00 (power-down giữa lần đọc; SẠCH nhất, giống driver kernel).
# 0xD0 = 1101 0000 : đo X (A2A1A0=101), 12-bit, differential, PD=00
# 0x90 = 1001 0000 : đo Y (A2A1A0=001), 12-bit, differential, PD=00
CMD_X  = 0xD0
CMD_Y  = 0x90
# -------------------------------------------------------------------


def log(*a):
    if DEBUG:
        print(*a, flush=True)


def _pstdev(vals):
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    return (sum((v - m) ** 2 for v in vals) / n) ** 0.5


class ADS7846:
    def __init__(self):
        self.spi = spidev.SpiDev()
        self.spi.open(SPI_BUS, SPI_CS)
        self.spi.max_speed_hz = SPI_HZ
        self.spi.mode = 0

    def _read12(self, cmd):
        r = self.spi.xfer2([cmd, 0x00, 0x00])
        return (((r[1] << 8) | r[2]) >> 3) & 0xFFF

    def read_raw(self):
        """Đọc SAMPLES mẫu X,Y; trả về (x_med, y_med, std) với std = độ lệch chuẩn
        lớn hơn giữa hai trục, tính trên phần lõi (đã bỏ 1/4 số mẫu ngoài cùng mỗi phía)."""
        xs, ys = [], []
        for _ in range(SAMPLES):
            self._read12(CMD_X)          # đọc bỏ (đổi kênh, cho ADC settle)
            xs.append(self._read12(CMD_X))
            self._read12(CMD_Y)
            ys.append(self._read12(CMD_Y))
        xs.sort(); ys.sort()
        xm = xs[len(xs) // 2]
        ym = ys[len(ys) // 2]
        # lõi: bỏ 1/4 mẫu mỗi đầu để loại ngoại lai, rồi đo độ lệch chuẩn
        k = max(1, SAMPLES // 4)
        xc = xs[k:-k] if len(xs) > 2 * k else xs
        yc = ys[k:-k] if len(ys) > 2 * k else ys
        xstd = _pstdev(xc)
        ystd = _pstdev(yc)
        return xm, ym, max(xstd, ystd)

    def close(self):
        try:
            self.spi.close()
        except Exception:
            pass


def is_touch(x, y, std):
    if std > MAX_STD:
        return False
    if not (TOUCH_X_LO <= x <= TOUCH_X_HI):
        return False
    if not (TOUCH_Y_LO <= y <= TOUCH_Y_HI):
        return False
    return True


def map_to_screen(x, y):
    # Ưu tiên đa thức phi tuyến (chính xác nhất — bù méo góc/rìa).
    if _POLY is not None:
        u = x / 4095.0
        v = y / 4095.0
        if _POLY_N == 4:
            basis = (1.0, u, v, u * v)                      # bilinear
        else:
            basis = (1.0, u, v, u * v, u * u, v * v)         # biquadratic
        n = _POLY_N
        a = _POLY[0:n]
        b = _POLY[n:2 * n]
        sx = sum(a[i] * basis[i] for i in range(n))
        sy = sum(b[i] * basis[i] for i in range(n))
        sx = int(round(min(SCREEN_W - 1, max(0.0, sx))))
        sy = int(round(min(SCREEN_H - 1, max(0.0, sy))))
        return sx, sy
    # Phép biến đổi affine 6 tham số (xử lý xoay/nghiêng, tuyến tính).
    if _AFFINE is not None:
        A, B, C, D, E, F = _AFFINE
        sx = A * x + B * y + C
        sy = D * x + E * y + F
        sx = int(round(min(SCREEN_W - 1, max(0.0, sx))))
        sy = int(round(min(SCREEN_H - 1, max(0.0, sy))))
        return sx, sy
    # Cách cũ: chuẩn hóa min/max theo từng trục + swap/invert.
    fx = (x - CAL_X_MIN) / float(CAL_X_MAX - CAL_X_MIN)
    fy = (y - CAL_Y_MIN) / float(CAL_Y_MAX - CAL_Y_MIN)
    fx = min(1.0, max(0.0, fx))
    fy = min(1.0, max(0.0, fy))
    if INVERT_X:
        fx = 1.0 - fx
    if INVERT_Y:
        fy = 1.0 - fy
    if SWAP_XY:
        fx, fy = fy, fx
    sx = int(fx * (SCREEN_W - 1))
    sy = int(fy * (SCREEN_H - 1))
    return sx, sy


def make_uinput():
    # QUAN TRỌNG: chỉ khai báo BTN_TOUCH (KHÔNG BTN_LEFT) + INPUT_PROP_DIRECT
    # để udev phân loại là TOUCHSCREEN. Nếu thêm BTN_LEFT, udev phân loại nhầm
    # thành MOUSE -> X evdev xử lý sai chế độ -> sự kiện bấm bị nuốt (chạm không click).
    cap = {
        e.EV_KEY: [e.BTN_TOUCH],
        e.EV_ABS: [
            (e.ABS_X, AbsInfo(value=0, min=0, max=SCREEN_W - 1, fuzz=0, flat=0, resolution=0)),
            (e.ABS_Y, AbsInfo(value=0, min=0, max=SCREEN_H - 1, fuzz=0, flat=0, resolution=0)),
            (e.ABS_PRESSURE, AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0)),
        ],
    }
    try:
        return UInput(cap, name="ADS7846 Userspace Touch", version=0x1,
                      input_props=[e.INPUT_PROP_DIRECT])
    except TypeError:
        # python-evdev cũ không có input_props
        return UInput(cap, name="ADS7846 Userspace Touch", version=0x1)


def main():
    dev = ADS7846()
    ui = make_uinput()
    log("uinput device tạo xong:", ui.device.path if ui.device else "?")

    running = {"v": True}
    def stop(*_):
        running["v"] = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    period = 1.0 / POLL_HZ
    touching = False
    release_cnt = 0
    sm_x = sm_y = None      # vị trí đã làm mượt (theo tọa độ màn hình)
    settle = 0             # số điểm đã ổn định kể từ khi bắt đầu chạm

    try:
        while running["v"]:
            t0 = time.perf_counter()
            x, y, std = dev.read_raw()
            if is_touch(x, y, std):
                sx, sy = map_to_screen(x, y)
                if not touching:
                    # BẮT ĐẦU chạm mới: chưa phát vội. Đợi ổn định vài điểm để tránh
                    # điểm nhiễu lúc ngón tay vừa chạm (nguyên nhân "lệch tùy đường kéo").
                    touching = True
                    settle = 1
                    sm_x, sm_y = float(sx), float(sy)
                    release_cnt = 0
                    # chưa gửi BTN_TOUCH/tọa độ ở điểm đầu (có thể nhiễu)
                elif settle < SETTLE_SAMPLES:
                    # vẫn trong giai đoạn ổn định: cập nhật vị trí, chưa phát
                    settle += 1
                    sm_x, sm_y = float(sx), float(sy)   # bám sát điểm mới nhất
                    if settle >= SETTLE_SAMPLES:
                        # đủ ổn định -> giờ mới "đặt bút" tại vị trí ổn định
                        ui.write(e.EV_ABS, e.ABS_X, int(round(sm_x)))
                        ui.write(e.EV_ABS, e.ABS_Y, int(round(sm_y)))
                        ui.write(e.EV_ABS, e.ABS_PRESSURE, 200)
                        ui.write(e.EV_KEY, e.BTN_TOUCH, 1)
                        ui.syn()
                    release_cnt = 0
                else:
                    # đang chạm ổn định: trung bình động làm mượt
                    sm_x = SMOOTHING * sm_x + (1.0 - SMOOTHING) * sx
                    sm_y = SMOOTHING * sm_y + (1.0 - SMOOTHING) * sy
                    ui.write(e.EV_ABS, e.ABS_X, int(round(sm_x)))
                    ui.write(e.EV_ABS, e.ABS_Y, int(round(sm_y)))
                    ui.syn()
                    release_cnt = 0
                    log(f"touch raw=({x},{y}) std={std:.0f} -> screen=({int(round(sm_x))},{int(round(sm_y))})")
            else:
                if touching:
                    release_cnt += 1
                    if release_cnt >= RELEASE_DEBOUNCE:
                        touching = False
                        settle = 0
                        sm_x = sm_y = None   # RESET hẳn bộ lọc khi nhấc tay
                        ui.write(e.EV_ABS, e.ABS_PRESSURE, 0)
                        ui.write(e.EV_KEY, e.BTN_TOUCH, 0)
                        ui.syn()
                        log("release")
            dt = time.perf_counter() - t0
            if dt < period:
                time.sleep(period - dt)
    finally:
        if touching:
            ui.write(e.EV_KEY, e.BTN_TOUCH, 0)
            ui.syn()
        ui.close()
        dev.close()
        log("Đã dừng sạch.")


if __name__ == "__main__":
    main()

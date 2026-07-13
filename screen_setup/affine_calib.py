#!/usr/bin/env python3
"""
Hiệu chỉnh AFFINE độ chính xác cao cho cảm ứng XPT2046/ADS7846.

Thu 9 điểm (lưới 3x3) trải khắp màn hình, giải bình phương tối thiểu để ra
phép biến đổi affine 6 tham số:
    sx = A*rawX + B*rawY + C
    sy = D*rawX + E*rawY + F
Phép này xử lý ĐỒNG THỜI xoay, nghiêng, co giãn, dịch chuyển -> chính xác hơn
hẳn cách min/max + swap/invert (vốn giả định tấm cảm ứng thẳng hàng hoàn hảo).

Sau khi tính xong, chương trình VÀO CHẾ ĐỘ KIỂM TRA: chạm đâu, chấm đỏ hiện đó
(qua chính affine vừa tính) + vòng xanh vẫn ở tâm — anh thấy ngay có khớp không.

Kết quả affine ghi ra: affine_result.txt (dòng "A B C D E F")
"""
import tkinter as tk
import spidev, time, json, os

# --- đọc ADC thô (giống driver: 125kHz, PD=00, differential, lấy trung vị) ---
spi = spidev.SpiDev(); spi.open(0, 1); spi.max_speed_hz = 125000; spi.mode = 0
CMD_X, CMD_Y = 0xD0, 0x90
def r12(c):
    v = spi.xfer2([c, 0, 0]); return (((v[1] << 8) | v[2]) >> 3) & 0xFFF
def _std(a):
    n = len(a)
    if n < 2: return 0.0
    m = sum(a)/n; return (sum((t-m)**2 for t in a)/n) ** 0.5
def read_raw(ns=10):
    xs, ys = [], []
    for _ in range(ns):
        r12(CMD_X); xs.append(r12(CMD_X)); r12(CMD_Y); ys.append(r12(CMD_Y))
    xs.sort(); ys.sort()
    k = max(1, ns // 4)
    return xs[ns//2], ys[ns//2], max(_std(xs[k:-k]), _std(ys[k:-k]))

# --- giải hệ 3x3 (khử Gauss) cho hồi quy z = a*rx + b*ry + c ---
def solve3(M, v):
    # M: 3x3, v: 3 -> nghiệm 3
    import copy
    A = [row[:] + [v[i]] for i, row in enumerate(M)]
    n = 3
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(A[r][col]))
        A[col], A[piv] = A[piv], A[col]
        if abs(A[col][col]) < 1e-12:
            return None
        pv = A[col][col]
        for j in range(col, n+1):
            A[col][j] /= pv
        for r in range(n):
            if r != col:
                f = A[r][col]
                for j in range(col, n+1):
                    A[r][j] -= f * A[col][j]
    return [A[i][n] for i in range(n)]

def fit_plane(pts):
    # pts: list of (rx, ry, z) -> trả (a,b,c) sao cho z ~ a*rx + b*ry + c
    Sxx=Sxy=Sx=Syy=Sy=Sn=0.0
    Sxz=Syz=Sz=0.0
    for rx, ry, z in pts:
        Sxx+=rx*rx; Sxy+=rx*ry; Sx+=rx
        Syy+=ry*ry; Sy+=ry; Sn+=1
        Sxz+=rx*z; Syz+=ry*z; Sz+=z
    M=[[Sxx,Sxy,Sx],[Sxy,Syy,Sy],[Sx,Sy,Sn]]
    return solve3(M, [Sxz,Syz,Sz])

# Lưới mục tiêu 3x3 = 9 điểm, đưa SÁT GÓC hơn (10%/90%) để giảm ngoại suy tới góc.
# Kết hợp model bilinear -> 4 góc được kéo về đúng chỗ. Ấn CHẮC từng điểm.
GX = [0.10, 0.50, 0.90]
GY = [0.12, 0.50, 0.88]
TARGETS = [(fx, fy) for fy in GY for fx in GX]   # 9 điểm

class Calib:
    def __init__(self, root):
        self.root = root
        root.attributes("-fullscreen", True); root.configure(bg="black")
        self.W = root.winfo_screenwidth(); self.H = root.winfo_screenheight()
        self.c = tk.Canvas(root, width=self.W, height=self.H, bg="black", highlightthickness=0)
        self.c.pack()
        self.idx = 0
        self.data = []            # (sx_target, sy_target, rawx, rawy)
        self.samples = []
        self.affine = None
        self.mode = "collect"     # collect -> test
        self.show()
        self.root.after(150, self.poll)

    def target_px(self):
        fx, fy = TARGETS[self.idx]
        return int(fx*self.W), int(fy*self.H)

    def show(self):
        self.c.delete("all")
        if self.idx >= len(TARGETS):
            self.compute(); return
        tx, ty = self.target_px()
        r = 22
        self.c.create_oval(tx-r, ty-r, tx+r, ty+r, outline="#00ff66", width=4)
        self.c.create_line(tx-r-6, ty, tx+r+6, ty, fill="#00ff66", width=2)
        self.c.create_line(tx, ty-r-6, tx, ty+r+6, fill="#00ff66", width=2)
        self.c.create_text(self.W//2, 24,
                           text=f"Chạm & GIỮ tâm dấu +   [{self.idx+1}/{len(TARGETS)}]",
                           fill="white", font=("DejaVu Sans", 14, "bold"))
        self.samples = []

    def poll(self):
        try:
            x, y, std = read_raw()
        except Exception:
            self.root.after(60, self.poll); return
        # ngưỡng CHẶT: chỉ nhận khi ấn chắc & rất ổn định (std < 25)
        touching = (60 < x < 4030 and 60 < y < 4030 and std < 25)
        if self.mode == "collect":
            if touching:
                self.samples.append((x, y))
                tx, ty = self.target_px()
                # vòng tiến độ: càng giữ lâu chấm vàng càng to
                prog = min(14, len(self.samples))
                self.c.create_oval(tx-prog, ty-prog, tx+prog, ty+prog,
                                   fill="#ffdd00", outline="")
                # cần 14 mẫu ổn định liên tiếp -> đảm bảo ngón tay đứng yên, ấn chắc
                if len(self.samples) >= 14:
                    xs = sorted(s[0] for s in self.samples[-14:])
                    ys = sorted(s[1] for s in self.samples[-14:])
                    rx, ry = xs[7], ys[7]
                    tpx, tpy = self.target_px()
                    self.data.append((tpx, tpy, rx, ry))
                    self.c.create_text(self.W//2, self.H-24, text="✓ Đã ghi! Nhấc tay.",
                                       fill="#00ff66", font=("DejaVu Sans", 13, "bold"))
                    self.idx += 1
                    self.samples = []
                    self.root.after(1000, self.show)
                    self.root.after(1100, self.poll)
                    return
            else:
                self.samples = []
        else:  # test mode
            self.c.delete("live")
            if touching and self.affine:
                A,B,C,D,E,F = self.affine
                sx = min(self.W-1, max(0, int(A*x+B*y+C)))
                sy = min(self.H-1, max(0, int(D*x+E*y+F)))
                self.c.create_oval(sx-10, sy-10, sx+10, sy+10, outline="#ff3333",
                                   width=3, tags="live")
                self.c.create_oval(sx-2, sy-2, sx+2, sy+2, fill="#ff3333",
                                   outline="", tags="live")
        self.root.after(50, self.poll)

    def compute(self):
        # LƯU dữ liệu raw để backend fit đa thức/nhiều mô hình
        try:
            with open("/home/bbsw/ads7846-userspace/calib_points.json", "w") as f:
                json.dump(self.data, f)
        except Exception:
            pass
        # tính affine ngay (cho chế độ kiểm tra xem trước)
        pts_sx = [(rx, ry, tpx) for (tpx, tpy, rx, ry) in self.data]
        pts_sy = [(rx, ry, tpy) for (tpx, tpy, rx, ry) in self.data]
        abc = fit_plane(pts_sx)
        deF = fit_plane(pts_sy)
        if not abc or not deF:
            self.c.delete("all")
            self.c.create_text(self.W//2, self.H//2, text="Lỗi tính toán.\nThử lại.",
                               fill="red", font=("DejaVu Sans", 16, "bold"))
            self.root.after(3000, self.root.destroy); return
        self.affine = abc + deF
        # đánh giá sai số trung bình trên chính 9 điểm
        err = 0.0
        for (tpx, tpy, rx, ry) in self.data:
            px = abc[0]*rx+abc[1]*ry+abc[2]
            py = deF[0]*rx+deF[1]*ry+deF[2]
            err += ((px-tpx)**2 + (py-tpy)**2) ** 0.5
        err /= len(self.data)
        with open("/home/bbsw/ads7846-userspace/affine_result.txt", "w") as f:
            f.write(" ".join(f"{v:.8g}" for v in self.affine) + "\n")
            f.write(f"# sai so trung binh = {err:.1f} px\n")
        # vào chế độ kiểm tra
        self.mode = "test"
        self.c.delete("all")
        self.c.create_text(self.W//2, self.H//2,
            text=f"XONG! Sai số TB ≈ {err:.1f}px\n\nCHẠM thử khắp màn hình:\nvòng đỏ phải trùng ngón tay.\n(tự đóng sau 30s)",
            fill="#00ff66", font=("DejaVu Sans", 13, "bold"), justify="center")
        self.root.after(30000, self.root.destroy)

if __name__ == "__main__":
    os.environ.setdefault("DISPLAY", ":0")
    root = tk.Tk(); Calib(root); root.mainloop()
    spi.close()

#!/usr/bin/env python3
"""
Đọc calib_points.json (các điểm [tpx,tpy,rx,ry]), fit 2 mô hình:
  - AFFINE (6 tham số, tuyến tính)
  - ĐA THỨC bậc 2 (biquadratic, 12 tham số, bắt méo phi tuyến)
Kiểm định chéo leave-one-out cho từng mô hình, chọn mô hình chính xác nhất,
in ra TS_AFFINE hoặc TS_POLY để nạp vào driver.
"""
import json, sys

def load():
    return json.load(open("/home/bbsw/ads7846-userspace/calib_points.json"))

# ---------- giải hệ tuyến tính NxN (khử Gauss có xoay trụ) ----------
def solve(M, b):
    n = len(M)
    A = [row[:] + [b[i]] for i, row in enumerate(M)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(A[r][col]))
        A[col], A[piv] = A[piv], A[col]
        if abs(A[col][col]) < 1e-15:
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

# ---------- bình phương tối thiểu tuyến tính: z ~ phi(feat)·coef ----------
def lstsq(feats, zs):
    # feats: list các vector cơ sở (mỗi cái độ dài m); zs: list giá trị
    m = len(feats[0])
    ATA = [[0.0]*m for _ in range(m)]
    ATz = [0.0]*m
    for phi, z in zip(feats, zs):
        for i in range(m):
            ATz[i] += phi[i]*z
            for j in range(m):
                ATA[i][j] += phi[i]*phi[j]
    return solve(ATA, ATz)

# ---------- cơ sở ----------
def phi_affine(rx, ry):
    return [rx, ry, 1.0]

def phi_bilinear(rx, ry):
    u = rx/4095.0; v = ry/4095.0
    return [1.0, u, v, u*v]

def phi_poly(rx, ry):
    u = rx/4095.0; v = ry/4095.0
    return [1.0, u, v, u*v, u*u, v*v]

def fit(points, phi):
    feats = [phi(rx, ry) for (tpx, tpy, rx, ry) in points]
    ax = lstsq(feats, [p[0] for p in points])
    ay = lstsq(feats, [p[1] for p in points])
    return ax, ay

def predict(phi, ax, ay, rx, ry):
    f = phi(rx, ry)
    return (sum(f[i]*ax[i] for i in range(len(f))),
            sum(f[i]*ay[i] for i in range(len(f))))

def rms_resid(points, phi, ax, ay):
    s = 0.0
    for (tpx, tpy, rx, ry) in points:
        px, py = predict(phi, ax, ay, rx, ry)
        s += (px-tpx)**2 + (py-tpy)**2
    return (s/len(points))**0.5

def loo(points, phi):
    errs = []
    for i in range(len(points)):
        train = [p for j, p in enumerate(points) if j != i]
        ax, ay = fit(train, phi)
        if ax is None or ay is None:
            continue
        px, py = predict(phi, ax, ay, points[i][2], points[i][3])
        errs.append(((px-points[i][0])**2 + (py-points[i][1])**2)**0.5)
    return sum(errs)/len(errs) if errs else 1e9, (max(errs) if errs else 1e9)

def main():
    pts = load()
    print(f"Số điểm hiệu chỉnh: {len(pts)}")
    if len(pts) < 6:
        print("Cần >=6 điểm để fit đa thức."); sys.exit(1)

    # số điểm tối thiểu cho mỗi model: cần > số tham số/trục
    models = [("AFFINE", phi_affine, 3)]
    if len(pts) >= 6:  models.append(("BILINEAR", phi_bilinear, 4))
    if len(pts) >= 9:  models.append(("BIQUADRATIC", phi_poly, 6))

    results = {}
    for name, phi, npar in models:
        ax, ay = fit(pts, phi)
        if ax is None:
            print(f"{name}: không giải được"); continue
        resid = rms_resid(pts, phi, ax, ay)
        loo_mean, loo_max = loo(pts, phi)
        results[name] = (ax, ay, resid, loo_mean, loo_max, npar)
        print(f"[{name:<12}] RMS fit={resid:5.1f}px | kiểm-định-chéo LOO TB={loo_mean:5.1f}px max={loo_max:5.1f}px")

    # chọn theo LOO trung bình (ước lượng sai số thực tế; tự phạt overfit)
    best = min(results, key=lambda k: results[k][3])
    ax, ay, resid, loo_mean, loo_max, npar = results[best]
    print(f"\n===> MÔ HÌNH TỐT NHẤT: {best}  (LOO {loo_mean:.1f}px ~ {loo_mean*0.155:.1f}mm)")

    if best == "AFFINE":
        s = ",".join(f"{v:.8g}" for v in (ax + ay))  # A,B,C,D,E,F
        print(f"TS_AFFINE={s}")
        out = {"kind": "affine", "value": s}
    else:
        # bilinear (4/trục -> 8 số) hoặc biquadratic (6/trục -> 12 số)
        s = ",".join(f"{v:.8g}" for v in (ax + ay))
        print(f"TS_POLY={s}")
        out = {"kind": "poly", "value": s}
    json.dump(out, open("/home/bbsw/ads7846-userspace/best_model.json", "w"))
    print("(Đã lưu best_model.json)")

if __name__ == "__main__":
    main()

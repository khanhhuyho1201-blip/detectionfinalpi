#!/usr/bin/env python3
"""
train_speed_model.py — train model nhỏ điều tốc theo trọng lượng chồng bài.

Học từ log serial các mẻ THẬT (feed_dataset.csv):
  1. DT-MODEL  (MLP 2-16-16-1, numpy): (speed, rem_frac) -> log(dt)
     "ở tốc X với chồng còn Y%, nhịp lá ra sẽ là bao nhiêu"
  2. SLIP-MODEL (logistic):            (speed, rem_frac) -> P(trượt)
     học từ mẻ hỏng 600ms (len>120 = bóng che phình = chồng vảy/trượt)

Suy ra ĐƯỜNG CONG TỐC TỐI ƯU: với mỗi mức chồng còn lại, tốc nào cho nhịp
đúng đích mà P(trượt) < SLIP_CAP. Xuất weights JSON cho Pi inference.

Chạy: python3 train_speed_model.py  (in metrics + ghi speed_model.json)
"""
import csv, json, math
import numpy as np

rng = np.random.default_rng(42)
HERE = "/home/bbsw/CMD_PLAY_CARD/card_machine/ml_speed"
SLIP_CAP = 0.05          # trần xác suất trượt chấp nhận được
VAL_RUN = "v72_good"     # giữ mẻ này làm validation (không train)

# ── load ──────────────────────────────────────────────────────────────────────
rows = list(csv.DictReader(open(f"{HERE}/feed_dataset.csv")))
for r in rows:
    for k in ("dt", "tgt", "len", "card_n", "slip_flag"):
        r[k] = int(r[k])
    r["rem_frac"] = float(r["rem_frac"])

# mẫu sạch cho DT-model: lá đơn nhịp bình thường, bỏ startup ramp + outlier stall
clean = [r for r in rows if r["slip_flag"] == 0 and r["card_n"] > 25
         and 150 <= r["dt"] <= 2000 and r["tgt"] >= 220]
train = [r for r in clean if r["run"] != VAL_RUN]
val   = [r for r in clean if r["run"] == VAL_RUN]

def feats(rs):
    return np.array([[r["tgt"] / 500.0, r["rem_frac"]] for r in rs])

Xtr, Xva = feats(train), feats(val)
ytr = np.log(np.array([r["dt"] for r in train], dtype=float))
yva = np.log(np.array([r["dt"] for r in val], dtype=float))
mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
Xtr_n, Xva_n = (Xtr - mu) / sd, (Xva - mu) / sd

# ── DT-MODEL: MLP 2-16-16-1 (tanh), Adam ─────────────────────────────────────
H = 16
W1 = rng.normal(0, 0.5, (2, H)); b1 = np.zeros(H)
W2 = rng.normal(0, 0.5, (H, H)); b2 = np.zeros(H)
W3 = rng.normal(0, 0.5, (H, 1)); b3 = np.zeros(1)
params = [W1, b1, W2, b2, W3, b3]
mom = [np.zeros_like(p) for p in params]
vel = [np.zeros_like(p) for p in params]

def fwd(X, ps):
    w1, b1, w2, b2, w3, b3 = ps
    h1 = np.tanh(X @ w1 + b1)
    h2 = np.tanh(h1 @ w2 + b2)
    return (h2 @ w3 + b3).ravel(), (h1, h2)

lr, beta1, beta2, eps = 0.01, 0.9, 0.999, 1e-8
for it in range(1, 6001):
    yp, (h1, h2) = fwd(Xtr_n, params)
    err = (yp - ytr)[:, None]                     # N,1
    n = len(ytr)
    gW3 = h2.T @ err / n; gb3 = err.mean(0)
    d2 = (err @ params[4].T) * (1 - h2 ** 2)
    gW2 = h1.T @ d2 / n; gb2 = d2.mean(0)
    d1 = (d2 @ params[2].T) * (1 - h1 ** 2)
    gW1 = Xtr_n.T @ d1 / n; gb1 = d1.mean(0)
    grads = [gW1, gb1, gW2, gb2, gW3, gb3]
    for i, (p, g) in enumerate(zip(params, grads)):
        mom[i] = beta1 * mom[i] + (1 - beta1) * g
        vel[i] = beta2 * vel[i] + (1 - beta2) * g * g
        mh = mom[i] / (1 - beta1 ** it); vh = vel[i] / (1 - beta2 ** it)
        p -= lr * mh / (np.sqrt(vh) + eps)

yp_tr, _ = fwd(Xtr_n, params)
yp_va, _ = fwd(Xva_n, params)
mae_tr = float(np.mean(np.abs(np.exp(yp_tr) - np.exp(ytr))))
mae_va = float(np.mean(np.abs(np.exp(yp_va) - np.exp(yva))))
print(f"DT-MODEL  train n={len(train)} MAE={mae_tr:.0f}ms | VAL ({VAL_RUN}) n={len(val)} MAE={mae_va:.0f}ms")

# ── SLIP-MODEL: logistic (speed, rem_frac, speed×(1-rem)) ───────────────────
# LABEL SẠCH: sau khi chồng vảy bắt đầu (cascade) thì len phình ở MỌI tốc độ →
# nếu lấy hết sẽ học nhầm "chồng nhẹ = trượt bất kể tốc". Chỉ giữ CỬA SỔ KHỞI PHÁT
# của mẻ hỏng (lá 145-185) làm mẫu dương; bỏ vùng cascade (>185); negatives =
# mẫu sạch của TẤT CẢ các mẻ (đủ dải tốc 220-500).
slip_rows = [r for r in rows if r["card_n"] > 25 and r["tgt"] >= 220
             and not (r["run"] == "v71_fail600" and r["card_n"] > 185)]
Xs = np.array([[r["tgt"] / 500.0, r["rem_frac"], (r["tgt"] / 500.0) * (1 - r["rem_frac"])]
               for r in slip_rows])
ys = np.array([r["slip_flag"] for r in slip_rows], dtype=float)
mus, sds = Xs.mean(0), Xs.std(0) + 1e-9
Xs_n = (Xs - mus) / sds
w = np.zeros(3); b = 0.0
for _ in range(8000):
    p = 1 / (1 + np.exp(-(Xs_n @ w + b)))
    g = Xs_n.T @ (p - ys) / len(ys); gb = float((p - ys).mean())
    w -= 0.1 * g; b -= 0.1 * gb
p = 1 / (1 + np.exp(-(Xs_n @ w + b)))
acc = float(((p > 0.5) == (ys > 0.5)).mean())
print(f"SLIP-MODEL n={len(ys)} (slip={int(ys.sum())}) acc={acc:.2%}")

# ── PHYS-MODEL (deploy chính): dt = A(rem)/spd_n + B(rem), spd_n = speed/500 ──
#   Dạng vật lý ĐƠN ĐIỆU theo speed -> nghịch đảo công thức đóng, không lượn
#   sóng ngoài vùng data như MLP tự do (MLP giữ trong JSON để tham khảo).
U = np.array([[500.0 / r["tgt"], (500.0 / r["tgt"]) * r["rem_frac"], 1.0, r["rem_frac"]]
              for r in train])
yd = np.array([r["dt"] for r in train], dtype=float)
# RECENCY WEIGHTS: trạng thái máy (FF/EEPROM, mòn con lăn) thay đổi giữa các mẻ —
# ưu tiên mẻ GẦN NHẤT mô tả đúng máy hiện tại (v74 ×3, v72 ×2, cũ ×1)
RW = {"v75_model2": 4.0, "v74_model": 2.0, "v72_good": 1.5}
wts = np.sqrt(np.array([RW.get(r["run"], 1.0) for r in train]))
coef, *_ = np.linalg.lstsq(U * wts[:, None], yd * wts, rcond=None)
a0, a1, b0, b1 = [float(c) for c in coef]
Uv = np.array([[500.0 / r["tgt"], (500.0 / r["tgt"]) * r["rem_frac"], 1.0, r["rem_frac"]]
               for r in val])
mae_phys = float(np.mean(np.abs(Uv @ coef - np.array([r["dt"] for r in val], dtype=float))))
print(f"PHYS-MODEL dt=(a0+a1*rem)*500/spd+(b0+b1*rem): a0={a0:.1f} a1={a1:.1f} b0={b0:.1f} b1={b1:.1f} | VAL MAE={mae_phys:.0f}ms")

def dt_pred(speed, rem):
    return (a0 + a1 * rem) * 500.0 / speed + (b0 + b1 * rem)

def speed_for_dt(rem, dt_target):
    denom = dt_target - (b0 + b1 * rem)
    if denom <= 10:  # không đạt được -> trả trần
        return 9999
    return (a0 + a1 * rem) * 500.0 / denom

def slip_pred(speed, rem):
    x = (np.array([speed / 500.0, rem, (speed / 500.0) * (1 - rem)]) - mus) / sds
    return float(1 / (1 + np.exp(-(float(x @ w) + b))))

def safe_cap(rem):
    """Trần tốc AN TOÀN theo BẰNG CHỨNG vận hành sạch bền vững (không tin
    logistic ở vùng ít data): nặng grip khỏe chạy nhanh được, nhẹ phải chậm.
    Bao v2 (2026-07-02, sau mẻ model-driven): sustained-clean mới 424@nặng,
    420@giữa, 388@nhẹ → nới 360+90rem thành 395+55rem (rem1:450 = trần chống
    mờ camera theo kinh nghiệm v6.2; rem0:395 ≈ 388+margin nhỏ vì đã sustained)."""
    return 395.0 + 65.0 * max(0.0, min(1.0, rem))  # v3b: đỉnh 460 (chủ động tăng tốc, model giữ ổn định; 460 ≈ trần chống mờ)

def optimal_speed(rem, dt_target, lo=240, hi=520):
    safe_hi = min(hi, safe_cap(rem))
    spd = speed_for_dt(rem, dt_target)          # công thức đóng (đơn điệu)
    return round(max(lo, min(safe_hi, spd))), round(safe_hi)

print("\nĐƯỜNG CONG TỐC TỐI ƯU (dt đích 520ms, trần trượt 5%):")
print(f"{'còn lại':>8} {'tốc c/s':>8} {'trần an toàn':>12} {'dt dự đoán':>10} {'P(trượt)':>9}")
curve = []
for rem in np.arange(0.90, 0.04, -0.05):  # vùng có data; đầu mẻ firmware tự ramp
    spd, safe = optimal_speed(rem, 520)
    curve.append((round(rem, 2), spd))
    print(f"{rem:>8.2f} {spd:>8} {safe:>12} {dt_pred(spd, rem):>10.0f} {slip_pred(spd, rem):>9.2%}")

# ── export weights ────────────────────────────────────────────────────────────
model = {
    "meta": {"trained": "2026-07-02", "samples": len(rows), "clean": len(clean),
             "val_run": VAL_RUN, "val_mae_ms": round(mae_va, 1),
             "slip_cap": SLIP_CAP, "note": "dt=exp(MLP([spd/500,rem] norm)); slip=sigmoid(lin)"},
    "dt_mlp": {"mu": mu.tolist(), "sd": sd.tolist(),
               "W1": params[0].tolist(), "b1": params[1].tolist(),
               "W2": params[2].tolist(), "b2": params[3].tolist(),
               "W3": params[4].tolist(), "b3": params[5].tolist()},
    "phys": {"a0": a0, "a1": a1, "b0": b0, "b1": b1, "val_mae_ms": round(mae_phys, 1),
             "form": "dt = (a0+a1*rem)*500/speed + (b0+b1*rem)"},
    "slip_lr": {"mu": mus.tolist(), "sd": sds.tolist(), "w": w.tolist(), "b": float(b),
                "note": "advisory only — safe cap dùng đường bao bằng chứng"},
    "safe_cap": {"base": 395.0, "slope": 65.0, "note": "cap = base + slope*rem_frac (v3b, đỉnh 460 aggressive)"},
    "speed_curve_dt520": curve,
}
out = f"{HERE}/speed_model.json"
json.dump(model, open(out, "w"), indent=1)
print(f"\n→ weights: {out}")

"""
Virtual test (no printer hardware) that the Pi's QR printing matches the admin
logic: QR-ONLY, RESPONSIVE fill to the paper width, CRISP (integer dots/module),
for every printer size — the same output whether it goes out over Bluetooth
(escpos_file), WiFi thermal (escpos_net) or CUPS.

Run on the Pi:  ../.venv/bin/python test_pi_print_virtual.py
"""
import re
import sys

import printer

RUN_ID = "250d3bb4-b325-4c8a-b0b8-8fc806ef167d"
_fails = []
def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        _fails.append(name)

# 58mm=384, 80mm=576, tiny 40mm=320 dots — the widths escpos_net/file pass in
WIDTHS = {"58mm/384": 384, "80mm/576": 576, "40mm/320": 320}

print("[1] _qr_fill: RESPONSIVE fill + CRISP, adapts to every printer width")
for label, w in WIDTHS.items():
    img, box = printer._qr_fill(RUN_ID, w)
    fill_pct = round(img.width / w * 100)
    check(f"{label}: square QR image", img.width == img.height)
    check(f"{label}: fills the width ({fill_pct}% of {w} dots)", 85 <= fill_pct <= 100)
    check(f"{label}: never exceeds paper width", img.width <= w)
    check(f"{label}: crisp — integer dots/module ≥3 (box={box})", box >= 3)

print("[2] bigger paper -> bigger QR (adapts to printer size, not fixed)")
w58 = printer._qr_fill(RUN_ID, 384)[0].width
w80 = printer._qr_fill(RUN_ID, 576)[0].width
check(f"80mm QR ({w80}) larger than 58mm QR ({w58})", w80 > w58)

print("[3] _compose_page (CUPS path): QR ONLY, no id/text")
page = printer._compose_page(RUN_ID)
check("compose_page returns a square QR (no extra text band)", page.width == page.height)

def black_span(img):
    """(leftmost, rightmost) x with a black pixel — for measuring centering."""
    px = img.convert("L").load()
    W, H = img.size
    left = right = None
    for x in range(W):
        col_black = any(px[x, y] < 128 for y in range(0, H, max(1, H // 60)))
        if col_black:
            if left is None:
                left = x
            right = x
    return left, right

print("[4] CENTERING (pixel-measured): QR dead-centre on the REAL paper width")
for label, w in WIDTHS.items():
    img, _ = printer._qr_fill(RUN_ID, w)
    canvas = printer._center_on_width(img, w)
    check(f"{label}: canvas is exactly paper width ({canvas.width} dots, /8)",
          canvas.width == (w - w % 8) and canvas.width % 8 == 0)
    left, right = black_span(canvas)
    lmar, rmar = left, canvas.width - 1 - right
    check(f"{label}: QR not clipped (black inside 0..{canvas.width-1})",
          left is not None and left >= 0 and right <= canvas.width - 1)
    check(f"{label}: CENTERED — left margin {lmar} ≈ right margin {rmar}",
          abs(lmar - rmar) <= 1)

print("[5] source check: BOTH thermal backends pre-centre on real width (no center=True)")
src = open("printer.py", encoding="utf-8").read()
for cls in ("EscposNetPrinter", "EscposFilePrinter"):
    m = re.search(r"class %s\b.*?(?=\nclass |\Z)" % cls, src, re.S)
    body = m.group(0) if m else ""
    check(f"{cls}: uses _qr_fill (responsive fill)", "_qr_fill(" in body)
    check(f"{cls}: centres via _center_on_width", "image(_center_on_width(" in body)
    # the actual bad CALL is gone (a comment may still mention center=True)
    check(f"{cls}: no p.image(..center=True) call", "image(img, center=True)" not in body)

print("[6] render CENTERED canvases for visual proof (58mm + 80mm)")
for label, w in (("58", 384), ("80", 576)):
    img, _ = printer._qr_fill(RUN_ID, w)
    canvas = printer._center_on_width(img, w)
    out = f"/tmp/qrc_{label}mm.png"
    canvas.save(out)
    print(f"    saved {out}  (canvas {canvas.width}x{canvas.height}px, QR {img.width}px centred)")

print()
if _fails:
    print("RESULT: FAIL (%d) -> %s" % (len(_fails), ", ".join(_fails)))
    sys.exit(1)
print("RESULT: ALL PASS")

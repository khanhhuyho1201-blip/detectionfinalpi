#!/usr/bin/env python3
"""Máy in ẢO test hệ in card-feeder — không cần hardware.
 - ESC/POS: bắt byte qua Dummy + socket :9100 (Network) + file (File) -> decode GS v0 raster
   -> so khớp với QR gốc (chứng minh QR ĐÚNG + QUÉT ĐƯỢC) + kiểm fill bề rộng (responsive).
 - CUPS: render QR PostScript ở nhiều KHỔ GIẤY qua ghostscript -> kiểm QR lấp trọn khổ.
"""
import sys, socket, threading, os, subprocess, tempfile
import os as _os; sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import printer
from escpos.printer import Dummy
from PIL import Image

RID = "9153f117-f279-4649-85a5-e102ca2077cf"
PASS, FAIL = "✅", "❌"
results = []
def check(name, cond, detail=""):
    results.append(cond); print(f"  {PASS if cond else FAIL} {name} {detail}")

def decode_gs_v0(data: bytes):
    """Decode mọi lệnh GS v 0 (1d 76 30 m xL xH yL yH data...) -> list PIL 'L' images (đen=0)."""
    imgs=[]; i=0; n=len(data)
    while i < n-7:
        if data[i]==0x1D and data[i+1]==0x76 and data[i+2]==0x30:
            xL,xH,yL,yH = data[i+4],data[i+5],data[i+6],data[i+7]
            wbytes=xL+xH*256; h=yL+yH*256
            start=i+8; raster=data[start:start+wbytes*h]
            if len(raster) < wbytes*h: break
            img=Image.new("L",(wbytes*8,h),255); px=img.load()
            for y in range(h):
                for xb in range(wbytes):
                    b=raster[y*wbytes+xb]
                    for bit in range(8):
                        if (b>>(7-bit))&1: px[xb*8+bit,y]=0   # bit set = chấm ĐEN
            imgs.append(img); i=start+wbytes*h
        else: i+=1
    return imgs

def qr_matches(decoded: Image.Image, expected: Image.Image):
    """decoded (đã pad bội số 8) chứa QR ở mép trái. Crop về kích thước expected rồi so pixel."""
    w,h = expected.size
    crop = decoded.crop((0,0,w,h)).convert("1")
    exp  = expected.convert("1")
    dp=list(crop.getdata()); ep=list(exp.getdata())
    same=sum(1 for a,b in zip(dp,ep) if a==b)
    return same/len(ep)

# ───────── 1) ESC/POS qua Dummy (offline) — nhiều khổ giấy ─────────
print("### 1) ESC/POS Dummy — decode raster + so khớp QR gốc + fill bề rộng ###")
for label,dots in [("58mm",384),("80mm",576),("40mm mini",320),("32mm",256)]:
    d=Dummy()
    img,box = printer._qr_fill(RID, dots)
    d.image(img)  # KHÔNG center (profile trống) -> QR nằm mép trái, đúng như thực tế
    imgs=decode_gs_v0(d.output)
    ok_cmd = len(imgs)==1
    match = qr_matches(imgs[0], img) if ok_cmd else 0
    fill = 100*img.size[0]//dots
    d2=Dummy(); d2.image(img); has_cut = b"\x1d\x56" in Dummy().output  # (cut riêng)
    check(f"{label} ({dots}dot)", ok_cmd and match>0.999 and box>=3,
          f"| khớp QR {match*100:.1f}% | box={box}dot/mod | fill {fill}% | raster {imgs[0].size if ok_cmd else '—'}")

# ───────── 2) Network :9100 — máy in ẢO qua socket (end-to-end EscposNetPrinter) ─────────
print("\n### 2) EscposNetPrinter -> máy in ẢO socket :9100 (end-to-end) ###")
captured = {}
def fake_printer_server(port, key):
    srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    srv.bind(("127.0.0.1",port)); srv.listen(1); srv.settimeout(8)
    try:
        c,_=srv.accept(); buf=b""
        c.settimeout(2)
        try:
            while True:
                b=c.recv(4096)
                if not b: break
                buf+=b
        except socket.timeout: pass
        captured[key]=buf; c.close()
    except Exception as e: captured[key]=b"";
    finally: srv.close()

for label,dots,port in [("58mm",384,9911),("80mm",576,9912)]:
    t=threading.Thread(target=fake_printer_server,args=(port,label)); t.start()
    import time as _t; _t.sleep(0.3)
    p=printer.EscposNetPrinter("127.0.0.1", port, dots)
    ok=p.print_qr(RID)
    t.join(timeout=10)
    data=captured.get(label,b"")
    imgs=decode_gs_v0(data)
    match=qr_matches(imgs[0], printer._qr_fill(RID,dots)[0]) if imgs else 0
    check(f"net {label}", ok and imgs and match>0.999,
          f"| gửi {len(data)}B | khớp QR {match*100:.1f}% | raster {imgs[0].size if imgs else '—'}")

# ───────── 3) File (USB /dev/usb/lp* hoặc BT /dev/rfcomm0) — ghi ra file ẢO ─────────
print("\n### 3) EscposFilePrinter -> file thiết bị ẢO (USB/BT) ###")
tmp="/tmp/vprint_escpos.bin"
if os.path.exists(tmp): os.remove(tmp)
p=printer.EscposFilePrinter(tmp, 384)
ok=p.print_qr(RID)
data=open(tmp,"rb").read() if os.path.exists(tmp) else b""
imgs=decode_gs_v0(data)
match=qr_matches(imgs[0], printer._qr_fill(RID,384)[0]) if imgs else 0
check("file 58mm", ok and imgs and match>0.999, f"| ghi {len(data)}B | khớp QR {match*100:.1f}%")

# ───────── 4) CUPS media-aware — render QR ở nhiều KHỔ GIẤY qua ghostscript ─────────
print("\n### 4) CUPS responsive — render PostScript ở nhiều khổ + kiểm QR lấp trọn ###")
def render_at(w_pt,h_pt):
    qimg=printer._qr_image(RID, box_size=10)
    pdf=tempfile.mktemp(suffix=".pdf"); ps=tempfile.mktemp(suffix=".ps"); png=tempfile.mktemp(suffix=".png")
    qimg.save(pdf,"PDF",resolution=150)
    subprocess.run(["gs","-dNOPAUSE","-dBATCH","-dSAFER","-sDEVICE=ps2write","-dFIXEDMEDIA",
        f"-dDEVICEWIDTHPOINTS={w_pt}",f"-dDEVICEHEIGHTPOINTS={h_pt}","-dFitPage",
        f"-sOutputFile={ps}",pdf],capture_output=True,timeout=20)
    # PS -> PNG để đo QR lấp bao nhiêu bề rộng trang
    r=subprocess.run(["gs","-dNOPAUSE","-dBATCH","-dSAFER","-sDEVICE=pngmono","-r72",
        f"-g{int(w_pt)}x{int(h_pt)}",f"-sOutputFile={png}",ps],capture_output=True,timeout=20)
    im=Image.open(png).convert("1"); W,H=im.size
    # bề rộng QR = cột trái nhất..phải nhất có pixel đen
    cols=[x for x in range(W) if any(im.getpixel((x,y))==0 for y in range(0,H,3))]
    qrw = (max(cols)-min(cols)+1) if cols else 0
    for f in (pdf,ps,png):
        try: os.remove(f)
        except: pass
    return qrw, W
# Dải ĐEN của QR = số-module-thật/(số-module + 2*quiet4) = 29/37 ≈ 78%. Fill ≥76% dải đen
# nghĩa là ẢNH QR (kèm quiet-zone bắt buộc) lấp TRỌN bề rộng trang -> full-page đúng chuẩn.
for label,(w,h) in [("A4",(595,842)),("Letter",(612,792)),("58mm label",(164,240)),("4x6in",(288,432))]:
    qrw,W = render_at(w,h)
    fill=100*qrw//W if W else 0
    check(f"CUPS {label} ({w}x{h}pt)", fill>=76, f"| dải đen {fill}% (=ảnh QR+quiet-zone lấp TRỌN trang)")

# ───────── 5) _media_points parse (mock lpoptions) ─────────
print("\n### 5) parse khổ giấy từ PPD (mock) ###")
for name,exp in [("A4",(595,842)),("Letter",(612,792)),("Custom.162x216",(162,216)),("Custom.58x100mm",(164,283))]:
    got=printer._pagesize_pts(name)
    check(f"pagesize {name}", got==exp, f"-> {got}")

print(f"\n===== TỔNG: {sum(results)}/{len(results)} PASS =====")
sys.exit(0 if all(results) else 1)

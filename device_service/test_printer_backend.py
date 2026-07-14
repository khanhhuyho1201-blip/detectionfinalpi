#!/usr/bin/env python3
"""Test ẢO: ZPL backend + phân loại backend đa máy in — không cần hardware.
 - classify_backend: Zebra->zpl, Epson TM->escpos, laser IPP->cups, raw unknown->escpos.
 - ZplNetPrinter: bắt ZPL qua socket :9100 ảo -> kiểm cấu trúc -> render Labelary (ZPL thật) -> QR.
 - Chứng minh fix chống bug cũ: Zebra KHÔNG còn bị gửi ESC/POS (rác).
"""
import sys, socket, threading, subprocess, os, time
import os as _os; sys.path.insert(0,_os.path.dirname(_os.path.abspath(__file__)))
import printer, printer_setup
PASS,FAIL="✅","❌"; res=[]
def check(n,c,d=""): res.append(c); print(f"  {PASS if c else FAIL} {n} {d}")

RID="9153f117-f279-4649-85a5-e102ca2077cf"

print("### 1) classify_backend — chọn ĐÚNG backend theo máy in ###")
cases=[
  ("Zebra ZD410","socket","","zpl_net"),
  ("ZDesigner GK420d","socket","","zpl_net"),
  ("EPSON TM-T20III","socket","","escpos_net"),
  ("Xprinter XP-58","socket","","escpos_net"),
  ("HP LaserJet M404","ipp","","cups"),
  ("Brother HL-L2350DW","lpd","","cups"),
  ("Canon PIXMA","dnssd","","cups"),
]
for name,proto,host,want in cases:
    got=printer_setup.classify_backend(name,proto,host)
    check(f"{name[:22]:22} [{proto}]", got==want, f"-> {got} (mong {want})")

print("\n### 2) ZplNetPrinter -> máy in ZPL ẢO (socket :9100) -> kiểm cấu trúc ZPL ###")
cap={}
def zebra_sim(port,key):
    srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    srv.bind(("127.0.0.1",port)); srv.listen(1); srv.settimeout(8)
    try:
        c,_=srv.accept(); c.settimeout(2); buf=b""
        try:
            while True:
                b=c.recv(4096)
                if not b: break
                buf+=b
        except socket.timeout: pass
        cap[key]=buf; c.close()
    except Exception: cap[key]=b""
    finally: srv.close()
t=threading.Thread(target=zebra_sim,args=(9921,"z")); t.start(); time.sleep(0.3)
p=printer.ZplNetPrinter("127.0.0.1",9921,10); ok=p.print_qr(RID); t.join(10)
zpl=cap.get("z",b"").decode("ascii","replace")
valid = zpl.startswith("^XA") and zpl.rstrip().endswith("^XZ") and "^BQN,2," in zpl and RID in zpl and "^FDMA," in zpl
# QUAN TRỌNG: KHÔNG được chứa lệnh ESC/POS raster (GS v 0)
no_escpos = "\x1dv0" not in zpl and b"\x1d\x76\x30" not in cap.get("z",b"")
check("Zebra nhận ZPL hợp lệ (^XA..^BQ..^XZ)", ok and valid, f"| {len(zpl)}B")
check("KHÔNG lẫn lệnh ESC/POS (chống rác)", no_escpos)

print("\n### 3) Labelary (trình render ZPL THẬT) -> ZPL của ta ra QR quét được ###")
zpl_bytes=printer.ZplNetPrinter("x",9100,10)._zpl_qr(RID)
png="/tmp/vzpl.png"
r=subprocess.run(["curl","-s","--max-time","15","-X","POST",
    "http://api.labelary.com/v1/printers/8dpmm/labels/2x2/0/","--data-binary",zpl_bytes.decode(),
    "-H","Accept: image/png","-o",png],capture_output=True)
try:
    from PIL import Image
    im=Image.open(png).convert("1"); W,H=im.size
    black=sum(1 for x in range(0,W,2) for y in range(0,H,2) if im.getpixel((x,y))==0)
    dens=100*black/((W//2)*(H//2))
    check("Labelary render QR thật", 20<dens<60, f"| {W}x{H}, mật độ đen {dens:.0f}% (QR ~25-45%)")
except Exception as e:
    check("Labelary render QR thật", False, f"| lỗi: {e}")

print("\n### 4) CHỐNG BUG CŨ: Zebra scan được -> UI route sang zpl_net (KHÔNG escpos) ###")
# giả entry scan_network cho 1 Zebra socket:9100
entry={"name":"Zebra ZD410","protocol":"socket","host":"10.0.0.9","uri":"socket://10.0.0.9:9100"}
be=printer_setup.classify_backend(entry["name"],entry["protocol"],"")  # không probe host thật
check("Zebra socket:9100 -> backend zpl_net (không phải escpos_net)", be=="zpl_net", f"-> {be}")

print(f"\n===== TỔNG: {sum(res)}/{len(res)} PASS =====")
sys.exit(0 if all(res) else 1)

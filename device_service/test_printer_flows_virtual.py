#!/usr/bin/env python3
"""Test ẢO 3 LUỒNG máy in với driver THẬT — không cần máy in vật lý.

  USB  : queue CUPS-PDF (cups-pdf:/) -> chạy TRỌN chuỗi driver CUPS (PNG->PS->ghostscript
         ->PDF) -> pdftoppm -> zbarimg GIẢI MÃ QR phải ra đúng run_id.
  WiFi : (a) raw socket 9100 (thermal) — bắt byte ESC/POS raster + lệnh cắt;
         (b) IPP driverless (ippeveprinter = máy in mạng AirPrint thật trên localhost).
  BT   : đường ghi file/serial (EscposFilePrinter) qua pty ảo — bắt raster + cắt.
         (Pair/bind rfcomm thật cần hardware — đã thống nhất test sau.)

  + luật chọn máy: queue ảo cups-pdf KHÔNG được cướp ưu tiên của USB cắm trực tiếp.
"""
import os, sys, glob, json, subprocess, tempfile, time, socket, threading, pty

sys.path.insert(0, "/home/bbsw/workspace/detectionfinalpi/device_service")
RID = "9153f117-f279-4649-85a5-e102ca2077cf"
res = []
def check(name, cond, extra=""):
    res.append(bool(cond)); print(f"  {'✅' if cond else '❌'} {name} {extra}")

import printer

# ─────────────────────────────────────────────────────────────
print("### LUỒNG USB / DRIVER-CHAIN — CUPS-PDF (in thật qua driver, giải mã QR) ###")
OUT = os.path.expanduser("~/PDF")     # Debian cups-pdf.conf: Out ${HOME}/PDF
os.makedirs(OUT, exist_ok=True)
# tự dựng queue VPDF nếu chưa có (máy mới chạy test là có ngay)
if subprocess.run(["lpstat", "-v", "VPDF"], capture_output=True).returncode != 0:
    subprocess.run(["lpadmin", "-p", "VPDF", "-E", "-v", "cups-pdf:/",
                    "-m", "lsb/usr/cups-pdf/CUPS-PDF_opt.ppd"], capture_output=True, timeout=30)
before = set(glob.glob(OUT + "/*.pdf"))
p = printer.CupsPrinter(cups_name="VPDF")
ok = p.print_qr(RID)
check("CupsPrinter(VPDF).print_qr -> True", ok is True)
pdf = None
for _ in range(30):                       # chờ cups-pdf ghi file (tối đa 15s)
    new = set(glob.glob(OUT + "/*.pdf")) - before
    if new: pdf = sorted(new)[-1]; break
    time.sleep(0.5)
check("PDF được tạo ra", pdf is not None, f"-> {pdf}")
decoded = ""
if pdf:
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-png", "-r", "150", pdf, td + "/pg"],
                       capture_output=True, timeout=60)
        pngs = sorted(glob.glob(td + "/pg*.png"))
        check("PDF render được ảnh", len(pngs) >= 1, f"-> {len(pngs)} trang")
        if pngs:
            r = subprocess.run(["zbarimg", "--quiet", pngs[0]],
                               capture_output=True, text=True, timeout=60)
            decoded = r.stdout.strip().replace("QR-Code:", "")
check("GIẢI MÃ QR từ bản in == run_id", decoded == RID, f"-> {decoded[:40]!r}")

# ─────────────────────────────────────────────────────────────
print("\n### LUỒNG WIFI (a) — thermal raw socket 9100 (bắt byte thật) ###")
buf = bytearray(); done = threading.Event()
def srv():
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 9455)); s.listen(1); s.settimeout(10)
    try:
        c, _ = s.accept(); c.settimeout(5)
        while True:
            b = c.recv(65536)
            if not b: break
            buf.extend(b)
    except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
        s.close(); done.set()
threading.Thread(target=srv, daemon=True).start(); time.sleep(0.3)
np = printer.EscposNetPrinter("127.0.0.1", 9455, 576)
ok = np.print_qr(RID); done.wait(12)
check("EscposNetPrinter.print_qr -> True", ok is True)
check("nhận được dữ liệu qua mạng", len(buf) > 5000, f"-> {len(buf)} bytes")
check("có ESC/POS raster (GS v 0)", b"\x1dv0" in bytes(buf) or b"\x1d\x76\x30" in bytes(buf))
check("có lệnh CẮT cuối phiếu", b"\x1dV" in bytes(buf) or b"\x1bi" in bytes(buf) or b"\x1bm" in bytes(buf))

# ─────────────────────────────────────────────────────────────
print("\n### LUỒNG WIFI (b) — IPP DRIVERLESS (ippeveprinter = máy in mạng thật) ###")
SPOOL = os.environ.get("IPPSPOOL", "/tmp/ippspool"); os.makedirs(SPOOL, exist_ok=True)
ipp = subprocess.Popen(["ippeveprinter", "-p", "8631", "-d", SPOOL, "-k", "VirtualIPP"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2.5)
try:
    r = subprocess.run(["lpadmin", "-p", "VIPP", "-E", "-v",
                        "ipp://localhost:8631/ipp/print", "-m", "everywhere"],
                       capture_output=True, text=True, timeout=40)
    check("tạo queue driverless (-m everywhere)", r.returncode == 0, (r.stderr or "").strip()[:60])
    before = set(glob.glob(SPOOL + "/*"))
    ok = printer.CupsPrinter(cups_name="VIPP").print_qr(RID)
    check("CupsPrinter(VIPP).print_qr -> True", ok is True)
    doc = None
    for _ in range(30):
        new = [f for f in set(glob.glob(SPOOL + "/*")) - before if os.path.getsize(f) > 1000]
        if new: doc = sorted(new)[-1]; break
        time.sleep(0.5)
    check("máy in IPP ảo NHẬN được tài liệu", doc is not None,
          f"-> {os.path.basename(doc) if doc else '?'} ({os.path.getsize(doc) if doc else 0}B)")
finally:
    ipp.terminate()
    subprocess.run(["lpadmin", "-x", "VIPP"], capture_output=True)

# ─────────────────────────────────────────────────────────────
print("\n### LUỒNG BLUETOOTH — đường ghi serial/file (pty ảo) + tool BT ###")
m, s = pty.openpty(); sl = os.ttyname(s)
cap = bytearray(); stop = threading.Event()
def rd():
    os.set_blocking(m, False)
    t0 = time.time()
    while not stop.is_set() and time.time() - t0 < 10:
        try: cap.extend(os.read(m, 65536))
        except (BlockingIOError, OSError): time.sleep(0.05)
threading.Thread(target=rd, daemon=True).start()
bp = printer.EscposFilePrinter(sl, 576)          # như /dev/rfcomm0 sau khi bind
ok = bp.print_qr(RID); time.sleep(1.5); stop.set()
check("EscposFilePrinter(pty~rfcomm).print_qr -> True", ok is True)
check("bytes tới 'cổng BT'", len(cap) > 5000, f"-> {len(cap)} bytes")
check("raster + cắt trong luồng BT", (b"\x1dv0" in bytes(cap) or b"\x1d\x76\x30" in bytes(cap))
      and (b"\x1dV" in bytes(cap) or b"\x1bi" in bytes(cap)))
for tool in ("bluetoothctl", "rfcomm"):
    check(f"tool {tool} sẵn sàng", subprocess.run(["which", tool], capture_output=True).returncode == 0)

# ─────────────────────────────────────────────────────────────
print("\n### LUẬT CHỌN MÁY: queue ảo cups-pdf KHÔNG cướp chỗ USB cắm trực tiếp ###")
q = printer._usb_cups_queue()
check("_usb_cups_queue bỏ qua cups-pdf:/ (chỉ nhận usb://)", q is None, f"-> {q}")
if os.path.exists("/dev/usb/lp0"):
    gp = printer.get_printer()
    check("get_printer (chưa config) -> vẫn USB Masung", isinstance(gp, printer.EscposFilePrinter)
          and getattr(gp, "_cut", "") == "legacy", f"-> {type(gp).__name__}")
else:
    print("  (bỏ qua: máy in USB không cắm)")

print(f"\n===== TỔNG: {sum(res)}/{len(res)} PASS =====")
sys.exit(0 if all(res) else 1)

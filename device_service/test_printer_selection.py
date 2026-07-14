#!/usr/bin/env python3
"""Test ẢO luồng CHỌN máy in (get_printer) — KHÔNG cần máy in thật.

Chốt đúng yêu cầu user 2026-07-14:
  • Đã REMOVE máy in WiFi -> QUÊN HOÀN TOÀN, KHÔNG bao giờ in ra queue WiFi/CUPS còn sót.
  • Cắm USB trực tiếp -> in ra USB.
  • KHÔNG remove -> nhớ qua mọi lần khởi động (printer.json).
  • Không có gì -> NullPrinter, KHÔNG in bừa.
"""
import os, sys, json, tempfile

# thư mục device cô lập TRƯỚC khi import settings (printer.json nằm ở đây)
_TMP = tempfile.mkdtemp(prefix="prnsel_")
os.environ["CARD_DEVICE_DIR"] = _TMP
os.environ.pop("CARD_FAKE_PRINTER", None)     # cần NullPrinter báo KHÔNG sẵn sàng
os.environ.pop("CARD_PRINTER_BACKEND", None)  # cần path env-override TẮT
sys.path.insert(0, "/home/bbsw/workspace/detectionfinalpi/device_service")

import printer, printer_setup
from settings import settings

CFG = str(settings.paths.printer_cfg)
res = []
def check(name, cond, extra=""):
    res.append(bool(cond)); print(f"  {'✅' if cond else '❌'} {name} {extra}")

def clear_cfg():
    try: os.remove(CFG)
    except FileNotFoundError: pass

def write_cfg(d):
    with open(CFG, "w") as f: json.dump(d, f)

# giữ hàm THẬT để test riêng bộ lọc disabled (mục I)
_real_usb_cups_queue = printer._usb_cups_queue

# ── monkeypatch điểm phần cứng: USB queue CUPS + /dev/usb/lp* + tồn tại device ──
_state = {"usb_queue": None, "usb_glob": [], "exists": set()}
printer._usb_cups_queue = lambda: _state["usb_queue"]
printer.glob.glob = lambda pat: list(_state["usb_glob"]) if pat.startswith("/dev/usb/lp") else []
_real_exists = os.path.exists
printer.os.path.exists = lambda p: (p in _state["exists"]) or _real_exists(p)
def set_hw(usb_queue=None, usb_glob=(), exists=()):
    _state["usb_queue"] = usb_queue
    _state["usb_glob"]  = list(usb_glob)
    _state["exists"]    = set(exists)

RID = "9153f117-f279-4649-85a5-e102ca2077cf"

print("### A) ĐÃ REMOVE (không cfg), KHÔNG USB -> QUÊN, không in bừa ###")
clear_cfg(); set_hw()
p = printer.get_printer()
check("get_printer -> NullPrinter", isinstance(p, printer.NullPrinter), f"-> {type(p).__name__}")
check("NullPrinter.is_available == False", p.is_available() is False)
check("NullPrinter.print_qr == False (KHÔNG in)", p.print_qr(RID) is False)

print("\n### B) ĐÃ REMOVE + có queue WiFi/mạng CÒN SÓT + KHÔNG USB -> KHÔNG in ra WiFi ###")
# _usb_cups_queue chỉ trả USB; queue mạng còn sót KHÔNG match -> vẫn NullPrinter (đây LÀ bug cũ)
clear_cfg(); set_hw(usb_queue=None, usb_glob=())
p = printer.get_printer()
check("KHÔNG rơi về CupsPrinter(queue WiFi sót)", not isinstance(p, printer.CupsPrinter), f"-> {type(p).__name__}")
check("=> NullPrinter (đã quên máy WiFi cũ)", isinstance(p, printer.NullPrinter))

print("\n### C) ĐÃ REMOVE + cắm USB thô (/dev/usb/lp0) -> in ra USB ###")
clear_cfg(); set_hw(usb_glob=["/dev/usb/lp0"], exists=["/dev/usb/lp0"])
p = printer.get_printer()
check("get_printer -> EscposFilePrinter", isinstance(p, printer.EscposFilePrinter), f"-> {type(p).__name__}")
check("in ra ĐÚNG /dev/usb/lp0", getattr(p, "_device", "") == "/dev/usb/lp0")
check("USB sẵn sàng (available)", p.is_available() is True)

print("\n### D) ĐÃ REMOVE + USB có driver CUPS (usb://) -> in qua CUPS queue USB ###")
clear_cfg(); set_hw(usb_queue="USB_HL2320", usb_glob=["/dev/usb/lp0"])
p = printer.get_printer()
check("get_printer -> CupsPrinter", isinstance(p, printer.CupsPrinter), f"-> {type(p).__name__}")
check("dùng ĐÚNG queue USB (không phải WiFi)", getattr(p, "_cups_name", "") == "USB_HL2320")

print("\n### E) CÓ cấu hình WiFi (escpos_net) -> nhớ, KỆ USB/queue sót (config THẮNG) ###")
write_cfg({"backend": "escpos_net", "name": "Epson-WiFi", "address": "10.9.9.9", "port": 9100})
set_hw(usb_queue="USB_HL2320", usb_glob=["/dev/usb/lp0"], exists=["/dev/usb/lp0"])
p = printer.get_printer()
check("config THẮNG USB -> EscposNetPrinter", isinstance(p, printer.EscposNetPrinter), f"-> {type(p).__name__}")
check("in ĐÚNG máy WiFi đã setup 10.9.9.9", getattr(p, "_host", "") == "10.9.9.9")

print("\n### F) 'reboot' (tiến trình mới) mà KHÔNG remove -> vẫn nhớ máy WiFi ###")
# printer.json vẫn còn từ E -> get_printer đọc lại từ file = nhớ qua khởi động
p2 = printer.get_printer()
check("vẫn nhớ config sau 'reboot'", isinstance(p2, printer.EscposNetPrinter) and getattr(p2, "_host", "") == "10.9.9.9")

print("\n### G) REMOVE (cups) -> xoá queue + quên printer.json HOÀN TOÀN ###")
write_cfg({"backend": "cups", "name": "Brother-WiFi", "cups_name": "Brother_WiFi"})
_removed = {"names": []}
printer_setup.cups_remove = lambda n: _removed["names"].append(n)   # ghi lại lời gọi lpadmin -x
printer_setup.remove_printer()
check("printer.json đã bị xoá", not os.path.exists(CFG))
check("đã gọi cups_remove ĐÚNG queue", _removed["names"] == ["Brother_WiFi"], f"-> {_removed['names']}")
set_hw()   # không USB
p = printer.get_printer()
check("sau remove -> NullPrinter (quên hoàn toàn)", isinstance(p, printer.NullPrinter), f"-> {type(p).__name__}")

print("\n### H) get_status BÁO THẬT máy nào sẽ in (source) ###")
clear_cfg(); set_hw(usb_glob=["/dev/usb/lp0"], exists=["/dev/usb/lp0"])
st = printer_setup.get_status()
check("USB trực tiếp: configured=False, available=True, source=usb",
      st["configured"] is False and st["available"] is True and st["source"] == "usb", f"-> {st['source']}")
clear_cfg(); set_hw()
st = printer_setup.get_status()
check("Không gì: available=False, source=none", st["available"] is False and st["source"] == "none", f"-> {st['source']}")
write_cfg({"backend": "escpos_net", "name": "Epson-WiFi", "address": "10.9.9.9"})
set_hw()
st = printer_setup.get_status()
check("Có config: configured=True, source=config, label=Epson-WiFi",
      st["configured"] is True and st["source"] == "config" and st["label"] == "Epson-WiFi")

print("\n### I) _usb_cups_queue: CHỈ queue USB đang BẬT, bỏ disabled + bỏ mạng (review fix) ###")
class _FakeRun:
    def __init__(self, out): self.stdout = out
def _mk_lpstat(vout, pout):
    def run(args, **kw):
        if args[:2] == ["lpstat", "-v"]: return _FakeRun(vout)
        if args[:2] == ["lpstat", "-p"]: return _FakeRun(pout)
        return _FakeRun("")
    return run
import subprocess as _sp
_orig_run = printer.subprocess.run
def with_lpstat(vout, pout):
    printer.subprocess.run = _mk_lpstat(vout, pout)
    try: return _real_usb_cups_queue()
    finally: printer.subprocess.run = _orig_run

# USB đang BẬT -> chọn
q = with_lpstat("device for HL2320: usb://Brother/HL-L2320D?serial=x\n",
                "printer HL2320 is idle.  enabled since ...\n")
check("USB enabled -> chọn queue", q == "HL2320", f"-> {q}")
# USB nhưng DISABLED (CUPS stop sau in hỏng / rút máy) -> BỎ (đây là lỗi CONFIRMED đã vá)
q = with_lpstat("device for HL2320: usb://Brother/HL-L2320D?serial=x\n",
                "printer HL2320 disabled since ... - reason unplugged\n")
check("USB DISABLED -> bỏ (không mở START cho máy chết)", q is None, f"-> {q}")
# queue MẠNG (dnssd/socket) enabled -> KHÔNG khớp (quên WiFi)
q = with_lpstat("device for EpsonWiFi: socket://10.9.9.9:9100\n",
                "printer EpsonWiFi is idle.  enabled since ...\n")
check("queue mạng -> KHÔNG khớp", q is None, f"-> {q}")
# IPP-over-USB loopback enabled -> chọn (máy USB driverless)
q = with_lpstat("device for HP_USB: ipp://localhost:60000/ipp/print\n",
                "printer HP_USB is idle.  enabled since ...\n")
check("ipp://localhost (IPP-over-USB) -> chọn", q == "HP_USB", f"-> {q}")

print(f"\n===== TỔNG: {sum(res)}/{len(res)} PASS =====")
clear_cfg()
sys.exit(0 if all(res) else 1)

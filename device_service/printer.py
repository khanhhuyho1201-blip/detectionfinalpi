"""
Printer abstraction for the Card Machine device.

Prints ONLY a QR code (encoding the full run id / UUID and nothing else) — no
title, no id text, no hint. Scanning it shows the session id as plain text. The
business logic talks to the `Printer` interface, so swapping the hardware later
(thermal ESC/POS, Brother label, …) only means adding another backend.

Backend priority:
  1. printer.json trong settings.paths.printer_cfg (written by the Printer Setup UI)
  2. settings.printer.backend — CARD_PRINTER_BACKEND (legacy fallback, default "cups")

Backends:
  cups        — PNG → CUPS lp  (laser / inkjet / label printers)
  escpos_net  — ESC/POS over TCP socket (WiFi thermal printers, port 9100)
  escpos_file — ESC/POS over /dev/usb/lp* or /dev/rfcomm0 (USB / BT)
"""

import glob
import logging
import os
import re
import socket
import subprocess
import tempfile
import time
from urllib.parse import unquote, urlparse

from settings import settings

logger = logging.getLogger("card_device.printer")

# CARD_FAKE_PRINTER=1  → bench/test mode: is_available() returns True and
# print_qr() logs + returns True without touching CUPS (mirrors FAKE_CAMERA).
FAKE_PRINTER = settings.fake.printer

# QR encodes the full run id and nothing else (no URL, no metadata) so a scan
# reveals only the id, which the admin then looks up on the web.


_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_TEST_TEXT = "Card Feeder — Test Print OK"


def _text_image(text: str):
    """Render a single line of text on a small canvas with DPI set so CUPS prints 1 page."""
    from PIL import Image, ImageDraw, ImageFont
    DPI = 150
    try:
        font = ImageFont.truetype(_FONT_PATH, 36)
    except Exception:
        font = ImageFont.load_default()
    tmp = Image.new("RGB", (1, 1), "white")
    bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0] + 60
    h = bbox[3] - bbox[1] + 60
    img = Image.new("RGB", (max(w, 300), max(h, 80)), "white")
    draw = ImageDraw.Draw(img)
    draw.text(((img.width - (bbox[2] - bbox[0])) // 2,
               (img.height - (bbox[3] - bbox[1])) // 2),
              text, fill="black", font=font)
    img.info["dpi"] = (DPI, DPI)
    return img


class Printer:
    """Interface every printer backend implements."""

    def is_available(self) -> bool:
        raise NotImplementedError

    def print_qr(self, run_id: str) -> bool:
        """Print a QR (full run_id). Returns True on success."""
        raise NotImplementedError

    def print_text(self, text: str) -> bool:
        """Print a single line of text. Returns True on success."""
        raise NotImplementedError


def _qr_image(text: str, box_size: int = 10):
    """Build a QR PIL image encoding exactly `text`."""
    import qrcode
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=4,
    )
    qr.add_data(text)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def _qr_fill(text: str, target_dots: int, border: int = 4, min_module_dots: int = 3):
    """RESPONSIVE QR: build a QR sized to FILL `target_dots` wide (paper width in dots).
    Integer dots/module -> crisp module edges (no interpolation blur). Returns
    (image, module_dots). module_dots < min_module_dots => paper too narrow to print a
    reliably-scannable QR at this width (caller should warn).
    Payload is a short UUID (~29 modules + quiet zone ~37), so even a 40mm/320-dot roll
    still yields ~7 dots/module — scannable. That's why fill-to-width is safe on tiny paper."""
    import qrcode
    probe = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=border)
    probe.add_data(text)
    probe.make(fit=True)
    total = probe.modules_count + 2 * border          # modules incl. quiet zone
    box = max(min_module_dots, int(target_dots) // total)   # integer dots/module -> fill width
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=box, border=border)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img, box


# ── responsive media size for CUPS: honour the printer's REAL paper, not hardcoded A4 ──
_PAGESIZE_PTS = {"A4": (595, 842), "Letter": (612, 792), "A5": (420, 595),
                 "A6": (298, 420), "Legal": (612, 1008), "A3": (842, 1191)}


def _pagesize_pts(name: str | None):
    """Map a CUPS PageSize name to (w_pt, h_pt). Handles Custom.WxH in pt or mm."""
    if not name:
        return None
    if name in _PAGESIZE_PTS:
        return _PAGESIZE_PTS[name]
    m = re.match(r"(?:Custom\.)?(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(mm)?$", name)
    if m:
        w, h = float(m.group(1)), float(m.group(2))
        if m.group(3) == "mm":
            w, h = w * 72.0 / 25.4, h * 72.0 / 25.4
        return (round(w), round(h))
    return None


def _media_points(printer_name: str):
    """(w_pt, h_pt) of the printer's DEFAULT media from its PPD, or None (raw queue/unknown).
    Lets the QR fill whatever paper the queue is set to (A4, label roll, 4x6…) instead of
    always A4. Raw queues (no PPD) return None -> caller falls back to A4 (sane for PS lasers)."""
    try:
        out = subprocess.run(["lpoptions", "-p", printer_name, "-l"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if line.startswith("PageSize") and ":" in line:
            opts = line.split(":", 1)[1].split()
            default = next((t[1:] for t in opts if t.startswith("*")), None)
            return _pagesize_pts(default)
    return None


def _compose_page(run_id: str):
    """QR only — no title, no id text, no hint.

    The QR encodes exactly `run_id`, so scanning it with a phone shows the
    session id as plain text. `box_size=10` + `border=4` gives a standard quiet
    zone; CUPS `fit-to-page` then scales this square up to fill the paper.
    """
    return _qr_image(run_id, box_size=10)


def _parse_job_id(lp_stdout: str):
    """'request id is Brother-123 (1 file(s))' -> 'Brother-123'."""
    m = re.search(r"request id is (\S+)", lp_stdout or "")
    return m.group(1) if m else None


_JOB_FAIL_MSGS = ("unavailable", "may not exist", "unable to", "offline", "check the",
                  "turned off", "cannot", "no such", "connection refused", "timed out")


def _wait_job(printer_name: str, job_id: str, timeout: int = 18) -> bool:
    """[review v28.3] lp-BÁO-THẬT: True nếu job IN XONG; False nếu máy in lỗi (mất kết nối / hết
    giấy / offline). Bắt qua: queue disabled + STATUS chi tiết của job (CUPS ghi 'printer may not
    exist or is unavailable...' khi backend không nối được — queue KHÔNG tự disable). Chậm-mà-ổn
    (job xếp hàng, không lỗi) -> timeout trả True (đừng báo oan máy in chậm)."""
    deadline = time.monotonic() + timeout
    bad = 0
    while time.monotonic() < deadline:
        try:
            ps = subprocess.run(["lpstat", "-p", printer_name],
                                capture_output=True, text=True, timeout=5).stdout.lower()
            if "disabled" in ps or "not accepting" in ps:
                logger.warning("print FAILED: queue %s disabled/rejecting (job %s)", printer_name, job_id)
                return False
        except Exception:
            pass
        try:
            comp = subprocess.run(["lpstat", "-W", "completed", "-o", printer_name],
                                  capture_output=True, text=True, timeout=5).stdout
            if job_id in comp:
                return True
            notc = subprocess.run(["lpstat", "-W", "not-completed", "-o", printer_name],
                                  capture_output=True, text=True, timeout=5).stdout
            if job_id not in notc:      # hết pending + không thấy completed -> in xong đã purge -> OK
                return True
            # job VẪN pending: đọc STATUS chi tiết -> có thông điệp lỗi kết nối/máy in?
            detail = subprocess.run(["lpstat", "-l", "-o", printer_name],
                                    capture_output=True, text=True, timeout=5).stdout.lower()
            if any(k in detail for k in _JOB_FAIL_MSGS):
                bad += 1
                if bad >= 3:            # lỗi ổn định ~2s -> máy in KHÔNG in được thật
                    logger.warning("print FAILED: máy in lỗi/không tới (job %s): %s",
                                   job_id, next((k for k in _JOB_FAIL_MSGS if k in detail), "?"))
                    return False
            else:
                bad = 0
        except Exception:
            pass
        time.sleep(0.6)
    return True   # timeout: job chậm-mà-không-lỗi (xếp hàng) -> chấp nhận, đừng báo oan


class CupsPrinter(Printer):
    """Print to an A4 (or any CUPS) printer via the `lp` command."""

    # cache kết quả probe mạng (ippfind/TCP tốn 2-3s) — TTL ngắn để vòng poll 5s nhẹ
    # TTL 3s (2026-07-03, cũ 10s): vòng poll kiosk 5s — cache 10s làm trạng thái
    # lệch pha (bật máy in xong vẫn đỏ tới ~15s). 3s → mỗi vòng poll probe tươi
    # → bật máy in là xanh trong ≤1 vòng poll. Probe khi máy thức chỉ ~0.03s.
    _NET_PROBE_TTL = 3.0
    _net_probe = (0.0, True)   # (timestamp, reachable)

    def __init__(self, cups_name: str | None = None, cups_uri: str | None = None):
        # cups_name from printer.json takes priority; fallback to first lpstat result
        self._cups_name = cups_name
        self._cups_uri = cups_uri or ""

    def _printer_name(self) -> str | None:
        if self._cups_name:
            return self._cups_name
        try:
            out = subprocess.run(
                ["lpstat", "-p"], capture_output=True, text=True, timeout=5
            ).stdout
        except Exception as e:
            logger.warning("lpstat failed: %s", e)
            return None
        for line in out.splitlines():
            if line.startswith("printer ") and "disabled" not in line:
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]
        return None

    def is_available(self) -> bool:
        if FAKE_PRINTER:
            return True
        if self._printer_name() is None:
            return False
        # BUG cũ: chỉ check tên trong config → máy in TẮT/rớt mạng vẫn báo
        # "connected" (CUPS giữ queue "idle" kể cả khi printer biến mất) → không
        # bao giờ hiện "Printer disconnected" và in QR cuối mẻ fail im lặng.
        # Fix: probe sự hiện diện THẬT trên mạng (mDNS + TCP 631), cache 10s.
        if settings.printer.assume_ok:   # van thoát khẩn cấp
            return True
        now = time.monotonic()
        ts, ok = CupsPrinter._net_probe
        if now - ts < CupsPrinter._NET_PROBE_TTL:
            return ok
        ok = self._net_reachable()
        CupsPrinter._net_probe = (now, ok)
        return ok

    def _net_reachable(self) -> bool:
        """True nếu máy in thật sự hiện diện. dnssd:// → ippfind (mDNS) rồi TCP
        cổng in; ipp/http/socket/lpd → TCP thẳng. Scheme khác (usb://...) →
        không probe được qua mạng, giữ hành vi cũ (True)."""
        # ƯU TIÊN URI thật của queue CUPS (lpstat -v): cups_add đã PHÂN GIẢI
        # hostname mDNS trần (vd lpd://BRW14AC604DD8C0/... → lpd://192.168.2.14/...)
        # — URI gốc trong printer.json có thể không resolve được từ Python
        # (không có nss-mdns cho tên trần) → probe fail oan → đèn đỏ giả.
        uri = ""
        try:
            out = subprocess.run(["lpstat", "-v"], capture_output=True,
                                 text=True, timeout=5).stdout
            name = self._printer_name() or ""
            for line in out.splitlines():
                if name and f"for {name}:" in line:
                    uri = line.split(":", 1)[1].strip()
                    break
        except Exception:
            pass
        if not uri:
            uri = self._cups_uri
        if not uri:
            return True   # không đọc được URI → đừng báo lỗi oan
        if uri.startswith("dnssd://"):
            instance = unquote(uri[len("dnssd://"):].split("/")[0])
            if not instance.endswith("."):
                instance += "."
            try:
                r = subprocess.run(["ippfind", instance, "-T", "3", "--print"],
                                   capture_output=True, text=True, timeout=8)
                resolved = (r.stdout or "").strip().splitlines()
                if r.returncode != 0 or not resolved:
                    return False              # không quảng bá trên mạng → mất máy in
                uri = resolved[0]             # ipp://host:631/... → probe TCP tiếp
            except FileNotFoundError:
                return True   # không có ippfind → giữ hành vi cũ
            except Exception:
                return False
        p = urlparse(uri)
        if p.scheme in ("ipp", "ipps", "http", "https", "socket", "lpd") and p.hostname:
            port = p.port or {"ipps": 631, "ipp": 631, "http": 80, "https": 443,
                              "socket": 9100, "lpd": 515}[p.scheme]
            # CHỐNG ĐÈN ĐỎ GIẢ (2026-07-03): máy in WiFi (Brother) ngủ tiết kiệm
            # điện → SYN đầu bị nuốt >2s → đỏ oan; chính probe đánh thức nó.
            # STATE-AWARE (nhạy hơn, cùng ngày): chỉ retry-đánh-thức khi ĐANG XANH
            # mà bỗng fail (nghi ngủ). Khi ĐANG ĐỎ sẵn (máy tắt hẳn) → probe nhanh
            # 1 phát/vòng, khỏi chờ+retry 6s — bật máy in lên là connect OK ngay
            # → xanh trong ≤1 vòng poll thay vì ~15s.
            _, was_ok = CupsPrinter._net_probe
            try:
                with socket.create_connection((p.hostname, port), timeout=2):
                    return True
            except Exception:
                if not was_ok:
                    return False          # đang đỏ sẵn — khỏi retry, đỡ kéo vòng poll
            time.sleep(1.0)               # đang xanh mà fail → nghi ngủ, cho nó dậy
            try:
                with socket.create_connection((p.hostname, port), timeout=3):
                    return True
            except Exception:
                return False
        return True

    def _lp_image(self, img, printer_name: str) -> bool:
        """PIL image → PDF → PostScript (via gs) → lp. 1 page guaranteed."""
        pdf_path = ps_path = None
        try:
            pdf_tmp = tempfile.NamedTemporaryFile(prefix="prn_", suffix=".pdf", delete=False)
            pdf_path = pdf_tmp.name
            pdf_tmp.close()
            img.save(pdf_path, "PDF", resolution=150)

            ps_tmp = tempfile.NamedTemporaryFile(prefix="prn_", suffix=".ps", delete=False)
            ps_path = ps_tmp.name
            ps_tmp.close()

            # PDF → PostScript, FitPage fills the sheet. RESPONSIVE: use the printer's REAL
            # media size (from its PPD) so the QR fills A4 / label roll / 4x6 / whatever the
            # queue is set to — not always A4. Raw queue (no PPD) -> A4 (sane for PS lasers).
            w_pt, h_pt = _media_points(printer_name) or (595, 842)
            gs = subprocess.run(
                ["gs", "-dNOPAUSE", "-dBATCH", "-dSAFER",
                 "-sDEVICE=ps2write", "-dFIXEDMEDIA",
                 f"-dDEVICEWIDTHPOINTS={w_pt}", f"-dDEVICEHEIGHTPOINTS={h_pt}",
                 "-dFitPage", f"-sOutputFile={ps_path}", pdf_path],
                capture_output=True, text=True, timeout=20
            )
            if gs.returncode != 0:
                logger.warning("gs failed: %s", gs.stdout[:200])
                return False

            res = subprocess.run(
                ["lp", "-d", printer_name, ps_path],
                capture_output=True, text=True, timeout=30
            )
            if res.returncode != 0:
                logger.warning("lp failed: %s", res.stderr.strip())
                return False
            # lp chỉ XẾP HÀNG -> trả về ngay dù máy in chưa in / lỗi. Poll trạng thái JOB thật:
            #   queue bị dừng/không nhận (hết giấy, lỗi) hoặc job bị huỷ -> báo THẤT BẠI (không "im lặng OK").
            job = _parse_job_id(res.stdout)
            if job:
                return _wait_job(printer_name, job, timeout=20)
            return True   # không đọc được job id -> giữ hành vi cũ, đừng báo lỗi oan
        except Exception as e:
            logger.exception("_lp_image failed: %s", e)
            return False
        finally:
            for p in (pdf_path, ps_path):
                if p:
                    try:
                        os.remove(p)
                    except Exception:
                        pass

    def print_qr(self, run_id: str) -> bool:
        if FAKE_PRINTER:
            logger.info("FAKE_PRINTER: simulated print for %s", run_id)
            return True
        name = self._printer_name()
        if not name:
            logger.warning("No CUPS printer available; cannot print")
            return False
        ok = self._lp_image(_compose_page(run_id), name)
        if ok:
            logger.info("Printed QR for %s → %s", run_id, name)
        return ok

    def print_text(self, text: str) -> bool:
        if FAKE_PRINTER:
            logger.info("FAKE_PRINTER: simulated cups text print: %s", text)
            return True
        name = self._printer_name()
        if not name:
            logger.warning("No CUPS printer available; cannot print text")
            return False
        return self._lp_image(_text_image(text), name)


class EscposNetPrinter(Printer):
    """ESC/POS over TCP socket — WiFi thermal printers (port 9100)."""

    def __init__(self, host: str, port: int = 9100, width_dots: int = 384):
        self._host = host
        self._port = port
        self._width = int(width_dots)   # bề rộng in (dot): 58mm=384, 80mm=576 — QR fill trọn

    def is_available(self) -> bool:
        try:
            s = socket.create_connection((self._host, self._port), timeout=3)
            s.close()
            return True
        except Exception:
            return False

    def print_qr(self, run_id: str) -> bool:
        if FAKE_PRINTER:
            logger.info("FAKE_PRINTER: simulated escpos_net print for %s", run_id)
            return True
        try:
            from escpos.printer import Network
            p = Network(self._host, self._port, timeout=15)
            img, box = _qr_fill(run_id, self._width)     # RESPONSIVE: fill paper width
            if box < 3:
                logger.warning("EscposNet: giấy %ddot quá hẹp cho QR quét được (%d dot/module)",
                               self._width, box)
            p.image(img, center=True)
            p.cut()
            p.close()
            logger.info("EscposNet printed QR for %s → %s:%s (%ddot, box=%d)",
                        run_id, self._host, self._port, self._width, box)
            return True
        except Exception as e:
            logger.exception("EscposNet print_qr failed: %s", e)
            return False

    def print_text(self, text: str) -> bool:
        if FAKE_PRINTER:
            logger.info("FAKE_PRINTER: simulated escpos_net text: %s", text)
            return True
        try:
            from escpos.printer import Network
            p = Network(self._host, self._port, timeout=15)
            p.set(align="center", bold=True, double_height=True, double_width=True)
            p.text(text + "\n")
            p.ln(3)
            p.cut()
            p.close()
            return True
        except Exception as e:
            logger.exception("EscposNet print_text failed: %s", e)
            return False


class ZplNetPrinter(Printer):
    """ZPL over TCP :9100 — Zebra label printers (ZD410/ZD420/GK420/ZDesigner…).
    Zebra KHÔNG hiểu ESC/POS raster (in ra rác) — ZPL là ngôn ngữ gốc của nó. Dùng lệnh QR
    gốc ^BQ ở magnification tối đa (10) -> QR to nhất ZPL cho phép (lấp nhãn nhỏ/vừa)."""

    def __init__(self, host: str, port: int = 9100, mag: int = 10):
        self._host = host
        self._port = port
        self._mag = max(1, min(10, int(mag)))   # ZPL QR magnification 1..10 (10 = lớn nhất)

    def is_available(self) -> bool:
        try:
            socket.create_connection((self._host, self._port), timeout=3).close()
            return True
        except Exception:
            return False

    def _zpl_qr(self, run_id: str) -> bytes:
        # ^BQN,2,<mag> = QR model 2 + magnification; ^FDMA,<data> = error-correction M + auto mode.
        # (đã verify render QR thật qua Labelary — trình render ZPL chuẩn.)
        return ("^XA\n^CI28\n^FO20,20\n^BQN,2,%d\n^FDMA,%s^FS\n^XZ\n"
                % (self._mag, run_id)).encode("ascii", "ignore")

    def print_qr(self, run_id: str) -> bool:
        if FAKE_PRINTER:
            logger.info("FAKE_PRINTER: simulated zpl_net print for %s", run_id)
            return True
        try:
            with socket.create_connection((self._host, self._port), timeout=15) as s:
                s.sendall(self._zpl_qr(run_id))
            logger.info("ZplNet printed QR for %s → %s:%s", run_id, self._host, self._port)
            return True
        except Exception as e:
            logger.exception("ZplNet print_qr failed: %s", e)
            return False

    def print_text(self, text: str) -> bool:
        if FAKE_PRINTER:
            return True
        try:
            zpl = ("^XA\n^CI28\n^FO20,20^A0N,40,40^FD%s^FS\n^XZ\n" % text).encode("ascii", "ignore")
            with socket.create_connection((self._host, self._port), timeout=15) as s:
                s.sendall(zpl)
            return True
        except Exception as e:
            logger.exception("ZplNet print_text failed: %s", e)
            return False


def _ensure_rfcomm(device: str, mac: str) -> bool:
    """BT BỀN QUA REBOOT: rfcomm bind chỉ làm lúc pair -> sau reboot /dev/rfcomm0 MẤT. Ở đây tự
    bind LẠI từ MAC đã pair khi node thiếu (self-heal, khỏi cần systemd riêng). Thiết bị trusted
    -> connect + bind là node hiện lại, mở là in được. (rfcomm/bluetoothctl cần quyền — user ở
    group bluetooth/dialout trên Pi.)"""
    if not device.startswith("/dev/rfcomm"):
        return os.path.exists(device)
    if os.path.exists(device):
        return True
    if not mac:
        return False
    try:
        subprocess.run(["bluetoothctl", "connect", mac], capture_output=True, timeout=8)
    except Exception:
        pass
    try:
        subprocess.run(["rfcomm", "bind", device, mac, "1"], capture_output=True, timeout=8)
    except Exception:
        return False
    for _ in range(10):
        if os.path.exists(device):
            logger.info("RFCOMM re-bound %s -> %s (BT self-heal sau reboot)", device, mac)
            return True
        time.sleep(0.3)
    return os.path.exists(device)


class EscposFilePrinter(Printer):
    """ESC/POS over a device file — USB (/dev/usb/lp*) or BT RFCOMM (/dev/rfcomm0)."""

    def __init__(self, device: str, width_dots: int = 384, bt_mac: str = ""):
        self._device = device
        self._width = int(width_dots)   # bề rộng in (dot): 58mm=384, 80mm=576 — QR fill trọn
        self._mac = bt_mac or ""        # MAC BT (để tự bind lại RFCOMM sau reboot)

    def is_available(self) -> bool:
        if self._mac and self._device.startswith("/dev/rfcomm"):
            return _ensure_rfcomm(self._device, self._mac)
        return os.path.exists(self._device)

    def _heal(self):
        if self._mac and self._device.startswith("/dev/rfcomm"):
            _ensure_rfcomm(self._device, self._mac)

    def print_qr(self, run_id: str) -> bool:
        if FAKE_PRINTER:
            logger.info("FAKE_PRINTER: simulated escpos_file print for %s", run_id)
            return True
        self._heal()                                     # BT: bind lại RFCOMM nếu mất (sau reboot)
        try:
            from escpos.printer import File
            p = File(self._device, auto_cut=True)
            img, box = _qr_fill(run_id, self._width)     # RESPONSIVE: fill paper width
            if box < 3:
                logger.warning("EscposFile: giấy %ddot quá hẹp cho QR quét được (%d dot/module)",
                               self._width, box)
            p.image(img, center=True)
            p.cut()
            p.close()
            logger.info("EscposFile printed QR for %s → %s (%ddot, box=%d)",
                        run_id, self._device, self._width, box)
            return True
        except Exception as e:
            logger.exception("EscposFile print_qr failed: %s", e)
            return False

    def print_text(self, text: str) -> bool:
        if FAKE_PRINTER:
            logger.info("FAKE_PRINTER: simulated escpos_file text: %s", text)
            return True
        self._heal()                                     # BT: bind lại RFCOMM nếu mất (sau reboot)
        try:
            from escpos.printer import File
            p = File(self._device, auto_cut=True)
            p.set(align="center", bold=True, double_height=True, double_width=True)
            p.text(text + "\n")
            p.ln(3)
            p.cut()
            p.close()
            return True
        except Exception as e:
            logger.exception("EscposFile print_text failed: %s", e)
            return False


_BACKENDS = {"cups": CupsPrinter, "escpos_net": EscposNetPrinter,
             "escpos_file": EscposFilePrinter, "zpl_net": ZplNetPrinter}


def get_printer() -> Printer:
    """Return the printer backend.
    Priority: printer.json (Setup UI) → settings.printer.backend → CupsPrinter.
    """
    # 1. Setup-UI config file
    try:
        from printer_setup import load_cfg
        cfg = load_cfg()
        if cfg:
            backend = cfg.get("backend", "cups")
            w = int(cfg.get("width_dots", 384))          # bề rộng thermal: 58mm=384 mặc định
            if backend == "zpl_net":
                return ZplNetPrinter(cfg["address"], int(cfg.get("port", 9100)), int(cfg.get("mag", 10)))
            if backend == "escpos_net":
                return EscposNetPrinter(cfg["address"], int(cfg.get("port", 9100)), w)
            if backend in ("escpos_file", "escpos_bt"):
                return EscposFilePrinter(cfg.get("device", "/dev/usb/lp0"), w, cfg.get("bt_mac", ""))
            if backend == "cups":
                return CupsPrinter(cups_name=cfg.get("cups_name"),
                                   cups_uri=cfg.get("cups_uri"))
    except Exception as e:
        logger.debug("printer cfg load: %s", e)

    # 2. USB AUTO-DETECT (backend-only, KHÔNG hiển thị trên UI — theo yêu cầu anh):
    #    máy in USB thô cắm vào + CHƯA cấu hình gì + không có queue CUPS -> tự dùng ESC/POS file.
    #    CHỈ /dev/usb/lp* (usblp = máy in thật). TUYỆT ĐỐI KHÔNG đụng /dev/ttyUSB* (đó là Arduino
    #    card feeder!) — quét nhầm là hỏng máy. -> đường Arduino an toàn tuyệt đối.
    try:
        usb = sorted(glob.glob("/dev/usb/lp*"))
        if usb and CupsPrinter()._printer_name() is None:
            logger.info("USB printer auto-detected (no config, no CUPS queue) -> %s", usb[0])
            return EscposFilePrinter(usb[0])
    except Exception as e:
        logger.debug("usb auto-detect: %s", e)

    # 3. Legacy env var (settings.printer.backend)
    name = settings.printer.backend.lower()
    return _BACKENDS.get(name, CupsPrinter)()


# module-level singleton + convenience helpers
_printer: Printer | None = None


def _get() -> Printer:
    global _printer
    if _printer is None:
        _printer = get_printer()
    return _printer


def printer_available() -> bool:
    return _get().is_available()


def print_run_qr(run_id: str) -> bool:
    return _get().print_qr(run_id)


def print_text_line(text: str) -> bool:
    return _get().print_text(text)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) >= 2 and sys.argv[1] == "--check":
        print("printer available:", printer_available())
    elif len(sys.argv) >= 2 and sys.argv[1] == "--render":
        # render a sample page to a file without printing (for testing)
        rid = sys.argv[2] if len(sys.argv) > 2 else "9153f117-f279-4649-85a5-e102ca2077cf"
        _compose_page(rid).save("/tmp/qr_sample.png")
        print("rendered /tmp/qr_sample.png")
    else:
        rid = sys.argv[1] if len(sys.argv) > 1 else "test-run-id"
        print("printed:", print_run_qr(rid))

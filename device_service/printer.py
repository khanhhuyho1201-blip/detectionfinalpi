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

import logging
import os
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


def _compose_page(run_id: str):
    """QR only — no title, no id text, no hint.

    The QR encodes exactly `run_id`, so scanning it with a phone shows the
    session id as plain text. `box_size=10` + `border=4` gives a standard quiet
    zone; CUPS `fit-to-page` then scales this square up to fill the paper.
    """
    return _qr_image(run_id, box_size=10)


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

            # PDF → A4 PostScript (Brother printers speak PS natively)
            gs = subprocess.run(
                ["gs", "-dNOPAUSE", "-dBATCH", "-dSAFER",
                 "-sDEVICE=ps2write", "-dFIXEDMEDIA",
                 "-dDEVICEWIDTHPOINTS=595", "-dDEVICEHEIGHTPOINTS=842",
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
            ok = res.returncode == 0
            if not ok:
                logger.warning("lp failed: %s", res.stderr.strip())
            return ok
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

    def __init__(self, host: str, port: int = 9100):
        self._host = host
        self._port = port

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
            p.image(_compose_page(run_id), center=True)
            p.cut()
            p.close()
            logger.info("EscposNet printed QR for %s → %s:%s", run_id, self._host, self._port)
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


class EscposFilePrinter(Printer):
    """ESC/POS over a device file — USB (/dev/usb/lp*) or BT RFCOMM (/dev/rfcomm0)."""

    def __init__(self, device: str):
        self._device = device

    def is_available(self) -> bool:
        return os.path.exists(self._device)

    def print_qr(self, run_id: str) -> bool:
        if FAKE_PRINTER:
            logger.info("FAKE_PRINTER: simulated escpos_file print for %s", run_id)
            return True
        try:
            from escpos.printer import File
            p = File(self._device, auto_cut=True)
            p.image(_compose_page(run_id), center=True)
            p.cut()
            p.close()
            logger.info("EscposFile printed QR for %s → %s", run_id, self._device)
            return True
        except Exception as e:
            logger.exception("EscposFile print_qr failed: %s", e)
            return False

    def print_text(self, text: str) -> bool:
        if FAKE_PRINTER:
            logger.info("FAKE_PRINTER: simulated escpos_file text: %s", text)
            return True
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


_BACKENDS = {"cups": CupsPrinter, "escpos_net": EscposNetPrinter, "escpos_file": EscposFilePrinter}


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
            if backend == "escpos_net":
                return EscposNetPrinter(cfg["address"], int(cfg.get("port", 9100)))
            if backend in ("escpos_file", "escpos_bt"):
                return EscposFilePrinter(cfg.get("device", "/dev/usb/lp0"))
            if backend == "cups":
                return CupsPrinter(cups_name=cfg.get("cups_name"),
                                   cups_uri=cfg.get("cups_uri"))
    except Exception as e:
        logger.debug("printer cfg load: %s", e)

    # 2. Legacy env var (settings.printer.backend)
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

"""
server.py — local web server for the kiosk UI.

Serves the casino UI (web/index.html), a JSON state endpoint the page polls, the
action endpoints the buttons call, and an MJPEG camera stream. One Controller
instance drives the machine; Chromium (kiosk.sh) points at this on localhost.
"""

import io
import logging
import os
import time

from flask import Flask, Response, jsonify, request

from controller import Controller
from settings import settings

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("card_web")

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
HOST = settings.web.host
PORT = settings.web.port

app = Flask(__name__)
ctrl = Controller()

# a dark 16:9 placeholder shown by the MJPEG stream when nothing is recording
def _placeholder():
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (640, 360), (8, 8, 11)).save(buf, "JPEG", quality=60)
        return buf.getvalue()
    except Exception:
        return b""
PLACEHOLDER = _placeholder()


@app.route("/")
def index():
    # Serve as an explicit text/html body. send_from_directory adds a
    # Content-Disposition/conditional-response that made Chromium render the
    # page as plain source text on the kiosk; returning the bytes directly
    # with a clean text/html mimetype renders normally.
    with open(os.path.join(WEB, "index.html"), "r", encoding="utf-8") as f:
        resp = Response(f.read(), mimetype="text/html")
    # no-cache: trình duyệt (Chrome ngoài) LUÔN nạp bản mới nhất, khỏi phải
    # hard-refresh sau mỗi lần cập nhật UI. HTML nhỏ nên tải lại không đáng kể.
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


_last_poll = [time.monotonic()]


@app.route("/api/state")
def state():
    _last_poll[0] = time.monotonic()      # the page is alive (for the kiosk watchdog)
    return jsonify(ctrl.snapshot())


@app.route("/api/_alive")
def alive():
    # 200 if the page polled recently; 503 if it went silent (renderer crashed)
    age = time.monotonic() - _last_poll[0]
    return ("ok", 200) if age < 30 else ("stale", 503)


@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    # JS sends this when /api/state is unreachable so the kiosk watchdog knows
    # the renderer is alive — only the server is restarting, not the tab crashed.
    _last_poll[0] = time.monotonic()
    return ("ok", 200)


@app.route("/api/start", methods=["POST"])
def start():
    return jsonify({"ok": ctrl.start()})


@app.route("/api/home", methods=["POST"])
def api_home():
    # v28.3: user bấm nút HOME/RESET -> stepper leo về chạm công tắc top (thủ công, không tự động)
    return jsonify({"ok": ctrl.home()})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    return jsonify({"ok": ctrl.reset()})

@app.route("/api/cancel", methods=["POST"])
def cancel():
    return jsonify({"ok": ctrl.cancel()})


@app.route("/api/dismiss", methods=["POST"])
def dismiss():
    """Bấm OK trên màn lỗi → về Ready (không start ngay — chờ người sắp bài)."""
    return jsonify({"ok": ctrl.dismiss_error()})


@app.route("/api/retry", methods=["POST"])
def retry():
    return jsonify({"ok": ctrl.retry()})


@app.route("/api/log", methods=["POST"])
def log():
    return jsonify({"ok": ctrl.send_log()})


@app.route("/api/history")
def history():
    return jsonify(ctrl.history())


@app.route("/api/print", methods=["POST"])
def print_qr():
    d = request.get_json(force=True, silent=True) or {}
    return jsonify({"ok": ctrl.print_qr(d.get("run_id", ""))})


@app.route("/api/print_prompt/ack", methods=["POST"])
def print_prompt_ack():
    # UI đã xử lý popup hỏi in QR (đồng ý/từ chối) — xóa prompt khỏi state
    return jsonify({"ok": ctrl.ack_print_prompt()})


# ── Printer Setup API ─────────────────────────────────────────────────────────

@app.route("/api/printer/status")
def printer_status():
    import printer_setup
    return jsonify(printer_setup.get_status())


@app.route("/api/printer/scan/usb")
def printer_scan_usb():
    import printer_setup
    return jsonify({"printers": printer_setup.scan_usb()})


@app.route("/api/printer/scan/network")
def printer_scan_network():
    import printer_setup
    return jsonify({"printers": printer_setup.scan_network()})


@app.route("/api/printer/manual", methods=["POST"])
def printer_manual():
    # NHẬP IP TAY: gõ IP/host máy in mạng không auto-discover -> dò cổng + phân loại backend
    import printer_setup
    d = request.get_json(force=True, silent=True) or {}
    return jsonify(printer_setup.manual_entry(d.get("address", "")))


@app.route("/api/printer/scan/bt", methods=["POST"])
def printer_scan_bt():
    import printer_setup
    devices = printer_setup.scan_bt(timeout=12)
    return jsonify({"devices": devices})


@app.route("/api/printer/bt/pair", methods=["POST"])
def printer_bt_pair():
    import printer_setup
    d = request.get_json(force=True, silent=True) or {}
    mac = d.get("mac", "").strip()
    if not mac:
        return jsonify({"ok": False, "error": "mac required"})
    return jsonify(printer_setup.bt_pair(mac))


@app.route("/api/printer/add", methods=["POST"])
def printer_add():
    import printer_setup
    cfg = request.get_json(force=True, silent=True) or {}
    if not cfg.get("backend"):
        return jsonify({"ok": False, "error": "backend required"})
    return jsonify(printer_setup.add_printer(cfg))


@app.route("/api/printer", methods=["DELETE"])
def printer_remove():
    import printer_setup
    printer_setup.remove_printer()
    return jsonify({"ok": True})


@app.route("/api/printer/test", methods=["POST"])
def printer_test():
    import printer_setup
    return jsonify(printer_setup.test_print())


@app.route("/api/wifi/connected", methods=["POST"])
def wifi_connected():
    """wifi_portal (cùng máy) báo đã nối mạng xong → tắt QR ngay (≤1s) thay vì
    chờ vòng _refresh_wifi 5s (+probe máy in). Vô hại nếu bị gọi bậy: chỉ refresh
    cache trạng thái WiFi từ nmcli, không đổi cấu hình gì."""
    ctrl.notify_wifi_connected()
    return jsonify({"ok": True})


@app.route("/api/wifi/setup", methods=["POST"])
def wifi_setup():
    """Bật AP mode để re-setup WiFi — gọi từ Settings > WiFi > Setup."""
    import subprocess
    import threading
    here = os.path.dirname(os.path.abspath(__file__))
    ap = os.path.join(here, "wifi", "wifi_ap.sh")  # [gom folder 2026-07] wifi_ap.sh chuyển vào device_service/wifi/

    # HIỆN QR NGAY (lạc quan) — khỏi chờ _refresh_wifi phát hiện AP (mỗi 5s, còn bị
    # probe máy in đẩy trễ). AP lên nền song song; user vừa cầm điện thoại là AP đã sẵn.
    try:
        ctrl.mark_wifi_setup_pending()
    except Exception:
        pass

    _ssid = settings.wifi.ap_ssid
    def _start():
        time.sleep(0.3)
        cmd = ["sudo", "-n", "bash", ap, "up"]
        if _ssid:
            cmd = ["sudo", "-n", "env", f"CARD_AP_SSID={_ssid}", "bash", ap, "up"]
        subprocess.run(cmd, capture_output=True, text=True, timeout=150)
    threading.Thread(target=_start, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/quit", methods=["POST"])
def quit_app():
    # Close the whole kiosk: stop the systemd user unit (kills Chromium + this
    # server). It's an explicit stop, so systemd won't auto-restart it.
    # Re-open with: systemctl --user start card-device.service (or reboot).
    import subprocess
    import threading

    def _stop():
        time.sleep(0.4)  # let this HTTP response flush before teardown
        subprocess.Popen(["systemctl", "--user", "stop", "card-device.service"])
    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/device")
def device():
    return jsonify(ctrl.device_info())


# (route /api/reset đã đăng ký ở trên — bản trùng thứ 2 xoá 2026-07-03)


@app.route("/api/enroll", methods=["POST"])
def enroll():
    d = request.get_json(force=True, silent=True) or {}
    return jsonify(ctrl.enroll(d.get("server_url", "").strip(),
                               d.get("device_id", "").strip(),
                               d.get("setup_token", "").strip()))


@app.route("/api/pair/begin", methods=["POST"])
def pair_begin():
    """UI calls this on an un-enrolled device (WiFi up, no credentials) to get a
    pairing code to show as a QR. Idempotent; a no-op if already enrolled."""
    return jsonify(ctrl.begin_pairing())


@app.route("/api/pair/status")
def pair_status():
    return jsonify(ctrl.pairing_status())


@app.route("/preview_test.mjpeg")
def preview_test():
    """Xem thử camera khi máy IDLE (test thủ công qua Chrome) — tự tắt khi
    bấm START hoặc đóng tab >5s. Máy đang chạy → chỉ trả ảnh chờ."""
    boundary = "frame"
    def gen():
        while True:
            jpeg = ctrl.test_preview_jpeg() or PLACEHOLDER
            if jpeg:
                yield (b"--" + boundary.encode() + b"\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
            time.sleep(0.1)
    return Response(gen(), mimetype=f"multipart/x-mixed-replace; boundary={boundary}")


@app.route("/preview.mjpeg")
def preview():
    boundary = "frame"
    def gen():
        while True:
            jpeg = ctrl.preview_jpeg() or PLACEHOLDER
            if jpeg:
                yield (b"--" + boundary.encode() + b"\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
            time.sleep(0.1)  # ~10 fps preview — light on the Pi
    return Response(gen(), mimetype=f"multipart/x-mixed-replace; boundary={boundary}")


if __name__ == "__main__":
    logger.info("kiosk web server on http://%s:%d", HOST, PORT)
    app.run(host=HOST, port=PORT, threaded=True)

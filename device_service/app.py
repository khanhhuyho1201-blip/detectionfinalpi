"""
Card Machine — Raspberry Pi Client
Tkinter UI for touchscreen interaction.

Auto cycle (one button): pre-flight checks (server → camera → motor, each shown
on the UI) → turn on camera → turn on motor → count to the server-given target
→ auto-stop + upload. The Start gate reopens ONLY after a successful upload.
"""

import io
import logging
import os
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageTk

from api_client import APIClient
from camera import Recorder, delete_video, probe as camera_probe
from parser import MachineStatus, parse_line
from serial_link import SerialLink
from settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("card_device")

# ── constants ──
HEARTBEAT_INTERVAL = settings.enroll.heartbeat_interval  # seconds
PREVIEW_REFRESH_MS = 33     # ms between preview frame updates (~30 fps)
PREVIEW_BOX_W = 800         # preview frame box size in pixels (16:9)
PREVIEW_BOX_H = 450

# upload retry: number of attempts to send the clip before giving up.
UPLOAD_MAX_RETRIES = settings.upload.max_retries
UPLOAD_RETRY_DELAY = settings.upload.retry_delay

# how long each check step's ✓ stays visible so the operator can read it
# (server/motor checks are near-instant).
STEP_PAUSE = settings.ui.step_pause  # seconds

# default server URLs for the enroll screen — both are editable by the user
DEFAULT_SERVER_URL = settings.enroll.default_server_url   # "Đặt máy mới"
TEST_SERVER_URL    = settings.enroll.test_server_url      # "Dùng máy test"

# test credentials, prefilled by the "Dùng máy test" button (values in .env).
TEST_DEVICE_ID    = settings.enroll.test_device_id
TEST_SETUP_TOKEN  = settings.enroll.test_setup_token

# ── palette ──
BG       = "#0F1318"
SURFACE  = "#161C24"
BORDER   = "#252D38"
TEXT     = "#C8D0DC"
TEXT_HI  = "#EEF1F6"
TEXT_DIM = "#6B7A8D"
ACCENT   = "#F0A500"
GREEN    = "#3DD68C"
YELLOW   = "#F0C000"
RED      = "#F05252"
BLUE     = "#4A9EFF"

# pre-flight checks shown before a batch starts (key, label)
CHECK_STEPS = (("server", "Máy chủ"), ("camera", "Camera"), ("motor", "Motor"))


class CardApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Card Machine")
        self.configure(bg=BG)
        self._fullscreen = settings.ui.fullscreen
        self.attributes("-fullscreen", self._fullscreen)
        self.bind("<Escape>", lambda e: self._set_fullscreen(False))
        self.bind("<F11>", lambda e: self._toggle_fullscreen())

        self._client: APIClient | None = None
        self._run_id: str | None = None
        self._recorder: Recorder | None = None
        self._recording = False

        # ── motor / serial control ──
        self._link: SerialLink | None = None
        self._status = MachineStatus()
        self._target = settings.batch.target_fallback
        self._can_start = True                 # gate: only opens after a successful upload
        self._finishing = False                # guard against double auto-finish
        self._status_evt = threading.Event()   # set on any serial line (motor handshake)
        self._pending_upload: tuple[str, str] | None = None  # (run_id, video_path) on failure

        self._setup_fonts()
        self._build_ui()
        self._start_serial()
        self._boot()
        if settings.ui.autostart:   # demo/test: auto-press Bắt đầu
            self.after(1200, self.start_cycle)

    # ── fullscreen ──
    def _set_fullscreen(self, on: bool):
        self._fullscreen = on
        self.attributes("-fullscreen", on)

    def _toggle_fullscreen(self):
        self._set_fullscreen(not self._fullscreen)

    # ── fonts ──
    def _setup_fonts(self):
        self.f_title  = tkfont.Font(family="DejaVu Sans", size=22, weight="bold")
        self.f_label  = tkfont.Font(family="DejaVu Sans", size=13)
        self.f_small  = tkfont.Font(family="DejaVu Sans", size=11)
        self.f_mono   = tkfont.Font(family="DejaVu Sans Mono", size=11)
        self.f_btn    = tkfont.Font(family="DejaVu Sans", size=15, weight="bold")
        self.f_status = tkfont.Font(family="DejaVu Sans Mono", size=10)
        self.f_count  = tkfont.Font(family="DejaVu Sans", size=72, weight="bold")

    # ── build UI ──
    def _build_ui(self):
        topbar = tk.Frame(self, bg=SURFACE, height=48)
        topbar.pack(fill="x", side="top")
        topbar.pack_propagate(False)

        tk.Label(topbar, text="● Card Machine", bg=SURFACE, fg=ACCENT,
                 font=self.f_label).pack(side="left", padx=16, pady=12)

        self._lbl_conn = tk.Label(topbar, text="—", bg=SURFACE, fg=TEXT_DIM,
                                  font=self.f_status)
        self._lbl_conn.pack(side="right", padx=16)

        self._container = tk.Frame(self, bg=BG)
        self._container.pack(fill="both", expand=True)
        self._container.grid_rowconfigure(0, weight=1)
        self._container.grid_columnconfigure(0, weight=1)

        self._pages = {}
        for PageClass in (EnrollPage, MainPage):
            page = PageClass(self._container, self)
            self._pages[PageClass.__name__] = page
            page.grid(row=0, column=0, sticky="nsew")

    def show(self, name: str):
        self._pages[name].tkraise()
        self._pages[name].on_show()

    # ── serial link to the Arduino ──
    def _start_serial(self):
        self._link = SerialLink(on_line=self._on_serial_line)
        self._link.start()

    def _on_serial_line(self, line: str):
        """Runs on the serial reader thread — keep it light, marshal UI to Tk."""
        self._status, event = parse_line(line, self._status)
        self._status.connected = self._link.connected if self._link else False
        self._status_evt.set()  # any line proves the controller is alive (handshake)
        if self._recording:
            self.after(0, self._pages["MainPage"].update_count,
                       self._status.count, self._target)
            if event in ("done", "stall") and not self._finishing:
                # target reached / out of leaves. [LIMIT] is a non-fatal warning,
                # NOT a batch end — see parser.py.
                self._finishing = True
                self.after(0, self._auto_finish, event)

    # ── boot ──
    def _boot(self):
        # bench mode: skip enroll/network, use an in-memory fake server so the
        # full cycle can run with CARD_SERIAL_PORT=sim and no real backend.
        if settings.fake.server:
            self._client = _FakeClient()
            self.show("MainPage")
            self._update_conn(True)
            return
        creds = settings.credentials.load()
        if creds:
            self._client = APIClient(
                creds["server_url"],
                creds["device_id"],
                creds["device_key"],
            )
            self.show("MainPage")
            self._start_heartbeat()
        else:
            self.show("EnrollPage")

    # ── heartbeat ──
    def _start_heartbeat(self):
        def loop():
            while True:
                if self._client:
                    ok = self._client.heartbeat()
                    self.after(0, self._update_conn, ok)
                time.sleep(HEARTBEAT_INTERVAL)
        threading.Thread(target=loop, daemon=True).start()

    def _update_conn(self, ok: bool):
        if ok:
            self._lbl_conn.config(text="● online", fg=GREEN)
        else:
            self._lbl_conn.config(text="● offline", fg=RED)

    # ── auto cycle: checks → camera → motor → count → auto stop + upload ──
    def start_cycle(self):
        """Start button: pre-flight checks, then turn on camera + motor. Fully
        automatic from here — the batch stops itself at the target."""
        if not self._can_start or self._recording:
            return
        self._can_start = False
        self._recording = True
        self._finishing = False
        page = self._pages["MainPage"]
        page.begin_cycle()
        threading.Thread(target=self._run_cycle, daemon=True).start()

    def _run_cycle(self):
        page = self._pages["MainPage"]
        run_id = None
        try:
            # CHECK 1 — server. Registering the run IS the health check, and it
            # tells us how many leaves this batch is.
            self.after(0, page.set_check, "server", "run", "")
            try:
                resp = self._client.start_run()
            except Exception as e:
                logger.warning(f"start_run failed: {e}")
                return self._abort("server", "Không kết nối máy chủ", None)
            if not resp.get("ok"):
                return self._abort("server", f"Máy chủ từ chối: {resp.get('reason','?')}", None)
            run_id = resp["run_id"]
            self._run_id = run_id
            self._target = self._extract_target(resp)
            self.after(0, page.set_check, "server", "ok", f"mục tiêu {self._target} lá")
            time.sleep(STEP_PAUSE)

            # CHECK 2 — camera connection (probe only, not recording yet)
            self.after(0, page.set_check, "camera", "run", "")
            ok, msg = camera_probe(settings.camera.check_timeout)
            if not ok:
                return self._abort("camera", f"Camera: {msg}", run_id)
            self.after(0, page.set_check, "camera", "ok", msg)
            time.sleep(STEP_PAUSE)

            # CHECK 3 — motor controller handshake (send S, await ST). No spin.
            self.after(0, page.set_check, "motor", "run", "")
            if not self._motor_handshake():
                return self._abort("motor", "Arduino không phản hồi", run_id)
            self.after(0, page.set_check, "motor", "ok",
                       "simulator" if self._link.is_sim else self._link.port)
            time.sleep(STEP_PAUSE)

            # ── all checks passed → TURN ON camera, then motor ──
            self.after(0, page.set_state, "registered", run_id)
            self._recorder = Recorder(run_id)
            self._recorder.start()
            if self._recorder.error:
                return self._abort("camera", f"Camera lỗi: {self._recorder.error}", run_id)

            self._status = MachineStatus()        # reset the count baseline
            self._link.send(f"N{self._target}")   # set batch target on the Arduino
            self._link.send("B1")                 # spin the motor
            self.after(0, page.enter_recording, run_id, self._target)
        except Exception as e:
            logger.exception("cycle failed")
            self._abort("server", str(e)[:60], run_id)

    def _abort(self, step: str, msg: str, run_id):
        """A check (or startup) failed before the batch ran: clean up and reopen
        the Start gate — nothing was recorded."""
        if self._link:
            self._link.send("B0")
        if self._recorder:
            try:
                self._recorder.stop_and_discard()
            except Exception:
                pass
            self._recorder = None
        if run_id:
            try:
                self._client.cancel_run(run_id)
            except Exception:
                pass
        self._recording = False
        self._finishing = False
        self._can_start = True
        self.after(0, self._pages["MainPage"].set_check, step, "fail", msg)
        self.after(0, self._pages["MainPage"].checks_failed, msg)

    def _extract_target(self, resp: dict) -> int:
        """The server decides the batch size; read the first key it provides."""
        for k in settings.batch.target_keys:
            v = resp.get(k)
            if isinstance(v, int) and v > 0:
                logger.info(f"batch target from server[{k}] = {v}")
                return v
        logger.info(f"server gave no target; fallback {settings.batch.target_fallback}")
        return settings.batch.target_fallback

    def _motor_handshake(self) -> bool:
        """Send S and wait for any line back (proves the controller is alive).
        Does NOT spin the motor."""
        if self._link is None:
            return False
        if self._link.is_sim:
            return True
        if not self._link.connected:
            return False
        self._status_evt.clear()
        self._link.send("S")
        return self._status_evt.wait(settings.serial.motor_check_timeout)

    # ── auto finish (target reached / out of leaves) ──
    def _auto_finish(self, reason: str):
        if not self._recording:
            return
        if self._link:
            self._link.send("B0")  # idempotent: motor off + home
        self._pages["MainPage"].set_state("stopping")
        threading.Thread(target=self._finish_thread, args=(reason,), daemon=True).start()

    def _finish_thread(self, reason: str = "done"):
        page = self._pages["MainPage"]
        run_id = self._run_id
        recorder = self._recorder
        try:
            video_path = recorder.stop_and_keep() if recorder else None
            if not video_path:
                self.after(0, page.set_error, "Quay lỗi, không có video")
                self._can_start = True   # nothing to send; let operator retry
                self.after(0, page.set_can_start, True)
                return

            ok = self._upload_with_retry(run_id, video_path, page)
            if ok:
                delete_video(video_path)
                self._pending_upload = None
                self.after(0, page.set_state, "done", run_id)
                self.after(0, page.refresh_history)
                self._can_start = True   # ← GATE opens ONLY after a successful send
                self.after(0, page.set_can_start, True)
            else:
                # keep the clip for a manual retry; the Start gate stays CLOSED
                self._pending_upload = (run_id, video_path)
                self.after(0, page.set_upload_failed,
                           f"Gửi thất bại sau {UPLOAD_MAX_RETRIES} lần")
        except Exception as e:
            logger.exception("finish failed")
            self.after(0, page.set_error, str(e)[:60])
        finally:
            self._recorder = None
            self._recording = False
            self._finishing = False

    def _upload_with_retry(self, run_id: str, video_path: str, page) -> bool:
        """Upload + complete, retrying up to UPLOAD_MAX_RETRIES times.
        A fresh presigned URL is fetched each attempt (URLs can expire)."""
        for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
            self.after(0, page.set_state, "uploading", "", attempt)
            try:
                url_data = self._client.get_upload_url(run_id)
                if not self._client.upload_video(url_data["upload_url"], video_path):
                    raise RuntimeError("PUT to storage failed")
                result = self._client.complete_upload(run_id, url_data["object_key"])
                if result.get("ok"):
                    logger.info(f"Upload ok on attempt {attempt}: {run_id}")
                    return True
                raise RuntimeError(f"server rejected: {result}")
            except Exception as e:
                logger.warning(f"Upload attempt {attempt}/{UPLOAD_MAX_RETRIES} failed: {e}")
                if attempt < UPLOAD_MAX_RETRIES:
                    time.sleep(UPLOAD_RETRY_DELAY)
        return False

    def retry_upload(self):
        """'Gửi lại' button: re-send the clip that failed. The Start gate stays
        closed until this succeeds."""
        if not self._pending_upload or self._recording:
            return
        run_id, video_path = self._pending_upload
        threading.Thread(target=self._retry_thread,
                         args=(run_id, video_path), daemon=True).start()

    def _retry_thread(self, run_id: str, video_path: str):
        page = self._pages["MainPage"]
        ok = self._upload_with_retry(run_id, video_path, page)
        if ok:
            delete_video(video_path)
            self._pending_upload = None
            self.after(0, page.set_state, "done", run_id)
            self.after(0, page.refresh_history)
            self._can_start = True
            self.after(0, page.set_can_start, True)
        else:
            self.after(0, page.set_upload_failed,
                       f"Gửi lại thất bại sau {UPLOAD_MAX_RETRIES} lần")

    def cancel_recording(self):
        """Cancel button: abort the running batch, discard the clip."""
        if not self._recording:
            return
        if self._link:
            self._link.send("B0")   # stop the motor immediately
        page = self._pages["MainPage"]
        page.set_state("cancelling")
        threading.Thread(target=self._cancel_thread, daemon=True).start()

    def _cancel_thread(self):
        page = self._pages["MainPage"]
        run_id = self._run_id
        recorder = self._recorder
        try:
            if recorder:
                recorder.stop_and_discard()
            try:
                self._client.cancel_run(run_id)
            except Exception as e:
                logger.warning(f"cancel_run server call failed: {e}")
            self.after(0, page.set_state, "idle")
            self.after(0, page.refresh_history)
        finally:
            self._recorder = None
            self._recording = False
            self._finishing = False
            self._can_start = True   # aborted batch → reopen the gate
            self.after(0, page.set_can_start, True)

    def get_preview_frame(self) -> bytes | None:
        if self._recorder:
            return self._recorder.get_latest_jpeg()
        return None

    # ── enroll ──
    def do_enroll(self, server_url: str, device_id: str, setup_token: str):
        threading.Thread(
            target=self._enroll_thread,
            args=(server_url, device_id, setup_token),
            daemon=True,
        ).start()

    def _enroll_thread(self, server_url, device_id, setup_token):
        try:
            data = APIClient.enroll(server_url, device_id, setup_token)
            creds = {
                "server_url": server_url,
                "device_id": data["device_id"],
                "device_key": data["device_key"],
            }
            settings.credentials.save(creds)
            self._client = APIClient(server_url, data["device_id"], data["device_key"])
            self.after(0, self._on_enrolled)
        except Exception as e:
            self.after(0, self._pages["EnrollPage"].set_error, f"Enroll thất bại: {e}")

    def _on_enrolled(self):
        self._start_heartbeat()
        self.show("MainPage")

    def do_reset(self):
        settings.credentials.clear()
        self._client = None
        self._run_id = None
        self.show("EnrollPage")


# ── Enroll Page ──
class EnrollPage(tk.Frame):
    def __init__(self, parent, app: CardApp):
        super().__init__(parent, bg=BG)
        self._app = app
        self._build()

    def _build(self):
        wrap = tk.Frame(self, bg=BG)
        wrap.place(relx=.5, rely=.5, anchor="center")

        tk.Label(wrap, text="Cài đặt thiết bị", bg=BG, fg=TEXT_HI,
                 font=self._app.f_title).pack(pady=(0, 6))
        tk.Label(wrap, text="Chọn chế độ, hoặc nhập thông tin từ admin để kích hoạt máy này.",
                 bg=BG, fg=TEXT_DIM, font=self._app.f_small).pack(pady=(0, 16))

        # mode picker — fills the Server URL field with a preset (still editable)
        mode_row = tk.Frame(wrap, bg=BG)
        mode_row.pack(pady=(0, 20))
        tk.Button(mode_row, text="Đặt máy mới", bg=SURFACE, fg=TEXT_HI,
                  activebackground=BORDER, activeforeground=TEXT_HI,
                  relief="flat", cursor="hand2", font=self._app.f_small,
                  padx=18, pady=10,
                  command=lambda: self._use_server(DEFAULT_SERVER_URL)
                  ).pack(side="left", padx=6)
        tk.Button(mode_row, text="Dùng máy test", bg=SURFACE, fg=ACCENT,
                  activebackground=BORDER, activeforeground=ACCENT,
                  relief="flat", cursor="hand2", font=self._app.f_small,
                  padx=18, pady=10,
                  command=self._use_test_machine
                  ).pack(side="left", padx=6)

        for attr, label, default in [
            ("_inp_url",   "Server URL",    DEFAULT_SERVER_URL),
            ("_inp_id",    "Device ID",     ""),
            ("_inp_token", "Setup Token",   ""),
        ]:
            tk.Label(wrap, text=label, bg=BG, fg=TEXT_DIM,
                     font=self._app.f_small).pack(anchor="w")
            inp = tk.Entry(wrap, bg=SURFACE, fg=TEXT_HI, insertbackground=TEXT_HI,
                           relief="flat", font=self._app.f_mono,
                           highlightthickness=1, highlightbackground=BORDER,
                           highlightcolor=ACCENT, width=38)
            inp.insert(0, default)
            inp.pack(pady=(2, 12), ipady=8, ipadx=8)
            setattr(self, attr, inp)

        self._lbl_err = tk.Label(wrap, text="", bg=BG, fg=RED,
                                 font=self._app.f_small, wraplength=420)
        self._lbl_err.pack(pady=(0, 12))

        self._btn = _BigButton(wrap, self._app, text="Kích hoạt",
                               command=self._submit)
        self._btn.pack(pady=(4, 0))

    @staticmethod
    def _fill(entry: tk.Entry, value: str):
        entry.delete(0, "end")
        entry.insert(0, value)

    def _use_server(self, url: str):
        """Fill the Server URL field with a preset — user can still edit it."""
        self._fill(self._inp_url, url)
        self._lbl_err.config(text="")
        self._inp_id.focus_set()

    def _use_test_machine(self):
        """Prefill server URL + test device id + setup token — all still editable."""
        self._fill(self._inp_url, TEST_SERVER_URL)
        self._fill(self._inp_id, TEST_DEVICE_ID)
        self._fill(self._inp_token, TEST_SETUP_TOKEN)
        self._lbl_err.config(text="")

    def _submit(self):
        url   = self._inp_url.get().strip()
        did   = self._inp_id.get().strip()
        token = self._inp_token.get().strip()
        if not url or not did or not token:
            self.set_error("Vui lòng điền đầy đủ thông tin.")
            return
        self._lbl_err.config(text="")
        self._btn.config(state="disabled", text="Đang kích hoạt…")
        self._app.do_enroll(url, did, token)

    def set_error(self, msg: str):
        self._lbl_err.config(text=msg)
        self._btn.config(state="normal", text="Kích hoạt")

    def on_show(self):
        self._lbl_err.config(text="")
        self._btn.config(state="normal", text="Kích hoạt")


# ── Main Page ──
class MainPage(tk.Frame):
    # statuses shown in the on-device history list, with a label + colour
    _HIST_STATUS = {
        "recording":  ("Đang quay",   RED),
        "uploaded":   ("Đã gửi",      BLUE),
        "processing": ("Đang xử lý",  BLUE),
        "done":       ("Hoàn tất",    GREEN),
        "failed":     ("Thất bại",    TEXT_DIM),
    }

    def __init__(self, parent, app: CardApp):
        super().__init__(parent, bg=BG)
        self._app = app
        self._preview_imgtk = None
        self._preview_job = None
        self._current_run_id = ""
        self._build()

    def _build(self):
        app = self._app

        # Two-column layout: left → video preview, right → controls + history
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        # ── LEFT COLUMN: video preview ──
        left = tk.Frame(self, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(24, 12), pady=24)
        prev_wrap = tk.Frame(left, bg="#000000", width=PREVIEW_BOX_W,
                             height=PREVIEW_BOX_H,
                             highlightthickness=1, highlightbackground=BORDER)
        prev_wrap.pack(expand=True)
        prev_wrap.pack_propagate(False)
        self._preview = tk.Label(prev_wrap, bg="#000000")
        self._preview.pack(fill="both", expand=True)
        self._show_preview_placeholder()

        # ── RIGHT COLUMN: controls (top) + session history (bottom) ──
        right = tk.Frame(self, bg=BG)
        right.grid(row=0, column=1, sticky="nsew", padx=(12, 24), pady=24)
        self._build_history(right)

        ctrl_area = tk.Frame(right, bg=BG)
        ctrl_area.pack(side="top", fill="both", expand=True)
        stack = tk.Frame(ctrl_area, bg=BG)
        stack.place(relx=.5, rely=.5, anchor="center")

        # status indicator row
        ind_row = tk.Frame(stack, bg=BG)
        ind_row.pack(side="top", pady=(0, 14))
        self._canvas = tk.Canvas(ind_row, width=16, height=16, bg=BG,
                                 highlightthickness=0)
        self._canvas.pack(side="left", padx=(0, 8))
        self._dot = self._canvas.create_oval(2, 2, 14, 14, fill=TEXT_DIM, outline="")
        self._lbl_state = tk.Label(ind_row, text="Sẵn sàng", bg=BG,
                                   fg=TEXT_DIM, font=app.f_label)
        self._lbl_state.pack(side="left")

        # pre-flight checklist: server → camera → motor
        self._chk_box = tk.Frame(stack, bg=BG)
        self._chk_box.pack(side="top", pady=(0, 12))
        self._chk = {}
        for key, label in CHECK_STEPS:
            row = tk.Frame(self._chk_box, bg=BG)
            row.pack(anchor="w", pady=2)
            icon = tk.Label(row, text="○", bg=BG, fg=TEXT_DIM,
                            font=app.f_label, width=2)
            icon.pack(side="left")
            name = tk.Label(row, text=label, bg=BG, fg=TEXT_DIM,
                            font=app.f_small, width=8, anchor="w")
            name.pack(side="left")
            detail = tk.Label(row, text="", bg=BG, fg=TEXT_DIM, font=app.f_small)
            detail.pack(side="left", padx=(6, 0))
            self._chk[key] = (icon, name, detail)

        # big batch counter (n / target)
        self._lbl_count = tk.Label(stack, text="—", bg=BG, fg=TEXT_HI,
                                   font=app.f_count)
        self._lbl_count.pack(side="top", pady=(0, 10))

        # button column — swaps between [Bắt đầu] / [Hủy] / [Gửi lại]
        self._btn_row = tk.Frame(stack, bg=BG)
        self._btn_row.pack(side="top", pady=8)
        self._btn_start = _BigButton(self._btn_row, app, text="Bắt đầu",
                                     command=app.start_cycle)
        self._btn_cancel = tk.Button(
            self._btn_row, text="Hủy", bg=SURFACE, fg=RED,
            activebackground="#2a1414", activeforeground=RED,
            relief="flat", cursor="hand2", font=app.f_btn,
            padx=28, pady=14, command=app.cancel_recording,
        )
        self._btn_retry = _BigButton(self._btn_row, app, text="Gửi lại",
                                     command=app.retry_upload)

        # info / message line
        self._lbl_info = tk.Label(stack, text="", bg=BG, fg=TEXT_DIM,
                                  font=app.f_mono, wraplength=360)
        self._lbl_info.pack(side="top", pady=(16, 8))

        # reset (bottom of stack)
        tk.Button(stack, text="Đặt lại thiết bị", bg=BG, fg=TEXT_DIM,
                  activebackground=BG, activeforeground=RED,
                  relief="flat", font=app.f_small, cursor="hand2",
                  command=app.do_reset).pack(side="top", pady=(24, 0))

    # ── session history (bottom of right column) ──
    def _build_history(self, parent):
        app = self._app
        box = tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                       highlightbackground=BORDER)
        box.pack(side="bottom", fill="x", pady=(16, 0))

        head = tk.Frame(box, bg=SURFACE)
        head.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(head, text="Các lượt đã quay", bg=SURFACE, fg=TEXT_HI,
                 font=app.f_small).pack(side="left")
        tk.Button(head, text="⟳", bg=SURFACE, fg=TEXT_DIM, relief="flat",
                  activebackground=SURFACE, activeforeground=ACCENT,
                  cursor="hand2", font=app.f_small,
                  command=self.refresh_history).pack(side="right")

        self._hist_list = tk.Frame(box, bg=SURFACE, height=150)
        self._hist_list.pack(fill="x", padx=12, pady=(0, 10))
        self._hist_list.pack_propagate(False)
        self._hist_empty = tk.Label(self._hist_list, text="Chưa có lượt nào",
                                    bg=SURFACE, fg=TEXT_DIM, font=app.f_small)
        self._hist_empty.pack(pady=12)

    def refresh_history(self):
        if not self._app._client:
            return
        threading.Thread(target=self._fetch_history, daemon=True).start()

    def _fetch_history(self):
        try:
            runs = self._app._client.list_runs()
        except Exception as e:
            logger.warning(f"list_runs failed: {e}")
            return
        self.after(0, self._render_history, runs)

    def _render_history(self, runs: list):
        for w in self._hist_list.winfo_children():
            w.destroy()
        if not runs:
            tk.Label(self._hist_list, text="Chưa có lượt nào", bg=SURFACE,
                     fg=TEXT_DIM, font=self._app.f_small).pack(pady=12)
            return
        for r in runs[:8]:
            row = tk.Frame(self._hist_list, bg=SURFACE)
            row.pack(fill="x", pady=2)
            label, color = self._HIST_STATUS.get(
                r.get("status", ""), (r.get("status", "—"), TEXT_DIM))
            qpos = r.get("queue_position", 0)
            if r.get("status") == "uploaded" and qpos:
                label = f"Hàng chờ #{qpos}"
                color = BLUE
            tk.Label(row, text=self._short(r.get("run_id", "")), bg=SURFACE,
                     fg=TEXT, font=self._app.f_mono).pack(side="left")
            tk.Label(row, text=label, bg=SURFACE, fg=color,
                     font=self._app.f_small).pack(side="right")

    # ── preview ──
    def _show_preview_placeholder(self):
        self._preview.config(image="", text="Khung video sẽ hiện khi quay",
                             fg=TEXT_DIM, font=self._app.f_small,
                             compound="center")

    def _start_preview_loop(self):
        self._update_preview()

    def _stop_preview_loop(self):
        if self._preview_job is not None:
            self.after_cancel(self._preview_job)
            self._preview_job = None
        self._preview_imgtk = None
        self._show_preview_placeholder()

    def _update_preview(self):
        jpeg = self._app.get_preview_frame()
        if jpeg:
            try:
                img = Image.open(io.BytesIO(jpeg))
                img = img.resize((PREVIEW_BOX_W, PREVIEW_BOX_H))
                self._preview_imgtk = ImageTk.PhotoImage(img)
                self._preview.config(image=self._preview_imgtk, text="")
            except Exception:
                pass
        self._preview_job = self.after(PREVIEW_REFRESH_MS, self._update_preview)

    # ── button layout (only one set visible at a time) ──
    def _show_only(self, *btns):
        for b in (self._btn_start, self._btn_cancel, self._btn_retry):
            b.pack_forget()
        for b in btns:
            b.pack(side="top", pady=6, fill="x")

    # ── pre-flight checklist ──
    _CHK_ICON = {"pending": ("○", TEXT_DIM), "run": ("⟳", BLUE),
                 "ok": ("✓", GREEN), "fail": ("✕", RED)}

    def begin_cycle(self):
        """Reset the checklist and enter the checking phase (Cancel available)."""
        for key, _ in CHECK_STEPS:
            self.set_check(key, "pending", "")
        self._lbl_count.config(text="…")
        self._canvas.itemconfig(self._dot, fill=BLUE)
        self._lbl_state.config(text="Đang kiểm tra…", fg=BLUE)
        self._lbl_info.config(text="")
        self._show_only(self._btn_cancel)
        self._btn_cancel.config(state="normal")

    def set_check(self, key: str, state: str, detail: str = ""):
        icon, name, det = self._chk[key]
        ch, color = self._CHK_ICON.get(state, ("○", TEXT_DIM))
        icon.config(text=ch, fg=color)
        name.config(fg=(TEXT_DIM if state == "pending" else color))
        if detail:
            det.config(text=detail, fg=TEXT_DIM)
        elif state in ("pending", "run"):
            det.config(text="")

    def checks_failed(self, msg: str):
        self._stop_preview_loop()
        self._canvas.itemconfig(self._dot, fill=RED)
        self._lbl_state.config(text="Kiểm tra thất bại", fg=RED)
        self._lbl_info.config(text=msg, fg=RED)
        self._lbl_count.config(text="—")
        self._show_only(self._btn_start)
        self._btn_start.config(state="normal", text="Thử lại")

    # ── counter ──
    def update_count(self, count: int, target: int):
        self._lbl_count.config(text=f"{count} / {target}")

    # called by app when camera + motor have started successfully
    def enter_recording(self, run_id: str = "", target: int = 0):
        self._current_run_id = run_id or self._current_run_id
        self.update_count(0, target)
        self.set_state("recording", self._current_run_id)
        self._show_only(self._btn_cancel)
        self._btn_cancel.config(state="normal")
        self._start_preview_loop()

    def set_can_start(self, on: bool):
        self._btn_start.config(state="normal" if on else "disabled")

    def set_upload_failed(self, msg: str):
        # gate stays CLOSED: only [Gửi lại] is shown, no [Bắt đầu]
        self._stop_preview_loop()
        self._canvas.itemconfig(self._dot, fill=RED)
        self._lbl_state.config(text="Gửi thất bại", fg=RED)
        self._lbl_info.config(text=msg + " — bấm Gửi lại", fg=RED)
        self._show_only(self._btn_retry)
        self._btn_retry.config(state="normal")

    def set_state(self, state: str, run_id: str = "", attempt: int = 0):
        cfg = {
            "registered": (GREEN,    "Đã kiểm tra — đang bật…"),
            "recording":  (RED,      "● Đang quay…"),
            "stopping":   (BLUE,     "Đủ số — đang kết thúc…"),
            "cancelling": (TEXT_DIM, "Đang hủy…"),
            "uploading":  (BLUE,     "Đang gửi máy chủ…"),
            "done":       (GREEN,    "✓ Xong! Đã gửi. Sẵn sàng lượt kế."),
            "idle":       (TEXT_DIM, "Sẵn sàng"),
        }
        color, label = cfg.get(state, (TEXT_DIM, state))
        if state == "uploading" and attempt > 1:
            label = f"Đang gửi lại… ({attempt}/{UPLOAD_MAX_RETRIES})"
        self._canvas.itemconfig(self._dot, fill=color)
        self._lbl_state.config(text=label, fg=color)

        if run_id:
            self._current_run_id = run_id

        if state == "idle":
            self._stop_preview_loop()
            self._lbl_count.config(text="—")
            for key, _ in CHECK_STEPS:
                self.set_check(key, "pending", "")
            self._show_only(self._btn_start)
            self._btn_start.config(
                state="normal" if self._app._can_start else "disabled",
                text="Bắt đầu")
        elif state == "done":
            self._stop_preview_loop()
            self._show_only(self._btn_start)
            self._btn_start.config(state="normal", text="Bắt đầu")
        elif state in ("stopping", "uploading", "cancelling"):
            self._btn_cancel.config(state="disabled")
        # 'registered'/'recording' keep the Cancel button shown by begin_cycle /
        # enter_recording.

        # info line: which session this is
        if state in ("registered", "recording", "stopping", "uploading", "done") \
                and self._current_run_id:
            self._lbl_info.config(text=f"Session {self._short(self._current_run_id)}",
                                  fg=TEXT_DIM)
        elif state == "idle":
            self._lbl_info.config(text="")

    @staticmethod
    def _short(run_id: str) -> str:
        return run_id[:8] if run_id else "—"

    def set_error(self, msg: str):
        self._stop_preview_loop()
        self._canvas.itemconfig(self._dot, fill=RED)
        self._lbl_state.config(text="Lỗi", fg=RED)
        self._lbl_info.config(text=msg, fg=RED)
        self._show_only(self._btn_start)
        self._btn_start.config(state="normal", text="Thử lại")

    def on_show(self):
        self.set_state("idle")
        self.refresh_history()


# ── reusable big button ──
class _BigButton(tk.Button):
    def __init__(self, parent, app: CardApp, **kwargs):
        super().__init__(
            parent,
            bg=ACCENT, fg="#000",
            activebackground="#d49300",
            activeforeground="#000",
            relief="flat",
            cursor="hand2",
            font=app.f_btn,
            padx=32, pady=14,
            **kwargs,
        )


# ── bench-only fake server (CARD_FAKE_SERVER=1): no network, for sim testing ──
class _FakeClient:
    _n = 0

    def heartbeat(self):
        return True

    def start_run(self):
        _FakeClient._n += 1
        return {"ok": True, "run_id": f"fake{_FakeClient._n:04d}",
                "target": settings.fake.target}

    def get_upload_url(self, run_id):
        return {"upload_url": "fake://", "object_key": run_id}

    def upload_video(self, upload_url, video_path):
        return True

    def complete_upload(self, run_id, object_key):
        return {"ok": True}

    def cancel_run(self, run_id):
        return {"ok": True}

    def list_runs(self):
        return []


if __name__ == "__main__":
    app = CardApp()
    try:
        app.mainloop()
    finally:
        if app._link:
            app._link.stop()

"""
ui.py — single-page touchscreen UI.

Big START/STOP button, a large count (137 / 412), a coloured status light, a
leaf-count stepper, an optional camera preview, and a scrolling log. It holds a
Session and calls session.start()/stop()/set_total(); it renders snapshots from
session.status() on a timer. No serial/camera code lives here.
"""

import io
import tkinter as tk
from tkinter import font as tkfont

import config
from parser import DONE, ERROR, IDLE, OFF, RUN, WARN

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

# ── palette (matches device_service) ──
BG = "#0F1318"
SURFACE = "#161C24"
BORDER = "#252D38"
TEXT = "#C8D0DC"
TEXT_HI = "#EEF1F6"
TEXT_DIM = "#6B7A8D"
ACCENT = "#F0A500"
GREEN = "#3DD68C"
YELLOW = "#F0C000"
RED = "#F05252"

PREVIEW_W, PREVIEW_H = 480, 270

# state -> (dot colour, label)
_STATE_VIEW = {
    IDLE:  (TEXT_DIM, "Sẵn sàng"),
    OFF:   (TEXT_DIM, "Đã tắt"),
    RUN:   (GREEN,    "● Đang chạy…"),
    WARN:  (YELLOW,   "⚠ Dính lá"),
    DONE:  (GREEN,    "✓ Xong mẻ!"),
}


class MachineUI(tk.Tk):
    def __init__(self, session):
        super().__init__()
        self._session = session
        self.title("Card Machine — Start/Stop")
        self.configure(bg=BG)
        self._fullscreen = config.FULLSCREEN
        self.attributes("-fullscreen", self._fullscreen)
        self.bind("<Escape>", lambda e: self._set_fullscreen(False))
        self.bind("<F11>", lambda e: self._set_fullscreen(not self._fullscreen))

        self._preview_imgtk = None
        self._total = session.total
        self._setup_fonts()
        self._build()
        self._tick()  # start the render loop
        if config.AUTOSTART:
            self.after(800, self._on_start)

    # ── fullscreen ──
    def _set_fullscreen(self, on: bool):
        self._fullscreen = on
        self.attributes("-fullscreen", on)

    def _setup_fonts(self):
        self.f_title = tkfont.Font(family="DejaVu Sans", size=18, weight="bold")
        self.f_count = tkfont.Font(family="DejaVu Sans", size=88, weight="bold")
        self.f_total = tkfont.Font(family="DejaVu Sans", size=30)
        self.f_state = tkfont.Font(family="DejaVu Sans", size=20, weight="bold")
        self.f_btn = tkfont.Font(family="DejaVu Sans", size=26, weight="bold")
        self.f_step = tkfont.Font(family="DejaVu Sans", size=24, weight="bold")
        self.f_small = tkfont.Font(family="DejaVu Sans", size=12)
        self.f_log = tkfont.Font(family="DejaVu Sans Mono", size=10)

    # ── layout ──
    def _build(self):
        # top bar: title + connection indicator
        top = tk.Frame(self, bg=SURFACE, height=44)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)
        tk.Label(top, text="● Card Machine", bg=SURFACE, fg=ACCENT,
                 font=self.f_title).pack(side="left", padx=16)
        self._lbl_conn = tk.Label(top, text="—", bg=SURFACE, fg=TEXT_DIM,
                                  font=self.f_small)
        self._lbl_conn.pack(side="right", padx=16)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)

        # left column: status + count + controls
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=24, pady=16)

        # status light + label
        srow = tk.Frame(left, bg=BG)
        srow.pack(pady=(8, 4))
        self._canvas = tk.Canvas(srow, width=22, height=22, bg=BG, highlightthickness=0)
        self._canvas.pack(side="left", padx=(0, 10))
        self._dot = self._canvas.create_oval(3, 3, 19, 19, fill=TEXT_DIM, outline="")
        self._lbl_state = tk.Label(srow, text="Sẵn sàng", bg=BG, fg=TEXT_DIM,
                                   font=self.f_state)
        self._lbl_state.pack(side="left")

        # big count
        self._lbl_count = tk.Label(left, text="0", bg=BG, fg=TEXT_HI, font=self.f_count)
        self._lbl_count.pack(pady=(8, 0))
        self._lbl_total = tk.Label(left, text=self._total_text(), bg=BG, fg=TEXT_DIM,
                                   font=self.f_total)
        self._lbl_total.pack(pady=(0, 8))

        # leaf-count stepper (disabled while running)
        step = tk.Frame(left, bg=BG)
        step.pack(pady=(4, 12))
        self._btn_minus = self._step_btn(step, "−", lambda: self._bump(-config.TOTAL_STEP))
        self._btn_minus.pack(side="left", padx=6)
        tk.Label(step, text="số lá / mẻ", bg=BG, fg=TEXT_DIM,
                 font=self.f_small).pack(side="left", padx=12)
        self._btn_plus = self._step_btn(step, "+", lambda: self._bump(config.TOTAL_STEP))
        self._btn_plus.pack(side="left", padx=6)

        # START / STOP (swapped in place)
        self._btn_start = tk.Button(
            left, text="BẮT ĐẦU", bg=GREEN, fg="#06231a",
            activebackground="#33b377", activeforeground="#06231a",
            relief="flat", cursor="hand2", font=self.f_btn,
            padx=60, pady=26, command=self._on_start)
        self._btn_stop = tk.Button(
            left, text="DỪNG", bg=RED, fg="#2a0c0c",
            activebackground="#c43d3d", activeforeground="#2a0c0c",
            relief="flat", cursor="hand2", font=self.f_btn,
            padx=70, pady=26, command=self._on_stop)
        self._btn_start.pack(pady=8)

        self._lbl_hint = tk.Label(left, text="", bg=BG, fg=TEXT_DIM, font=self.f_small)
        self._lbl_hint.pack(pady=(4, 0))

        # right column: optional preview + log
        right = tk.Frame(body, bg=BG, width=520)
        right.pack(side="right", fill="both", padx=(0, 16), pady=16)
        right.pack_propagate(False)

        if config.CAMERA_ENABLED:
            pv = tk.Frame(right, bg="#000", width=PREVIEW_W, height=PREVIEW_H,
                          highlightthickness=1, highlightbackground=BORDER)
            pv.pack(pady=(4, 10))
            pv.pack_propagate(False)
            self._preview = tk.Label(pv, bg="#000", fg=TEXT_DIM,
                                     text="Khung video khi quay", font=self.f_small)
            self._preview.pack(fill="both", expand=True)
        else:
            self._preview = None

        tk.Label(right, text="Nhật ký", bg=BG, fg=TEXT_DIM,
                 font=self.f_small).pack(anchor="w")
        logbox = tk.Frame(right, bg=SURFACE, highlightthickness=1,
                          highlightbackground=BORDER)
        logbox.pack(fill="both", expand=True)
        self._log = tk.Text(logbox, bg=SURFACE, fg=TEXT, font=self.f_log,
                            relief="flat", wrap="none", state="disabled",
                            highlightthickness=0)
        self._log.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(logbox, command=self._log.yview)
        sb.pack(side="right", fill="y")
        self._log.config(yscrollcommand=sb.set)

    def _step_btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, bg=SURFACE, fg=TEXT_HI,
                         activebackground=BORDER, activeforeground=TEXT_HI,
                         relief="flat", cursor="hand2", font=self.f_step,
                         width=2, padx=10, pady=6, command=cmd)

    # ── helpers ──
    def _total_text(self) -> str:
        return "/ kéo hết" if self._total == 0 else f"/ {self._total}"

    def _bump(self, delta: int):
        if self._session.is_running:
            return
        self._total = max(config.TOTAL_MIN, min(config.TOTAL_MAX, self._total + delta))
        self._session.set_total(self._total)
        self._lbl_total.config(text=self._total_text())

    # ── button actions ──
    def _on_start(self):
        self._session.set_total(self._total)
        self._session.start()

    def _on_stop(self):
        self._session.stop()

    # ── log (called from session via root.after) ──
    def append_log(self, line: str):
        self._log.config(state="normal")
        self._log.insert("end", line + "\n")
        # trim
        n = int(self._log.index("end-1c").split(".")[0])
        if n > config.LOG_MAX_LINES:
            self._log.delete("1.0", f"{n - config.LOG_MAX_LINES}.0")
        self._log.see("end")
        self._log.config(state="disabled")

    # ── periodic render ──
    def _tick(self):
        s = self._session.status()
        self._render(s)
        if self._preview is not None:
            self._update_preview()
        self.after(config.UI_POLL_MS, self._tick)

    def _render(self, s):
        # connection
        if self._session._link.is_sim:
            self._lbl_conn.config(text="● Simulator", fg=ACCENT)
        elif s.connected:
            self._lbl_conn.config(text="● Arduino", fg=GREEN)
        else:
            self._lbl_conn.config(text="● Chờ Arduino…", fg=RED)

        # count + total
        self._lbl_count.config(text=str(s.count))
        self._lbl_total.config(text=self._total_text())

        # status light + label.
        # The finished-batch result is LATCHED in session.outcome(): right after
        # B0 the firmware sends ST st=OFF err=NONE (doMachineOff resets lastErr),
        # which would otherwise wipe the DONE/STALL result off the screen.
        oc = self._session.outcome()
        running = self._session.is_running
        if oc:
            color, label = self._outcome_view(oc)
        elif running:
            color, label = _STATE_VIEW.get(s.state, (GREEN, "● Đang chạy…"))
        elif s.state == ERROR:
            color, label = RED, "✕ Lỗi"
        else:
            color, label = _STATE_VIEW.get(s.state, (TEXT_DIM, s.state))
        self._canvas.itemconfig(self._dot, fill=color)
        self._lbl_state.config(text=label, fg=color)

        # button + stepper enable state
        if running:
            self._btn_start.pack_forget()
            self._btn_stop.pack(pady=8)
            self._set_stepper(False)
        else:
            self._btn_stop.pack_forget()
            self._btn_start.pack(pady=8)
            self._set_stepper(True)
            # block START until we actually have a link (sim always ok)
            can_start = s.connected
            self._btn_start.config(state="normal" if can_start else "disabled")
            self._lbl_hint.config(
                text="" if can_start else "Đang chờ kết nối Arduino…")

    def _outcome_view(self, oc):
        """Colour + label for a finished batch (latched in session.outcome())."""
        kind, cnt, tot = oc["kind"], oc["count"], oc["total"]
        if kind == "done":
            return GREEN, "✓ Xong mẻ!"
        if kind == "stall":
            # firmware reports STALL ("no more leaves") in both modes:
            #   pull-all (target 0) -> that's the normal end -> green
            #   fixed target        -> stopped before target -> flag amber
            if tot == 0:
                return GREEN, f"✓ Hết lá — đã xong ({cnt})"
            return YELLOW, f"⚠ Dừng sớm: {cnt}/{tot}"
        if kind == "limit":
            return RED, "✕ Chạm giới hạn"
        return TEXT_DIM, f"■ Đã dừng ({cnt})"   # manual stop

    def _set_stepper(self, on: bool):
        st = "normal" if on else "disabled"
        self._btn_minus.config(state=st)
        self._btn_plus.config(state=st)

    def _update_preview(self):
        if ImageTk is None:
            return
        jpeg = self._session.get_preview_frame()
        if not jpeg:
            return
        try:
            img = Image.open(io.BytesIO(jpeg)).resize((PREVIEW_W, PREVIEW_H))
            self._preview_imgtk = ImageTk.PhotoImage(img)
            self._preview.config(image=self._preview_imgtk, text="")
        except Exception:
            pass

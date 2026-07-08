"""
Session — orchestrates one batch around the START/STOP buttons.

START (operator):   send N<total>  ->  camera.start()  ->  send B1
STOP / DONE / STALL: send B0  ->  camera.stop+keep  ->  upload (if enabled)

The Arduino can also end the batch on its own ([DONE] = target reached,
[STALL] = out of leaves); on_line() detects that and finishes automatically,
so the operator gets the same outcome whether they press STOP or the machine
runs out.

Threading model:
  * the serial reader thread calls on_line() — it must stay fast, so any slow
    work (stopping ffmpeg, uploading) is pushed onto a worker thread.
  * status + log are guarded by a lock; the UI reads snapshots via status().
  * the UI is notified through on_status()/on_log(line) callbacks, which the
    UI wraps in root.after(0, ...) to marshal back onto the Tk thread.
"""

import datetime
import logging
import threading

import config
from parser import ERR_NONE, RUN, MachineStatus, parse_line

logger = logging.getLogger("bss.session")


class Session:
    def __init__(self, link, camera_factory=None, uploader=None):
        self._link = link
        self._camera_factory = camera_factory   # callable(batch_id) -> Recorder | None
        self._uploader = uploader               # callable(video_path, meta) -> bool | None
        self._lock = threading.Lock()
        self._status = MachineStatus()
        self._running = False        # a batch we started is active
        self._finishing = False      # guard against double-finish
        self._recorder = None
        self._batch_id = None
        self._batch_started = None
        self._total = config.DEFAULT_TOTAL
        # latched result of the last finished batch (survives the trailing
        # ST st=OFF the firmware sends after B0); cleared on the next start.
        # {"kind": done|stall|limit|stopped, "count": int, "total": int} | None
        self._outcome = None

        # UI hooks (set via set_callbacks); default no-ops
        self._on_status = lambda: None
        self._on_log = lambda line: None

    # ── wiring ──
    def set_callbacks(self, on_status=None, on_log=None) -> None:
        if on_status:
            self._on_status = on_status
        if on_log:
            self._on_log = on_log

    @property
    def total(self) -> int:
        return self._total

    def set_total(self, total: int) -> None:
        self._total = max(config.TOTAL_MIN, min(config.TOTAL_MAX, int(total)))

    @property
    def is_running(self) -> bool:
        return self._running

    def status(self) -> MachineStatus:
        with self._lock:
            s = self._status.copy()
            s.connected = self._link.connected
            return s

    def outcome(self) -> dict | None:
        with self._lock:
            return dict(self._outcome) if self._outcome else None

    # ── incoming serial lines ──
    def on_line(self, line: str) -> None:
        with self._lock:
            self._status, event = parse_line(line, self._status)
            self._status.connected = self._link.connected
            if event in ("done", "stall") and self._running:
                # latch the terminal result NOW, before B0 -> ST st=OFF arrives
                self._outcome = {"kind": event, "count": self._status.count,
                                 "total": self._total}
        self._on_log(line)
        self._on_status()
        if event in ("done", "stall"):
            # machine ended the batch itself (target reached / out of leaves).
            # [LIMIT] is a non-fatal warning, NOT a batch end — see parser.py.
            self._auto_finish(event)

    # ── START ──
    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._finishing = False
            self._outcome = None   # clear the previous batch result
        threading.Thread(target=self._do_start, daemon=True).start()
        return True

    def _do_start(self) -> None:
        self._batch_id = datetime.datetime.now().strftime("batch_%Y%m%d_%H%M%S")
        self._batch_started = datetime.datetime.now()
        total = self._total
        logger.info("START batch %s (total=%d)", self._batch_id, total)

        # reset the displayed count immediately (firmware also resets cardCount=0
        # on B1) so the UI shows 0/total the moment START is pressed
        with self._lock:
            self._status.count = 0
            self._status.error = ERR_NONE
            self._status.state = RUN
        self._on_status()

        # 1) tell the Arduino how many leaves this batch
        self._link.send(f"N{total}")

        # 2) start the camera (optional) BEFORE the motor, so nothing is missed
        if self._camera_factory:
            try:
                rec = self._camera_factory(self._batch_id)
                rec.start()
                if getattr(rec, "error", None):
                    logger.error("camera error: %s", rec.error)
                    self._on_log(f"[PI] Camera loi: {rec.error}")
                else:
                    self._recorder = rec
            except Exception as e:
                logger.exception("camera start failed")
                self._on_log(f"[PI] Camera loi: {e}")

        # 3) run the motor
        self._link.send("B1")
        self._on_log(f"[PI] START {self._batch_id} (N={total})")
        self._on_status()

    # ── STOP (operator) ──
    def stop(self) -> bool:
        if not self._running:
            return False
        with self._lock:
            self._outcome = {"kind": "stopped", "count": self._status.count,
                             "total": self._total}
        # stop the motor immediately, then tidy up off-thread
        self._link.send("B0")
        threading.Thread(target=self._finish, args=("stop",), daemon=True).start()
        return True

    # ── machine-initiated finish ──
    def _auto_finish(self, reason: str) -> None:
        if not self._running:
            return
        self._link.send("B0")  # idempotent; ensure motor off + homed
        threading.Thread(target=self._finish, args=(reason,), daemon=True).start()

    def _finish(self, reason: str) -> None:
        with self._lock:
            if self._finishing or not self._running:
                return
            self._finishing = True
        try:
            self._on_log(f"[PI] FINISH ({reason})")
            video_path = None
            rec = self._recorder
            self._recorder = None
            if rec is not None:
                try:
                    video_path = rec.stop_and_keep()
                except Exception:
                    logger.exception("camera stop failed")

            if video_path and self._uploader:
                meta = self._build_meta(reason)
                try:
                    ok = self._uploader(video_path, meta)
                    self._on_log(f"[PI] Upload {'OK' if ok else 'FAIL'}")
                except Exception as e:
                    logger.exception("upload failed")
                    self._on_log(f"[PI] Upload loi: {e}")
            elif video_path:
                self._on_log(f"[PI] Video luu tai: {video_path}")
        finally:
            with self._lock:
                self._running = False
                self._finishing = False
            self._on_status()

    def _build_meta(self, reason: str) -> dict:
        s = self.status()
        return {
            "batch_id": self._batch_id,
            "total": self._total,
            "count": s.count,
            "error": s.error,
            "reason": reason,
            "started_at": self._batch_started.isoformat() if self._batch_started else None,
            "ended_at": datetime.datetime.now().isoformat(),
        }

    # ── ask the Arduino for an immediate status line ──
    def request_status(self) -> None:
        self._link.send("S")

    def get_preview_frame(self) -> bytes | None:
        rec = self._recorder
        return rec.get_latest_jpeg() if rec is not None else None

    def shutdown(self) -> None:
        if self._recorder is not None:
            try:
                self._recorder.stop_and_discard()
            except Exception:
                pass
        self._link.stop()

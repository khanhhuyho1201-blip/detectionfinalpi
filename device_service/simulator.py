"""
FakeArduino — a software stand-in for the real board.

It mimics the REAL firmware ("SMART CARD FEEDER v5.51") closely enough to test
the whole Pi5 app with no hardware. SerialLink uses it when SERIAL_PORT == "sim".

Faithful details that matter for the Pi:
  * B0 / doMachineOff emits  [MACHINE] OFF + [MACHINE] READY + ST st=OFF err=NONE
    (the trailing OFF that the Pi must NOT let wipe the DONE/STALL result)
  * reaching target  -> [DONE] da dem <n> la   + ST st=DONE   (no "total=")
  * out of leaves    -> [STALL] khong co la ... + ST st=ERROR err=STALL
  * spd= is PWM 0..255 (NOT leaves/sec), matching firmware

Commands in : B1, B0, N<n>, S      Pacing knobs: BSS_SIM_LEAF_MS / _CLUMP_PCT / _STALL_AT
"""

import logging
import random
import threading
import time

from settings import settings

logger = logging.getLogger("bss.sim")

ST_PERIOD = 0.25


class FakeArduino:
    def __init__(self, emit):
        self._emit = emit
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._state = "IDLE"     # IDLE | RUN | DONE | ERROR | OFF
        self._err = "NONE"       # NONE | CLUMP | STALL | LIMIT
        self._count = 0
        self._total = settings.batch.target_fallback
        # per-batch knobs from settings (BSS_SIM_* trong .env)
        self._leaf_dt = settings.sim.leaf_ms / 1000.0
        self._clump = settings.sim.clump_pct
        self._stall = settings.sim.stall_at

    # ── lifecycle ──
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._emit("[STATUS] simulator ready")
        self._emit_st()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    # ── command intake ──
    def handle_command(self, cmd: str) -> None:
        cmd = cmd.strip()
        if not cmd:
            return
        head = cmd[0].upper()
        if head == "B" and cmd[1:].strip() == "1":
            self._do_run()
        elif head == "B" and cmd[1:].strip() == "0":
            self._do_stop()
        elif head == "N":
            try:
                self._total = max(0, int(cmd[1:].strip() or "0"))
            except ValueError:
                pass
            self._emit_st()
        elif head == "S":
            self._emit_st()

    def _do_run(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._state = "RUN"
            self._err = "NONE"
            self._count = 0
            # capture pacing knobs for this batch (BSS_SIM_* qua settings)
            self._leaf_dt = settings.sim.leaf_ms / 1000.0
            self._clump = settings.sim.clump_pct
            self._stall = settings.sim.stall_at
        self._emit("[MACHINE] ON")
        self._emit_st()

    def _do_stop(self) -> None:
        # mirrors firmware doMachineOff(): runs on ANY B0, resets lastErr,
        # homes, and reports st=OFF err=NONE.
        with self._lock:
            self._running = False
            self._state = "OFF"
            self._err = "NONE"
        self._emit("[MACHINE] OFF")
        self._emit("[MACHINE] READY")
        self._emit_st()

    # ── worker ──
    def _loop(self) -> None:
        next_leaf = time.monotonic()
        next_st = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                running = self._running
                leaf_dt = self._leaf_dt
            if running and now >= next_leaf:
                next_leaf = now + leaf_dt
                self._tick_leaf()
            if running and now >= next_st:
                next_st = now + ST_PERIOD
                self._emit_st()
            time.sleep(0.01)

    def _tick_leaf(self) -> None:
        # Mo phong TRUNG THUC firmware: moi la co 2 pha.
        #  1) SUON XUONG (la TOI sensor): firmware phat ST n=cardCount+1 (LAC QUAN, real-time).
        #  2) SUON LEN (la ROI, da do do dai): cardCount += n, phat [CARD] + ST n=cardCount (CHOT).
        # FALSE-TRIGGER: la cham sensor roi TRUOT lai -> phat +1 roi TUT ve (khong [CARD]).
        #   Day chinh la nguon glitch LUI tren phan cung that -> dung de test Pi co chong lui khong.
        with self._lock:
            count = self._count        # committed hien tai
            total = self._total
            clump_pct, stall_at = self._clump, self._stall
            leaf_ms = int(self._leaf_dt * 1000)
            false_pct = settings.sim.false_pct
            optimistic = settings.sim.optimistic

        # --- FALSE TRIGGER: arrival +1 roi khong chot (tai hien lui o serial tho) ---
        if optimistic and false_pct > 0 and random.random() * 100.0 < false_pct:
            self._emit_st_n(count + 1)   # sensor LOW: hien lac quan +1
            time.sleep(0.02)
            self._emit_st_n(count)       # truot lai: TUT ve committed (glitch lui tho)
            return                        # KHONG [CARD], khong tang count

        # --- LA THAT: arrival (+1) -> commit ---
        if optimistic:
            self._emit_st_n(count + 1)   # SUON XUONG: real-time tick khi la toi
            time.sleep(0.01)

        with self._lock:
            self._count += 1
            count = self._count
        if random.random() * 100.0 < clump_pct:
            self._emit(f"[CLUMP] {count} la 1 luc len=820 ratio=1.9")
        dt = leaf_ms + random.randint(-3, 3)
        pwm = 130 + random.randint(-8, 8)
        self._emit(f"[CARD] #{count} | REM={max(0,total-count)} | dt={dt}ms | PWM={pwm}")  # SUON LEN: CHOT
        self._emit_st_n(count)           # ST committed

        if stall_at > 0 and count >= stall_at:
            self._finish(stall=True)
            return
        if total > 0 and count >= total:
            self._finish(stall=False)

    def _emit_st_n(self, n: int) -> None:
        """Phat 1 dong ST st=RUN voi n= chi dinh (mo phong emitStatus cua firmware)."""
        with self._lock:
            state, err, total, running = self._state, self._err, self._total, self._running
        spd = 130 + random.randint(-8, 8) if running else 0
        self._emit(f"ST st={state} n={n} tot={total} err={err} spd={spd}")

    def _finish(self, stall: bool) -> None:
        with self._lock:
            self._running = False
            count = self._count
            if stall:
                self._state, self._err = "ERROR", "STALL"
            else:
                self._state, self._err = "DONE", "NONE"
        if stall:
            self._emit(f"[STALL] khong co la 1500ms -> DA DUNG MOTOR tai #{count}")
        else:
            self._emit(f"[DONE] da dem {count} la (hoan tat me)")
        self._emit_st()

    def _emit_st(self) -> None:
        with self._lock:
            state, err, count, total = self._state, self._err, self._count, self._total
            running = self._running
        spd = 130 + random.randint(-8, 8) if running else 0
        self._emit(f"ST st={state} n={count} tot={total} err={err} spd={spd}")

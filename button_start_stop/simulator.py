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
import os
import random
import threading
import time

import config

logger = logging.getLogger("bss.sim")

ST_PERIOD = 0.25


def _leaf_ms() -> int:
    return int(os.environ.get("BSS_SIM_LEAF_MS", "80"))


def _clump_pct() -> float:
    return float(os.environ.get("BSS_SIM_CLUMP_PCT", "2"))


def _stall_at() -> int:
    return int(os.environ.get("BSS_SIM_STALL_AT", "0"))


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
        self._total = config.DEFAULT_TOTAL
        # per-batch knobs, captured at B1 from the env
        self._leaf_dt = _leaf_ms() / 1000.0
        self._clump = _clump_pct()
        self._stall = _stall_at()

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
            # capture pacing knobs for this batch (lets tests/runtime change them)
            self._leaf_dt = _leaf_ms() / 1000.0
            self._clump = _clump_pct()
            self._stall = _stall_at()
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
        with self._lock:
            self._count += 1
            count = self._count
            total = self._total
            clump_pct, stall_at, leaf_ms = self._clump, self._stall, int(self._leaf_dt * 1000)

        if random.random() * 100.0 < clump_pct:
            self._emit(f"[CLUMP] {count} la 1 luc len=820 ratio=1.9")

        dt = leaf_ms + random.randint(-3, 3)
        pwm = 130 + random.randint(-8, 8)   # PWM 0..255, not leaves/sec
        self._emit(f"[CARD] #{count} | REM={max(0,total-count)} | dt={dt}ms | PWM={pwm}")

        if stall_at > 0 and count >= stall_at:
            self._finish(stall=True)
            return
        if total > 0 and count >= total:
            self._finish(stall=False)

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

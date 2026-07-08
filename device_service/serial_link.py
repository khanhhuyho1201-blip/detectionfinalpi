"""
SerialLink — one object the rest of the app talks to, whether the other end is
a real Arduino on /dev/ttyACM0 or the built-in simulator (SERIAL_PORT="sim").

  * background reader thread splits incoming bytes into lines -> on_line(text)
  * send(cmd) appends "\n" and writes (routes to the simulator in sim mode)
  * auto-reconnect: if the port can't open or drops, it keeps retrying so
    unplugging/replugging the USB cable never crashes the app
"""

import logging
import threading

from settings import settings

logger = logging.getLogger("bss.serial")

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover - pyserial is present on the Pi
    serial = None


class SerialLink:
    def __init__(self, on_line, port: str | None = None, baud: int | None = None):
        self._on_line = on_line
        self._port = port or settings.serial.port
        self._baud = baud or settings.serial.baud
        self._is_sim = self._port == "sim"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ser = None
        self._sim = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected or self._is_sim

    @property
    def is_sim(self) -> bool:
        return self._is_sim

    @property
    def port(self) -> str:
        return self._port

    # ── lifecycle ──
    def start(self) -> None:
        if self._is_sim:
            from simulator import FakeArduino
            self._sim = FakeArduino(emit=self._emit_line)
            self._sim.start()
            self._connected = True
            logger.info("serial: using simulator")
            return
        if serial is None:
            logger.error("pyserial not installed — cannot open %s", self._port)
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sim:
            self._sim.stop()
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
        if self._thread:
            self._thread.join(timeout=2)

    # ── send ──
    def send(self, cmd: str) -> bool:
        cmd = cmd.strip()
        if self._is_sim:
            if self._sim:
                self._sim.handle_command(cmd)
                return True
            return False
        with self._lock:
            ser = self._ser
        if ser is None:
            logger.warning("send(%r) dropped — port not open", cmd)
            return False
        try:
            ser.write((cmd + "\n").encode("ascii", "ignore"))
            ser.flush()
            return True
        except Exception as e:
            logger.warning("send(%r) failed: %s", cmd, e)
            return False

    # ── internals ──
    def _emit_line(self, line: str) -> None:
        try:
            self._on_line(line)
        except Exception:
            logger.exception("on_line callback raised")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ser = serial.Serial(self._port, self._baud, timeout=0.2)
            except Exception as e:
                self._connected = False
                logger.info("open %s failed (%s) — retry in %ss",
                            self._port, e, settings.serial.reconnect_delay)
                self._stop.wait(settings.serial.reconnect_delay)
                continue

            # Force a clean UNO reset and drop any garbage left over from a hot
            # unplug/replug. Without this the re-opened stream stays framing-
            # desynced: the reader only sees noise with no newline, so the
            # handshake never sees a line and fails with MCU-02 until the whole
            # service is restarted. Toggling DTR low->high retriggers the auto-
            # reset; flushing after boot discards the boot/garbage bytes.
            try:
                ser.dtr = False
                self._stop.wait(0.1)
                ser.dtr = True
            except Exception:
                pass

            with self._lock:
                self._ser = ser
            # UNO auto-resets when the port opens — give it time to boot.
            self._stop.wait(settings.serial.boot_delay)
            try:
                ser.reset_input_buffer()
                ser.reset_output_buffer()
            except Exception:
                pass
            self._connected = True
            logger.info("serial open: %s @ %d", self._port, self._baud)

            buf = b""
            try:
                while not self._stop.is_set():
                    chunk = ser.read(256)
                    if not chunk:
                        continue  # read timeout, nothing yet
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        text = raw.decode("utf-8", "replace").strip("\r\n ")
                        if text:
                            self._emit_line(text)
            except Exception as e:
                logger.info("serial read error (%s) — reconnecting", e)
            finally:
                self._connected = False
                with self._lock:
                    self._ser = None
                try:
                    ser.close()
                except Exception:
                    pass

            if not self._stop.is_set():
                self._stop.wait(settings.serial.reconnect_delay)

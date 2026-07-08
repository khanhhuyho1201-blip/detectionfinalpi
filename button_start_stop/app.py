"""
app.py — entry point. Wires config -> serial link + session (+ optional camera
and uploader) -> touchscreen UI, then runs the Tk loop.

Run:
    python3 app.py                 # real Arduino on /dev/ttyACM0
    BSS_SERIAL_PORT=sim python3 app.py     # no hardware, built-in simulator
    BSS_CAMERA=1 BSS_SERIAL_PORT=sim python3 app.py   # + webcam recording
"""

import logging

import config
from serial_link import SerialLink
from session import Session
from ui import MachineUI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bss")


def main() -> None:
    # optional camera (M5)
    camera_factory = None
    if config.CAMERA_ENABLED:
        from camera import Recorder
        camera_factory = lambda batch_id: Recorder(batch_id)  # noqa: E731

    # optional uploader (M6) — None when disabled / unconfigured
    from uploader import make_uploader
    uploader = make_uploader()

    # serial <-> session: the lambda resolves `session` at call time, so it is
    # fine that session is created on the next line.
    link = SerialLink(on_line=lambda line: session.on_line(line))
    session = Session(link, camera_factory=camera_factory, uploader=uploader)

    ui = MachineUI(session)
    # push each serial line into the log widget on the Tk thread
    session.set_callbacks(on_log=lambda line: ui.after(0, ui.append_log, line))

    link.start()
    log.info("ready: serial=%s camera=%s upload=%s",
             config.SERIAL_PORT, config.CAMERA_ENABLED, bool(uploader))
    try:
        ui.mainloop()
    finally:
        session.shutdown()


if __name__ == "__main__":
    main()

"""
Central configuration for the button_start_stop app.

Every value can be overridden with an environment variable (prefix BSS_) so the
same code runs on the bench (simulator, no camera/server) and on the real
machine (Arduino + webcam + upload) without edits.

Milestone mapping (see README.md):
  M1-M3  serial + UI            -> works with SERIAL_PORT=sim, CAMERA off, UPLOAD off
  M5     camera                 -> BSS_CAMERA=1
  M6     upload                 -> BSS_UPLOAD=1 (+ server URL/creds)
"""

import os


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ── Serial link to the Arduino (Part C protocol) ──
# Set to "sim" to use the built-in fake Arduino (simulator.py) — no hardware
# needed. Otherwise a real device path like /dev/ttyACM0.
SERIAL_PORT = os.environ.get("BSS_SERIAL_PORT", "/dev/ttyACM0")
SERIAL_BAUD = int(os.environ.get("BSS_SERIAL_BAUD", "115200"))
# UNO auto-resets when the port opens; wait this long before trusting it.
ARDUINO_BOOT_DELAY = float(os.environ.get("BSS_ARDUINO_BOOT", "2.0"))
# How long to wait before retrying a port that failed to open / dropped.
SERIAL_RECONNECT_DELAY = float(os.environ.get("BSS_SERIAL_RECONNECT", "2.0"))

# ── Batch ──
# Number of leaves per batch. The UI lets the operator change this before
# starting (appendix item 6). N0 means "pull until empty" (no target).
DEFAULT_TOTAL = int(os.environ.get("BSS_DEFAULT_TOTAL", "412"))
TOTAL_MIN = int(os.environ.get("BSS_TOTAL_MIN", "0"))
TOTAL_MAX = int(os.environ.get("BSS_TOTAL_MAX", "9999"))
TOTAL_STEP = int(os.environ.get("BSS_TOTAL_STEP", "10"))

# ── Camera (M5) ──
CAMERA_ENABLED = _env_bool("BSS_CAMERA", False)
VIDEO_DEVICE = os.environ.get("BSS_VIDEO_DEVICE", "/dev/video0")
VIDEO_SIZE = os.environ.get("BSS_VIDEO_SIZE", "1280x720")
VIDEO_FPS = os.environ.get("BSS_VIDEO_FPS", "30")
CAM_EXPOSURE = os.environ.get("BSS_EXPOSURE", "400")
VIDEO_DIR = os.path.expanduser(os.environ.get("BSS_VIDEO_DIR", "~/bss_videos"))

# ── Upload (M6) — server is not finalised yet (appendix item 5) ──
# When off, finished clips are kept locally and the path is logged. When on,
# uploader.py POSTs the clip + metadata to UPLOAD_URL. Fill the endpoint/fields
# in uploader.py once the server contract is known.
UPLOAD_ENABLED = _env_bool("BSS_UPLOAD", False)
UPLOAD_URL = os.environ.get("BSS_UPLOAD_URL", "")
UPLOAD_MAX_RETRIES = int(os.environ.get("BSS_UPLOAD_MAX_RETRIES", "5"))
UPLOAD_RETRY_DELAY = float(os.environ.get("BSS_UPLOAD_RETRY_DELAY", "2"))

# ── UI ──
FULLSCREEN = _env_bool("BSS_FULLSCREEN", True)
# debug/demo: auto-press START shortly after launch (useful for screenshots)
AUTOSTART = _env_bool("BSS_AUTOSTART", False)
LOG_MAX_LINES = int(os.environ.get("BSS_LOG_MAX_LINES", "200"))
UI_POLL_MS = int(os.environ.get("BSS_UI_POLL_MS", "100"))

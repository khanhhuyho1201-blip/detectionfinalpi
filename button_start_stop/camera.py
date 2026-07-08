"""
Recorder — records the USB webcam to an MP4 while exposing live preview frames.

Lifted from the proven device_service implementation: one ffmpeg process reads
the cam once and produces two outputs — an H.264 MP4 file (kept/uploaded) and an
MJPEG stream on stdout (decoded into preview frames for the UI). Reads its
settings from config.py and names the file after the batch id.

Used only when config.CAMERA_ENABLED is true; the rest of the app runs fine
without a camera (serial-only bench testing).
"""

import logging
import os
import signal
import subprocess
import threading

import config

logger = logging.getLogger("bss.camera")

_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"

# preview frame size pushed to the UI (smaller = lighter for Tk)
PREVIEW_W = 640
PREVIEW_H = 360


def ensure_dir() -> None:
    os.makedirs(config.VIDEO_DIR, exist_ok=True)


class Recorder:
    def __init__(self, batch_id: str):
        ensure_dir()
        self.batch_id = batch_id
        self.output_path = os.path.join(config.VIDEO_DIR, f"{batch_id}.mp4")
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._running = False
        self._error: str | None = None

    @property
    def error(self) -> str | None:
        return self._error

    def _lock_exposure(self) -> None:
        """Force manual exposure so the cam keeps full fps in low light.
        Best-effort: missing v4l2-ctl or unsupported control is non-fatal."""
        try:
            subprocess.run(
                ["v4l2-ctl", "--device", config.VIDEO_DEVICE,
                 "--set-ctrl", "auto_exposure=1",
                 "--set-ctrl", f"exposure_time_absolute={config.CAM_EXPOSURE}"],
                check=False, capture_output=True, timeout=5,
            )
        except Exception as e:
            logger.warning("Could not set manual exposure: %s", e)

    def start(self) -> None:
        self._lock_exposure()
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", config.VIDEO_SIZE,
            "-framerate", config.VIDEO_FPS,
            "-i", config.VIDEO_DEVICE,
            # output 1: the file we keep / upload
            "-map", "0:v", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            self.output_path,
            # output 2: live preview frames as MJPEG on stdout
            "-map", "0:v", "-vf", f"scale={PREVIEW_W}:{PREVIEW_H}",
            "-q:v", "7", "-f", "mjpeg", "pipe:1",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            )
        except FileNotFoundError:
            self._error = "ffmpeg not found"
            logger.error(self._error)
            return
        self._running = True
        self._reader = threading.Thread(target=self._read_frames, daemon=True)
        self._reader.start()
        logger.info("Recording started -> %s", self.output_path)

    def _read_frames(self) -> None:
        """Split the MJPEG stdout stream into individual JPEG frames.
        Must drain until EOF so ffmpeg's pipe never fills (else it deadlocks)."""
        buf = b""
        stdout = self._proc.stdout
        while True:
            chunk = stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(_JPEG_SOI)
                if start < 0:
                    break
                end = buf.find(_JPEG_EOI, start + 3)
                if end < 0:
                    if start > 0:
                        buf = buf[start:]
                    break
                end += 2
                frame = buf[start:end]
                buf = buf[end:]
                with self._lock:
                    self._latest_jpeg = frame

    def get_latest_jpeg(self) -> bytes | None:
        with self._lock:
            return self._latest_jpeg

    def _stop_proc(self) -> int:
        if not self._proc:
            self._running = False
            return -1
        try:
            # SIGINT lets ffmpeg flush the moov atom and write a valid MP4
            self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                self._proc.wait(timeout=5)
        except Exception as e:
            logger.warning("Error stopping ffmpeg: %s", e)
            try:
                self._proc.kill()
            except Exception:
                pass
        self._running = False
        rc = self._proc.returncode if self._proc.returncode is not None else 0
        if self._reader:
            self._reader.join(timeout=3)
        return rc

    def stop_and_keep(self) -> str | None:
        self._stop_proc()
        if not os.path.exists(self.output_path) or os.path.getsize(self.output_path) == 0:
            logger.error("Recording stopped but output file is missing/empty")
            return None
        logger.info("Recording kept: %s (%d bytes)",
                    self.output_path, os.path.getsize(self.output_path))
        return self.output_path

    def stop_and_discard(self) -> None:
        self._stop_proc()
        delete_video(self.output_path)
        logger.info("Recording discarded")


def delete_video(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

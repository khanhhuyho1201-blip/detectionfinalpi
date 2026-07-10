import logging
import os
import signal
import subprocess
import threading

from settings import settings

logger = logging.getLogger(__name__)

# Giá trị lấy từ settings.py (.env) — giữ tên constant vì module khác
# import từ đây (controller/app dùng TMP_DIR).
TMP_DIR = str(settings.paths.tmp_dir)
VIDEO_DEVICE = settings.camera.device
VIDEO_SIZE = settings.camera.size
VIDEO_FPS = settings.camera.fps
FAKE_CAMERA = settings.fake.camera
# Values: "notfound" → CAM-01 style failure, "busy" → CAM-02 style failure,
#         "ok" → probe passes immediately (no real device needed), "" → real check
# CARD_FAKE_RECORDER=1 makes Recorder run without ffmpeg/hardware (test only):
# writes a placeholder file and serves a static JPEG as the preview frame.
FAKE_RECORDER = settings.fake.recorder
# CARD_FAKE_RECORDER=1: Recorder.start() creates a tiny placeholder MP4 instead of
# running ffmpeg. Lets the full cycle (warmup→recording→upload) run without hardware.

# Exposure. AUTO exposure looks evenly bright but the cam drops to ~20 fps in
# the dim chamber, so the feed stutters. We default to MANUAL exposure = 400
# (40ms): measured as the highest exposure that still holds the full 30 fps
# (>=500 drops to ~24 fps). At 400 the lit counting slot — where the cards
# actually are — reads bright and sharp, only the chamber edges go dark, and
# the feed is smooth.
#
# CARD_EXPOSURE env override:
#   "<number>" (default 400) -> manual mode, exposure_time_absolute=<number>
#                               (156..500 keep 30 fps; higher = brighter but slower)
#   "auto"                   -> auto_exposure (evenly bright, ~20 fps)
CAM_EXPOSURE = settings.camera.exposure

# preview frame size (smaller = lighter for the Tk UI)
PREVIEW_W = settings.camera.preview_w
PREVIEW_H = settings.camera.preview_h

_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"


def ensure_tmp():
    os.makedirs(TMP_DIR, exist_ok=True)


def _fsync_file(path: str) -> None:
    """Flush a file's data to stable storage (power-loss durability)."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def _fsync_dir(path: str) -> None:
    """Flush a directory entry (so a rename is durable across power loss)."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def probe(timeout: float = 5.0) -> tuple[bool, str]:
    """Lightweight camera-connection check (does NOT start recording).

    Confirms the device node exists and v4l2 can enumerate its formats. Returns
    (ok, message). Used by the pre-flight check sequence.
    """
    if FAKE_CAMERA == "notfound":
        return False, f"Không thấy {VIDEO_DEVICE}"
    if FAKE_CAMERA == "busy":
        return False, f"{VIDEO_DEVICE} không phản hồi v4l2"
    if FAKE_CAMERA == "ok":
        return True, f"{VIDEO_DEVICE} (simulated)"
    if not os.path.exists(VIDEO_DEVICE):
        return False, f"Không thấy {VIDEO_DEVICE}"
    try:
        r = subprocess.run(
            ["v4l2-ctl", "--device", VIDEO_DEVICE, "--list-formats"],
            capture_output=True, timeout=timeout,
        )
    except FileNotFoundError:
        logger.info("v4l2-ctl không có — bỏ qua kiểm tra định dạng (CAM-07)")
        return True, f"{VIDEO_DEVICE} (chưa kiểm định dạng)"
    except Exception as e:
        return False, str(e)[:60]
    if r.returncode != 0:
        return False, f"{VIDEO_DEVICE} không phản hồi v4l2"
    return True, VIDEO_DEVICE


class Recorder:
    """Records the webcam to an MP4 while emitting live preview frames.

    A single ffmpeg process reads the USB cam once and produces two outputs:
      1. an H.264 MP4 file (what we upload)
      2. an MJPEG stream on stdout (decoded into preview frames for the UI)

    Recording runs until stop_and_keep() (finish, keep file) or
    stop_and_discard() (cancel, delete file) is called — no fixed duration.
    """

    def __init__(self, run_id: str):
        ensure_tmp()
        self.run_id = run_id
        # Record to a *.mp4.part sidecar; atomically rename to the final *.mp4 only
        # after a clean stop (moov flushed). A power cut mid-recording can then only
        # ever leave a *.mp4.part (ignored + cleaned on boot), NEVER a truncated
        # *.mp4 that _scan_pending would upload as if valid (BA_kiosk_and_video_power_loss.md R1).
        self.output_path = os.path.join(TMP_DIR, f"{run_id}.mp4")
        self.part_path = self.output_path + ".part"
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._running = False
        self._error: str | None = None

    def _set_exposure(self) -> None:
        """Configure exposure/white-balance before recording.

        Default ("auto"): auto exposure + auto white balance so the feed is
        bright and correctly coloured in the dim chamber. A numeric
        CARD_EXPOSURE locks manual exposure to that value (old behaviour).

        Best-effort: if v4l2-ctl is missing or a control is unsupported we just
        log and carry on — recording still works, only image quality may dip.
        """
        if CAM_EXPOSURE.lower() == "auto":
            ctrls = ["auto_exposure=3",            # 3 = Aperture Priority (auto)
                     "white_balance_automatic=1",
                     "brightness=0"]               # undo any leftover negative bias
        else:
            ctrls = ["auto_exposure=1",            # 1 = Manual
                     f"exposure_time_absolute={CAM_EXPOSURE}",
                     "white_balance_automatic=1",
                     "brightness=0"]
        try:
            cmd = ["v4l2-ctl", "--device", VIDEO_DEVICE]
            for c in ctrls:
                cmd += ["--set-ctrl", c]
            subprocess.run(cmd, check=False, capture_output=True, timeout=5)
        except Exception as e:
            logger.warning(f"Could not set camera exposure: {e}")

    def start(self) -> None:
        if FAKE_RECORDER:
            self._start_fake()
            return
        self._set_exposure()
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-video_size", VIDEO_SIZE,
            "-framerate", VIDEO_FPS,
            "-i", VIDEO_DEVICE,
            # output 1: the file we keep / upload. ultrafast + zerolatency so the
            # Pi's CPU encodes in real time (default 'medium' ran at ~0.56x and
            # dropped frames -> stutter). Keeps 720p; crf 24 trims the file a lot
            # (was ~1.3MB/s at default, now far smaller) so uploads are quicker.
            "-map", "0:v", "-c:v", "libx264",
            "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "24",
            "-pix_fmt", "yuv420p",
            # force the mp4 muxer: the output ends in ".part" so ffmpeg can't infer
            # the container from the extension.
            "-f", "mp4", self.part_path,
            # output 2: live preview frames as MJPEG on stdout
            "-map", "0:v", "-vf", f"scale={PREVIEW_W}:{PREVIEW_H}",
            "-q:v", "7", "-f", "mjpeg", "pipe:1",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError:
            self._error = "ffmpeg not found"
            logger.error(self._error)
            return

        self._running = True
        self._reader = threading.Thread(target=self._read_frames, daemon=True)
        self._reader.start()
        logger.info(f"Recording started -> {self.output_path}")

    # Minimal valid 1x1 black JPEG (raw bytes, no PIL dependency)
    _BLACK_JPEG = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
        b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
        b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x1e"
        b"C  C\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08"
        b"\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03"
        b"\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12"
        b"!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1"
        b"\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTU"
        b"VWXYZ cdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93"
        b"\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9"
        b"\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6"
        b"\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2"
        b"\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7"
        b"\xf8\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd4P\x00"
        b"\x00\x00\x1f\xff\xd9"
    )

    def _start_fake(self) -> None:
        """Fake recorder: create a placeholder MP4 immediately, emit a black JPEG frame."""
        ensure_tmp()
        # Write a non-empty placeholder file (valid enough for os.path.getsize checks)
        # to the .part sidecar — stop_and_keep() renames it to the final .mp4.
        with open(self.part_path, "wb") as fh:
            fh.write(b"\x00" * 1024)   # 1 KB placeholder
        with self._lock:
            self._latest_jpeg = self._BLACK_JPEG
        self._running = True
        logger.info(f"Fake recorder started -> {self.output_path}")

    def _read_frames(self) -> None:
        """Split the MJPEG stdout stream into individual JPEG frames."""
        buf = b""
        stdout = self._proc.stdout
        # Drain until ffmpeg closes stdout (EOF). We must NOT gate on _running:
        # if we stop reading while ffmpeg is still alive, its stdout pipe fills,
        # ffmpeg blocks on write and never reacts to SIGINT -> deadlock on stop.
        while True:
            chunk = stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            # extract the most recent complete JPEG in the buffer
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

    @property
    def error(self) -> str | None:
        return self._error

    def _stop_proc(self) -> int:
        """Stop ffmpeg gracefully so the MP4 is finalised, return rc.

        The reader thread keeps draining stdout the whole time so ffmpeg's
        pipe never fills — otherwise it would block on write and ignore the
        signal. We flip _running off only after the process has exited.
        """
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
            logger.warning(f"Error stopping ffmpeg: {e}")
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
        """Finish recording; atomically publish the .part file as the final .mp4.

        The .mp4 only ever appears AFTER a clean stop + fsync + rename, so its mere
        existence is a power-safe "fully saved on the Pi" marker. Returns the final
        path, or None on failure.
        """
        self._stop_proc()
        if not os.path.exists(self.part_path):
            logger.error("Recording stopped but .part file is missing")
            return None
        size = os.path.getsize(self.part_path)
        if size == 0:
            logger.error("Recording stopped but .part file is empty")
            delete_video(self.part_path)
            return None
        # durably publish: fsync data → atomic rename → fsync dir (so the rename
        # itself survives a power cut). After this, a power loss can no longer
        # leave a half-written .mp4.
        _fsync_file(self.part_path)
        try:
            os.replace(self.part_path, self.output_path)
        except Exception as e:
            logger.error(f"Could not publish recording {self.part_path} -> {self.output_path}: {e}")
            return None
        _fsync_dir(os.path.dirname(self.output_path) or ".")
        logger.info(f"Recording kept: {self.output_path} ({size} bytes)")
        return self.output_path

    def stop_and_discard(self) -> None:
        """Cancel recording and delete any partial/final file."""
        self._stop_proc()
        delete_video(self.part_path)
        delete_video(self.output_path)
        logger.info("Recording discarded")


class TestPreview:
    """Xem live camera để KIỂM TRA khi máy idle — không ghi file, không đụng
    luồng run. Cùng tham số v4l2 với Recorder. /dev/video chỉ 1 tiến trình dùng
    được → controller PHẢI stop() cái này trước khi Recorder chạy (đã gọi trong
    start()), và poll-loop tự stop khi không ai xem >5s."""

    def __init__(self):
        self._proc = None
        self._latest = None
        self.error = None

    def start(self) -> bool:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", VIDEO_SIZE, "-framerate", VIDEO_FPS,
            "-i", VIDEO_DEVICE,
            "-vf", f"scale={PREVIEW_W}:{PREVIEW_H}",
            "-q:v", "7", "-f", "mjpeg", "pipe:1",
        ]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, bufsize=0)
        except Exception as e:
            self.error = str(e)
            return False
        threading.Thread(target=self._read, daemon=True).start()
        return True

    def _read(self):
        buf = b""
        p = self._proc
        try:
            while p and p.poll() is None:
                chunk = p.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:                       # tách từng frame JPEG (SOI..EOI)
                    s = buf.find(b"\xff\xd8")
                    e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                    if s < 0 or e < 0:
                        if s > 0:
                            buf = buf[s:]
                        break
                    self._latest = buf[s:e + 2]
                    buf = buf[e + 2:]
        except Exception:
            pass

    def get_latest_jpeg(self):
        return self._latest

    def stop(self):
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            try:
                p.kill()
                p.wait(timeout=3)
            except Exception:
                pass


def delete_video(path: str) -> None:
    """XÓA THẬT video local. Caller (_auto_finish/retry) chỉ gọi hàm này SAU khi
    upload đã xác nhận thành công (res=="ok") hoặc run đã hết hạn không thể gửi
    lại ("gone") — nên không bao giờ mất video chưa gửi. (Hook tuning giữ video
    vào ~/card_keep đã gỡ 2026-07-03 — nó làm đầy thẻ SD, 4GB/56 video.)"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

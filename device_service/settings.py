"""
settings.py — nguồn cấu hình DUY NHẤT của device_service.

Thứ tự ưu tiên (cao thắng thấp):
  1. Biến môi trường thật (systemd Environment=, shell export, test_sim.py)
  2. File /home/bbsw/workspace/.env  (KEY=VALUE, dòng bắt đầu # là comment)
  3. Default khai báo ngay trong file này

Cách dùng:
    from settings import settings

    settings.serial.port            # "/dev/ttyACM0" | "sim"
    settings.paths.credentials      # Path .../card_device/credentials.json
    settings.credentials.load()     # dict | None
    settings.fake.server            # True khi CARD_FAKE_SERVER=1 (bench)

Muốn đổi cấu hình: sửa /home/bbsw/workspace/.env — không sửa code.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

WORKSPACE_DIR = Path("/home/bbsw/workspace")
ENV_FILE = WORKSPACE_DIR / ".env"


# ── nạp .env ──────────────────────────────────────────────────────────────────

def _load_env_file(path: Path = ENV_FILE) -> None:
    """Nạp KEY=VALUE từ `path` vào os.environ. Biến đã tồn tại giữ nguyên
    (env thật > .env), nên systemd/test override vẫn thắng."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


_load_env_file()


# ── ghi file an toàn khi cúp điện (atomic) ───────────────────────────────────

def atomic_write_text(path: Path, text: str) -> None:
    """Ghi `text` vào `path` theo kiểu atomic để chịu được cúp điện đột ngột:
    ghi ra 1 file tạm CÙNG thư mục → fsync nội dung → os.replace (rename atomic)
    → fsync thư mục. Mất điện ở bất kỳ thời điểm nào cũng CHỈ để lại HOẶC bản cũ
    nguyên vẹn HOẶC bản mới hoàn chỉnh — không bao giờ có file ghi dở/hỏng.
    Tên tạm cố định (`<tên>.tmp`) nên lần ghi sau ghi đè, không tích rác."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")   # 1 file tạm/đích, cùng filesystem
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)                  # rename atomic trong cùng fs
        dfd = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(dfd)                      # ép ghi cả entry thư mục (rename)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── đọc env có ép kiểu ───────────────────────────────────────────────────────

def _str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)))


def _float(key: str, default: float) -> float:
    return float(os.environ.get(key, str(default)))


def _flag(key: str, default: bool = False) -> bool:
    """'1' → True, giá trị khác → False, chưa đặt → default."""
    val = os.environ.get(key)
    return default if val is None else val == "1"


# ── nhóm cấu hình (mỗi nhóm 1 dataclass, gom về Settings ở cuối) ─────────────

@dataclass(frozen=True, slots=True)
class Paths:
    """Thư mục & file trạng thái của thiết bị."""
    device_dir: Path    # chứa credentials/printer cfg/printed_qr/speed model
    tmp_dir: Path       # video chờ upload (SYS-04 quét lại lúc boot)
    credentials: Path   # định danh thiết bị + server_url + token
    printer_cfg: Path   # cấu hình máy in (Printer Setup UI ghi)
    printed_qr: Path    # các run_id đã in QR (chống in trùng qua restart)
    speed_model: Path   # weights ML điều tốc (xoá/đổi tên = tắt model)
    serial_log: Path    # log serial thô cho dataset ML (tmpfs)

    @classmethod
    def load(cls) -> "Paths":
        device_dir = Path(_str("CARD_DEVICE_DIR", str(WORKSPACE_DIR / "card_device")))
        return cls(
            device_dir=device_dir,
            tmp_dir=Path(_str("CARD_TMP_DIR", "~/card_tmp")).expanduser(),
            credentials=Path(_str("CARD_CRED_FILE", str(device_dir / "credentials.json"))),
            printer_cfg=device_dir / "printer.json",
            printed_qr=device_dir / "printed_qr.txt",
            speed_model=device_dir / "speed_model.json",
            serial_log=Path(_str("CARD_SERIAL_LOG", "/tmp/serial_live.log")),
        )


@dataclass(frozen=True, slots=True)
class CredentialsStore:
    """Đọc/ghi/xoá credentials.json — liên kết với Paths.credentials."""
    path: Path

    def load(self) -> dict | None:
        """None nếu chưa enroll. JSON hỏng → raise (controller bắt, báo SYS-03)."""
        if not self.path.is_file():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: dict) -> None:
        # atomic: cúp điện lúc lưu vẫn không hỏng credentials.json (mất định danh)
        atomic_write_text(self.path, json.dumps(data, indent=2))

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class Serial:
    """Link Arduino (motor controller)."""
    port: str                   # "sim" = simulator không cần phần cứng
    baud: int
    boot_delay: float           # chờ Arduino boot sau khi mở cổng (s)
    reconnect_delay: float      # nghỉ giữa các lần mở lại cổng (s)
    motor_check_timeout: float  # chờ dòng ST sau lệnh S (handshake) (s)
    watchdog: float             # im lặng serial quá X giây khi đang quay → MCU-04

    @classmethod
    def load(cls) -> "Serial":
        return cls(
            port=_str("CARD_SERIAL_PORT", "/dev/ttyACM0"),
            baud=_int("CARD_SERIAL_BAUD", 115200),
            boot_delay=_float("CARD_ARDUINO_BOOT", 2.0),
            reconnect_delay=_float("CARD_SERIAL_RECONNECT", 2.0),
            motor_check_timeout=_float("CARD_MOTOR_CHECK_TIMEOUT", 3.0),
            watchdog=_float("CARD_SERIAL_WATCHDOG", 8.0),
        )


@dataclass(frozen=True, slots=True)
class Camera:
    """Webcam USB + ffmpeg ghi hình."""
    device: str
    size: str            # ffmpeg -video_size
    fps: str             # camera max cho MJPG 1280x720 là 30
    exposure: str        # số = exposure_time_absolute (giữ 30fps: 156..500), "auto" = ~20fps
    check_timeout: float # timeout probe v4l2 (s)
    warmup: float        # hiện live feed X giây trước khi motor chạy
    preview_w: int
    preview_h: int

    @classmethod
    def load(cls) -> "Camera":
        return cls(
            device=_str("CARD_VIDEO_DEVICE", "/dev/video0"),
            size=_str("CARD_VIDEO_SIZE", "1280x720"),
            fps=_str("CARD_VIDEO_FPS", "30"),
            exposure=_str("CARD_EXPOSURE", "400"),
            check_timeout=_float("CARD_CAMERA_CHECK_TIMEOUT", 5.0),
            warmup=_float("CARD_CAMERA_WARMUP", 2.5),
            preview_w=_int("CARD_PREVIEW_W", 640),
            preview_h=_int("CARD_PREVIEW_H", 360),
        )


@dataclass(frozen=True, slots=True)
class Batch:
    """Chỉ tiêu mẻ bài — server quyết định, đây là fallback."""
    target_keys: tuple[str, ...]  # key đầu tiên có mặt trong response start_run được dùng
    target_fallback: int

    @classmethod
    def load(cls) -> "Batch":
        return cls(
            target_keys=("target", "target_count", "quantity", "count",
                         "num_cards", "cards", "leaves"),
            target_fallback=_int("CARD_BATCH_TARGET", 412),
        )


@dataclass(frozen=True, slots=True)
class Upload:
    """Retry đẩy video lên server (UPL-04/05)."""
    max_retries: int
    retry_delay: float
    auto_resend: float   # giây giữa 2 lần TỰ gửi lại video kẹt (0 = tắt)

    @classmethod
    def load(cls) -> "Upload":
        return cls(
            max_retries=_int("CARD_UPLOAD_MAX_RETRIES", 5),
            retry_delay=_float("CARD_UPLOAD_RETRY_DELAY", 2.0),
            auto_resend=_float("CARD_AUTO_RESEND_INTERVAL", 60.0),
        )


@dataclass(frozen=True, slots=True)
class RunFlow:
    """Nhịp vòng đời một mẻ: chờ slot, poll status, khoá Start chờ AI."""
    slot_wait: float        # tổng budget chờ run mồ côi trên server tự hết hạn (s)
    slot_poll: float        # gap poll đầu tiên (s)
    slot_poll_max: float    # trần backoff (s)
    poll_interval: float    # poll run status để auto-in QR khi AI xong (s)
    result_wait_max: float  # khoá Start chờ AI tối đa (s) — hết giờ tự mở (an toàn)
    done_auto_idle: float   # sau "done" X giây tự về Sẵn sàng

    @classmethod
    def load(cls) -> "RunFlow":
        return cls(
            slot_wait=_float("CARD_RUN_SLOT_WAIT", 45.0),
            slot_poll=_float("CARD_RUN_SLOT_POLL", 3.0),
            slot_poll_max=_float("CARD_RUN_SLOT_POLL_MAX", 6.0),
            poll_interval=_float("CARD_RUN_POLL_INTERVAL", 5.0),
            result_wait_max=_float("CARD_RESULT_WAIT", 3600.0),
            done_auto_idle=_float("CARD_DONE_AUTO_IDLE", 5.0),
        )


@dataclass(frozen=True, slots=True)
class Wifi:
    """WiFi watchdog + captive portal cài mạng."""
    iface: str
    ap_con: str             # tên connection nmcli của hotspot (khớp trạng thái trên máy)
    ap_ssid: str            # SSID hotspot; rỗng = tự sinh CardFeeder-<đuôi device_id>
    portal_port: int
    manual_lock: str        # file lock: portal đang cầm tay → watchdog đứng yên
    check_every: float      # chu kỳ watchdog (s)
    grace: float            # offline quá X giây (không có mạng lưu) mới bật AP
    ap_timeout: float       # AP chưa cấu hình tự hạ sau X giây; 0 = không tự hạ
    boot_wait_saved: float  # chờ lúc boot khi ĐÃ có mạng lưu (s)
    boot_wait_unconfigured: float  # chờ lúc boot khi máy mới tinh (s)

    @classmethod
    def load(cls) -> "Wifi":
        return cls(
            iface=_str("CARD_WIFI_IFACE", "wlan0"),
            ap_con="CardFeederAP",
            ap_ssid=_str("CARD_AP_SSID", ""),
            portal_port=_int("CARD_PORTAL_PORT", 80),
            manual_lock=_str("CARD_WIFI_MANUAL_LOCK", "/run/card_wifi_manual.lock"),
            check_every=_float("CARD_WIFI_CHECK", 0.2),
            grace=_float("CARD_WIFI_GRACE", 0.2),
            ap_timeout=_float("CARD_AP_TIMEOUT", 0.0),
            boot_wait_saved=_float("CARD_WIFI_BOOTWAIT_SAVED", 8.0),
            boot_wait_unconfigured=_float("CARD_WIFI_BOOTWAIT_UNCONFIGURED", 0.2),
        )


@dataclass(frozen=True, slots=True)
class Printer:
    """Máy in QR. Backend thật chọn theo printer.json (Paths.printer_cfg)."""
    backend: str      # fallback khi chưa có printer.json: cups|escpos_net|escpos_file
    assume_ok: bool   # van khẩn cấp: bỏ probe mạng, luôn coi máy in sẵn sàng

    @classmethod
    def load(cls) -> "Printer":
        return cls(
            backend=_str("CARD_PRINTER_BACKEND", "cups"),
            assume_ok=_flag("CARD_PRINTER_ASSUME_OK"),
        )


@dataclass(frozen=True, slots=True)
class Web:
    """Flask server cho kiosk UI."""
    host: str
    port: int

    @classmethod
    def load(cls) -> "Web":
        return cls(
            host=_str("CARD_WEB_HOST", "127.0.0.1"),
            port=_int("CARD_WEB_PORT", 8800),
        )


@dataclass(frozen=True, slots=True)
class Enroll:
    """Màn hình enroll (app.py) + nhịp heartbeat. Token/id máy test nằm ở .env."""
    default_server_url: str
    test_server_url: str
    test_device_id: str
    test_setup_token: str
    heartbeat_interval: float  # giây giữa 2 lần ping server

    @classmethod
    def load(cls) -> "Enroll":
        return cls(
            default_server_url=_str("CARD_DEFAULT_SERVER_URL", "http://192.168.1.50:8040"),
            test_server_url=_str("CARD_TEST_SERVER_URL", "http://100.110.72.1:8040"),
            test_device_id=_str("CARD_TEST_DEVICE_ID", ""),
            test_setup_token=_str("CARD_TEST_SETUP_TOKEN", ""),
            heartbeat_interval=_float("CARD_HEARTBEAT_INTERVAL", 30.0),
        )


@dataclass(frozen=True, slots=True)
class Ui:
    """Kiosk/Tk UI."""
    fullscreen: bool
    autostart: bool     # demo/test: tự bấm Bắt đầu sau 1.2s
    step_pause: float   # mỗi bước pre-flight hiện ✓ bao lâu cho người đọc kịp (s)

    @classmethod
    def load(cls) -> "Ui":
        return cls(
            fullscreen=_str("CARD_FULLSCREEN", "1") != "0",
            autostart=_flag("CARD_AUTOSTART"),
            step_pause=_float("CARD_STEP_PAUSE", 0.9),
        )


@dataclass(frozen=True, slots=True)
class SpeedModel:
    """Model ML điều tốc theo tải (weights ở Paths.speed_model)."""
    # v3b: 580→520 — CHỦ ĐỘNG bám trần tốc (460→395 c/s); model = bộ GIỮ ỔN ĐỊNH
    # theo tải, sprint gap (v7.6) lo phần nhịp.
    dt_target: float  # ms mục tiêu cho 500 lá

    @classmethod
    def load(cls) -> "SpeedModel":
        return cls(dt_target=_float("CARD_SPEED_DT_TARGET", 520.0))


@dataclass(frozen=True, slots=True)
class Sim:
    """Simulator Arduino (CARD_SERIAL_PORT=sim). Giữ tên biến BSS_* cũ."""
    leaf_ms: int      # ms giữa 2 lá
    clump_pct: float  # % xác suất [CLUMP] mỗi lá
    stall_at: int     # ép STALL tại lá thứ N; 0 = tắt
    false_pct: float  # % xác suất FALSE-TRIGGER (lá tới sensor -> ST +1 nhưng KHONG chot; tai hien glitch lui)
    optimistic: int   # 1 = phat ST n=count+1 khi la TOI (real-time, giong firmware); 0 = chi phat committed

    @classmethod
    def load(cls) -> "Sim":
        return cls(
            leaf_ms=_int("BSS_SIM_LEAF_MS", 80),
            clump_pct=_float("BSS_SIM_CLUMP_PCT", 2.0),
            stall_at=_int("BSS_SIM_STALL_AT", 0),
            false_pct=_float("BSS_SIM_FALSE_PCT", 0.0),
            optimistic=_int("BSS_SIM_OPTIMISTIC", 1),
        )


@dataclass(frozen=True, slots=True)
class Fake:
    """Công tắc bench/test — mô phỏng server, camera, máy in không cần phần cứng.
    Chỉ dùng cho test_sim.py và chạy bench; sản xuất để nguyên (tắt hết)."""
    server: bool         # 1 = _FakeClient thay API thật
    offline: bool        # heartbeat giả báo mất mạng
    locked: str          # "1" = khoá cứng; "/path" = khoá khi file tồn tại
    srv_fail: str        # "down" | "reject" — start_run giả lỗi
    srv_reason: str      # lý do reject giả
    reject: bool         # start_run giả từ chối (SRV-03)
    recording_run: str   # "always" | số N — giả run mồ côi chặn start
    target: int          # target giả mỗi start_run
    upload_fail: bool    # upload giả thất bại
    run_flow: str        # "processing,processing,done" — dãy status giả
    camera: str          # "notfound" | "busy" | "ok" — probe camera giả
    recorder: bool       # Recorder không cần ffmpeg/phần cứng
    printer: bool        # máy in giả: available + log thay vì in thật

    @classmethod
    def load(cls) -> "Fake":
        return cls(
            server=_flag("CARD_FAKE_SERVER"),
            offline=_flag("CARD_FAKE_OFFLINE"),
            locked=_str("CARD_FAKE_LOCKED", ""),
            srv_fail=_str("CARD_FAKE_SRV_FAIL", ""),
            srv_reason=_str("CARD_FAKE_SRV_REASON", "hết hạn mức"),
            reject=_flag("CARD_FAKE_REJECT"),
            recording_run=_str("CARD_FAKE_RECORDING_RUN", ""),
            target=_int("CARD_FAKE_TARGET", 12),
            upload_fail=_flag("CARD_FAKE_UPLOAD_FAIL"),
            run_flow=_str("CARD_FAKE_RUN_FLOW", ""),
            camera=_str("CARD_FAKE_CAMERA", ""),
            recorder=_flag("CARD_FAKE_RECORDER"),
            printer=_flag("CARD_FAKE_PRINTER"),
        )


# ── gom tất cả về một mối ────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Settings:
    paths: Paths
    credentials: CredentialsStore
    serial: Serial
    camera: Camera
    batch: Batch
    upload: Upload
    run: RunFlow
    wifi: Wifi
    printer: Printer
    web: Web
    enroll: Enroll
    ui: Ui
    speed: SpeedModel
    sim: Sim
    fake: Fake

    @classmethod
    def load(cls) -> "Settings":
        paths = Paths.load()
        return cls(
            paths=paths,
            credentials=CredentialsStore(path=paths.credentials),
            serial=Serial.load(),
            camera=Camera.load(),
            batch=Batch.load(),
            upload=Upload.load(),
            run=RunFlow.load(),
            wifi=Wifi.load(),
            printer=Printer.load(),
            web=Web.load(),
            enroll=Enroll.load(),
            ui=Ui.load(),
            speed=SpeedModel.load(),
            sim=Sim.load(),
            fake=Fake.load(),
        )


settings = Settings.load()

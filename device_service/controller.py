"""
controller.py — headless orchestration for the web-kiosk UI.

Same business logic as the Tkinter app (app.py), minus the UI: pre-flight checks
(server -> camera -> motor), turn on camera + motor, count to the server-given
target, auto-stop + upload, and gate the next round on a successful upload.

Errors are structured objects (errors.py) so the UI renders the right colour +
title + hint + button. Includes the serial watchdog (MCU-04) and pending-video
scan (SYS-04) from BA_error_catalog.md §9.
"""

import datetime
import glob
import json
import logging
import os
import re
import subprocess
import threading
import time

import errors
from api_client import APIClient
from camera import TMP_DIR, Recorder, delete_video, probe as camera_probe
from parser import MachineStatus, parse_line, ERR_LINK, ERR_SENSOR

# v29: giải mã bitmask hw= từ firmware -> tên phần cứng (hiện trên chip/popup + gate START)
# UI 100% tiếng Anh, KHÔNG kèm tên chân (yêu cầu 2026-07-15)
_HW_NAMES = [(0x01, "Sensor / card tray"),
             (0x02, "Motor / encoder"),
             (0x04, "Limit switch / stepper")]
from printer import print_run_qr, printer_available
from settings import settings

from serial_link import SerialLink

try:
    import requests  # for exception types; guarded (SD/import resilience)
except Exception:
    requests = None

logger = logging.getLogger("card_ctrl")

UPLOAD_MAX_RETRIES = settings.upload.max_retries
UPLOAD_RETRY_DELAY = settings.upload.retry_delay
AUTO_RESEND_INTERVAL = settings.upload.auto_resend   # giây; 0 = tắt tự gửi lại
SERIAL_WATCHDOG = settings.serial.watchdog  # MCU-04
# device_has_recording_run self-heal: a previous run orphaned on the server
# (service killed mid-run) blocks every new start_run. The device CANNOT cancel
# an orphan (cancel needs that run's session token, lost with the old process ->
# server replies 403 invalid_session), but the server auto-expires stale
# recording runs after a TTL. So poll start_run for up to RUN_SLOT_WAIT seconds,
# waiting for the slot to free, instead of failing instantly and looping on Retry.
RUN_SLOT_WAIT = settings.run.slot_wait          # total wait budget (s)
RUN_SLOT_POLL = settings.run.slot_poll          # first poll gap (s)
RUN_SLOT_POLL_MAX = settings.run.slot_poll_max  # backoff cap (s)
# poll run statuses this often (s) to auto-print the QR when AI finishes (done)
RUN_POLL_INTERVAL = settings.run.poll_interval
# Sau upload OK, khóa Start chờ AI + popup tối đa chừng này giây (an toàn:
# server chết/mất mạng thì không kẹt máy vĩnh viễn — hết giờ tự mở khóa).
RESULT_WAIT_MAX = settings.run.result_wait_max
# WiFi status for the kiosk UI (full-screen QR when in setup/AP mode + signal icon)
AP_CON = settings.wifi.ap_con                   # NM connection name of our hotspot
AP_SSID_ENV = settings.wifi.ap_ssid             # từ service file — nguồn chính xác nhất
WIFI_IFACE = settings.wifi.iface
# remember printed run_ids across restarts so we never auto-print one twice
PRINTED_PATH = str(settings.paths.printed_qr)
WARNING_TTL = 3.0  # seconds a transient warning (clump/limit) stays on the UI
# Sau khi gửi xong (done), tự về "Sẵn sàng" sau bao nhiêu giây.
DONE_AUTO_IDLE = settings.run.done_auto_idle
# camera warm-up: show the live feed this long BEFORE the motor starts
CAMERA_WARMUP = settings.camera.warmup


class _FakeClient:
    """Bench server (CARD_FAKE_SERVER=1): no network, for sim testing."""
    _n = 0

    def heartbeat(self):
        if settings.fake.offline:
            return False
        # bench: mô phỏng khoá từ xa. "1" = khoá cứng; đường dẫn file = khoá
        # khi file TỒN TẠI (test bật/tắt khoá giữa chừng không cần đổi env)
        lk = settings.fake.locked
        if lk == "1":
            return "locked"
        if lk.startswith("/") and os.path.exists(lk):
            return "locked"
        return True

    _rr_seen = 0

    def start_run(self):
        fail = settings.fake.srv_fail
        if fail == "down":
            import requests as _rq
            raise _rq.exceptions.ConnectionError("simulated server unreachable")
        if fail == "reject":
            return {"ok": False, "reason": settings.fake.srv_reason}
        if settings.fake.reject:   # bench: test SRV-03
            return {"ok": False, "reason": "device quota exceeded"}
        # bench: simulate an orphaned recording run blocking start. "always" never
        # frees (-> SRV-07); an integer N frees after N rejections (-> self-heal).
        rr = settings.fake.recording_run
        if rr:
            _FakeClient._rr_seen += 1
            if rr == "always" or _FakeClient._rr_seen <= int(rr):
                return {"ok": False, "run_id": None, "status": None,
                        "session_token": None, "reason": "device_has_recording_run"}
        _FakeClient._n += 1
        # run mới bắt đầu → CARD_FAKE_RUN_FLOW đếm lại từ đầu (mô phỏng đúng
        # đời thật: status của run chỉ tiến triển từ lúc nó được tạo)
        _FakeClient._flow_i = 0
        return {"ok": True, "run_id": f"fake{_FakeClient._n:04d}",
                "target": settings.fake.target}

    def get_upload_url(self, run_id): return {"upload_url": "fake://", "object_key": run_id}

    def upload_video(self, url, path):
        return not settings.fake.upload_fail

    def upload_direct(self, run_id, path):
        # mô phỏng đường upload xuyên backend — fail cùng cờ với upload_video
        if settings.fake.upload_fail:
            raise RuntimeError("simulated direct upload failure")
        return {"ok": True}

    def complete_upload(self, run_id, key): return {"ok": True}
    def cancel_run(self, run_id): return {"ok": True}
    def cancel_own_run(self, run_id): return {"ok": True}

    def get_run_status(self, run_id):
        # bench: CARD_FAKE_RUN_STATUS lets R2 tests simulate the server ALREADY
        # having this run (uploaded/processing/done) vs not (recording).
        return {"run_id": run_id, "status": os.getenv("CARD_FAKE_RUN_STATUS", "recording")}

    _flow_i = 0

    def list_runs(self):
        # bench: CARD_FAKE_RUN_FLOW="processing,processing,done" — mỗi lần gọi
        # trả run "fakeflow1" với status kế tiếp trong dãy (giữ status cuối khi
        # hết dãy). Dùng test popup hỏi in QR khi run chuyển sang done.
        flow = settings.fake.run_flow
        if not flow:
            return []
        seq = [s.strip() for s in flow.split(",") if s.strip()]
        status = seq[min(_FakeClient._flow_i, len(seq) - 1)]
        _FakeClient._flow_i += 1
        # nếu đã có run start_run thật → dùng đúng id đó (test khóa Start khớp id)
        rid = f"fake{_FakeClient._n:04d}" if _FakeClient._n else "fakeflow1"
        return [{"run_id": rid, "status": status}]


class Controller:
    def __init__(self):
        self._lock = threading.Lock()
        self._status = MachineStatus()
        self._state = "idle"          # idle|checking|recording|uploading|done|failed
        self._target = settings.batch.target_fallback
        self._count = 0        # DISPLAY count (UI) — real-time, don dieu (max-guard)
        self._committed = 0    # COMMITTED count (chot that tu [CARD]/DONE) — cong gui 412 dung cai nay
        self._session = ""
        self._error = None            # structured error dict | None
        self._warning = None          # transient warning dict | None
        self._warning_ts = 0.0
        self._online = False
        self._can_start = True
        self._finishing = False
        self._recorder = None
        self._recording = False
        self._run_id = None
        self._pending_upload = None
        self._direct_unsupported = False   # server cũ không có PUT /runs/{id}/upload
        # mốc tự-gửi-lại: khởi tạo = bây giờ để lần đầu cách boot đúng 1 chu kỳ
        # (monotonic() tính từ lúc BOOT máy — nếu để 0.0 thì nổ ngay tick đầu)
        self._last_auto_resend = time.monotonic()
        self._status_evt = threading.Event()
        self._cancel_evt = threading.Event()
        self._test_preview = None              # xem thử camera khi idle (TestPreview)
        self._test_last_access = 0.0
        self._last_serial_ts = 0.0
        # v28.3 MANUAL HOME: True = user vừa bấm HOME, stepper đang leo về công tắc top.
        #   Tắt khi ST báo homed (lim=1) hoặc hết timeout. UI hiện "Homing…" trong lúc này.
        self._homing = False
        self._homing_ts = 0.0

        # ── QR auto-print ──
        self._printer_ok = False                 # cached (refreshed by the poll loop)
        self._printed = self._load_printed()      # run_ids already printed
        self._seen_status = {}                    # last status per run (transition detect)
        self._print_prompt = None                 # run_id chờ hỏi in QR trên kiosk | None
        # Sau khi upload OK: KHÓA nút Start tới khi AI server trả kết quả (run
        # done/failed) VÀ người dùng trả lời popup in QR (ack). Timeout an toàn
        # để không kẹt máy vĩnh viễn nếu server chết giữa chừng.
        self._await_result = None                 # run_id đang chờ AI + ack | None
        self._await_since = 0.0
        self._await_adopted = set()               # run đã nhận nuôi (không nhận lại sau timeout/ack)

        # ── khoá từ xa (admin bấm Khoá trên server) ──
        # True → UI phủ màn hình khoá "contact technical support", chặn Start,
        # đang chạy thì dừng. Tự mở khi admin Unlock (heartbeat poll nhanh 5s).
        self._server_locked = False

        # ── ML speed model (điều tốc theo trọng lượng chồng, học từ log) ──
        self._spdmodel = None
        self._spd_last_tx = 0.0
        self._load_speed_model()

        # ── WiFi status for the kiosk (refreshed by the poll loop) ──
        # setup=True → hotspot/AP is up (no real network) → kiosk shows full-screen QR
        self._wifi = {"setup": False, "connected": False, "signal": 0, "ssid": ""}
        # cửa sổ "lạc quan": user vừa bấm Setup → hiện QR NGAY (khỏi chờ _refresh_wifi
        # mỗi 5s + bị probe máy in đẩy trễ). Giữ setup=True tới khi AP lên thật/nối xong.
        self._wifi_setup_pending_until = 0.0

        # QR-scan pairing (un-enrolled device only): {code, server_url, status,
        # error} | None. status: pending → done (claimed & saved) / expired.
        self._pair = None

        self._client = self._make_client()
        self._link = SerialLink(on_line=self._on_serial_line)
        self._link.start()
        # v28.3: phần cứng THẬT -> mặc định "CHƯA home" (vị trí platform lúc boot không rõ:
        #   có thể vừa mất điện giữa mẻ). UI hiện nút HOME tới khi ST đầu tiên báo lim=1.
        #   Sim vẫn homed=True (parser mặc định) -> START luôn sẵn, không đổi hành vi cũ.
        if not self._link.is_sim:
            self._status.homed = False
        self._start_heartbeat()
        self._start_watchdog()
        self._start_heartbeat_deadman()   # v28.3: nuôi deadman firmware lúc motor chạy
        self._start_run_poll()        # printer status + auto-print QR on AI done
        self._scan_pending()          # SYS-04: leftover video from a previous run

    # ── client ──
    def _make_client(self):
        if settings.fake.server:
            self._online = True
            return _FakeClient()
        try:
            creds = settings.credentials.load()
        except Exception:
            logger.warning("credentials.json hỏng (SYS-03)")
            return None
        if not creds:
            logger.warning("no credentials — server actions will fail until enrolled")
            return None
        return APIClient(creds["server_url"], creds["device_id"], creds["device_key"])

    def _start_heartbeat(self):
        # Poll fast (3s) while offline so a just-booted Pi or a flaky network
        # recovers the "online" badge quickly; 10s when online (đủ nhanh để
        # lệnh Khoá từ admin có hiệu lực trong ~10s); 5s khi ĐANG bị khoá để
        # admin Mở khoá là máy nhả ra ngay.
        def loop():
            while True:
                if self._client:
                    try:
                        hb = self._client.heartbeat()
                    except Exception:
                        hb = False
                    was_locked = self._server_locked
                    self._server_locked = (hb == "locked")
                    # locked = server VẪN liên lạc được (chỉ là từ chối) → online
                    self._online = (hb is True) or self._server_locked
                    if self._server_locked and not was_locked:
                        logger.warning("ADMIN LOCK: server khoá thiết bị -> khoá màn hình")
                        if self._recording:
                            try:
                                self.cancel()   # dừng motor + huỷ mẻ đang chạy
                            except Exception:
                                pass
                    elif was_locked and not self._server_locked:
                        logger.warning("ADMIN UNLOCK: thiết bị được mở khoá")
                time.sleep(5 if self._server_locked else (10 if self._online else 3))
        threading.Thread(target=loop, daemon=True).start()

    # ── serial ──
    # ── SPEED MODEL (ML, học từ log các mẻ thật) ──────────────────────────────
    # File weights ~/.card_device/speed_model.json (train ở server, ml_speed/).
    # Dạng vật lý 4 hệ số: dt = (a0+a1*rem)*500/speed + (b0+b1*rem)
    # → speed(rem) cho nhịp đích, kẹp bởi bao an toàn cap = base + slope*rem
    # (học từ bằng chứng vận hành sạch). Gửi V<c/s> xuống Arduino tối đa 1 lần/2s
    # khi đang recording; firmware TTL 10s — mất lệnh là tự về governor nội bộ.
    # Tắt model: đổi tên/xoá file speed_model.json (không cần sửa code).
    SPEED_MODEL_PATH = settings.paths.speed_model
    SPEED_MODEL_DT_TARGET = settings.speed.dt_target   # v3b: 580->520 — CHỦ ĐỘNG bám trần tốc (460->395 c/s); model = bộ GIỮ ỔN ĐỊNH theo tải, sprint gap (v7.6) lo phần nhịp

    def _load_speed_model(self):
        try:
            with open(self.SPEED_MODEL_PATH) as f:
                m = json.load(f)
            phys = m["phys"]; cap = m["safe_cap"]
            self._spdmodel = {"a0": phys["a0"], "a1": phys["a1"],
                              "b0": phys["b0"], "b1": phys["b1"],
                              "cap_base": cap["base"], "cap_slope": cap["slope"]}
            logger.info("speed model loaded (val MAE %sms) — model-driven speed ON",
                        phys.get("val_mae_ms"))
        except FileNotFoundError:
            self._spdmodel = None
        except Exception as e:
            logger.warning("speed model load failed (%s) — dùng governor firmware", e)
            self._spdmodel = None

    def _model_speed(self, count):
        """Tốc c/s tối ưu cho mức chồng hiện tại (closed-form, không numpy)."""
        m = self._spdmodel
        target = max(1, self._target or 412)
        rem = max(0.0, min(1.0, (target - count) / 412.0))
        denom = self.SPEED_MODEL_DT_TARGET - (m["b0"] + m["b1"] * rem)
        spd = (m["a0"] + m["a1"] * rem) * 500.0 / denom if denom > 10 else 520.0
        cap = m["cap_base"] + m["cap_slope"] * rem
        # v23/v24: firmware EXT_SPD_LO=140 (rieng voi CAD_SPD_LO san governor). Model
        # SLOW20 dieu toc V140-195 (cham+deu ca qua trinh) -> phai kep dung [140,520]
        # khop firmware, ko se ep toc len 250 (nhanh hon ca v23 -> pha muc tieu cham).
        FW_SPD_LO, FW_SPD_HI = 70, 520
        v = min(cap, spd)                    # model dieu chinh theo tai (rem)
        return int(max(FW_SPD_LO, min(FW_SPD_HI, v)))

    def _maybe_send_model_speed(self):
        if not self._spdmodel or not self._recording:
            return
        now = time.monotonic()
        if now - self._spd_last_tx < 2.0:
            return
        self._spd_last_tx = now
        try:
            self._link.send(f"V{self._model_speed(self._count)}")
        except Exception:
            pass

    def _on_serial_line(self, line):
        # log serial thô cho ML/tuning (dataset train speed-model đọc file này).
        # TRẦN 20MB chống phình RAM (/tmp = tmpfs): chạm trần → ngừng ghi tới reboot.
        try:
            if not getattr(self, "_serial_log_full", False):
                with open(settings.paths.serial_log, "a") as _dbg:
                    if _dbg.tell() > 20 * 1024 * 1024:
                        self._serial_log_full = True
                        logger.warning("serial log chạm trần 20MB — ngừng ghi tới reboot")
                    else:
                        _dbg.write(f"{time.monotonic():.2f}  {line}\n")
        except Exception:
            pass
        self._last_serial_ts = time.monotonic()   # watchdog heartbeat (MCU-04)
        self._maybe_send_model_speed()             # model ML điều tốc (nếu có weights)
        should_finish = False
        finish_reason = None
        should_nomotor = False
        should_sensor = False
        should_linklost = False
        with self._lock:
            self._status, event = parse_line(line, self._status)
            if self._recording:
                # [v28 COUNT-RT] 2 BO DEM tach biet:
                #  DISPLAY (_count): real-time — bam ST lac quan (n=cardCount+1 khi la TOI),
                #    kep DON DIEU bang max -> UI nhay ngay khi la toi, KHONG BAO GIO lui.
                #  COMMITTED (_committed): chi tang tu [CARD] (event=card) / DONE = la CHOT that
                #    -> dung cho cong gui 412 (khong bi false-trigger lac quan lam sai).
                self._count = max(self._count, self._status.count)
                if event in ("card", "done"):
                    self._committed = max(self._committed, self._status.count)
            if self._recording and event == "clump":
                self._warning = errors.err("MCU-07"); self._warning_ts = time.monotonic()
            elif self._recording and event == "limit":
                self._warning = errors.err("MCU-08"); self._warning_ts = time.monotonic()
            # v6.4: motor commanded but encoder never turned -> abort FAST with a clear
            # "Motor not running — turn on motor power" (MCU-06), not the 13s STALL path.
            if self._recording and event == "sensor" and not self._finishing:
                # v29: firmware chưa nhận được lá nào (cardCount==0 sau 13s) -> cảm biến D4
                #   chết/tuột dây HOẶC khay bài rỗng. Abort mẻ ngay + báo lỗi rõ (MCU-11).
                self._finishing = True
                should_sensor = True
            elif self._recording and event == "nomotor" and not self._finishing:
                self._finishing = True
                should_nomotor = True
            elif self._recording and event in ("done", "stall") and not self._finishing:
                self._finishing = True
                should_finish = True
                finish_reason = event
            # [review v28.3] Firmware deadman đã DỪNG motor (mất liên lạc Pi khi đang chạy) ->
            #   ST báo err=LINK. Abort mẻ NGAY tại đây thay vì đợi watchdog MCU-04 (~8s im lặng).
            elif (self._recording and event == "st" and self._status.error == ERR_LINK
                  and not self._finishing):
                self._finishing = True
                should_linklost = True
            # v28.3 MANUAL HOME (KHÔNG auto): chỉ CẬP NHẬT cờ homed. Khi stepper đã chạm công tắc
            #   top (lim=1) thì tắt trạng thái "đang home". TUYỆT ĐỐI không tự gửi lệnh home —
            #   user phải bấm nút HOME để BIẾT + tự phát hiện stepper lỗi (yêu cầu của anh).
            if event == "st" and self._homing and self._status.homed:
                self._homing = False
                logger.info("stepper đã chạm công tắc top -> home xong, mở START")
        self._status_evt.set()
        if should_sensor:
            logger.warning("firmware err=SENSOR: chua nhan duoc la nao (cam bien D4 chet / het la dau me)")
            self._emergency(errors.err("MCU-11"))
        elif should_nomotor:
            self._emergency(errors.err("MCU-06"))
        elif should_linklost:
            logger.warning("firmware err=LINK (deadman) -> abort mẻ (mất liên lạc khi đang chạy)")
            self._emergency(errors.err("MCU-04"))
        elif should_finish:
            threading.Thread(target=self._auto_finish, args=(finish_reason,), daemon=True).start()

    def _motor_handshake(self):
        if self._link.is_sim:
            return True
        if not self._link.connected:
            return False
        self._status_evt.clear()
        self._link.send("S")
        return self._status_evt.wait(settings.serial.motor_check_timeout)


    # ── watchdog: serial silent too long WHILE THE MOTOR IS SPINNING -> MCU-04 ──
    # Gate on _state=="recording" (motor actually told to spin via B1), NOT on
    # _recording: the latter is True from start() through the server/camera/motor
    # pre-checks + camera warm-up, during which the Arduino is correctly SILENT
    # (it only streams lines after B1). Arming the watchdog then made it trip
    # MCU-04 spuriously before the motor ever ran — the run died with count=0
    # even though the hardware was fine. _last_serial_ts is reset right before B1.
    def _start_watchdog(self):
        def loop():
            tick = 0
            while True:
                time.sleep(1.0)
                tick += 1
                if (self._state == "recording" and not self._finishing and self._last_serial_ts
                        and (time.monotonic() - self._last_serial_ts) > SERIAL_WATCHDOG):
                    with self._lock:                       # [review v28.3] ghi _finishing trong lock (đua với writer khác)
                        self._finishing = True
                    logger.warning("serial watchdog tripped -> MCU-04")
                    self._emergency(errors.err("MCU-04"))
                # v28.3: home quá lâu vẫn chưa chạm công tắc (stepper kẹt/lỗi / công tắc hỏng)
                # -> thôi trạng thái "đang home" để UI hiện lại nút HOME cho user thử lại/kiểm tra.
                if self._homing and (time.monotonic() - self._homing_ts) > 60.0:
                    with self._lock:                       # [review v28.3] ghi _homing trong lock
                        self._homing = False
                    logger.warning("home quá 60s chưa chạm công tắc top -> huỷ trạng thái homing")
                # v28.2 HOME-GATING: lúc RẢNH firmware im lặng (ST chỉ stream khi RUN) ->
                # poll S mỗi 3s để luôn biết lim (chạm công tắc top?) -> UI mở nút START/HOME
                # realtime. Không poll khi đang home (firmware bận blocking ~30s).
                if (tick % 3 == 0 and not self._recording and not self._homing
                        and not self._link.is_sim and self._link.connected
                        and self._state not in ("checking", "warmup", "recording", "uploading")):
                    try:
                        self._link.send("S")
                    except Exception:
                        pass
        threading.Thread(target=loop, daemon=True).start()

    # ── v28.3 DEADMAN heartbeat: lúc motor ĐANG QUAY, gửi 1 byte mỗi ~400ms để firmware
    #    biết Pi còn sống. Pi treo/chết -> ngừng heartbeat -> firmware DỪNG MOTOR trong <1.5s.
    #    Gửi "\n" (dòng rỗng) — firmware chỉ cập nhật mốc nhận, không tốn thời gian phản hồi.
    def _start_heartbeat_deadman(self):
        def loop():
            while True:
                time.sleep(0.4)
                if (self._state == "recording" and not self._link.is_sim
                        and self._link.connected):
                    try:
                        self._link.send("")     # send() sẽ ghi "\n"
                    except Exception:
                        pass
        threading.Thread(target=loop, daemon=True).start()

    # ── v28.3 MANUAL HOME: user bấm nút HOME/RESET -> stepper leo về chạm công tắc top ──
    def home(self):
        with self._lock:
            if self._recording or self._state in ("checking", "warmup", "recording", "uploading"):
                return False
            if self._link.is_sim:
                return False
            if not self._link.connected:
                return False
            # [review v28.3] đang home rồi -> KHÔNG gửi H trùng (chống queue lệnh + re-arm 60s timeout)
            if self._homing:
                return False
            self._homing = True
            self._homing_ts = time.monotonic()
        logger.info("user bấm HOME -> gửi lệnh H (stepper leo về công tắc top)")
        # [review v28.3 CONFIRMED] Gửi H THẤT BẠI (link rớt đúng lúc) mà vẫn để _homing=True
        #   -> watchdog ngừng poll lim 60s, START khoá 60s cho lần home KHÔNG hề chạy.
        #   -> bắt kết quả send: thất bại thì HOÀN TÁC _homing ngay, trả False.
        ok = False
        try:
            ok = self._link.send("H")
        except Exception:
            ok = False
        if not ok:
            with self._lock:
                self._homing = False
            logger.warning("gửi H thất bại -> huỷ trạng thái homing (START không bị khoá oan)")
            return False
        return True

    def _emergency(self, err_obj):
        """Abort a RUNNING batch (discard video) with the given error."""
        try:
            self._link.send("B0")
        except Exception:
            pass
        rec = self._recorder
        self._recorder = None
        if rec:
            try:
                rec.stop_and_discard()
            except Exception:
                pass
        if self._run_id and self._client:
            try:
                self._client.cancel_run(self._run_id)
            except Exception:
                pass
        with self._lock:
            self._recording = False
            self._finishing = False
            self._can_start = True
            self._error = err_obj
            self._state = "failed"

    # ── QR auto-print: poll run statuses, print once when a run reaches done ──
    def _load_printed(self):
        try:
            with open(PRINTED_PATH) as f:
                return {ln.strip() for ln in f if ln.strip()}
        except Exception:
            return set()

    def _mark_printed(self, run_id):
        self._printed.add(run_id)
        try:
            os.makedirs(os.path.dirname(PRINTED_PATH), exist_ok=True)
            with open(PRINTED_PATH, "a") as f:
                f.write(run_id + "\n")
        except Exception as e:
            logger.warning("could not persist printed id: %s", e)

    def _refresh_wifi(self):
        """Cache WiFi status for the kiosk: setup (our AP up) / connected / signal.
        Uses nmcli; called from the poll loop (not per-snapshot) to stay light."""
        setup = connected = False
        signal = 0
        ssid = ""
        try:
            active = subprocess.run(
                ["nmcli", "-t", "-f", "NAME", "con", "show", "--active"],
                capture_output=True, text=True, timeout=5).stdout
            setup = AP_CON in active.split()
        except Exception:
            pass
        if not setup:
            try:
                out = subprocess.run(
                    ["nmcli", "-t", "-f", "IN-USE,SIGNAL,SSID", "dev", "wifi"],
                    capture_output=True, text=True, timeout=5).stdout
                for line in out.splitlines():
                    parts = line.split(":")
                    if parts and parts[0].strip() == "*":
                        try:
                            signal = int(parts[1])
                        except Exception:
                            signal = 0
                        ssid = ":".join(parts[2:])
                        connected = True
                        break
            except Exception:
                pass
        # CỬA SỔ LẠC QUAN: user vừa bấm Setup, AP đang lên (quét tươi + bật AP mất
        # vài giây — trong lúc đó máy VẪN CÒN nối mạng cũ) → GIỮ setup=True để QR
        # không nháy tắt rồi bật lại (bug 2026-07-03: connected cũ hủy nhầm cửa sổ).
        # Cửa sổ CHỈ hết khi: connect xong (notify_wifi_connected clear) / quá hạn.
        if not setup and time.monotonic() < self._wifi_setup_pending_until:
            setup = True
            connected = False   # UI đang chờ AP — đừng hiện icon "có mạng" gây lẫn
        ap_ssid = ""
        if setup:
            # Ưu tiên env var CARD_AP_SSID (set trong service file, luôn đúng)
            # Fallback: đọc từ nmcli profile (chậm hơn, có thể fail khi profile đang tạo)
            if AP_SSID_ENV:
                ap_ssid = AP_SSID_ENV
            elif self._wifi.get("ap_ssid"):
                ap_ssid = self._wifi["ap_ssid"]      # dùng lại tên đã biết (khỏi query lại)
            else:
                try:
                    r2 = subprocess.run(
                        ["nmcli", "-t", "-f", "802-11-wireless.ssid", "con", "show", AP_CON],
                        capture_output=True, text=True, timeout=5)
                    line = r2.stdout.strip()
                    ap_ssid = line.split(":", 1)[-1] if ":" in line else line
                except Exception:
                    pass
        self._wifi = {"setup": setup, "connected": connected,
                      "signal": signal, "ssid": ssid, "ap_ssid": ap_ssid}

    def mark_wifi_setup_pending(self):
        """Bấm Setup → hiện QR NGAY (lạc quan). SSID/pass AP cố định, biết trước →
        đặt setup=True + đọc ap_ssid 1 lần tức thì, khỏi chờ _refresh_wifi (mỗi 5s,
        còn bị probe máy in đẩy trễ). _refresh_wifi giữ QR tới khi AP lên thật/nối xong."""
        ap_ssid = AP_SSID_ENV
        if not ap_ssid:
            try:
                r = subprocess.run(
                    ["nmcli", "-t", "-f", "802-11-wireless.ssid", "con", "show", AP_CON],
                    capture_output=True, text=True, timeout=3)
                line = r.stdout.strip()
                ap_ssid = line.split(":", 1)[-1] if ":" in line else line
            except Exception:
                ap_ssid = ""
        self._wifi_setup_pending_until = time.monotonic() + 25.0
        self._wifi = {"setup": True, "connected": False,
                      "signal": 0, "ssid": "", "ap_ssid": ap_ssid}

    def notify_wifi_connected(self):
        """Portal báo 'đã nối mạng xong' (POST /api/wifi/connected từ cùng máy)
        → cập nhật trạng thái WiFi NGAY thay vì chờ vòng lặp 5s (+probe máy in)
        → QR trên kiosk tắt trong ≤1s sau khi điện thoại thấy Connected."""
        self._wifi_setup_pending_until = 0.0
        try:
            self._refresh_wifi()          # nmcli ~50-100ms, chạy ngay trong request
        except Exception:
            pass

    def _start_run_poll(self):
        def loop():
            while True:
                try:
                    self._printer_ok = printer_available()
                except Exception:
                    self._printer_ok = False
                self._refresh_wifi()
                # test-preview camera: không ai xem >5s → tắt ffmpeg, nhả camera
                if self._test_preview and (time.monotonic() - self._test_last_access) > 5:
                    self._stop_test_preview()
                if self._client:
                    try:
                        self._on_runs(self._client.list_runs())
                    except Exception as e:
                        logger.debug("run poll failed: %s", e)
                # an toàn: chờ kết quả AI quá lâu (server chết/mất mạng) → tự mở
                # khóa Start — đặt NGOÀI try list_runs để vẫn chạy khi server chết
                if self._await_result and (time.monotonic() - self._await_since) > RESULT_WAIT_MAX:
                    logger.warning("await-result %s timed out after %.0fs -> unlock Start",
                                   self._await_result, RESULT_WAIT_MAX)
                    self._await_result = None
                # TỰ GỬI LẠI: còn video chờ + đang lỗi upload (action=resend) +
                # server online → tự retry mỗi AUTO_RESEND_INTERVAL giây. Người
                # vận hành không phải bấm RESEND (nút vẫn giữ để gửi ngay tay).
                if (AUTO_RESEND_INTERVAL > 0 and self._pending_upload and self._online
                        and not self._recording and self._state == "failed"
                        and self._error and self._error.get("action") == "resend"
                        and (time.monotonic() - self._last_auto_resend) > AUTO_RESEND_INTERVAL):
                    self._last_auto_resend = time.monotonic()
                    logger.info("auto-resend: retrying pending upload")
                    try:
                        self.retry()
                    except Exception:
                        logger.exception("auto-resend failed")
                time.sleep(RUN_POLL_INTERVAL)
        threading.Thread(target=loop, daemon=True).start()

    def _on_runs(self, runs):
        """Khi một run chuyển (chưa done)→done (AI xử lý xong): bật popup hỏi in
        QR trên kiosk (print_prompt trong snapshot; UI hiện overlay Đồng ý/Từ
        chối). Run đã 'done' ngay lần đầu thấy (xong khi máy đang tắt) được đánh
        dấu SILENT — không popup, không catch-up. Mỗi run chỉ hỏi đúng 1 lần
        (persist qua PRINTED_PATH); muốn in lại dùng nút Print trong Settings."""
        for r in runs:
            rid = r.get("run_id")
            status = r.get("status", "")
            if not rid:
                continue
            prev = self._seen_status.get(rid)
            is_ours = (rid == self._await_result)   # run đang khóa Start chờ kết quả
            # NHẬN NUÔI chốt từ server-truth: run uploaded/processing (AI chưa xong)
            # → khóa Start, kể cả khi service vừa restart (chốt RAM cũ đã mất).
            # Fix lỗ hổng 2026-07-02: kiosk hiện Ready trong khi admin còn processing.
            if (status in ("uploaded", "processing") and self._await_result is None
                    and rid not in self._await_adopted
                    and self._run_age_sec(r) < RESULT_WAIT_MAX):
                self._await_result = rid
                self._await_since = time.monotonic()
                self._await_adopted.add(rid)
                is_ours = True
                logger.info("adopt pending run %s (%s) -> khóa Start chờ AI", rid, status)
            if rid not in self._printed:
                if status == "done" and (is_ours or (prev is not None and prev != "done")):
                    # run của mình LUÔN được hỏi khi done (kể cả AI xong nhanh
                    # hơn chu kỳ poll đầu tiên); run khác chỉ hỏi khi thấy
                    # chuyển trạng thái sang done.
                    self._mark_printed(rid)            # mark first → never re-prompt
                    self._print_prompt = rid
                    logger.info("print prompt for done run %s", rid)
                elif prev is None and status == "done":
                    self._mark_printed(rid)            # catch-up (boot) → skip silently
            if is_ours and status in ("failed", "cancelled"):
                # AI/server báo run hỏng → không có gì để in → mở khóa Start
                logger.warning("await-result run %s ended %s -> unlock Start", rid, status)
                self._await_result = None
            self._seen_status[rid] = status
        # [review v28.3] CHỐNG PHÌNH RAM 24/7: _seen_status/_await_adopted tích run_id vô hạn.
        #   Server chỉ trả các run GẦN ĐÂY -> tỉa 2 tập này về đúng cửa sổ đó (run cũ rớt khỏi
        #   danh sách thì không cần nhớ nữa). Giữ lại run đang chờ kết quả nếu còn hiệu lực.
        current_ids = {r.get("run_id") for r in runs if r.get("run_id")}
        if self._await_result:
            current_ids.add(self._await_result)
        self._seen_status = {k: v for k, v in self._seen_status.items() if k in current_ids}
        self._await_adopted &= current_ids

    def ack_print_prompt(self):
        """UI đã xử lý popup (đồng ý/từ chối) — xóa prompt; CHỈ mở khóa Start nếu
        chốt đang giữ đúng run của popup (popup mẻ cũ không được xoá chốt mẻ mới)."""
        rid = self._print_prompt
        self._print_prompt = None
        if self._await_result is None or self._await_result == rid:
            self._await_result = None
        return True

    @staticmethod
    def _run_age_sec(r):
        """Tuổi của run (giây) từ created_at ISO; thiếu/parse lỗi → coi như QUÁ HẠN
        (không nhận nuôi) — an toàn cho sim FakeClient không có created_at."""
        try:
            ts = r.get("created_at")
            dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else datetime.datetime.now()
            return (now - dt).total_seconds()
        except Exception:
            return 1e9

    def _clear_dangling_runs(self) -> bool:
        """Cancel this device's own runs stuck in 'recording' on the server —
        orphaned when the service was killed mid-run before it could cancel. Only
        'recording' is cleared; 'processing' is a legit AI stage (its QR still
        prints when it reaches done) so we never touch it. At this point the
        device has no active local run, so any server-side 'recording' is stale.
        Returns True if at least one was cancelled (caller retries start_run)."""
        if not self._client:
            return False
        # Never cancel a run that has a pending local upload — it is still live.
        with self._lock:
            pending_id = self._pending_upload[0] if self._pending_upload else None
        cleared = False
        try:
            for r in self._client.list_runs():
                if r.get("status") == "recording" and r.get("run_id"):
                    run_id = r["run_id"]
                    if run_id == pending_id:
                        logger.info("skipping dangling-clear for pending upload run %s", run_id)
                        continue
                    try:
                        # cancel-own: KHÔNG cần session-token của run (đã mất
                        # cùng process cũ) — trước đây gọi cancel_run thường
                        # nên LUÔN 403 invalid_session, orphan không bao giờ
                        # tự dọn được, Retry kẹt SRV-07 tới khi TTL server gặt.
                        self._client.cancel_own_run(run_id)
                        logger.info("cleared dangling recording run %s", run_id)
                        cleared = True
                    except Exception as e:
                        logger.warning("cancel dangling %s failed: %s", run_id, e)
        except Exception as e:
            logger.warning("list_runs for dangling-clear failed: %s", e)
        return cleared

    def print_qr(self, run_id):
        """Manual 'In' button — print this run's QR now."""
        run_id = (run_id or "").strip()
        if not run_id:
            return False
        return bool(print_run_qr(run_id))

    # ── SYS-04: pending video from a previous (interrupted) run ──
    def _scan_pending(self):
        # Clean stray *.mp4.part = a recording interrupted by a power cut / crash
        # mid-capture (never cleanly published). It has no moov atom → NOT uploadable;
        # the batch's cards already fell into the tray, so the operator re-runs. Never
        # let one masquerade as a complete pending video. ("*.mp4" glob below already
        # excludes ".part", so a valid video is never touched here.)
        try:
            for part in glob.glob(os.path.join(TMP_DIR, "*.mp4.part")):
                try:
                    os.remove(part)
                    logger.info("SYS-04: dọn video quay dở (mất điện/khởi động lại giữa mẻ): %s", part)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            vids = sorted(glob.glob(os.path.join(TMP_DIR, "*.mp4")), key=os.path.getmtime)
        except Exception:
            vids = []
        if not vids:
            return
        path = vids[-1]
        run_id = os.path.splitext(os.path.basename(path))[0]
        with self._lock:
            self._pending_upload = (run_id, path)
            self._error = errors.err("SYS-04")
            self._state = "failed"
            self._can_start = False
        logger.info("SYS-04: phát hiện video tồn đọng %s", path)

    # ── snapshot for the UI ──
    def snapshot(self):
        with self._lock:
            warn = self._warning if (self._warning and
                                     time.monotonic() - self._warning_ts < WARNING_TTL) else None
            return {
                "state": self._state,
                "count": self._count,
                "target": self._target,
                "online": self._online,
                "connected": self._link.connected,
                # v28.4: camera CÓ cắm vào Pi không (tín hiệu phần cứng cho icon). Rẻ: chỉ check
                #   TỒN TẠI device (KHÔNG mở -> không xung đột Recorder lúc đang quay). FAKE -> coi như có.
                "camera": bool(os.environ.get("CARD_FAKE_CAMERA")) or os.path.exists(settings.camera.device),
                # v29.1: dây cảm biến D4 (probe lúc đứng yên, LIVE — tự hồi khi cắm lại).
                # False -> UI mờ chip + popup "Cảm biến" + chặn START (không cần HOME).
                "sensor": bool(getattr(self._status, "sensor_ok", True)),
                # v29.2: dây encoder D2/D3 (probe idle, LIVE — tự hồi khi cắm lại).
                "encoder": bool(getattr(self._status, "encoder_ok", True)),
                # v29: latch phần cứng từ firmware -> tên để UI làm MỜ chip + liệt kê popup + chặn START.
                "hw_faults": [name for bit, name in _HW_NAMES
                              if int(getattr(self._status, "hw", 0)) & bit],
                # v28.2: True = stepper đang chạm công tắc top (home chuẩn) -> UI mở START.
                # Sim/firmware cũ không có cờ lim -> parser mặc định True (hành vi cũ giữ nguyên).
                # v29: BẤT KỲ latch phần cứng nào -> ép homed=False để UI hiện nút HOME (cử chỉ re-arm).
                #   Nếu không ép, latch lúc cardCount==0 (platform còn ở top, lim=1) sẽ hiện START -> kẹt.
                "homed": False if int(getattr(self._status, "hw", 0))
                         else bool(getattr(self._status, "homed", True)),
                # v28.3: True = user vừa bấm HOME, stepper đang leo về công tắc -> UI hiện "Homing…"
                "homing": bool(self._homing),
                "printer": self._printer_ok,
                "wifi": self._wifi,        # {setup, connected, signal, ssid}
                "session": self._session,
                "error": self._error,      # object | None
                "warning": warn,           # object | None (transient)
                "recording": self._recording,
                "print_prompt": self._print_prompt,  # run_id chờ hỏi in | None
                # true = upload xong, đang chờ AI + người dùng trả lời popup —
                # UI khóa nút Start (hiện "Processing") tới khi ack
                "awaiting_result": bool(self._await_result),
                # true = admin khoá từ xa → UI phủ màn hình khoá fullscreen
                "locked": self._server_locked,
                # QR-scan enroll: chưa có credentials → UI hiện QR để điện thoại
                # super-admin quét & tạo máy. pair = {code, server_url, status}.
                "enrolled": bool(self._client),
                "pair": self._pair,
            }

    def preview_jpeg(self):
        rec = self._recorder
        return rec.get_latest_jpeg() if rec else None

    def test_preview_jpeg(self):
        """Ảnh live cho /preview_test.mjpeg — CHỈ khi máy KHÔNG chạy (test camera
        thủ công qua Chrome). Bật ffmpeg ở lần gọi đầu; poll-loop tự tắt khi
        không ai xem >5s; start() tắt ngay trước preflight để nhả /dev/video."""
        with self._lock:
            if self._recording or self._state in ("checking", "warmup", "recording", "uploading"):
                return None
            self._test_last_access = time.monotonic()
            if self._test_preview is None:
                from camera import TestPreview
                tp = TestPreview()
                if not tp.start():
                    logger.warning("test preview không bật được: %s", tp.error)
                    return None
                self._test_preview = tp
                logger.info("test preview camera BẬT (xem thử qua /preview_test.mjpeg)")
        return self._test_preview.get_latest_jpeg()

    def _stop_test_preview(self):
        tp, self._test_preview = self._test_preview, None
        if tp:
            try:
                tp.stop()
                logger.info("test preview camera TẮT")
            except Exception:
                pass

    # ── actions ──
    def start(self):
        with self._lock:
            if self._server_locked:
                # máy đang bị admin khoá từ xa — chặn tuyệt đối
                return False
            if self._await_result:
                # đang chờ AI trả kết quả + người dùng trả lời popup in QR —
                # chưa cho chạy mẻ mới (UI hiện "Processing", nút disabled)
                return False
            if not self._can_start or self._recording or self._state in ("checking", "warmup", "recording"):
                return False
            # v29: còn latch lỗi phần cứng (cảm biến/motor/home) -> chưa cho start.
            #   (Firmware còn chặn tầng cuối bằng hwFault; UI mờ chip + hiện HOME.)
            if int(getattr(self._status, "hw", 0)):
                return False
            # v29.1/29.2: dây cảm biến/encoder đang RÚT (probe idle) -> chưa cho start (tự mở khi cắm lại)
            if not getattr(self._status, "sensor_ok", True):
                return False
            if not getattr(self._status, "encoder_ok", True):
                return False
            # v28.2 HOME-GATING: chưa chạm công tắc top -> KHÔNG start (UI đã mờ nút; đây là
            # backstop tầng service; firmware còn tầng cuối: B1 bị từ chối với err=NOHOME)
            if not getattr(self._status, "homed", True):
                return False
            # [review v28.3] đang home dở (stepper đang leo về công tắc) -> chưa cho start
            if self._homing:
                return False
            self._can_start = False
            self._recording = True
            self._finishing = False
            self._run_id = None          # clear stale run_id before new cycle
            self._cancel_evt.clear()
            self._error = None
            self._state = "checking"
            self._count = 0
            self._committed = 0
        # nhả /dev/video nếu đang xem thử camera (test preview) — Recorder cần nó
        self._stop_test_preview()
        threading.Thread(target=self._run_cycle, daemon=True).start()
        return True

    def _set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, "_" + k, v)

    def _auto_idle_after(self, run_id, delay=DONE_AUTO_IDLE):
        """Sau khi 'done', tự về 'idle' (Sẵn sàng) sau `delay` giây — chỉ khi
        vẫn còn ở đúng lượt 'done' đó (không ghi đè nếu user đã bấm Bắt đầu
        lượt mới, hoặc có lỗi/đổi trạng thái)."""
        def th():
            time.sleep(delay)
            with self._lock:
                if self._state == "done" and self._run_id == run_id and not self._recording:
                    self._state = "idle"
                    self._count = 0
                    self._session = ""
        threading.Thread(target=th, daemon=True).start()

    def _server_error(self, e):
        """Map a start_run() exception to the right SRV-xx code."""
        # AuthError từ bước mint token cũng có thể mang device_locked
        if "device_locked" in str(e):
            self._server_locked = True
            logger.warning("token/start bị chặn: device_locked -> khoá màn hình")
            return errors.err("SRV-03", reason="device_locked")
        try:
            import requests as _r       # lazy: works even if startup import failed
            R = _r.exceptions
        except Exception:
            R = None
        if R is not None:
            if isinstance(e, R.HTTPError):
                resp = getattr(e, "response", None)
                code = getattr(resp, "status_code", 0) or 0
                if code in (401, 403):
                    # KHOÁ TỪ XA: tuyệt đối không rơi vào SRV-04 (action=reset →
                    # auto factory-reset). Set cờ khoá — overlay sẽ phủ màn hình;
                    # trả SRV-03 (retry) vô hại phía sau overlay.
                    reason = ""
                    try:
                        d = resp.json().get("detail")
                        if isinstance(d, dict):
                            reason = str(d.get("reason", ""))
                    except Exception:
                        pass
                    if reason == "device_locked":
                        self._server_locked = True
                        logger.warning("start_run bị chặn: device_locked -> khoá màn hình")
                        return errors.err("SRV-03", reason="device_locked")
                    return errors.err("SRV-04")
                return errors.err("SRV-06", http=code)
            if isinstance(e, R.Timeout):
                return errors.err("SRV-02")
            if isinstance(e, R.ConnectionError):
                s = str(e).lower()
                if any(k in s for k in ("name or service", "temporary failure",
                                        "nodename", "getaddrinfo", "no address")):
                    return errors.err("SRV-01")   # DNS / no route
                return errors.err("SRV-02")        # refused / unreachable / timeout
        if isinstance(e, ValueError):              # JSON decode (incl. requests JSONDecodeError)
            return errors.err("SRV-06", http="?")
        return errors.err("SRV-02")

    def _await_run_slot(self):
        """Server says this device already has a recording run (orphan from a
        run that was killed before it could cancel). The device can't cancel an
        orphan (cancel needs its lost session token -> 403), but the server
        auto-expires stale recording runs. Best-effort clear, then poll start_run
        until the slot frees or RUN_SLOT_WAIT elapses.

        Returns the latest start_run() response (ok:true once the slot frees, or
        still device_has_recording_run after the window -> caller maps to SRV-07).
        Returns None if cancelled mid-wait. May raise on a network error (caller
        maps via _server_error)."""
        self._set(state="checking")              # keep UI in "checking", not failed
        self._clear_dangling_runs()              # best effort (only runs we still hold a token for)
        deadline = time.monotonic() + RUN_SLOT_WAIT
        delay = RUN_SLOT_POLL
        resp = {"ok": False, "reason": "device_has_recording_run"}
        while True:
            # the slot won't free instantly — wait first, but bail out at once on cancel
            if self._cancel_evt.wait(delay):
                return None
            resp = self._client.start_run()
            if resp.get("ok") or "recording_run" not in str(resp.get("reason", "")):
                return resp
            if time.monotonic() >= deadline:
                return resp                      # still blocked -> SRV-07
            delay = min(delay * 1.5, RUN_SLOT_POLL_MAX)

    def _run_cycle(self):
        run_id = None
        try:
            # CHECK 1 — server
            if self._cancel_evt.is_set():
                return
            if self._client is None:
                return self._abort(errors.err("SRV-05"), None)
            try:
                resp = self._client.start_run()
            except Exception as e:
                if self._cancel_evt.is_set():
                    return  # cancel raced with network error — suppress the error
                logger.warning(f"start_run failed: {e}")
                return self._abort(self._server_error(e), None)
            # Self-heal: the server rejects with reason `device_has_recording_run`
            # when a previous run was orphaned on the server (e.g. the service was
            # killed mid-run before it could cancel). The operator's "Retry" used
            # to just loop SRV-03 ("Server disconnected") forever — the device
            # cannot cancel an orphan (cancel needs the lost session token -> 403
            # invalid_session). The server auto-expires stale recording runs, so we
            # best-effort clear, then POLL start_run while the slot frees up.
            if not resp.get("ok") and "recording_run" in str(resp.get("reason", "")):
                try:
                    resp = self._await_run_slot()
                except Exception as e:
                    if self._cancel_evt.is_set():
                        return
                    logger.warning(f"start_run retry failed: {e}")
                    return self._abort(self._server_error(e), None)
                if resp is None:        # cancelled mid-wait
                    return
            if not resp.get("ok"):
                reason = str(resp.get("reason", "?"))
                # still blocked by our own orphan -> accurate, retryable message
                code = "SRV-07" if "recording_run" in reason else "SRV-03"
                return self._abort(errors.err(code, reason=resp.get("reason", "?")), None)
            run_id = resp["run_id"]
            self._run_id = run_id
            self._target = self._extract_target(resp)
            self._session = run_id
            if self._cancel_evt.is_set():
                # _cancel_thread may not have this run_id yet (cancel before CHECK 1 returned)
                try:
                    self._client.cancel_run(run_id)
                except Exception:
                    pass
                return

            # CHECK 2 — camera
            ok, msg = camera_probe(settings.camera.check_timeout)
            if not ok:
                if self._cancel_evt.is_set():
                    return
                low = (msg or "").lower()
                code = "CAM-01" if "not found" in low else "CAM-02"
                return self._abort(errors.err(code), run_id)
            if self._cancel_evt.is_set():
                return

            # CHECK 3 — motor controller handshake (no spin)
            # Give the link a short grace to come up: right after boot the
            # Arduino auto-resets when the port opens and needs ~2s, so a START
            # pressed immediately would otherwise fail MCU-01 spuriously.
            if not self._link.is_sim and not self._link.connected:
                t0 = time.monotonic()
                while (not self._link.connected
                       and time.monotonic() - t0 < settings.serial.motor_check_timeout):
                    if self._cancel_evt.is_set():
                        return
                    time.sleep(0.1)
                if not self._link.connected:
                    return self._abort(errors.err("MCU-01"), run_id)
            if self._cancel_evt.is_set():
                return
            if not self._motor_handshake():
                return self._abort(errors.err("MCU-02"), run_id)
            if self._cancel_evt.is_set():
                return

            # CHECK 4 — printer must be connected (the QR slip prints on it).
            # Per spec: no printer → the machine does NOT run, just shows the error.
            # (Bật lại 2026-07-02: từng bị tắt bằng `if False` thời QR-disabled —
            # giờ luồng popup in QR là tính năng chính nên check này bắt buộc.)
            if not printer_available():
                return self._abort(errors.err("PRN-01"), run_id)
            if self._cancel_evt.is_set():
                return

            # turn on camera and SHOW the live feed first; motor runs after warm-up
            self._recorder = Recorder(run_id)
            self._recorder.start()
            if self._recorder.error:
                if self._cancel_evt.is_set():
                    with self._lock:
                        rec, self._recorder = self._recorder, None
                    if rec:
                        try:
                            rec.stop_and_discard()
                        except Exception:
                            pass
                    return
                low = (self._recorder.error or "").lower()
                code = "CAM-03" if "ffmpeg" in low else "CAM-04"
                return self._abort(errors.err(code), run_id)

            # check if cancel raced with recorder start
            if self._cancel_evt.is_set():
                with self._lock:
                    if self._recorder is not None:
                        rec, self._recorder = self._recorder, None
                    else:
                        rec = None
                if rec:
                    try:
                        rec.stop_and_discard()
                    except Exception:
                        pass
                return

            with self._lock:
                self._status = MachineStatus()
                self._count = 0
                self._state = "warmup"        # UI shows camera; motor NOT running yet
            self._wait_camera_ready()         # ~2–3s so the feed is visible first

            if self._cancel_evt.is_set():
                # recorder still running; cancel_thread took it or will take it
                return

            with self._lock:
                self._state = "recording"
                self._last_serial_ts = time.monotonic()   # arm watchdog when motor starts
            self._link.send(f"N{self._target}")
            self._link.send("B1")
        except Exception as e:
            logger.exception("cycle failed")
            self._abort(errors.err("SRV-02"), run_id)

    def _wait_camera_ready(self, min_s=None, max_s=None):
        """Wait for the first preview frame, then keep it visible at least
        CAMERA_WARMUP seconds before the motor starts."""
        min_s = CAMERA_WARMUP if min_s is None else min_s
        max_s = (min_s + 1.5) if max_s is None else max_s
        t0 = time.monotonic()
        while self.preview_jpeg() is None and (time.monotonic() - t0) < max_s:
            time.sleep(0.1)
        rest = min_s - (time.monotonic() - t0)
        if rest > 0:
            time.sleep(rest)

    def _abort(self, err_obj, run_id):
        """Pre-flight / startup failure (nothing recorded yet)."""
        if self._link:
            try:
                self._link.send("B0")
            except Exception:
                pass
        with self._lock:
            rec, self._recorder = self._recorder, None   # lấy dưới lock (chống race với cancel)
        if rec:
            try:
                rec.stop_and_discard()
            except Exception:
                pass
        if run_id and self._client:
            try:
                self._client.cancel_run(run_id)
            except Exception:
                pass
        with self._lock:
            self._recording = False
            self._finishing = False
            self._can_start = True
            self._error = err_obj
            self._state = "failed"

    def _extract_target(self, resp):
        for k in settings.batch.target_keys:
            v = resp.get(k)
            if isinstance(v, int) and v > 0:
                return v
        return settings.batch.target_fallback

    def _auto_finish(self, reason):
        self._link.send("B0")
        # [v28] Dung COMMITTED count (la CHOT that tu [CARD]/DONE), KHONG dung display
        #   (_count co the +1 lac quan do false-trigger). Snap display = committed de
        #   con so cuoi cung tren UI = dung su that (khop popup/ket qua).
        committed = self._committed
        with self._lock:
            self._count = committed
        # STALL with 0 cards = true jam (roller spun but couldn't bite any card).
        if reason == "stall" and committed == 0:
            self._emergency(errors.err("MCU-05"))
            return
        # CHI GUI KHI DU target/target. Chua du (stall giua chung / thieu la) ->
        #   KHONG upload, huy run server-side, popup error "chua du la". User phai
        #   gom du 412 la roi START lai (moi START = me moi tu 0).
        target = self._target or settings.batch.target_fallback
        if committed < target:
            self._emergency(errors.err("MCU-10", count=committed, target=target))
            return
        # du target -> upload
        self._set(state="uploading")
        with self._lock:
            # lấy recorder DƯỚI LOCK — cancel() cũng lấy dưới lock; trước đây chỗ
            # này không lock → 2 thread có thể cùng lấy 1 recorder (vừa keep vừa discard)
            rec, self._recorder = self._recorder, None
        run_id = self._run_id
        video_path = None
        try:
            if rec:
                video_path = rec.stop_and_keep()
        except Exception:
            logger.exception("camera stop failed")
        if not video_path:
            with self._lock:
                self._recording = False; self._finishing = False; self._can_start = True
                self._error = errors.err("CAM-04"); self._state = "failed"
            return
        res = self._upload_with_retry(run_id, video_path)
        with self._lock:
            self._recording = False
            self._finishing = False
            if res == "ok":
                delete_video(video_path)
                self._pending_upload = None
                self._can_start = True
                self._error = None
                self._state = "done"
                self._await_result = run_id      # khóa Start chờ AI + popup ack
                self._await_since = time.monotonic()
                self._auto_idle_after(run_id)   # tự về "Sẵn sàng" sau 5s
            elif res == "gone":
                # run hết hạn/bị reap server-side -> KHÔNG gửi lại được -> bỏ video chết + mở khoá
                delete_video(video_path)
                self._pending_upload = None
                self._can_start = True
                self._error = errors.err("UPL-06")
                self._state = "failed"
            else:
                self._pending_upload = (run_id, video_path)
                self._error = errors.err("UPL-04", max=UPLOAD_MAX_RETRIES)
                self._state = "failed"

    @staticmethod
    def _endpoint_missing(e) -> bool:
        """HTTP 404/405 KHÔNG kèm reason có cấu trúc = server cũ chưa có route
        /upload (404 run_not_found thật thì detail là dict có reason)."""
        if not (requests and isinstance(e, requests.exceptions.HTTPError)):
            return False
        resp = getattr(e, "response", None)
        if resp is None or resp.status_code not in (404, 405):
            return False
        try:
            return not isinstance(resp.json().get("detail"), dict)
        except Exception:
            return True

    def _upload_with_retry(self, run_id, video_path):
        """-> "ok" | "gone" (run expired/rejected server-side = un-resendable) | "retry" (transient)."""
        for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
            try:
                # ĐƯỜNG CHÍNH: upload xuyên qua backend — chỉ cần link server sống,
                # không phụ thuộc MINIO_PUBLIC_ENDPOINT (fix triệt để UPL-05 khi
                # presigned URL trỏ host mà thiết bị không tới được).
                if not self._direct_unsupported and hasattr(self._client, "upload_direct"):
                    try:
                        if self._client.upload_direct(run_id, video_path).get("ok"):
                            logger.info(f"upload ok (via backend) attempt {attempt}: {run_id}")
                            return "ok"
                        raise RuntimeError("direct upload rejected")
                    except Exception as e:
                        if self._endpoint_missing(e):
                            # server cũ chưa có /upload → dùng presigned từ giờ trở đi
                            self._direct_unsupported = True
                            logger.info("direct upload unsupported by server → presigned fallback")
                        else:
                            raise
                # DỰ PHÒNG: presigned PUT thẳng MinIO (server cũ chưa có /upload)
                url = self._client.get_upload_url(run_id)
                if not self._client.upload_video(url["upload_url"], video_path):
                    raise RuntimeError("PUT failed")
                if self._client.complete_upload(run_id, url["object_key"]).get("ok"):
                    logger.info(f"upload ok attempt {attempt}: {run_id}")
                    return "ok"
            except Exception as e:
                # Permanent server rejection (4xx, e.g. 403 invalid_session / 404) = the run expired
                # or was reaped server-side. Resending can NEVER succeed -> report "gone" so the
                # caller discards the dead video and unblocks (no more infinite Resend loop).
                if requests and isinstance(e, requests.exceptions.HTTPError):
                    resp = getattr(e, "response", None)
                    if resp is not None and resp.status_code < 500:
                        logger.warning(f"upload permanent error {resp.status_code} (run gone): {e}")
                        return "gone"
                logger.warning(f"upload {attempt}/{UPLOAD_MAX_RETRIES} failed: {e}")
                if attempt < UPLOAD_MAX_RETRIES:
                    time.sleep(UPLOAD_RETRY_DELAY)
        return "retry"

    # (đã xoá 2026-07-03: bản reset() "mềm" cũ ở đây bị bản factory-reset phía
    #  dưới ĐÈ MẤT — Python lấy định nghĩa sau — nên nó là dead code từ lâu.
    #  /api/reset = factory reset, đúng ý đồ nút "Đặt lại thiết bị" + auto-reset.)

    def cancel(self):
        with self._lock:
            if not self._recording and self._state not in ("checking", "warmup", "recording"):
                return False
            self._cancel_evt.set()
            rec = self._recorder
            self._recorder = None
            run_id = self._run_id
        self._link.send("B0")
        threading.Thread(target=self._cancel_thread, args=(rec, run_id), daemon=True).start()
        return True

    def _cancel_thread(self, rec, run_id):
        try:
            if rec:
                rec.stop_and_discard()
            if run_id and self._client:
                try:
                    self._client.cancel_run(run_id)
                except Exception:
                    pass
        finally:
            with self._lock:
                self._recording = False; self._finishing = False
                self._can_start = True; self._error = None; self._state = "idle"; self._count = 0

    def dismiss_error(self):
        """Lỗi → bấm OK → VỀ READY (idle). Chủ đích KHÔNG start ngay: lỗi giữa mẻ
        = có lá đã rơi xuống khay, người vận hành phải gom đủ 412 lá rồi mới bấm
        START (mẻ luôn chạy trọn từ 0 — không có 'quay tiếp'). Không đụng
        credentials/AP (khác factory reset). Không áp dụng khi đang chạy/upload."""
        with self._lock:
            if self._recording or self._state in ("checking", "warmup", "recording", "uploading"):
                return False
            if self._pending_upload:
                return False       # còn video chờ gửi — phải Resend/hết hạn trước
            self._error = None
            self._state = "idle"
            self._count = 0
            self._can_start = True
            return True

    def _reconcile_pending(self, run_id) -> bool:
        """R2 (BA_kiosk_and_video_power_loss.md): the server is the source of truth,
        keyed by run_id. Before re-uploading a pending video, ask the server whether
        it ALREADY has it — a power cut AFTER the server received the clip but BEFORE
        the local file was deleted would otherwise re-send the whole 412-card video
        (and risk a duplicate on a non-idempotent backend).

        Returns True ONLY when the server POSITIVELY confirms it has the run
        (uploaded/processing/done) → caller discards the local file, no re-upload.
        Any doubt (recording, unknown status, missing endpoint, network error) →
        False → re-upload as normal; the upload path still discards a truly-gone run
        via its 4xx ('gone') handling, so we never wrongly delete an un-sent video."""
        if not self._client or not hasattr(self._client, "get_run_status"):
            return False
        try:
            st = self._client.get_run_status(run_id)
        except Exception as e:
            logger.debug("reconcile get_run_status(%s) failed: %s", run_id, e)
            return False
        status = str((st or {}).get("status", "")).lower()
        if status in ("uploaded", "processing", "done"):
            logger.info("R2: server already has run %s (%s) → discard local, skip re-upload",
                        run_id, status)
            return True
        return False

    def retry(self):
        if not self._pending_upload or self._recording:
            return False
        run_id, video_path = self._pending_upload
        self._set(state="uploading")
        def th():
            # R2: don't re-upload a video the server already has (power cut after
            # the server committed it, before the local delete). Adopt it as done.
            if self._reconcile_pending(run_id):
                with self._lock:
                    delete_video(video_path); self._pending_upload = None
                    self._can_start = True; self._error = None; self._state = "done"
                    self._run_id = run_id
                    self._await_result = run_id      # lock Start: wait for AI + QR ack
                    self._await_since = time.monotonic()
                self._auto_idle_after(run_id)
                return
            res = self._upload_with_retry(run_id, video_path)
            with self._lock:
                if res == "ok":
                    delete_video(video_path); self._pending_upload = None
                    self._can_start = True; self._state = "done"; self._error = None
                    self._run_id = run_id
                    self._await_result = run_id      # khóa Start chờ AI + popup ack
                    self._await_since = time.monotonic()
                elif res == "gone":
                    # run hết hạn server-side -> bỏ video chết + mở khoá (hết kẹt bấm Resend hoài)
                    delete_video(video_path); self._pending_upload = None
                    self._can_start = True; self._error = errors.err("UPL-06"); self._state = "failed"
                else:
                    self._error = errors.err("UPL-05", max=UPLOAD_MAX_RETRIES); self._state = "failed"
            if res == "ok":
                self._auto_idle_after(run_id)   # tự về "Sẵn sàng" sau 5s
        threading.Thread(target=th, daemon=True).start()
        return True

    def history(self):
        if not self._client:
            return []
        try:
            return self._client.list_runs()
        except Exception:
            return []

    # ── device settings (behind the gear) ──
    def device_info(self):
        try:
            creds = settings.credentials.load() or {}
        except Exception:
            creds = {}
        return {
            "enrolled": bool(creds),
            "device_id": creds.get("device_id", ""),
            "server_url": creds.get("server_url", ""),
            "ap_ssid": AP_SSID_ENV,
        }

    def reset(self):
        """Đặt lại thiết bị: dừng mẻ, xoá credentials, rồi BẬT WiFi AP để người
        dùng cài lại mạng (app sẽ chuyển sang màn cài WiFi). Bật AP ở thread nền
        vì nó ngắt mạng hiện tại — phải để /api/reset trả lời UI trước đã."""
        if self._recording:
            self.cancel()
        settings.credentials.clear()
        with self._lock:
            self._client = None
            self._online = False
            self._state = "idle"
            self._error = None
            self._can_start = True
            self._count = 0
            self._pending_upload = None
        logger.info("device reset — credentials cleared, bật AP cài WiFi")
        self._start_wifi_ap()
        return True

    def _start_wifi_ap(self):
        """Bật AP cài WiFi (chạy nền, không chặn). Cần sudoers NOPASSWD cho
        wifi_ap.sh (xem /etc/sudoers.d/card-wifi)."""
        here = os.path.dirname(os.path.abspath(__file__))
        ap = os.path.join(here, "wifi", "wifi_ap.sh")  # [gom folder 2026-07] wifi_ap.sh chuyển vào device_service/wifi/

        # Truyền CARD_AP_SSID qua sudo bằng wrapper env — sudo strip env mặc định,
        # nếu không SSID AP sẽ về default "CardFeeder-XXXX" trong khi QR trên màn
        # hình mã hoá CARD_AP_SSID → LỆCH → điện thoại báo không tìm thấy mạng.
        _ssid = settings.wifi.ap_ssid
        def th():
            time.sleep(1.5)   # cho /api/reset response về UI trước khi cắt mạng
            try:
                import subprocess
                cmd = ["sudo", "-n", "bash", ap, "up"]
                if _ssid:
                    cmd = ["sudo", "-n", "env", f"CARD_AP_SSID={_ssid}", "bash", ap, "up"]
                subprocess.run(cmd, capture_output=True, text=True, timeout=40)
                logger.info("WiFi AP bật cho cài đặt (ssid=%s)", _ssid or "default")
            except Exception as e:
                logger.warning("không bật được AP: %s", e)
        threading.Thread(target=th, daemon=True).start()

    # ── QR-scan pairing (un-enrolled device) ──────────────────────────────────
    # The device shows a QR "CMDPAIR:<code>"; a super-admin scans it on their
    # phone, names the machine, and the server hands back credentials which the
    # background poll picks up and saves — no typing on the device.
    @staticmethod
    def _http_json(method, url, payload):
        import urllib.request
        data = None
        # A real User-Agent is REQUIRED: the pairing server sits behind Cloudflare
        # (cmdtest.berp.vn), which 403s urllib's default "Python-urllib/*" UA.
        headers = {"Accept": "application/json",
                   "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) CardDevice/1.0"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())

    def begin_pairing(self):
        """Start (or return the existing) QR pairing session. Idempotent. Refuses
        if the device is already enrolled — so calling it on a live machine is a
        safe no-op that never overwrites credentials."""
        with self._lock:
            if self._client:
                return {"ok": False, "enrolled": True}
            if self._pair and self._pair.get("status") == "pending":
                return {"ok": True, **self._pair}
            import secrets
            code = "P" + secrets.token_urlsafe(18)   # ~25 chars, high entropy
            self._pair = {"code": code,
                          "server_url": settings.enroll.pair_server_url,
                          "status": "pending", "error": None}
        threading.Thread(target=self._pair_loop, args=(code,), daemon=True).start()
        with self._lock:
            return {"ok": True, **(self._pair or {})}

    def pairing_status(self):
        with self._lock:
            return {"enrolled": bool(self._client), "pair": self._pair}

    def _pair_loop(self, code):
        from urllib.error import HTTPError
        import socket
        base = settings.enroll.pair_server_url.rstrip("/")
        hint = socket.gethostname()

        def announce():
            try:
                self._http_json("POST", base + "/api/device/pair/announce",
                                {"pair_code": code, "name_hint": hint})
            except Exception as e:
                logger.warning("pair announce failed: %s", e)

        announce()
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            with self._lock:
                # superseded by a newer code, or already enrolled → stop
                if not self._pair or self._pair.get("code") != code or self._client:
                    return
            try:
                data = self._http_json("GET", base + "/api/device/pair/" + code, None)
            except HTTPError as he:
                data = None
                if he.code == 404:   # server lost the pending code (restart) → re-announce
                    announce()
            except Exception:
                data = None
            if data and data.get("status") == "claimed":
                creds = {"server_url": data.get("server_url") or base,
                         "device_id": data.get("device_id"),
                         "device_key": data.get("device_key")}
                if creds["device_id"] and creds["device_key"]:
                    self._apply_pair_creds(code, creds)
                    return
            time.sleep(2.0)
        with self._lock:
            if self._pair and self._pair.get("code") == code:
                self._pair["status"] = "expired"

    def _apply_pair_creds(self, code, creds):
        """Claimed → save credentials.json, rebuild the API client, verify the
        heartbeat. Mirrors enroll() so the 'online' badge lights immediately."""
        try:
            settings.credentials.save(creds)
            client = APIClient(creds["server_url"], creds["device_id"], creds["device_key"])
            with self._lock:
                self._client = client
                if (self._state == "failed" and self._error
                        and self._error.get("code") == "SRV-05"):
                    self._error = None
                    self._state = "idle"
                self._pair = {"code": code, "server_url": creds["server_url"],
                              "status": "done", "error": None}
            try:
                self._online = bool(client.heartbeat())
            except Exception:
                pass
            logger.info("device paired & enrolled via QR: %s", creds["device_id"])
        except Exception as e:
            logger.warning("apply pair creds failed: %s", e)
            with self._lock:
                if self._pair and self._pair.get("code") == code:
                    self._pair["status"] = "pending"
                    self._pair["error"] = str(e)[:120]

    def enroll(self, server_url, device_id, setup_token):
        if not (server_url and device_id and setup_token):
            return {"ok": False, "error": "Missing activation info"}
        try:
            data = APIClient.enroll(server_url, device_id, setup_token)
            creds = {"server_url": server_url,
                     "device_id": data["device_id"], "device_key": data["device_key"]}
            settings.credentials.save(creds)
            client = APIClient(server_url, data["device_id"], data["device_key"])
            with self._lock:
                self._client = client
                if self._state == "failed" and self._error and self._error.get("code") == "SRV-05":
                    self._error = None; self._state = "idle"
            try:
                self._online = bool(client.heartbeat())
            except Exception:
                pass
            logger.info("device enrolled: %s", data["device_id"])
            return {"ok": True}
        except Exception as e:
            logger.warning("enroll failed: %s", e)
            return {"ok": False, "error": str(e)[:120]}

    def send_log(self):
        """'Gửi log lỗi' — placeholder hook; wire to the server when the
        endpoint exists. For now it just records the intent."""
        code = (self._error or {}).get("code") if isinstance(self._error, dict) else None
        logger.info("operator sent error log: state=%s code=%s session=%s",
                    self._state, code, self._session)
        return True

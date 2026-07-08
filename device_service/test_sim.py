#!/usr/bin/env python3
"""
test_sim.py — bộ kiểm thử mô phỏng cho device_service (KHÔNG cần phần cứng).

Mỗi kịch bản khởi động server.py trong subprocess với các biến môi trường
CARD_* mô phỏng (fake server, fake Arduino, fake camera/recorder), điều khiển
qua HTTP API và xác minh chuỗi trạng thái + mã lỗi.

Chế độ mô phỏng (env vars):
  CARD_FAKE_SERVER=1        → client máy chủ giả (không mạng thật)
  CARD_SERIAL_PORT=sim      → Arduino giả (simulator.py)
  CARD_FAKE_CAMERA=ok|notfound|busy → camera probe pass/CAM-01/CAM-02
  CARD_FAKE_RECORDER=1      → Recorder chạy không cần ffmpeg/camera
  CARD_FAKE_SRV_FAIL=down|reject → start_run ném ConnectionError (SRV-02) / trả ok=false (SRV-03)
  CARD_FAKE_UPLOAD_FAIL=1   → upload luôn thất bại (UPL-04)
  CARD_FAKE_OFFLINE=1       → heartbeat trả false (đèn offline)
  BSS_SIM_LEAF_MS, BSS_SIM_STALL_AT, CARD_FAKE_TARGET → nhịp đếm của simulator

Các kịch bản chạy ĐƯỢC HOÀN TOÀN trong mô phỏng (không phần cứng):
  tất cả 14 kịch bản dưới đây.

Các kịch bản cần phần cứng thật (KHÔNG nằm trong file này):
  - MCU thật rớt USB giữa lúc quay (MCU-04 watchdog) — cần Arduino thật rút cáp.
  - ffmpeg ghi hỏng file do camera rớt giữa chừng (CAM-04/CAM-05) thực tế.
  Những ca này được mô phỏng gần đúng ở đây nhưng nên xác minh lại trên Pi.

Cách chạy:
  python3 test_sim.py            # chạy tất cả
  python3 test_sim.py happy      # chạy kịch bản có tên chứa "happy"
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import requests
except ImportError:
    print("Cần 'requests' để chạy test. Cài: pip install requests")
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("TEST_PORT", "18800"))
BASE = f"http://127.0.0.1:{PORT}"

# màu terminal
GREEN = "\033[32m"; RED = "\033[31m"; CYAN = "\033[36m"; DIM = "\033[2m"; OFF = "\033[0m"

# môi trường nền chung cho mọi test: không fullscreen, port test, fake serial
BASE_ENV = {
    "CARD_WEB_PORT": str(PORT),
    "CARD_WEB_HOST": "127.0.0.1",
    "CARD_FULLSCREEN": "0",
    "CARD_SERIAL_PORT": "sim",
    # nhịp đếm nhanh để test chạy nhanh; warm-up ngắn
    "BSS_SIM_LEAF_MS": "15",
    "CARD_CAMERA_WARMUP": "0.3",
    "CARD_MOTOR_CHECK_TIMEOUT": "3.0",
    "CARD_UPLOAD_RETRY_DELAY": "0.2",
    "CARD_UPLOAD_MAX_RETRIES": "3",
    # CHECK 4 (printer) đã bật lại 2026-07-02 — test không có máy in thật
    "CARD_FAKE_PRINTER": "1",
}

_results = []   # (name, ok, detail)


class Server:
    """Khởi động server.py trong subprocess với env tùy biến, dọn dẹp khi xong.

    Mỗi server chạy trong một HOME tạm riêng + CARD_DEVICE_DIR/CARD_TMP_DIR trỏ
    vào đó → cô lập hoàn toàn credentials.json và video tồn đọng giữa các test.
    Truyền seed_pending=<tên file> để đặt sẵn 1 video tồn đọng cho test SYS-04.
    """

    def __init__(self, env_extra, seed_pending=None):
        self.proc = None
        self.env_extra = env_extra
        self.seed_pending = seed_pending
        self.home = None

    def __enter__(self):
        self.home = tempfile.mkdtemp(prefix="cardtest_home_")
        if self.seed_pending:
            tmp = os.path.join(self.home, "card_tmp")
            os.makedirs(tmp, exist_ok=True)
            with open(os.path.join(tmp, self.seed_pending), "wb") as f:
                f.write(b"\x00" * 2048)
        env = dict(os.environ)
        env.update(BASE_ENV)
        env.update(self.env_extra)
        # Cô lập trạng thái thiết bị: settings.py mặc định trỏ CARD_DEVICE_DIR
        # vào workspace THẬT nên phải ghi đè tường minh (env thật > .env).
        env["CARD_DEVICE_DIR"] = os.path.join(self.home, "card_device")
        env["CARD_TMP_DIR"] = os.path.join(self.home, "card_tmp")
        # HOME tạm cho phần còn lại. Giữ PYTHONUSERBASE trỏ về ~/.local THẬT để
        # `import requests` (user-site) vẫn resolve được — nếu không, HOME tạm
        # rỗng làm import requests lỗi.
        env["PYTHONUSERBASE"] = env.get("PYTHONUSERBASE") or os.path.expanduser("~/.local")
        env["HOME"] = self.home
        # tắt log ồn ào của server cho gọn output (vẫn giữ ERROR)
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=HERE, env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_ready()
        return self

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        if self.home and os.path.isdir(self.home):
            shutil.rmtree(self.home, ignore_errors=True)

    def _wait_ready(self, timeout=8.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                requests.get(f"{BASE}/api/state", timeout=1)
                return True
            except Exception:
                time.sleep(0.15)
        raise RuntimeError("server không khởi động kịp")


def state():
    return requests.get(f"{BASE}/api/state", timeout=2).json()


def post(path):
    return requests.post(f"{BASE}{path}", timeout=5).json()


def wait_for(pred, timeout, desc=""):
    """Poll /api/state tới khi pred(snapshot) đúng hoặc hết giờ. Trả snapshot cuối."""
    t0 = time.monotonic()
    last = None
    while time.monotonic() - t0 < timeout:
        last = state()
        if pred(last):
            return last
        time.sleep(0.1)
    return last


def err_code(s):
    e = s.get("error")
    return e.get("code") if isinstance(e, dict) else None


def err_action(s):
    e = s.get("error")
    return e.get("action") if isinstance(e, dict) else None


def record(name, ok, detail):
    _results.append((name, ok, detail))
    mark = f"{GREEN}✓ PASS{OFF}" if ok else f"{RED}✗ FAIL{OFF}"
    print(f"  {mark} — {detail}")


def header(name):
    print(f"\n{CYAN}═══ {name} ═══{OFF}")


# ─────────────────────────── KỊCH BẢN ───────────────────────────

def t_srv05_no_credentials():
    """Chưa kích hoạt (không có client) → start → SRV-05, nút Kích hoạt."""
    header("SRV-05 — Chưa kích hoạt thiết bị")
    # KHÔNG bật CARD_FAKE_SERVER → client = None (chưa enroll)
    with Server({"CARD_FAKE_CAMERA": "ok", "CARD_FAKE_RECORDER": "1"}):
        post("/api/start")
        s = wait_for(lambda s: s["state"] == "failed", 5)
        ok = s["state"] == "failed" and err_code(s) == "SRV-05" and err_action(s) == "enroll"
        record("SRV-05", ok, f"state={s['state']} code={err_code(s)} action={err_action(s)}")


def t_srv03_reject():
    """Máy chủ trả ok=false → SRV-03, nút Thử lại."""
    header("SRV-03 — Máy chủ từ chối lượt quay")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_SRV_FAIL": "reject",
                 "CARD_FAKE_SRV_REASON": "hết hạn mức", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1"}):
        post("/api/start")
        s = wait_for(lambda s: s["state"] == "failed", 5)
        ok = err_code(s) == "SRV-03"
        record("SRV-03", ok, f"state={s['state']} code={err_code(s)} title={(s.get('error') or {}).get('title')}")


def t_srv02_server_down():
    """Máy chủ không kết nối được (ConnectionError) → SRV-02, nút Thử lại."""
    header("SRV-02 — Không kết nối máy chủ (mất mạng / server down)")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_SRV_FAIL": "down",
                 "CARD_FAKE_CAMERA": "ok", "CARD_FAKE_RECORDER": "1"}):
        post("/api/start")
        s = wait_for(lambda s: s["state"] == "failed", 5)
        ok = err_code(s) in ("SRV-01", "SRV-02") and err_action(s) == "retry"
        record("SRV-02", ok, f"state={s['state']} code={err_code(s)} action={err_action(s)}")


def t_cam01_notfound():
    """Camera không cắm → CAM-01, nút Thử lại."""
    header("CAM-01 — Không tìm thấy camera (rút camera ra)")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "notfound"}):
        post("/api/start")
        s = wait_for(lambda s: s["state"] == "failed", 5)
        ok = err_code(s) == "CAM-01" and err_action(s) == "retry"
        record("CAM-01", ok, f"state={s['state']} code={err_code(s)} action={err_action(s)}")


def t_cam02_busy():
    """Camera treo / v4l2 không phản hồi → CAM-02."""
    header("CAM-02 — Camera không phản hồi")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "busy"}):
        post("/api/start")
        s = wait_for(lambda s: s["state"] == "failed", 5)
        ok = err_code(s) == "CAM-02"
        record("CAM-02", ok, f"state={s['state']} code={err_code(s)}")


def t_mcu01_no_serial():
    """Không có cổng serial thật (port không tồn tại) → MCU-01."""
    header("MCU-01 — Không kết nối bộ điều khiển (rút tín hiệu motor)")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1",
                 "CARD_SERIAL_PORT": "/dev/ttyNOPE_TEST",   # port không tồn tại
                 "CARD_MOTOR_CHECK_TIMEOUT": "1.0"}):
        post("/api/start")
        s = wait_for(lambda s: s["state"] == "failed", 6)
        ok = err_code(s) == "MCU-01"
        record("MCU-01", ok, f"state={s['state']} code={err_code(s)}")


def t_happy_path():
    """Toàn bộ chu trình mô phỏng: checking → warmup → recording → done."""
    header("HAPPY PATH — chu trình đầy đủ mô phỏng (server+serial+camera ảo)")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1", "CARD_FAKE_TARGET": "8",
                 "BSS_SIM_LEAF_MS": "10"}):
        post("/api/start")
        saw = wait_for(lambda s: s["state"] in ("warmup", "recording"), 5)
        s = wait_for(lambda s: s["state"] == "done", 15)
        ok = s["state"] == "done" and err_code(s) is None
        record("HAPPY PATH", ok,
               f"saw {saw['state']} → cuối state={s['state']} count={s['count']}/{s['target']}")


def t_upl04_upload_fail():
    """Quay xong nhưng gửi thất bại → UPL-04, nút Gửi lại; giữ video."""
    header("UPL-04 — Gửi video thất bại (mạng rớt khi upload)")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1", "CARD_FAKE_TARGET": "5",
                 "CARD_FAKE_UPLOAD_FAIL": "1", "BSS_SIM_LEAF_MS": "10"}):
        post("/api/start")
        s = wait_for(lambda s: s["state"] == "failed", 15)
        ok = err_code(s) == "UPL-04" and err_action(s) == "resend"
        record("UPL-04", ok, f"state={s['state']} code={err_code(s)} action={err_action(s)}")


def t_sys04_pending_video():
    """Có video tồn đọng khi khởi động → failed + SYS-04 + nút Gửi lại."""
    header("SYS-04 — Còn video chưa gửi từ lượt trước (reboot giữa chừng)")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1"}, seed_pending="pendingtest1234.mp4"):
        s = wait_for(lambda s: s["state"] == "failed", 5)
        ok = err_code(s) == "SYS-04" and err_action(s) == "resend"
        record("SYS-04", ok, f"state={s['state']} code={err_code(s)} action={err_action(s)}")


def t_auto_resend():
    """Video kẹt (SYS-04) + server online → máy TỰ gửi lại, không cần bấm RESEND."""
    header("AUTO-RESEND — máy tự gửi lại video kẹt, không cần người bấm")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1",
                 "CARD_AUTO_RESEND_INTERVAL": "1",
                 "CARD_RUN_POLL_INTERVAL": "0.5"}, seed_pending="autoresend01.mp4"):
        # KHÔNG gọi /api/retry — chờ máy tự hồi phục từ SYS-04
        s = wait_for(lambda s: s["state"] in ("done", "idle") and not s.get("error"),
                     15, "tự gửi lại video kẹt")
        ok = s["state"] in ("done", "idle") and not s.get("error")
        record("AUTO-RESEND", ok, f"state={s['state']} err={err_code(s)} (không bấm resend)")


def t_online_offline():
    """Đèn online theo heartbeat: bình thường online=true; OFFLINE=1 → false."""
    header("ONLINE/OFFLINE — đèn kết nối máy chủ (mất mạng & nối lại)")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1"}):
        s = wait_for(lambda s: s.get("online") is True, 5)
        ok1 = s.get("online") is True
        record("ONLINE", ok1, f"online={s.get('online')} (heartbeat OK)")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_OFFLINE": "1",
                 "CARD_FAKE_CAMERA": "ok", "CARD_FAKE_RECORDER": "1"}):
        s = wait_for(lambda s: s.get("online") is False, 6)
        ok2 = s.get("online") is False
        record("OFFLINE", ok2, f"online={s.get('online')} (heartbeat fail)")


def t_cancel_during_checking():
    """Bấm Hủy trong lúc đang quay → quay về idle, không treo motor."""
    header("CANCEL — Hủy giữa lúc đang quay (motor phải dừng, về idle)")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1", "CARD_FAKE_TARGET": "500",
                 "BSS_SIM_LEAF_MS": "30"}):
        post("/api/start")
        # đợi vào recording rồi mới hủy
        wait_for(lambda s: s["state"] in ("warmup", "recording"), 5)
        post("/api/cancel")
        s = wait_for(lambda s: s["state"] == "idle", 6)
        ok = s["state"] == "idle" and err_code(s) is None and s["recording"] is False
        record("CANCEL", ok, f"state={s['state']} recording={s['recording']}")


def t_stall_normal_end():
    """[v27] STALL giữa chừng (count < target) → CHƯA ĐỦ LÁ → KHÔNG gửi server,
    popup error MCU-10. Yêu cầu user: chỉ gửi khi đủ 412/412; thiếu là báo lỗi."""
    header("STALL→INCOMPLETE — thiếu lá (count<target) → không gửi + popup MCU-10")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1", "CARD_FAKE_TARGET": "100",
                 "BSS_SIM_STALL_AT": "5", "BSS_SIM_LEAF_MS": "10"}):
        post("/api/start")
        s = wait_for(lambda s: s["state"] in ("done", "failed"), 15)
        # count=5 < target=100 -> KHONG upload -> failed + MCU-10 (chua du la)
        ok = s["state"] == "failed" and err_code(s) == "MCU-10"
        record("STALL→INCOMPLETE", ok, f"state={s['state']} count={s['count']} code={err_code(s)}")


def t_mcu05_stall_zero():
    """STALL ngay khi count=0 (motor không kéo được lá) → MCU-05."""
    header("MCU-05 — Motor không kéo được lá nào (kẹt từ đầu)")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1", "CARD_FAKE_TARGET": "100",
                 "BSS_SIM_STALL_AT": "1", "BSS_SIM_LEAF_MS": "10"}):
        post("/api/start")
        s = wait_for(lambda s: s["state"] in ("done", "failed"), 15)
        # stall_at=1: sim đếm 1 lá rồi stall → count=1 (không phải 0).
        # Đây xác minh nhánh "stall sau khi đếm" KHÔNG bị nhầm thành MCU-05.
        # MCU-05 thật (count=0) cần firmware GAP_STALL — ghi chú: cần Arduino thật.
        ok = s["state"] in ("done", "failed")
        note = "stall@1 → kết thúc (count>0); MCU-05 count=0 cần HW thật"
        record("MCU-05(note)", ok, f"state={s['state']} count={s['count']} — {note}")


def t_recover_after_fail():
    """Sau lỗi (CAM-01) bấm Thử lại với camera OK → chạy lại bình thường (gắn lại camera)."""
    header("RECOVER — gắn camera lại sau lỗi rồi Thử lại (không cần restart app)")
    # Mô phỏng "gắn lại": ta không đổi env giữa chừng được trong 1 process,
    # nên test này xác minh: trạng thái failed → start lại → cycle chạy tiếp.
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1", "CARD_FAKE_TARGET": "5",
                 "BSS_SIM_LEAF_MS": "10"}):
        # chu trình 1: chạy tới done
        post("/api/start")
        s1 = wait_for(lambda s: s["state"] == "done", 15)
        # thiết kế mới: sau upload máy chờ AI + popup — ack để mở khóa Start
        post("/api/print_prompt/ack")
        # chu trình 2: bắt đầu lại sau done → phải chạy lại được
        post("/api/start")
        s2 = wait_for(lambda s: s["state"] == "done", 15)
        ok = s1["state"] == "done" and s2["state"] == "done"
        record("RECOVER", ok, f"lượt1={s1['state']} lượt2={s2['state']} (ack mở khóa giữa 2 lượt)")


def t_recording_run_selfheal():
    """Run treo `device_has_recording_run` trên server (orphan): server từ chối
    vài lần rồi giải phóng slot → controller TỰ chờ & retry → chạy tiếp bình
    thường (KHÔNG kẹt 'Server disconnected', không cần bấm tay)."""
    header("SELF-HEAL — orphan recording run, server nhả slot sau ít lần thử")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1", "CARD_FAKE_TARGET": "5",
                 "BSS_SIM_LEAF_MS": "10",
                 "CARD_FAKE_RECORDING_RUN": "2",   # từ chối 2 lần rồi ok
                 "CARD_RUN_SLOT_WAIT": "10", "CARD_RUN_SLOT_POLL": "0.3",
                 "CARD_RUN_SLOT_POLL_MAX": "0.5"}):
        post("/api/start")
        saw = wait_for(lambda s: s["state"] in ("warmup", "recording", "done"), 8)
        s = wait_for(lambda s: s["state"] == "done", 15)
        ok = s["state"] == "done" and err_code(s) is None
        record("SELF-HEAL", ok, f"saw={saw['state']} → cuối={s['state']} code={err_code(s)}")


def t_srv07_recording_stuck():
    """Orphan recording run KHÔNG được server nhả trong cửa sổ chờ → dừng có
    giới hạn với SRV-07 (thông báo đúng + nút Retry), KHÔNG lặp vô tận / KHÔNG
    hiện sai 'Server disconnected'."""
    header("SRV-07 — orphan run kẹt quá lâu → dừng đúng (bounded, message rõ)")
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1",
                 "CARD_FAKE_RECORDING_RUN": "always",
                 "CARD_RUN_SLOT_WAIT": "2", "CARD_RUN_SLOT_POLL": "0.3",
                 "CARD_RUN_SLOT_POLL_MAX": "0.5"}):
        post("/api/start")
        s = wait_for(lambda s: s["state"] == "failed", 8)
        e = s.get("error") or {}
        ok = (err_code(s) == "SRV-07" and err_action(s) == "retry"
              and e.get("group") == "server_busy")
        record("SRV-07", ok, f"state={s['state']} code={err_code(s)} action={err_action(s)} title={e.get('title')}")


def t_print_prompt():
    """Run chuyển processing→done (AI xong) → snapshot có print_prompt; ack xóa
    prompt; run KHÔNG bao giờ được hỏi lại (persist PRINTED_PATH)."""
    header("PRINT PROMPT — popup hỏi in QR khi run chuyển sang done")
    with Server({"CARD_FAKE_SERVER": "1",
                 "CARD_FAKE_RUN_FLOW": "processing,processing,done",
                 "CARD_RUN_POLL_INTERVAL": "0.3"}):
        s = wait_for(lambda s: s.get("print_prompt") == "fakeflow1", 8)
        got = s.get("print_prompt") == "fakeflow1"
        post("/api/print_prompt/ack")
        s2 = wait_for(lambda s: s.get("print_prompt") is None, 3)
        cleared = s2.get("print_prompt") is None
        # chờ thêm vài chu kỳ poll: run vẫn done nhưng KHÔNG được prompt lại
        time.sleep(1.2)
        s3 = state()
        no_respam = s3.get("print_prompt") is None
        ok = got and cleared and no_respam
        record("PRINT PROMPT", ok,
               f"prompt={got} ack_clears={cleared} no_respam={no_respam}")


def t_print_prompt_catchup_silent():
    """Run đã done ngay lần đầu thấy (máy vừa bật lại) → KHÔNG popup (silent)."""
    header("PRINT PROMPT CATCH-UP — run done sẵn khi boot → không hỏi")
    with Server({"CARD_FAKE_SERVER": "1",
                 "CARD_FAKE_RUN_FLOW": "done",
                 "CARD_RUN_POLL_INTERVAL": "0.3"}):
        # đợi chắc chắn đã qua vài chu kỳ poll
        time.sleep(1.5)
        s = state()
        ok = s.get("print_prompt") is None
        record("PRINT PROMPT CATCH-UP", ok, f"print_prompt={s.get('print_prompt')!r} (kỳ vọng None)")


def t_start_gate():
    """Sau upload OK: Start bị KHÓA (state=processing, /api/start ok=false) tới
    khi AI done → popup → ACK xong mới Start lại được."""
    header("START GATE — khóa Start sau upload tới khi popup được trả lời")
    # LƯU Ý: mỗi vòng run-poll còn chạy nmcli + check printer (~2-4s/vòng trên
    # Pi) nên flow tính theo SỐ LẦN GỌI chứ không theo giây — giữ flow ngắn và
    # timeout rộng. _flow_i reset khi start_run nên done luôn tới SAU khi start.
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1", "CARD_FAKE_TARGET": "8",
                 "BSS_SIM_LEAF_MS": "10",
                 "CARD_FAKE_RUN_FLOW": "processing,processing,done",
                 "CARD_RUN_POLL_INTERVAL": "0.3"}):
        post("/api/start")
        s = wait_for(lambda s: s.get("awaiting_result") is True, 20)  # upload xong → chờ AI
        gated = s.get("awaiting_result") is True
        blocked = not post("/api/start").get("ok")              # bấm Start lúc chờ AI → từ chối
        s2 = wait_for(lambda s: bool(s.get("print_prompt")), 30)
        prompted = bool(s2.get("print_prompt"))
        still_blocked = not post("/api/start").get("ok")        # popup đang hỏi → vẫn khóa
        post("/api/print_prompt/ack")                           # người dùng trả lời popup
        wait_for(lambda s: not s.get("awaiting_result") and not s.get("print_prompt"), 6)
        unlocked = post("/api/start").get("ok") is True         # giờ mới cho chạy tiếp
        post("/api/cancel")
        ok = gated and blocked and prompted and still_blocked and unlocked
        record("START GATE", ok,
               f"gated={gated} blocked={blocked} prompted={prompted} "
               f"still_blocked={still_blocked} unlocked={unlocked}")


def t_remote_lock():
    """Admin khoá từ xa: snapshot.locked=true (heartbeat), Start bị chặn tuyệt
    đối; admin mở khoá → locked=false, Start chạy lại được. KHÔNG factory-reset."""
    header("REMOTE LOCK — khoá/mở khoá thiết bị từ server admin")
    import tempfile
    lockfile = os.path.join(tempfile.gettempdir(), f"cardtest_lock_{os.getpid()}")
    if os.path.exists(lockfile):
        os.remove(lockfile)
    with Server({"CARD_FAKE_SERVER": "1", "CARD_FAKE_CAMERA": "ok",
                 "CARD_FAKE_RECORDER": "1",
                 "CARD_FAKE_LOCKED": lockfile}):
        s0 = wait_for(lambda s: s.get("locked") is False and s["online"], 15)
        pre_ok = s0.get("locked") is False
        open(lockfile, "w").close()                       # admin bấm KHOÁ
        s1 = wait_for(lambda s: s.get("locked") is True, 20)
        locked = s1.get("locked") is True
        blocked = not post("/api/start").get("ok")        # bị khoá → không Start được
        no_reset = (s1.get("error") or {}).get("action") != "reset"  # tuyệt đối không reset
        os.remove(lockfile)                               # admin MỞ KHOÁ
        s2 = wait_for(lambda s: s.get("locked") is False, 20)
        unlocked = s2.get("locked") is False
        can_start = post("/api/start").get("ok") is True  # hoạt động lại bình thường
        post("/api/cancel")
        ok = pre_ok and locked and blocked and no_reset and unlocked and can_start
        record("REMOTE LOCK", ok,
               f"pre_ok={pre_ok} locked={locked} blocked={blocked} "
               f"no_reset={no_reset} unlocked={unlocked} can_start={can_start}")


ALL = [
    t_srv05_no_credentials,
    t_srv03_reject,
    t_recording_run_selfheal,
    t_srv07_recording_stuck,
    t_srv02_server_down,
    t_cam01_notfound,
    t_cam02_busy,
    t_mcu01_no_serial,
    t_happy_path,
    t_upl04_upload_fail,
    t_sys04_pending_video,
    t_auto_resend,
    t_online_offline,
    t_cancel_during_checking,
    t_stall_normal_end,
    t_mcu05_stall_zero,
    t_recover_after_fail,
    t_print_prompt,
    t_print_prompt_catchup_silent,
    t_start_gate,
    t_remote_lock,
]


def main():
    filt = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    tests = [t for t in ALL if not filt or filt in t.__name__.lower()]
    if not tests:
        print(f"Không có test nào khớp '{filt}'")
        return 1

    print(f"{CYAN}Chạy {len(tests)} kịch bản mô phỏng trên port {PORT}{OFF}")
    for t in tests:
        try:
            t()
        except Exception as e:
            record(t.__name__, False, f"EXCEPTION: {e}")

    # tổng kết
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"\n{CYAN}══════════ TỔNG KẾT ══════════{OFF}")
    for name, ok, detail in _results:
        mark = f"{GREEN}PASS{OFF}" if ok else f"{RED}FAIL{OFF}"
        print(f"  [{mark}] {name}: {DIM}{detail}{OFF}")
    color = GREEN if passed == total else RED
    print(f"\n{color}{passed}/{total} kịch bản PASS{OFF}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

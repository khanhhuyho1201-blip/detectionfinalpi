"""
Virtual test of the video-durability logic (power loss / server-or-wifi loss),
against the REAL controller.py + camera.py in FAKE mode with an isolated tmp dir.
Proves the answers to the operator's scenarios BEFORE we explain them.

Run on Pi:
  CARD_FAKE_SERVER=1 CARD_SIM=1 CARD_FAKE_CAMERA=1 \
  CARD_TMP_DIR=/tmp/durtest CARD_AUTO_RESEND_INTERVAL=99999 \
  CARD_RUN_POLL_INTERVAL=99999 CARD_UPLOAD_MAX_RETRIES=2 CARD_UPLOAD_RETRY_DELAY=0 \
  ../.venv/bin/python test_durability_virtual.py
"""
import os, time, glob, sys
import controller as C
from camera import TMP_DIR
import errors

_fails = []
def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name);
    if not cond: _fails.append(name)

def mkfile(name, data=b"x" * 32):
    p = os.path.join(TMP_DIR, name)
    with open(p, "wb") as f: f.write(data)
    return p

def clear_tmp():
    for f in glob.glob(os.path.join(TMP_DIR, "*")):
        try: os.remove(f)
        except Exception: pass

def wait_state(c, want, t=6.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if c._state == want: return True
        time.sleep(0.05)
    return c._state == want

os.makedirs(TMP_DIR, exist_ok=True); clear_tmp()
c = C.Controller()
time.sleep(0.5)   # let init settle

print("[1] BOOT recovery (_scan_pending): .part(interrupted) discarded, .mp4(saved) -> SYS-04 pending")
clear_tmp()
mkfile("aaaa-part-run.mp4.part")      # power cut MID-recording: only a .part exists
saved = mkfile("bbbb-done-run.mp4")   # fully-saved batch waiting to send
c._pending_upload = None; c._error = None
c._scan_pending()
check("stray .part deleted (interrupted record = lost, re-run)", not os.path.exists(os.path.join(TMP_DIR,"aaaa-part-run.mp4.part")))
check("complete .mp4 kept (saved on Pi5, NOT lost)", os.path.exists(saved))
check("-> pending_upload set to the saved run", c._pending_upload and c._pending_upload[0] == "bbbb-done-run")
check("-> error SYS-04 (Upload failed / resend), Start blocked", c._error and c._error.get("code") == "SYS-04" and c._can_start is False)

print("[2] _reconcile_pending: server truth by run_id")
c._client.get_run_status = lambda rid: {"status": "processing"}
check("server says processing -> True (already has it)", c._reconcile_pending("r") is True)
c._client.get_run_status = lambda rid: {"status": "recording"}
check("server says recording -> False (not uploaded yet)", c._reconcile_pending("r") is False)
c._client.get_run_status = lambda rid: (_ for _ in ()).throw(Exception("net down"))
check("network error -> False (safe: re-upload, never wrongly discard)", c._reconcile_pending("r") is False)

print("[3] Resend when server ALREADY has it (power cut after commit, before delete) -> no re-upload")
clear_tmp(); p = mkfile("cccc.mp4")
c._pending_upload = ("cccc", p); c._state = "failed"; c._error = errors.err("UPL-04")
c._client.get_run_status = lambda rid: {"status": "done"}
def _boom(*a, **k): raise AssertionError("must NOT re-upload when server already has it")
c._client.upload_direct = _boom
c.retry(); wait_state(c, "done")
check("local file discarded", not os.path.exists(p))
check("state=done, no re-upload, pending cleared", c._state == "done" and c._pending_upload is None)

print("[4] Resend when server does NOT have it (normal deferred upload) -> uploads, done")
clear_tmp(); p = mkfile("dddd.mp4")
c._pending_upload = ("dddd", p); c._state = "failed"; c._error = errors.err("UPL-04")
c._client.get_run_status = lambda rid: {"status": "recording"}
c._client.upload_direct = lambda rid, path: {"ok": True}
c.retry(); wait_state(c, "done")
check("uploaded then file deleted", not os.path.exists(p))
check("state=done, pending cleared", c._state == "done" and c._pending_upload is None)

print("[5] Upload while SERVER DOWN / WIFI LOST -> 'retry': file KEPT, stays pending")
clear_tmp(); p = mkfile("eeee.mp4")
c._client.get_run_status = lambda rid: {"status": "recording"}
c._client.upload_direct = lambda rid, path: (_ for _ in ()).throw(Exception("connection refused"))
res = c._upload_with_retry("eeee", p)
check("upload result = 'retry' (transient)", res == "retry")
check("video file STILL on Pi5 (not lost)", os.path.exists(p))

print("[6] 4xx classification [M6]: only run_not_found -> gone; other 4xx KEEP video")
import requests as _rq
def _mk(status, reason):
    class _Resp:
        status_code = status
        def json(self): return {"detail": {"reason": reason}} if reason else {}
    def _raise(*a, **k):
        e = _rq.exceptions.HTTPError(str(status)); e.response = _Resp(); raise e
    return _raise
c._client.upload_direct = _mk(404, "run_not_found")
res = c._upload_with_retry("eeee", p)
check("run_not_found 404 -> gone", res == "gone")
c._client.upload_direct = _mk(403, "device_locked")
c._client.upload_own = _mk(403, "device_locked")
res = c._upload_with_retry("eeee", p)
check("device_locked 403 -> retry (NOT gone)", res == "retry")
check("video KEPT after device_locked", os.path.exists(p))
c._client.upload_direct = _mk(413, "")
c._client.upload_own = _mk(413, "")
res = c._upload_with_retry("eeee", p)
check("bare 413 -> retry (NOT gone)", res == "retry")
check("video KEPT after 413", os.path.exists(p))
def _own_ok(*a, **k): return {"ok": True}
c._client.upload_direct = _mk(403, "invalid_session")
c._client.upload_own = _own_ok
res = c._upload_with_retry("eeee", p)
check("invalid_session -> upload_own -> ok [C5]", res == "ok")

clear_tmp()
print()
if _fails: print("RESULT: FAIL (%d) -> %s" % (len(_fails), ", ".join(_fails))); sys.exit(1)
print("RESULT: ALL PASS")

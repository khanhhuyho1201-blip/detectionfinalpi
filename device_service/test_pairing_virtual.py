"""
Virtual test of the QR-pairing state machine (device side) — the flow the kiosk
uses to re-enroll after an admin deletes the device:
  begin_pairing() -> shows CMDPAIR:<code> -> announce -> poll -> 'claimed'
  -> _apply_pair_creds() saves creds -> enrolled again.
Mocks the pairing server (no network); creds are written to a TEMP dir, never the
real ~/workspace/card_device.

Run on Pi (UN-enrolled state so begin_pairing works):
  CARD_SERIAL_PORT=sim CARD_FAKE_CAMERA=ok CARD_FAKE_RECORDER=1 \
  CARD_DEVICE_DIR=/tmp/pairtest CARD_CRED_FILE=/tmp/pairtest/credentials.json \
  ../.venv/bin/python test_pairing_virtual.py
"""
import os, sys, time, glob, shutil

TMP = os.environ["CARD_DEVICE_DIR"]
shutil.rmtree(TMP, ignore_errors=True); os.makedirs(TMP, exist_ok=True)

import controller as C

_fails = []
def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond: _fails.append(name)

c = C.Controller()
time.sleep(0.4)

# scripted pairing server: announce -> {}, then poll returns pending then claimed
_st = {"polls": 0}
def fake_http(method, url, payload=None):
    if url.endswith("/announce"):
        return {}
    _st["polls"] += 1
    if _st["polls"] < 2:
        return {"status": "pending"}
    return {"status": "claimed", "server_url": "https://paired.example",
            "device_id": "dev-new-001", "device_key": "key-new-001"}
c._http_json = fake_http   # instance attr shadows the staticmethod

print("[1] start un-enrolled (no creds) -> begin_pairing gives a QR code")
check("device starts UN-enrolled", bool(c._client) is False)
r = c.begin_pairing()
check("begin_pairing ok + returns a code", r.get("ok") and r.get("code", "").startswith("P"))
check("pair status pending", c._pair and c._pair.get("status") == "pending")
check("QR payload would be CMDPAIR:<code>", ("CMDPAIR:" + c._pair["code"]).startswith("CMDPAIR:P"))
code1 = c._pair["code"]

print("[2] begin_pairing is idempotent while pending (same code)")
r2 = c.begin_pairing()
check("same code returned (no churn)", r2.get("code") == code1)

print("[3] admin claims -> device saves creds -> becomes ENROLLED")
ok = False
for _ in range(60):            # up to ~6s for the poll loop (2s cadence) to claim
    if c._client is not None:
        ok = True; break
    time.sleep(0.1)
check("device is now enrolled (client built)", ok and bool(c._client))
check("pair status -> done", c._pair and c._pair.get("status") == "done")
creds = glob.glob(os.path.join(TMP, "credentials.json"))
check("credentials.json written to TEMP dir", len(creds) == 1)
if creds:
    import json
    d = json.load(open(creds[0]))
    check("creds hold the claimed device_id/server", d.get("device_id") == "dev-new-001"
          and d.get("server_url") == "https://paired.example")

print("[4] begin_pairing REFUSES once enrolled (never overwrites a live device)")
r3 = c.begin_pairing()
check("refused with enrolled=True", r3.get("ok") is False and r3.get("enrolled") is True)

print("[5] pairing_status reflects enrolled + done")
ps = c.pairing_status()
check("pairing_status enrolled True", ps.get("enrolled") is True)

shutil.rmtree(TMP, ignore_errors=True)
print()
if _fails: print("RESULT: FAIL (%d) -> %s" % (len(_fails), ", ".join(_fails))); sys.exit(1)
print("RESULT: ALL PASS")

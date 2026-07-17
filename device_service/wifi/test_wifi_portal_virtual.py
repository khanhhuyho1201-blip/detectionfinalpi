"""
Virtual test of the captive-portal logic (wifi_portal.py) WITHOUT touching the
real radio: nmcli + subprocess + the AP script are all mocked. Verifies scan
parsing, the connect happy-path (state ok + kiosk notified), wrong-password
(AP brought back up so the user can retry), and the "another phone" busy lock.

Run on Pi (from the wifi/ dir): ../../.venv/bin/python test_wifi_portal_virtual.py
"""
import sys, time, types, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wifi_portal as W

_fails = []
def check(n, c):
    print(("  ok  " if c else " FAIL ") + n)
    if not c: _fails.append(n)

class Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc; self.stdout = out; self.stderr = err

AP_NAME = "CMD - BBSW"
SCEN = {"connect_rc": 0, "connect_err": ""}
ap_up_calls = {"n": 0}
notify_calls = {"n": 0}

def fake_nmcli(*args, timeout=20):
    a = list(args)
    if a[:3] == ["dev", "wifi", "connect"]:
        return Done(SCEN["connect_rc"], "", SCEN["connect_err"])
    if a[:5] == ["-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi"] or a[:2] == ["-t", "-f"] and "SSID,SIGNAL,SECURITY" in a:
        return Done(0, "HomeWiFi:80:WPA2\nCMD - BBSW:90:WPA2\nOffice:45:WPA1\nHomeWiFi:60:WPA2\n")
    if "con" in a and "show" in a:
        return Done(0, "")
    return Done(0, "")

def fake_subprocess_run(cmd, **kw):
    # scan()'s direct 'con show --active' -> no AP active; AP script 'up' -> spy
    if isinstance(cmd, list) and "up" in cmd and any("wifi_ap" in str(c) for c in cmd):
        ap_up_calls["n"] += 1
        return Done(0, "AP up")
    if isinstance(cmd, list) and "--active" in cmd:
        return Done(0, "")           # no AP active during scan
    return Done(0, "")

W.nmcli = fake_nmcli
W.subprocess.run = fake_subprocess_run
W._get_ap_name = lambda: AP_NAME
W._notify_kiosk_connected = lambda: notify_calls.__setitem__("n", notify_calls["n"] + 1)
W.MANUAL_LOCK_FILE = "/tmp/test_manual.lock"

client = W.app.test_client()

def wait_state(want, t=6):
    end = time.time() + t
    while time.time() < end:
        with W._conn_lock:
            if W._conn["state"] == want: return True
        time.sleep(0.1)
    return W._conn["state"] == want

print("[1] scan: lists networks, EXCLUDES our AP SSID, dedupes, sorts by signal")
r = client.get("/api/wifi/scan").get_json()
ssids = [n["ssid"] for n in r["networks"]]
check("AP SSID excluded", AP_NAME not in ssids)
check("home + office present, deduped", ssids == ["HomeWiFi", "Office"])
check("sorted by signal desc", r["networks"][0]["signal"] >= r["networks"][-1]["signal"])
check("secure flag parsed", r["networks"][0]["secure"] is True)

print("[2] connect HAPPY path: state -> ok, kiosk notified, no AP re-up")
SCEN["connect_rc"] = 0; SCEN["connect_err"] = ""
ap_up_calls["n"] = 0; notify_calls["n"] = 0
resp = client.post("/api/wifi/connect", json={"ssid": "HomeWiFi", "password": "secret123"}).get_json()
check("connect accepted (pending + connect_id)", resp.get("ok") and resp.get("connect_id"))
check("reaches state=ok", wait_state("ok"))
check("kiosk notified to drop QR", notify_calls["n"] == 1)
check("AP NOT brought back up on success", ap_up_calls["n"] == 0)

print("[3] connect WRONG PASSWORD: AP brought back up, state=error")
with W._conn_lock:
    W._conn["state"] = "idle"; W._conn["error"] = None
SCEN["connect_rc"] = 4; SCEN["connect_err"] = "Error: Secrets were required, but not provided (psk)."
ap_up_calls["n"] = 0
client.post("/api/wifi/connect", json={"ssid": "HomeWiFi", "password": "wrong"})
check("reaches state=error", wait_state("error"))
with W._conn_lock:
    check("error = wrong_password", W._conn["error"] == "wrong_password")
check("AP brought back up so user can retry", ap_up_calls["n"] >= 1)

print("[4] BUSY lock: a second phone while connecting is refused")
with W._conn_lock:
    W._conn["state"] = "connecting"
r2 = client.post("/api/wifi/connect", json={"ssid": "X", "password": "y"}).get_json()
check("second phone gets busy", r2.get("error") == "busy")
with W._conn_lock:
    W._conn["state"] = "idle"

print("[5] connect refuses empty SSID")
r3 = client.post("/api/wifi/connect", json={"ssid": "", "password": "x"}).get_json()
check("empty ssid rejected", r3.get("ok") is False)

print()
if _fails: print("RESULT: FAIL (%d) -> %s" % (len(_fails), ", ".join(_fails))); sys.exit(1)
print("RESULT: ALL PASS")

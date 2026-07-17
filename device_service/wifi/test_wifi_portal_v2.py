"""
test_wifi_portal_v2.py — virtual test bổ sung cho wifi_portal.py (mock nmcli,
không đụng radio): capport RFC8908, probe 302, đã xoá "Closing setup window",
và setup LẶP LẠI nhiều vòng (ok -> idle -> connect lại) không giới hạn.

Chạy trên Pi (từ thư mục wifi/): ../../.venv/bin/python test_wifi_portal_v2.py
"""
import sys, time, os
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
    if "SSID,SIGNAL,SECURITY" in ",".join(a):
        return Done(0, "HomeWiFi:80:WPA2\nCMD - BBSW:90:WPA2\nOffice:45:WPA1\n")
    return Done(0, "")

def fake_subprocess_run(cmd, **kw):
    if isinstance(cmd, list) and "up" in cmd and any("wifi_ap" in str(c) for c in cmd):
        ap_up_calls["n"] += 1
        return Done(0, "AP up")
    if isinstance(cmd, list) and "--active" in cmd:
        return Done(0, "")
    return Done(0, "")

W.nmcli = fake_nmcli
W.subprocess.run = fake_subprocess_run
W._get_ap_name = lambda: AP_NAME
W._notify_kiosk_connected = lambda: notify_calls.__setitem__("n", notify_calls["n"] + 1)
W.MANUAL_LOCK_FILE = "/tmp/test_manual_v2.lock"

client = W.app.test_client()

def wait_state(want, t=8):
    end = time.time() + t
    while time.time() < end:
        with W._conn_lock:
            if W._conn["state"] == want: return True
        time.sleep(0.1)
    return W._conn["state"] == want

print("[1] error-hold: GET / KHONG xoa error moi (tab captive moi khong pha feedback tab goc)")
with W._conn_lock:
    W._conn["state"] = "error"; W._conn["error"] = "wrong_password"
    W._conn["id"] = "99"; W._conn["t"] = time.time()
client.get("/")
with W._conn_lock:
    check("error MOI van con sau GET /", W._conn["state"] == "error" and W._conn["error"] == "wrong_password")
    check("id giu nguyen cho tab goc doc", W._conn["id"] == "99")
r = client.get("/api/wifi/status").get_json()
check("status van tra error cho tab goc", r.get("state") == "error")
with W._conn_lock:
    W._conn["t"] = time.time() - 61   # gia lap error CU > 60s
client.get("/")
with W._conn_lock:
    check("error CU (>60s) bi don ve idle", W._conn["state"] == "idle" and W._conn["error"] is None)
with W._conn_lock:
    W._conn["state"] = "ok"
client.get("/")
with W._conn_lock:
    check("'ok' van reset NGAY nhu cu", W._conn["state"] == "idle")

print("[2] captive probes -> 302 ve portal (popup trigger)")
for path in ("/generate_204", "/gen_204", "/hotspot-detect.html", "/ncsi.txt",
             "/connecttest.txt", "/redirect", "/generate204", "/miui/detectportal.php"):
    r = client.get(path)
    check("%s -> 302 portal" % path,
          r.status_code == 302 and r.headers.get("Location") == "http://10.42.0.1/")
r = client.get("/duong-dan-la-hoac-probe-moi")
check("404 catch-all (khong /api) -> 302 portal",
      r.status_code == 302 and r.headers.get("Location") == "http://10.42.0.1/")
r = client.get("/api/khong-ton-tai")
check("/api/* 404 tra JSON, KHONG redirect", r.status_code == 404 and (r.get_json() or {}).get("error") == "not_found")

print("[3] trang portal: da XOA 'Closing setup window', ten AP dung")
with W._conn_lock:
    W._conn["state"] = "idle"; W._conn["error"] = None; W._conn["id"] = None
page = client.get("/").get_data(as_text=True)
check("khong con 'Closing setup window' trong HTML tinh (s7msg)", 'id="s7msg"' not in page)
check("AP name = CMD - BBSW", 'var AP_NAME="CMD - BBSW"' in page)
check("man S7 Connected! van con", "Connected!" in page)

print("[4] setup LAP LAI nhieu vong: ok -> GET / (phien moi) -> connect lai OK (x3)")
ok_rounds = 0
for i in range(3):
    client.get("/")   # phone mo portal -> reset ok/error -> idle
    with W._conn_lock:
        st = W._conn["state"]
    if st != "idle":
        break
    SCEN["connect_rc"] = 0; SCEN["connect_err"] = ""
    resp = client.post("/api/wifi/connect", json={"ssid": "HomeWiFi", "password": "pw%d" % i}).get_json()
    if not (resp.get("ok") and wait_state("ok")):
        break
    ok_rounds += 1
check("3/3 vong setup lien tiep deu OK (khong bi khoa 1 lan)", ok_rounds == 3)
check("kiosk duoc notify du 3 lan", notify_calls["n"] == 3)

print("[5] sai pass giua chung -> error -> AP restore -> thu lai van OK")
client.get("/")
SCEN["connect_rc"] = 4; SCEN["connect_err"] = "Error: Secrets were required, but not provided (psk)."
ap_up_calls["n"] = 0
client.post("/api/wifi/connect", json={"ssid": "HomeWiFi", "password": "sai"})
check("state=error (wrong_password)", wait_state("error") and W._conn["error"] == "wrong_password")
check("AP duoc bat lai de retry", ap_up_calls["n"] >= 1)
client.get("/")   # phone quay lai portal
SCEN["connect_rc"] = 0; SCEN["connect_err"] = ""
resp = client.post("/api/wifi/connect", json={"ssid": "HomeWiFi", "password": "dung"}).get_json()
check("retry sau sai pass -> OK", resp.get("ok") and wait_state("ok"))

print()
if _fails:
    print("RESULT: FAIL (%d) -> %s" % (len(_fails), ", ".join(_fails))); sys.exit(1)
print("RESULT: ALL PASS")

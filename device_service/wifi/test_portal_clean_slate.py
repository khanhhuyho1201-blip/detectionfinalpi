"""
test_portal_clean_slate.py — test QUEN mang cu khi setup mang moi (may di dong).
Mock nmcli day du: theo doi con delete / con modify. Kiem tra:
 [1] setup mang moi OK -> XOA cac profile wifi cu KHAC (dia diem cu)
 [2] KHONG xoa AP (CardFeederAP) va KHONG xoa chinh mang vua noi
 [3] profile trung SSID (khac NAME) KHONG bi xoa
 [4] autoconnect-priority=100 dat cho mang moi
 [5] FORGET_OLD=0 -> GIU tat ca (khong xoa gi)
Chay tren Pi: ../../.venv/bin/python test_portal_clean_slate.py
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

deleted = []
modified = []
SCAN_VISIBLE = {"v": ""}   # scan rong -> mang cu coi nhu ngoai tam
# profile dang luu: NAME -> ssid that
PROFILES = {
    "HomeNew": "HomeNew",
    "CardFeederAP": "CMD - BBSW",
    "OldCafe": "OldCafe",
    "itt": "itt",
    "HomeNew-2": "HomeNew",   # profile trung SSID HomeNew (khac NAME)
}

def fake_nmcli(*args, timeout=20):
    a = list(args)
    if a[:3] == ["dev", "wifi", "connect"]:
        return Done(0, "", "")   # connect OK
    if a[:1] == ["-t"] and "NAME,TYPE" in a and "show" in a:
        lines = []
        for name in PROFILES:
            lines.append("%s:802-11-wireless" % name)
        lines.append("Wired:802-3-ethernet")
        return Done(0, "\n".join(lines) + "\n")
    if "802-11-wireless.ssid" in a and "show" in a:
        name = a[-1]
        return Done(0, "802-11-wireless.ssid:%s\n" % PROFILES.get(name, ""))
    if a[:2] == ["con", "delete"]:
        deleted.append(a[2]); return Done(0)
    if a[:2] == ["con", "modify"]:
        modified.append(tuple(a[2:])); return Done(0)
    if "SSID,SIGNAL,SECURITY" in ",".join(a):
        return Done(0, "HomeNew:80:WPA2\n")
    if a[:2] == ["-t", "-f"] and "SSID" in a and "wifi" in a and "list" in a:
        return Done(0, SCAN_VISIBLE["v"])
    return Done(0, "")

W.nmcli = fake_nmcli
W._get_ap_name = lambda: "CMD - BBSW"
W._notify_kiosk_connected = lambda: None
W.MANUAL_LOCK_FILE = "/tmp/test_clean_slate.lock"
W.subprocess.run = lambda *a, **k: Done(0)   # AP script / nft calls
import os as _os
_os.sync = lambda: None   # khong sync that trong test

client = W.app.test_client()

def wait_state(want, t=8):
    end = time.time() + t
    while time.time() < end:
        with W._conn_lock:
            if W._conn["state"] == want: return True
        time.sleep(0.1)
    return W._conn["state"] == want

print("[A] FORGET_OLD=1 (mac dinh): setup 'HomeNew' -> quen mang cu")
W.FORGET_OLD_ON_SETUP = True
deleted.clear(); modified.clear()
with W._conn_lock:
    W._conn["state"] = "idle"; W._conn["error"] = None; W._conn["id"] = None
client.get("/")
resp = client.post("/api/wifi/connect", json={"ssid": "HomeNew", "password": "pw12345"}).get_json()
check("connect accepted", resp.get("ok"))
check("reaches ok", wait_state("ok"))
time.sleep(0.5)
check("[1] xoa OldCafe (dia diem cu)", "OldCafe" in deleted)
check("[1] xoa itt (dia diem cu)", "itt" in deleted)
check("[2] KHONG xoa CardFeederAP (AP)", "CardFeederAP" not in deleted)
check("[2] KHONG xoa HomeNew (mang vua noi)", "HomeNew" not in deleted)
check("[3] KHONG xoa HomeNew-2 (trung SSID HomeNew)", "HomeNew-2" not in deleted)
check("[4] priority=100 cho HomeNew", any("HomeNew" in m and "100" in m for m in modified))

print("[C] mang cu CON TRONG TAM (dual-band cung cho) -> KHONG xoa, chi ha priority")
W.FORGET_OLD_ON_SETUP = True
deleted.clear(); modified.clear()
SCAN_VISIBLE["v"] = "OldCafe\nHomeNew\n"   # OldCafe van thay; itt thi khong
with W._conn_lock:
    W._conn["state"] = "idle"; W._conn["error"] = None; W._conn["id"] = None
client.get("/")
client.post("/api/wifi/connect", json={"ssid": "HomeNew", "password": "pw12345"})
wait_state("ok"); time.sleep(0.5)
check("OldCafe (con thay) KHONG bi xoa", "OldCafe" not in deleted)
check("OldCafe bi ha priority=0", any("OldCafe" in m and "0" in m for m in modified))
check("itt (ngoai tam) van bi xoa", "itt" in deleted)
SCAN_VISIBLE["v"] = ""

print("[B] FORGET_OLD=0: GIU tat ca")
W.FORGET_OLD_ON_SETUP = False
deleted.clear()
with W._conn_lock:
    W._conn["state"] = "idle"; W._conn["error"] = None; W._conn["id"] = None
client.get("/")
client.post("/api/wifi/connect", json={"ssid": "HomeNew", "password": "pw12345"})
wait_state("ok"); time.sleep(0.4)
check("[5] KHONG xoa gi khi FORGET_OLD=0", deleted == [])

print()
if _fails: print("RESULT: FAIL (%d) -> %s" % (len(_fails), ", ".join(_fails))); sys.exit(1)
print("RESULT: ALL PASS")

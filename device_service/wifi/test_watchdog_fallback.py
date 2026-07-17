"""
test_watchdog_fallback.py — test helper AP-fallback (mock run()=nmcli).
[1] _nm_unescape go '\:' va '\\' thong nhat
[2] saved_wifi_ssids doc SSID that + cache
[3] any_saved_ssid_visible: thay->True, khong thay->False, colon-SSID khop dung (review#4)
[4] khong saved wifi -> False
[5] SAVED_GRACE=180, SAVED_HARD_CEILING=360
Chay tren Pi: ../../.venv/bin/python test_watchdog_fallback.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wifi_watchdog as W

_fails = []
def check(n, c):
    print(("  ok  " if c else " FAIL ") + n)
    if not c: _fails.append(n)

class Done:
    def __init__(self, out=""): self.returncode = 0; self.stdout = out; self.stderr = ""

SCEN = {
    "con_show": "Home:802-11-wireless\nCardFeederAP:802-11-wireless\nWired:802-3-ethernet\n",
    "ssid_of": {"Home": "Home", "CardFeederAP": "CMD - BBSW"},
    "scan": "Home\nOffice\n",
}
def fake_run(*args, timeout=20):
    a = list(args)
    if "--active" in a and "NAME,TYPE,DEVICE,STATE" in a:
        return Done(SCEN.get("active", ""))
    if a[:2] == ["nmcli", "-t"] and "NAME,TYPE" in a and "show" in a:
        return Done(SCEN["con_show"])
    if "802-11-wireless.ssid" in a and "show" in a:
        return Done("802-11-wireless.ssid:%s\n" % SCEN["ssid_of"].get(a[-1], ""))
    if "SSID" in a and "wifi" in a and "list" in a:
        return Done(SCEN["scan"])
    return Done("")
W.run = fake_run
def fresh():  # xoa cache giua cac scenario
    W._saved_cache["t"] = None

print("[1] _nm_unescape")
check("go \\: -> :", W._nm_unescape("a\\:b") == "a:b")
check("go \\\\ -> \\", W._nm_unescape("a\\\\b") == "a\\b")

print("[2] saved_wifi_ssids (AP loai ra, doc ssid that)")
fresh()
check("chi 'Home'", W.saved_wifi_ssids(force=True) == {"Home"})

print("[3] any_saved_ssid_visible")
fresh(); SCEN["scan"] = "Home\nOffice\n"
check("Home con thay -> True", W.any_saved_ssid_visible() is True)
fresh(); SCEN["scan"] = "Office\nNeighbor\n"
check("Home mat -> False", W.any_saved_ssid_visible() is False)

print("[3b] colon-SSID khop dung (review#4 bug)")
fresh()
SCEN["con_show"] = "MyNet:802-11-wireless\nCardFeederAP:802-11-wireless\n"
SCEN["ssid_of"] = {"MyNet": "cafe\\:5G", "CardFeederAP": "CMD - BBSW"}  # ssid that = "cafe:5G"
SCEN["scan"] = "cafe\\:5G\nOther\n"   # scan list cung escape ':'
check("SSID 'cafe:5G' khop giua saved & visible (khong lech)", W.any_saved_ssid_visible() is True)

print("[4] khong saved wifi -> False")
fresh(); SCEN["con_show"] = "CardFeederAP:802-11-wireless\nWired:802-3-ethernet\n"
check("False", W.any_saved_ssid_visible() is False)

print("[4b] on_home_wifi: CHI tinh STATE=activated (fix doi-pass MAJOR)")
SCEN["active"] = "Home:802-11-wireless:wlan0:activated\ntailscale0:tun:tailscale0:activated\n"
check("activated -> True", W.on_home_wifi() is True)
SCEN["active"] = "Home:802-11-wireless:wlan0:activating\ntailscale0:tun:tailscale0:activated\n"
check("activating (NM dang thu pass SAI) -> False (timer tiep tuc dem)", W.on_home_wifi() is False)
SCEN["active"] = "CardFeederAP:802-11-wireless:wlan0:activated\n"
check("AP cua ta activated -> False (khong phai wifi nha)", W.on_home_wifi() is False)

print("[5] hang so timer")
check("SAVED_GRACE=180", W.SAVED_GRACE == 180.0)
check("SAVED_HARD_CEILING=360 (2x, cho doi-pass)", W.SAVED_HARD_CEILING == 360.0)

print()
if _fails: print("RESULT: FAIL (%d) -> %s" % (len(_fails), ", ".join(_fails))); sys.exit(1)
print("RESULT: ALL PASS")

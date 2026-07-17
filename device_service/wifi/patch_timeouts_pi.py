"""Patch timeout 40/45 -> 150 cho 2 caller wifi_ap.sh trong device_service
(controller.py _start_wifi_ap, server.py /api/wifi/setup). Chay TREN Pi:
  python3 patch_timeouts_pi.py
Idempotent: chay lai lan 2 se bao 'da patch'.
"""
import os

BASE = os.path.expanduser("~/workspace/detectionfinalpi/device_service")

def patch(path, old, new, label):
    src = open(path, encoding="utf-8").read()
    if new in src:
        print("da patch:", label)
        return
    n = src.count(old)
    assert n == 1, "%s: tim thay %d lan (can dung 1)" % (label, n)
    open(path, "w", encoding="utf-8").write(src.replace(old, new))
    print("patch OK:", label)

patch(os.path.join(BASE, "controller.py"),
      "subprocess.run(cmd, capture_output=True, text=True, timeout=40)",
      "subprocess.run(cmd, capture_output=True, text=True, timeout=150)",
      "controller._start_wifi_ap timeout 150")

patch(os.path.join(BASE, "server.py"),
      "subprocess.run(cmd, capture_output=True, text=True, timeout=45)",
      "subprocess.run(cmd, capture_output=True, text=True, timeout=150)",
      "server /api/wifi/setup timeout 150")

import py_compile
py_compile.compile(os.path.join(BASE, "controller.py"), doraise=True)
py_compile.compile(os.path.join(BASE, "server.py"), doraise=True)
print("py_compile ca 2 file OK")

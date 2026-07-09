"""
printer_setup.py — discovery, pairing, and configuration for the Printer Setup UI.

Three discovery paths:
  USB    → scans /dev/usb/lp* and /dev/ttyUSB* (ESC/POS thermal)
  WiFi   → lpinfo network discovery (socket/LPD/IPP)
  BT     → bluetoothctl scan, then pair + RFCOMM bind

Config persisted to settings.paths.printer_cfg (card_device/printer.json).
The printer.py module reads this to choose the right backend at runtime.
"""

import glob
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time

from pathlib import Path

from settings import settings, atomic_write_text

logger = logging.getLogger("card_device.printer_setup")

PRINTER_CFG = str(settings.paths.printer_cfg)
RFCOMM_DEV  = "/dev/rfcomm0"


# ── Config I/O ───────────────────────────────────────────────────────────────

def load_cfg() -> dict | None:
    try:
        with open(PRINTER_CFG) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("printer config read error: %s", e)
        return None


def save_cfg(cfg: dict) -> None:
    # atomic: cúp điện lúc lưu vẫn không hỏng printer.json
    atomic_write_text(Path(PRINTER_CFG), json.dumps(cfg, indent=2))
    # reset singleton so next print_run_qr picks the new backend
    import printer as _p
    _p._printer = None


def remove_cfg() -> None:
    try:
        os.remove(PRINTER_CFG)
    except FileNotFoundError:
        pass
    import printer as _p
    _p._printer = None


# ── USB discovery ─────────────────────────────────────────────────────────────

def scan_usb() -> list[dict]:
    """Return list of USB printer devices found on this machine."""
    results = []
    for dev in sorted(glob.glob("/dev/usb/lp*") + glob.glob("/dev/ttyUSB*")):
        info = {"device": dev, "name": os.path.basename(dev), "type": "usb"}
        try:
            out = subprocess.run(
                ["udevadm", "info", dev], capture_output=True, text=True, timeout=3
            ).stdout
            model  = re.search(r"ID_MODEL=([^\n]+)",  out)
            vendor = re.search(r"ID_VENDOR=([^\n]+)", out)
            if model:
                info["name"] = model.group(1).replace("_", " ").strip()
            if vendor:
                info["vendor"] = vendor.group(1).replace("_", " ").strip()
        except Exception:
            pass
        results.append(info)
    return results


# ── Network discovery ─────────────────────────────────────────────────────────

# Nhớ entry đẹp nhất từng thấy cho mỗi máy (danh tính MAC/IP) trong tiến trình —
# giữ tên/queue hiển thị ổn định khi lpinfo/ippfind chập chờn (xem cuối scan_network).
_BEST_SEEN: dict[str, dict] = {}


def scan_network() -> list[dict]:
    """Discover network printers: lpinfo -v (CUPS, đủ giao thức) + ippfind
    (mDNS, nhanh) chạy SONG SONG rồi gộp theo URI. Trước đây lpinfo chạy đơn
    không giới hạn thời gian riêng → hay chết vì subprocess timeout 15s và
    NUỐT LỖI trả rỗng.

    Nguồn 3 (2026-07-03): ARP-probe — máy in WiFi NGỦ tiết kiệm điện thường
    im lặng với mDNS/SNMP (lpinfo + ippfind cùng mù) nhưng TCP đánh thức được
    → quét các IP hàng xóm (ip neigh) vào cổng in 9100/631/515. Nhờ vậy máy in
    đang ngủ vẫn hiện ra khi Scan network."""
    seen: set[str] = set()
    results: list[dict] = []

    def _add(uri: str, name_hint: str = ""):
        uri = uri.strip()
        if "://" not in uri or uri in seen:
            return
        seen.add(uri)
        m = re.match(r"\w+://([^/:]+)", uri)
        host = m.group(1) if m else uri
        scheme = uri.split("://")[0]
        name = name_hint or host
        if re.match(r"BRW[0-9A-Fa-f]+", host):
            name = f"Brother ({host})"
        results.append({"uri": uri, "host": host, "name": name, "protocol": scheme})

    procs = {}
    # lpinfo nằm /usr/sbin — KHÔNG có trong PATH của systemd user service
    # (phát hiện 2026-07-03: nhánh lpinfo chết im lặng, scan chỉ còn ippfind
    #  → máy in LPD/socket không quảng bá mDNS sẽ không bao giờ hiện ra)
    _lpinfo = "/usr/sbin/lpinfo" if os.path.exists("/usr/sbin/lpinfo") else "lpinfo"
    try:  # --timeout: giới hạn thời gian dò của CHÍNH lpinfo (không bị kill ngang)
        procs["lpinfo"] = subprocess.Popen(
            [_lpinfo, "--timeout", "8", "-v"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except Exception as e:
        logger.warning("lpinfo spawn: %s", e)
    try:  # mDNS trực tiếp — thấy máy in IPP trong ~2-4s
        procs["ippfind"] = subprocess.Popen(
            ["ippfind", "-T", "4", "--print"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except Exception as e:
        logger.debug("ippfind spawn: %s", e)

    outs = {}
    for key, p in procs.items():
        try:
            outs[key], _ = p.communicate(timeout=14)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                outs[key], _ = p.communicate(timeout=2)
            except Exception:
                outs[key] = ""
            logger.warning("%s scan quá 14s — dùng kết quả một phần", key)
        except Exception as e:
            outs[key] = ""
            logger.warning("%s scan: %s", key, e)

    for line in (outs.get("lpinfo") or "").splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2:
            _add(parts[1])
    for line in (outs.get("ippfind") or "").splitlines():
        _add(line)

    # nguồn 3: ARP-probe các IP hàng xóm vào cổng in (tìm máy in đang NGỦ).
    # Chỉ thêm host CHƯA có từ lpinfo/ippfind (2 nguồn kia cho URI/tên đẹp hơn).
    known_hosts = {r["host"] for r in results}
    for ip, port in _arp_probe_printers():
        if ip in known_hosts:
            continue
        scheme = {9100: "socket", 631: "ipp", 515: "lpd"}[port]
        name = _mdns_name(ip)
        _add(f"{scheme}://{ip}", name_hint=name or "")

    # ── Gộp entry CÙNG MỘT MÁY vật lý (vd Brother thấy 2 lần: lpd theo TÊN
    #    `BRW...` + socket theo IP `192.168.2.14`). Gộp CHỈ KHI danh tính thật
    #    (MAC, sau đó IP) TRÙNG → hai máy khác nhau (MAC khác) KHÔNG bị gộp,
    #    không phân giải được thì giữ riêng → KHÔNG mất máy in nào.
    #    Giữ entry "tốt" nhất mỗi máy: ưu tiên IPP>LPD>socket + có queue-path +
    #    có tên đẹp (không phải IP trần). ──
    def _score(r: dict):
        proto = {"ipps": 4, "ipp": 3, "lpd": 2, "socket": 1}.get(r["protocol"], 0)
        tail = r["uri"].split("://", 1)[-1]
        has_path = 1 if "/" in tail else 0
        named = 0 if re.match(r"^\d+\.\d+\.\d+\.\d+$", r["host"]) else 1
        return (proto + has_path, named)

    idcache: dict[str, str] = {}
    merged: dict[str, dict] = {}
    for r in results:
        h = r["host"]
        ident = idcache.get(h) or idcache.setdefault(h, _host_identity(h))
        cur = merged.get(ident)
        if cur is None or _score(r) > _score(cur):
            merged[ident] = r

    # Ổn định HIỂN THỊ: nhớ entry ĐẸP NHẤT từng thấy cho mỗi máy (theo danh tính)
    # trong tiến trình. Khi một lần scan chỉ thấy qua socket/IP (lpinfo/ippfind
    # chập chờn), vẫn hiện tên/queue đẹp (Brother …/LPD) đã biết → không nhảy
    # giữa "Brother (…)" và IP trần. CHỈ áp cho máy CÓ MẶT trong scan này →
    # không bịa ra máy đã rút.
    out = []
    for ident, r in merged.items():
        prev = _BEST_SEEN.get(ident)
        if prev is not None and _score(prev) > _score(r):
            r = {**r, "name": prev["name"], "uri": prev["uri"],
                 "host": prev["host"], "protocol": prev["protocol"]}
        _BEST_SEEN[ident] = r
        out.append(r)
    return out


_PRINT_PORTS = (9100, 631, 515)   # JetDirect / IPP / LPD


def _arp_probe_printers() -> list[tuple]:
    """Quét các IPv4 trong bảng neighbor (ip neigh) vào cổng in — TCP connect
    đánh thức cả máy in đang ngủ (mDNS/SNMP thì bị nó im lặng). Trả [(ip, port)]."""
    import concurrent.futures as _fut
    ips = []
    try:
        out = subprocess.run(["ip", "-4", "neigh"], capture_output=True,
                             text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = line.split()
            if parts and not line.endswith("FAILED"):
                ips.append(parts[0])
    except Exception as e:
        logger.debug("ip neigh: %s", e)
        return []
    ips = ips[:32]   # trần an toàn cho mạng đông thiết bị

    def probe(ip):
        for port in _PRINT_PORTS:
            try:
                with socket.create_connection((ip, port), timeout=0.8):
                    return (ip, port)
            except Exception:
                continue
        return None

    found = []
    try:
        with _fut.ThreadPoolExecutor(max_workers=16) as ex:
            for r in ex.map(probe, ips):
                if r:
                    found.append(r)
    except Exception as e:
        logger.debug("arp probe: %s", e)
    return found


def _mdns_name(ip: str) -> str:
    """Tên đẹp từ reverse-mDNS (avahi), vd 192.168.2.14 → BRW14AC604DD8C0.local
    → khớp regex Brother trong _add. Không có avahi/không tên → chuỗi rỗng."""
    try:
        r = subprocess.run(["avahi-resolve-address", ip],
                           capture_output=True, text=True, timeout=3)
        name = (r.stdout or "").strip().split()[-1] if r.returncode == 0 else ""
        return name.removesuffix(".local") if name and name != ip else ""
    except Exception:
        return ""


_IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


def _ip_to_mac(ip: str) -> str:
    """MAC (thường, cách nhau ':') của IPv4 từ bảng neighbor. Thử arp cache
    trước (ARP-probe lúc scan đã điền sẵn); chưa có thì ping 1 phát rồi thử lại.
    Rỗng nếu không tra được."""
    for attempt in range(2):
        try:
            out = subprocess.run(["ip", "neigh", "show", ip], capture_output=True,
                                 text=True, timeout=3).stdout
        except Exception:
            return ""
        m = re.search(r"lladdr ([0-9a-fA-F:]{17})", out)
        if m:
            return m.group(1).lower()
        if attempt == 0:   # chưa có trong cache → ping điền arp rồi thử lại
            try:
                subprocess.run(["ping", "-c1", "-W1", ip], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=2)
            except Exception:
                return ""
    return ""


def _resolve_to_ip(host: str) -> str:
    """Tên → IPv4 (avahi mDNS rồi DNS). IP thì trả nguyên. Rỗng nếu không phân giải."""
    if _IPV4_RE.match(host):
        return host
    names = [host] + ([host + ".local"] if not host.endswith(".local") else [])
    for nm in names:
        try:
            out = subprocess.run(["avahi-resolve-host-name", "-4", nm],
                                 capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", out)
            if m:
                return m.group(1)
        except Exception:
            pass
    try:
        return socket.gethostbyname(host)
    except Exception:
        return ""


def _host_identity(host: str) -> str:
    """Danh tính CANONICAL của một host để GỘP entry cùng máy vật lý.
    Ưu tiên MAC (chắc nhất) > IP > chính host. Không phân giải được → trả
    'host:<host>' (giữ riêng, KHÔNG gộp nhầm → không mất máy in nào).
    Brother đặt tên `BRW/BRN` + 12 hex = chính MAC → rút thẳng, khỏi cần mạng."""
    m = re.match(r"^BR[NW]([0-9A-Fa-f]{12})$", host)
    if m:
        h = m.group(1).lower()
        return "mac:" + ":".join(h[i:i + 2] for i in range(0, 12, 2))
    ip = _resolve_to_ip(host)
    if not ip:
        return "host:" + host.lower()
    mac = _ip_to_mac(ip)
    return ("mac:" + mac) if mac else ("ip:" + ip)


# ── Bluetooth discovery ───────────────────────────────────────────────────────

def scan_bt(timeout: int = 12) -> list[dict]:
    """Scan for Bluetooth devices (blocking, timeout seconds). Returns found list.

    Fix 2026-07-02: bản cũ CHỈ bắt dòng `[NEW] Device` — thiết bị bluetoothctl
    đã biết từ trước chỉ phát `[CHG] Device ... RSSI` nên bị BỎ SÓT hết → quét
    lần 2 trả rỗng dù máy ở ngay cạnh. Giờ: bật nguồn adapter, bắt cả [NEW] lẫn
    [CHG] (đánh dấu hiện diện), và sau cùng gộp tên từ cache `bluetoothctl devices`."""
    # LƯU Ý bluez mới: bluetoothctl KHÔNG vào interactive mode khi stdin là pipe
    # (cách cũ Popen+stdin.write không chạy gì cả → luôn rỗng). Dùng cú pháp
    # non-interactive `--timeout N scan on` rồi đọc `bluetoothctl devices`.
    try:
        subprocess.run(["bluetoothctl", "power", "on"],
                       capture_output=True, text=True, timeout=5)
    except Exception:
        pass

    scan_out = ""
    try:
        scan_out = subprocess.run(
            ["bluetoothctl", "--timeout", str(int(timeout)), "scan", "on"],
            capture_output=True, text=True, timeout=timeout + 8).stdout or ""
    except Exception as e:
        logger.warning("BT scan error: %s", e)

    # output có mã màu ANSI chèn giữa ([\x1b[0;92mNEW\x1b[0m]) — lọc trước khi parse
    clean = re.sub(r"\x1b\[[0-9;]*m", "", scan_out)
    present = {m.upper() for m in re.findall(r"Device ([0-9A-Fa-f:]{17})", clean)}

    cached: dict[str, str] = {}
    try:
        out = subprocess.run(["bluetoothctl", "devices"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            m = re.match(r"Device ([0-9A-Fa-f:]{17}) (.+)", line.strip())
            if m:
                cached[m.group(1).upper()] = m.group(2).strip()
    except Exception:
        pass

    if present:
        # chỉ hiện thiết bị THẤY trong cửa sổ quét này; tên đẹp lấy từ cache
        result = {mac: cached.get(mac, mac) for mac in present}
    else:
        # parse quirk / bluez cũ → fallback: toàn bộ danh sách devices
        result = cached

    # ── LỌC RÁC ──────────────────────────────────────────────────────────────
    # Hiện ĐẦY ĐỦ thiết bị Bluetooth CÓ TÊN (máy in, laptop, tai nghe...), chỉ
    # bỏ RÁC = BLE không tên (tên thực chất chính là địa chỉ MAC). Phần lớn nhiễu
    # trong môi trường thật là điện thoại/wearable/beacon BLE phát MAC ngẫu nhiên
    # không tên → bỏ đi cho danh sách gọn, không giấu bất kỳ thiết bị có tên nào.
    return [{"mac": mac, "name": name} for mac, name in result.items()
            if not _bt_name_is_mac(mac, name)]


def _bt_name_is_mac(mac: str, name: str) -> bool:
    """True nếu 'tên' thực chất chỉ là địa chỉ MAC (BLE không quảng bá tên) →
    rác, không mang thông tin."""
    n = (name or "").strip().upper().replace("-", ":")
    return (not n) or (n == mac.upper())


# ── Bluetooth pairing ─────────────────────────────────────────────────────────

def bt_pair(mac: str) -> dict:
    """Pair, trust, connect a BT device and bind RFCOMM. Returns {ok, error}.

    Mọi 'error' trả về là CÂU SẠCH cho người dùng (không bao giờ đổ chuỗi
    exception/command thô ra UI). Timeout được bắt riêng -> câu thân thiện.
    Đăng ký agent NoInputNoOutput trước khi pair: máy in BT (không phím/màn)
    dùng kiểu 'Just Works', thiếu agent thì bluetoothctl treo tới hết timeout."""
    def run(args, t=20):
        """Chạy bluetoothctl; trả CompletedProcess, hoặc None nếu QUÁ THỜI GIAN."""
        try:
            return subprocess.run(["bluetoothctl"] + args,
                                  capture_output=True, text=True, timeout=t)
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return False   # lỗi khác (không phải timeout)

    try:
        # Bật adapter + agent (im lặng nếu đã bật) — giúp pair 'Just Works' xong ngay,
        # không treo chờ xác nhận. KHÔNG để lộ popup hệ thống; chỉ chờ trong timeout.
        for pre in (["power", "on"], ["agent", "NoInputNoOutput"], ["default-agent"]):
            try:
                subprocess.run(["bluetoothctl"] + pre, capture_output=True, timeout=6)
            except Exception:
                pass

        r = run(["pair", mac], t=22)
        if r is None:
            return {"ok": False, "error": "Pairing timed out. Turn the printer on and put it "
                                          "in pairing mode, then try again."}
        if r is False:
            return {"ok": False, "error": "Bluetooth is unavailable right now. Please try again."}
        blob = (r.stdout + r.stderr).lower()
        paired = (r.returncode == 0) or ("already paired" in blob) or ("successful" in blob)
        if not paired:
            return {"ok": False, "error": "Could not pair with this device. Make sure it is a "
                                          "Bluetooth printer in pairing mode, then try again."}

        run(["trust", mac], t=8)

        r = run(["connect", mac], t=15)
        if r is None:
            return {"ok": False, "error": "The device paired but did not connect in time. "
                                          "Try again."}
        if r is False or (r.returncode != 0 and "successful" not in (r.stdout + r.stderr).lower()):
            return {"ok": False, "error": "Paired, but could not connect to the device."}

        # Bind RFCOMM (channel 1 is standard for BT printers)
        try:
            subprocess.run(["rfcomm", "bind", RFCOMM_DEV, mac, "1"],
                           capture_output=True, timeout=6)
        except Exception:
            pass
        return {"ok": True}
    except Exception:
        # Không bao giờ đổ chuỗi lỗi thô ra UI
        return {"ok": False, "error": "Bluetooth error. Please try again."}


# ── Hostname → IP resolution ──────────────────────────────────────────────────

def _resolve_uri_host(uri: str) -> str:
    """
    If the URI hostname doesn't resolve via DNS, try to find its IP via
    `ip neigh` (ARP table). Brother printers use BRW<MAC> hostnames that
    aren't in DNS but are always in the ARP cache after a scan.
    Returns the URI with hostname replaced by IP if needed, else original.
    """
    import socket as _socket
    m = re.match(r"(\w+://)([^/:]+)(.*)", uri)
    if not m:
        return uri
    scheme, host, rest = m.groups()
    # Already an IP address — nothing to do
    if re.match(r"\d+\.\d+\.\d+\.\d+", host):
        return uri
    # Try normal DNS first
    try:
        _socket.getaddrinfo(host, None)
        return uri
    except Exception:
        pass
    # Fall back: scan ip neigh for a matching MAC (BRW + 6-byte MAC hex)
    mac_m = re.match(r"BRW([0-9A-Fa-f]{12})$", host, re.I)
    if mac_m:
        raw = mac_m.group(1).upper()
        # Format as AA:BB:CC:DD:EE:FF
        mac = ":".join(raw[i:i+2] for i in range(0, 12, 2))
        try:
            out = subprocess.run(["ip", "neigh"], capture_output=True, text=True, timeout=3).stdout
            for line in out.splitlines():
                if mac.lower() in line.lower():
                    ip = line.split()[0]
                    if re.match(r"\d+\.\d+\.\d+\.\d+", ip):
                        logger.info("Resolved %s → %s via ARP", host, ip)
                        return scheme + ip + rest
        except Exception as e:
            logger.debug("ARP lookup failed: %s", e)
    logger.warning("Cannot resolve hostname %s — using original URI", host)
    return uri


# ── CUPS printer registration ─────────────────────────────────────────────────

def cups_add(name: str, uri: str) -> dict:
    """Register a printer in CUPS via lpadmin. Returns {ok, cups_name, error}."""
    uri = _resolve_uri_host(uri)
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:32] or "Printer"
    try:
        subprocess.run(["lpadmin", "-x", safe], capture_output=True, timeout=5)
        r = subprocess.run(
            ["lpadmin", "-p", safe, "-E", "-v", uri, "-m", "raw"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or r.stdout).strip()}
        subprocess.run(["lpoptions", "-d", safe], capture_output=True, timeout=5)
        return {"ok": True, "cups_name": safe}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cups_remove(cups_name: str) -> None:
    try:
        subprocess.run(["lpadmin", "-x", cups_name], capture_output=True, timeout=5)
    except Exception:
        pass


# ── Add / remove printer (high-level) ────────────────────────────────────────

def add_printer(cfg: dict) -> dict:
    """
    Save printer config. For CUPS backends also registers via lpadmin.
    cfg fields:
      backend  : "escpos_net" | "escpos_file" | "escpos_bt" | "cups"
      name     : human-readable label
      address  : IP (escpos_net)
      port     : int, default 9100 (escpos_net)
      device   : /dev/path (escpos_file, escpos_bt)
      bt_mac   : MAC (escpos_bt)
      cups_uri : URI (cups)
    """
    backend = cfg.get("backend", "escpos_net")

    if backend == "cups":
        uri  = cfg.get("cups_uri", "")
        name = cfg.get("name", "Printer")
        if not uri:
            return {"ok": False, "error": "cups_uri is required"}
        r = cups_add(name, uri)
        if not r["ok"]:
            return r
        cfg["cups_name"] = r["cups_name"]

    save_cfg(cfg)
    return {"ok": True}


def remove_printer() -> None:
    cfg = load_cfg() or {}
    if cfg.get("backend") == "cups" and cfg.get("cups_name"):
        cups_remove(cfg["cups_name"])
    remove_cfg()


# ── Status ────────────────────────────────────────────────────────────────────

def get_status() -> dict:
    cfg = load_cfg()
    available = False
    if cfg:
        try:
            from printer import printer_available
            available = printer_available()
        except Exception:
            pass
    return {
        "configured": cfg is not None,
        "available":  available,
        "config":     cfg or {},
    }


# ── Test print ────────────────────────────────────────────────────────────────

_TEST_LINE = "Hồ Duy Trường đẹp trai, EM YÊU ANH"


def test_print() -> dict:
    try:
        from printer import print_text_line
        ok = print_text_line(_TEST_LINE)
        return {"ok": ok}
    except Exception as e:
        logger.exception("test print failed")
        return {"ok": False, "error": str(e)}

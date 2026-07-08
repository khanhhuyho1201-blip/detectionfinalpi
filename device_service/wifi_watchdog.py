"""
wifi_watchdog.py — tự bật AP cài đặt khi máy KHÔNG nối được WiFi nhà.

Logic:
  * Mỗi CHECK_EVERY giây, kiểm tra Pi có đang nối WiFi nhà (một connection wifi
    KHÁC AP của ta) hay không.
  * Nếu mất mạng nhà liên tục > GRACE giây -> bật AP "CardFeeder-XXXX" để người
    mua quét QR cài WiFi (wifi_portal phục vụ trang).
  * Khi đã nối lại WiFi nhà -> tắt AP.

AN TOÀN khi test từ xa: nếu AP đã bật > AP_TIMEOUT giây mà KHÔNG ai cấu hình
(vẫn chưa có WiFi nhà), watchdog tự HẠ AP và thử nối lại WiFi đã lưu, để máy/Pi
không bị kẹt ở AP mode và mất kết nối vĩnh viễn. Đặt AP_TIMEOUT=0 để tắt cơ chế
này (chế độ sản xuất thực thụ — AP giữ tới khi người mua cấu hình xong).
"""

import logging
import os
import subprocess
import time

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("wifi_watchdog")

from settings import settings

HERE = os.path.dirname(os.path.abspath(__file__))
AP_SCRIPT = os.path.join(HERE, "wifi_ap.sh")
IFACE = settings.wifi.iface
AP_CON = settings.wifi.ap_con
# Khi wifi_portal.py đang tự tay chuyển mạng (_do_connect), nó giữ file này —
# watchdog phải đứng yên, không tự ap_up()/ap_down() song song kẻo đá nhau
# (nguyên nhân gây "vào mạng vài giây rồi tự bật AP lại").
MANUAL_LOCK_FILE = settings.wifi.manual_lock

CHECK_EVERY = settings.wifi.check_every
GRACE = settings.wifi.grace
AP_TIMEOUT = settings.wifi.ap_timeout
BOOT_WAIT_SAVED = settings.wifi.boot_wait_saved
BOOT_WAIT_UNCONFIGURED = settings.wifi.boot_wait_unconfigured


def run(*args, timeout=20):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def ap_active() -> bool:
    r = run("nmcli", "-t", "-f", "NAME", "con", "show", "--active")
    return AP_CON in r.stdout.split()


def has_saved_wifi() -> bool:
    """True nếu NM có ít nhất một connection WiFi đã lưu (không kể AP của ta).
    Nếu có -> máy đã từng setup WiFi -> watchdog KHÔNG tự bật AP khi mất mạng
    (NM sẽ tự reconnect). AP chỉ bật khi user bấm Setup trong Settings."""
    r = run("nmcli", "-t", "-f", "NAME,TYPE", "con", "show")
    for line in r.stdout.splitlines():
        parts = line.replace("\\:", "\x00").split(":")
        parts = [p.replace("\x00", ":") for p in parts]
        if len(parts) >= 2 and parts[1] == "802-11-wireless" and parts[0] != AP_CON:
            return True
    return False


def has_ethernet() -> bool:
    """True nếu có ít nhất một ethernet interface đang connected (cắm dây LAN).
    Khi có LAN -> máy vẫn online -> KHÔNG bật AP dù không có WiFi saved."""
    r = run("nmcli", "-t", "-f", "TYPE,STATE", "device", "status")
    for line in r.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[0] == "ethernet" and parts[1] == "connected":
            return True
    return False


def on_home_wifi() -> bool:
    """True nếu đang nối một WiFi KHÁC AP của ta (tức WiFi nhà)."""
    r = run("nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "con", "show", "--active")
    for line in r.stdout.splitlines():
        parts = line.replace("\\:", "\x00").split(":")
        parts = [p.replace("\x00", ":") for p in parts]
        if len(parts) >= 3 and parts[1] == "802-11-wireless" and parts[2] == IFACE:
            if parts[0] != AP_CON:
                return True
    return False


def manual_lock_active() -> bool:
    """True nếu wifi_portal ĐANG tự tay chuyển mạng (giữ lock). CHỐNG LOCK MỒ CÔI:
    lock chứa PID; nếu tiến trình đó đã chết (portal crash/SIGKILL giữa chừng),
    lock là rác → xoá + coi như không khoá, để watchdog không tê liệt tới reboot."""
    try:
        with open(MANUAL_LOCK_FILE) as f:
            pid = int((f.read() or "0").strip() or 0)
    except FileNotFoundError:
        return False
    except Exception:
        return True   # đọc lỗi (đang ghi dở) → coi như đang khoá, an toàn
    if pid > 0:
        try:
            os.kill(pid, 0)   # PID còn sống?
            return True
        except ProcessLookupError:
            logger.warning("lock mồ côi (PID %d đã chết) -> xoá", pid)
            try:
                os.remove(MANUAL_LOCK_FILE)
            except Exception:
                pass
            return False
        except PermissionError:
            return True       # tồn tại nhưng khác user → coi như sống
    return True


def ap_up():
    logger.info("Bật AP cài đặt")
    run("bash", AP_SCRIPT, "up", timeout=40)


def ap_down():
    logger.info("Hạ AP, nối lại WiFi đã lưu")
    run("bash", AP_SCRIPT, "down", timeout=40)


def cleanup_stale_captive():
    """Gỡ luật captive (nft DNAT 80 → 10.42.0.1 + DNS hijack) còn sót lại khi
    Pi đã rời AP mode. Nếu không gỡ, mọi HTTP từ LAN vào Pi bị ném vào
    10.42.0.1 không tồn tại → timeout (đã dính 2026-07-02: NM tự nối lại
    WiFi nhà sau khi AP bật, không ai chạy wifi_ap.sh down)."""
    r = run("nft", "list", "table", "ip", "cardfeeder_captive", timeout=10)
    if r.returncode != 0:
        return False
    logger.warning("phát hiện luật captive còn sót khi đang ở WiFi nhà -> gỡ")
    run("nft", "delete", "table", "ip", "cardfeeder_captive", timeout=10)
    run("rm", "-f", "/etc/NetworkManager/dnsmasq-shared.d/card-captive.conf", timeout=10)
    return True


def startup_wait_seconds() -> float:
    if has_saved_wifi() or has_ethernet():
        return BOOT_WAIT_SAVED
    return BOOT_WAIT_UNCONFIGURED


def main():
    initial_wait = startup_wait_seconds()
    logger.info(
        "watchdog start: grace=%ss ap_timeout=%ss iface=%s startup_wait=%ss",
        GRACE,
        AP_TIMEOUT,
        IFACE,
        initial_wait,
    )
    time.sleep(initial_wait)

    offline_since = None
    ap_started_at = None
    captive_checked = False   # đã kiểm tra/gỡ luật captive sót cho lần nối WiFi nhà hiện tại

    while True:
        try:
            if manual_lock_active():
                offline_since = None
                ap_started_at = None
                captive_checked = False
                time.sleep(CHECK_EVERY)
                continue
            if ap_active():
                captive_checked = False
                if ap_started_at is None:
                    ap_started_at = time.monotonic()
                    if AP_TIMEOUT:
                        logger.info("phát hiện AP đang bật (nguồn khác) — đếm %ss để tự hạ", AP_TIMEOUT)
                    else:
                        logger.info("phát hiện AP đang bật (nguồn khác)")
                if (AP_TIMEOUT and (time.monotonic() - ap_started_at) > AP_TIMEOUT
                        and not manual_lock_active()):   # re-check: portal có thể vừa giành lock
                    logger.warning("AP quá %ss không ai cấu hình -> tự hạ để khôi phục", AP_TIMEOUT)
                    ap_down()
                    ap_started_at = None
                    offline_since = None
                    time.sleep(BOOT_WAIT_SAVED)
            elif on_home_wifi():
                offline_since = None
                if not captive_checked:
                    cleanup_stale_captive()
                    captive_checked = True
            else:
                if has_saved_wifi():
                    offline_since = None
                    logger.debug("mất WiFi nhưng có saved connection — chờ NM reconnect")
                elif has_ethernet():
                    offline_since = None
                    logger.debug("không có WiFi saved nhưng có LAN — không bật AP")
                else:
                    now = time.monotonic()
                    if offline_since is None:
                        offline_since = now
                        logger.info("không có saved WiFi — đếm %ss trước khi bật AP", GRACE)
                    elif now - offline_since >= GRACE and not manual_lock_active():
                        # re-check lock NGAY trước ap_up() — vá cửa sổ race: portal
                        # có thể vừa ghi lock sau lần check đầu vòng lặp (line ~155)
                        ap_up()
                        ap_started_at = time.monotonic()
        except Exception as e:
            logger.warning("watchdog loop error: %s", e)
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()

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

# [gom folder 2026-07] file ở device_service/wifi/ — thêm thư mục cha device_service/
#   vào sys.path để 'from settings import settings' chạy khi systemd gọi trực tiếp
#   .../wifi/wifi_watchdog.py (lúc đó sys.path[0] = wifi/, không có settings.py).
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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

# [FIX 2026-07-17] Fallback bat AP khi WiFi nha MAT HAN du da luu profile.
# Truoc: co saved wifi -> watchdog KHONG bao gio bat AP (cho NM reconnect vo han)
# -> doi router / doi pass / mang may di cho khac = WiFi cu mat han -> AP
# "CMD - BBSW" KHONG bao gio hien -> khong quet QR setup lai duoc. Gio: neu KHONG
# thay BAT KY SSID da luu nao trong scan suot SAVED_GRACE giay (mat han, khong
# phai blip) -> bat AP fallback. AP_TIMEOUT (600s) van tu ha + thu lai WiFi nha
# -> tu phuc hoi neu wifi ve. Dat CARD_WIFI_SAVED_GRACE=0 de TAT fallback nay.
SAVED_GRACE = float(os.environ.get("CARD_WIFI_SAVED_GRACE", "180"))
# [FIX review#1] Tran CUNG: doi PASS router (SSID VAN phat, van thay trong scan)
# thi any_saved_ssid_visible() luon True -> timer "SSID vang" khong bao gio dem ->
# AP khong bao gio bat. Nen: du SSID con thay, neu off home wifi lien tuc qua
# HARD_CEILING (NM khong auth duoc) -> van bat AP de setup lai (nhap pass moi).
SAVED_HARD_CEILING = SAVED_GRACE * 2


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


def _nm_unescape(v: str) -> str:
    """Go escape terse (-t) cua nmcli THONG NHAT: '\\:' -> ':', '\\\\' -> '\\'.
    [FIX review#4] Truoc day saved_wifi_ssids() KHONG unescape con any_saved_ssid_
    visible() thi co -> SSID chua ':' lech giua 2 set -> mismatch -> bat AP nham."""
    return v.replace("\\\\", "\x00").replace("\\:", ":").replace("\x00", "\\")


_saved_cache = {"t": None, "v": set()}   # cache SSID da luu (doi it) — [FIX review#7 perf]


def saved_wifi_ssids(force=False) -> set:
    """SSID that cua cac profile WiFi da luu (tru AP cua ta). Doc field
    802-11-wireless.ssid vi NAME co the khac SSID. Cache 30s: profile hiem khi
    doi, tranh spawn N+1 nmcli moi 2s."""
    now = time.monotonic()
    if not force and _saved_cache["t"] is not None and now - _saved_cache["t"] < 30:
        return _saved_cache["v"]
    r = run("nmcli", "-t", "-f", "NAME,TYPE", "con", "show")
    out = set()
    for line in r.stdout.splitlines():
        raw = line.replace("\\:", "\x00")
        parts = raw.split(":")
        if len(parts) >= 2 and parts[1] == "802-11-wireless":
            name = parts[0].replace("\x00", ":").replace("\\\\", "\\")
            if name == AP_CON:
                continue
            rs = run("nmcli", "-t", "-f", "802-11-wireless.ssid", "con", "show", name)
            ssid = _nm_unescape(rs.stdout.split(":", 1)[-1].strip()) if ":" in rs.stdout else ""
            out.add(ssid or name)
    _saved_cache["t"] = now
    _saved_cache["v"] = out
    return out


def any_saved_ssid_visible() -> bool:
    """True neu it nhat 1 SSID da luu dang HIEN trong scan (con trong tam song)."""
    names = saved_wifi_ssids()
    if not names:
        return False
    r = run("nmcli", "-t", "-f", "SSID", "dev", "wifi", "list", "--rescan", "no")
    visible = set()
    for line in r.stdout.splitlines():
        line = _nm_unescape(line.strip())
        if line:
            visible.add(line)
    return bool(names & visible)


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
    """True nếu ĐÃ NỐI XONG một WiFi KHÁC AP của ta (WiFi nhà, STATE=activated).
    [FIX review#1 MAJOR] Phải lọc STATE: `con show --active` liệt kê cả kết nối
    đang 'activating' (NM thử lại với pass SAI khi đổi mật khẩu router) — nếu tính
    luôn cái đó là "đã nối" thì off_home_since bị reset mỗi lần thử → trần cứng 360s
    (bật AP cho case đổi-pass) KHÔNG BAO GIỜ đạt. Chỉ 'activated' mới coi là on-home;
    'activating'/'deactivating' coi là off-home để timer tiếp tục đếm."""
    r = run("nmcli", "-t", "-f", "NAME,TYPE,DEVICE,STATE", "con", "show", "--active")
    for line in r.stdout.splitlines():
        parts = line.replace("\\:", "\x00").split(":")
        parts = [p.replace("\x00", ":") for p in parts]
        if (len(parts) >= 4 and parts[1] == "802-11-wireless" and parts[2] == IFACE
                and parts[3] == "activated" and parts[0] != AP_CON):
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
    # [FIX 2026-07-15] 40s < worst-case cua wifi_ap.sh (rescan 8s + 3 lan
    # 'nmcli --wait 25 con up' + backoff ~ 90s) -> kill giua chung lam mat luat
    # nft (popup cham) va giu lock. 150s bao het worst-case.
    run("bash", AP_SCRIPT, "up", timeout=150)


def ap_down():
    logger.info("Hạ AP, nối lại WiFi đã lưu")
    run("bash", AP_SCRIPT, "down", timeout=150)


def cleanup_stale_captive():
    """Gỡ luật captive (nft DNAT 80 → 10.42.0.1 + DNS hijack) còn sót lại khi
    Pi đã rời AP mode. Nếu không gỡ, mọi HTTP từ LAN vào Pi bị ném vào
    10.42.0.1 không tồn tại → timeout (đã dính 2026-07-02: NM tự nối lại
    WiFi nhà sau khi AP bật, không ai chạy wifi_ap.sh down)."""
    r = run("nft", "list", "table", "ip", "cardfeeder_captive", timeout=10)
    if r.returncode != 0:
        # [FIX 2026-07-15] van don conf DNS-hijack MO COI (crash giua ghi file va
        # cai nft -> file ton tai ma table khong co). Vo hai o station mode nhung
        # de lai la sai invariant "khong de gi sot lai".
        run("rm", "-f", "/etc/NetworkManager/dnsmasq-shared.d/card-captive.conf", timeout=10)
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
    saved_gone_since = None      # SSID đã lưu VẮNG khỏi scan (dời máy/tắt router)
    off_home_since = None        # off home wifi liên tục (kể cả SSID còn thấy — đổi pass)
    ap_started_at = None
    captive_checked = False   # đã kiểm tra/gỡ luật captive sót cho lần nối WiFi nhà hiện tại

    while True:
        try:
            if manual_lock_active():
                offline_since = None
                saved_gone_since = None
                off_home_since = None
                ap_started_at = None
                captive_checked = False
                time.sleep(CHECK_EVERY)
                continue
            if ap_active():
                captive_checked = False
                saved_gone_since = None
                off_home_since = None
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
                saved_gone_since = None
                off_home_since = None
                if not captive_checked:
                    cleanup_stale_captive()
                    captive_checked = True
            elif has_ethernet():
                # [FIX review#2] Online qua LAN -> KHONG BAO GIO bat AP, du co wifi
                # da luu bi mat song. Truoc day nhanh nay bi has_saved_wifi() che.
                offline_since = None
                saved_gone_since = None
                off_home_since = None
                logger.debug("mất WiFi nhưng có LAN — không bật AP")
            else:
                if has_saved_wifi():
                    offline_since = None
                    if SAVED_GRACE <= 0:
                        saved_gone_since = None
                        off_home_since = None
                    else:
                        now = time.monotonic()
                        if off_home_since is None:
                            off_home_since = now        # bat dau off home (tran cung)
                        # timer "SSID vang khoi scan" — reset khi CON thay
                        if any_saved_ssid_visible():
                            saved_gone_since = None
                        elif saved_gone_since is None:
                            saved_gone_since = now
                        # Bat AP fallback khi 1 trong 2:
                        #  (a) SSID vang khoi tam >= SAVED_GRACE  -> doi cho / tat router
                        #  (b) off home lien tuc >= HARD_CEILING  -> doi pass (SSID con
                        #      thay nhung NM khong bao gio auth duoc) [review#1]
                        gone = (saved_gone_since is not None
                                and now - saved_gone_since >= SAVED_GRACE)
                        stuck = now - off_home_since >= SAVED_HARD_CEILING
                        if (gone or stuck) and not manual_lock_active():
                            why = "SSID mất khỏi tầm sóng" if gone else "kẹt không auth được (đổi pass?)"
                            logger.warning("WiFi đã lưu %s — bật AP 'CMD - BBSW' để quét QR setup lại", why)
                            # [FIX review#7] reset timer + set ap_started_at TRUOC ap_up():
                            # neu ap_up() nem loi (timeout 150s) van co back-off, khong
                            # retry don moi vong.
                            ap_started_at = time.monotonic()
                            saved_gone_since = None
                            off_home_since = None
                            ap_up()
                else:
                    saved_gone_since = None
                    off_home_since = None
                    now = time.monotonic()
                    if offline_since is None:
                        offline_since = now
                        logger.info("không có saved WiFi — đếm %ds trước khi bật AP", int(GRACE))
                    elif now - offline_since >= GRACE and not manual_lock_active():
                        # re-check lock NGAY trước ap_up() — vá cửa sổ race.
                        # [FIX review#2] set ap_started_at + reset offline_since TRƯỚC
                        # ap_up(): nếu wifi_ap.sh fail (return non-zero, KHÔNG raise)
                        # thì có back-off thay vì gọi lại mỗi 2s.
                        ap_started_at = time.monotonic()
                        offline_since = None
                        ap_up()
        except Exception as e:
            logger.warning("watchdog loop error: %s", e)
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()

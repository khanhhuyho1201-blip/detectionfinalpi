#!/bin/bash
# wifi_ap.sh — bật/tắt WiFi access-point cho việc cài mạng ban đầu.
#
# Dùng NetworkManager (nmcli). KHÔNG cần hostapd/dnsmasq — NM tự cấp DHCP và đặt
# gateway 10.42.0.1 (trùng CARD_WIFI_PORTAL_URL trong QR).
#
#   wifi_ap.sh up      -> bật AP "CMD - BBSW" (đổi bằng env CARD_AP_SSID)
#   wifi_ap.sh down    -> tắt AP, để NM tự nối lại WiFi nhà đã lưu
#   wifi_ap.sh status  -> in trạng thái
#
# LƯU Ý: Pi chỉ có 1 card WiFi (wlan0). Bật AP sẽ NGẮT WiFi nhà hiện tại.
set -uo pipefail

# SERIALIZE up/down bằng flock: 2026-07-03 phát hiện race — "down" chạy trong
# khi "up" (thread nền của /api/wifi/setup) chưa xong → phần còn lại của up chạy
# SAU down → AP bật lại đè lên mạng vừa nối, máy kẹt AP mode. flock buộc lệnh
# sau đợi lệnh trước xong hẳn → thứ tự luôn tuần tự đúng.
exec 9>/run/card_wifi_ap.lock
# [FIX 2026-07-15] flock co timeout: nmcli ket (NM treo) tung giu lock VINH VIEN
# -> moi up/down sau deu block, caller timeout xep lop. 120s du cho up cham nhat.
flock -w 120 9 || { echo "LOI: khong lay duoc lock wifi_ap sau 120s -> thoat." >&2; exit 1; }

IFACE="${CARD_WIFI_IFACE:-wlan0}"
AP_CON="CardFeederAP"
CRED_FILE="${CARD_CRED_FILE:-/home/bbsw/.card_device/credentials.json}"
SUFFIX="$(python3 - "$CRED_FILE" <<'PY' 2>/dev/null || echo XXXX
import json,sys
try:
    d=json.load(open(sys.argv[1])); print(d.get("device_id","XXXX")[-4:].upper())
except Exception:
    print("XXXX")
PY
)"
AP_SSID="${CARD_AP_SSID:-CMD - BBSW}"
# [FIX MED 2026-07] GHIM mật khẩu AP = "cardfeeder" (1 nguồn duy nhất). QR trong
#   web/index.html hardcode "cardfeeder"; nếu ở đây cho ${CARD_AP_PASS:-...} rồi ai đó
#   set CARD_AP_PASS khác -> AP đổi pass mà QR vẫn "cardfeeder" -> điện thoại quét QR
#   tự nối FAIL (sai pass) mà không báo lỗi rõ. Cần đổi pass thì sửa CẢ 2 nơi.
AP_PASS="cardfeeder"
AP_ADDR="10.42.0.1/24"
AP_IP="10.42.0.1"
NFT_TABLE="cardfeeder_captive"
DNS_HIJACK_FILE="/etc/NetworkManager/dnsmasq-shared.d/card-captive.conf"

_dns_hijack_write() {
    cat > "$DNS_HIJACK_FILE" 2>/dev/null <<EOF
address=/#/$AP_IP
no-negcache
EOF
    chmod 0644 "$DNS_HIJACK_FILE" 2>/dev/null && echo "captive DNS hijack file OK" || echo "captive DNS hijack file lỗi"
}

captive_on() {
    nft delete table ip "$NFT_TABLE" 2>/dev/null
    nft add table ip "$NFT_TABLE" 2>/dev/null
    nft add chain ip "$NFT_TABLE" prerouting "{ type nat hook prerouting priority dstnat; }" 2>/dev/null
    nft add rule ip "$NFT_TABLE" prerouting iifname "$IFACE" tcp dport 80 ip daddr != "$AP_IP" dnat to "$AP_IP":80 2>/dev/null \
        && echo "captive redirect 80 bật" || echo "captive redirect 80 lỗi"
    # [FIX PERF 2026-07-15] Ép MỌI DNS về dnsmasq của AP: điện thoại đặt DNS cứng
    #   (8.8.8.8, DNS riêng của hãng) sẽ query thẳng ra ngoài — AP không có internet
    #   -> query chết timeout -> phone tưởng "có mạng nhưng chậm", KHÔNG bật popup.
    #   DNAT về 10.42.0.1:53 -> dnsmasq (address=/#/) trả lời tức thì -> probe chạy ngay.
    nft add rule ip "$NFT_TABLE" prerouting iifname "$IFACE" udp dport 53 ip daddr != "$AP_IP" dnat to "$AP_IP":53 2>/dev/null \
        && echo "captive DNS udp53 bật" || echo "captive DNS udp53 lỗi"
    nft add rule ip "$NFT_TABLE" prerouting iifname "$IFACE" tcp dport 53 ip daddr != "$AP_IP" dnat to "$AP_IP":53 2>/dev/null \
        && echo "captive DNS tcp53 bật" || echo "captive DNS tcp53 lỗi"
    # [FIX 2026-07-15] 'reject' KHÔNG hợp lệ trong chain type nat (nft từ chối lệnh,
    #   2>/dev/null nuốt mất -> rule 443/853 cũ chưa từng chạy). Thay bằng DNAT về
    #   cổng ĐÓNG trên Pi: kernel tự trả TCP RST / ICMP port-unreachable -> HTTPS,
    #   QUIC, DoT của phone fail-NGAY thay vì treo chờ timeout -> captive check
    #   kết luận nhanh -> popup bật nhanh.
    nft add rule ip "$NFT_TABLE" prerouting iifname "$IFACE" tcp dport 443 ip daddr != "$AP_IP" dnat to "$AP_IP":443 2>/dev/null \
        && echo "captive fastfail tcp443 bật" || echo "captive fastfail tcp443 lỗi"
    nft add rule ip "$NFT_TABLE" prerouting iifname "$IFACE" udp dport 443 ip daddr != "$AP_IP" dnat to "$AP_IP":443 2>/dev/null \
        && echo "captive fastfail udp443 (QUIC) bật" || echo "captive fastfail udp443 lỗi"
    nft add rule ip "$NFT_TABLE" prerouting iifname "$IFACE" tcp dport 853 ip daddr != "$AP_IP" dnat to "$AP_IP":853 2>/dev/null \
        && echo "captive fastfail tcp853 (DoT) bật" || echo "captive fastfail tcp853 lỗi"
}

captive_off() {
    rm -f "$DNS_HIJACK_FILE" 2>/dev/null && echo "captive DNS hijack tắt"
    nft delete table ip "$NFT_TABLE" 2>/dev/null && echo "captive redirect tắt"
}

_ap_profile_ok() {
    local existing
    existing=$(nmcli -t -f 802-11-wireless.ssid con show "$AP_CON" 2>/dev/null | sed 's/^[^:]*://')
    [ "$existing" = "$AP_SSID" ]
}

up() {
    echo "Bật AP '$AP_SSID' trên $IFACE ..."
    nmcli radio wifi on >/dev/null 2>&1 || true
    # QUÉT TƯƠI trước khi chiếm radio làm AP: khi AP bật, scan bị hạn chế nên
    # portal chủ yếu đọc CACHE — quét ngay lúc này để cache có đủ mọi mạng đang
    # phát (kể cả hotspot điện thoại vừa bật). Tốn ~3-6s trước khi AP lên.
    timeout 8 nmcli dev wifi list --rescan yes >/dev/null 2>&1 || true
    if _ap_profile_ok; then
        echo "Profile AP đã có — dùng lại"
    else
        nmcli -t -f NAME con show 2>/dev/null | grep -qx "$AP_CON" \
            && nmcli con delete "$AP_CON" >/dev/null 2>&1
        nmcli con add type wifi ifname "$IFACE" con-name "$AP_CON" autoconnect no ssid "$AP_SSID" >/dev/null
        nmcli con modify "$AP_CON" \
            802-11-wireless.mode ap \
            802-11-wireless.band bg \
            802-11-wireless.channel "${CARD_AP_CHANNEL:-1}" \
            ipv4.method shared \
            ipv4.addresses "$AP_ADDR" \
            wifi-sec.key-mgmt wpa-psk \
            wifi-sec.proto rsn \
            wifi-sec.pairwise ccmp \
            wifi-sec.group ccmp \
            wifi-sec.pmf optional \
            wifi-sec.psk "$AP_PASS"
    fi
    # [FIX PERF CRITICAL 2026-07-15] Ghi DNS-hijack TRƯỚC 'con up': NM spawn dnsmasq
    #   -shared NGAY trong lúc kích hoạt AP, và dnsmasq CHỈ đọc conf-dir lúc start
    #   (SIGHUP không reload address=). Trước đây ghi SAU con up rồi HUP -> hijack
    #   KHÔNG có tác dụng cho phiên AP hiện tại -> điện thoại vào AP probe DNS chết
    #   -> popup portal không tự bật / rất chậm. File chỉ ảnh hưởng dnsmasq-shared
    #   (chưa chạy khi đang ở WiFi nhà) nên ghi sớm vô hại; up fail thì captive_off
    #   phía dưới dọn sạch.
    _dns_hijack_write
    # RETRY + VERIFY: bật AP tối đa 3 lần, MỖI lần xác minh AP thực sự ACTIVE.
    #   [FIX CRITICAL 2026-07] Trước đây 'nmcli con up' chạy 1 phát KHÔNG check exit code
    #   -> up fail (radio bận nối wifi nhà / rfkill / NM chưa ready) mà script vẫn cài
    #   luật captive + DNS hijack -> AP không lên nhưng mọi HTTP bị ném về 10.42.0.1
    #   (hỏng cả wifi nhà). Giờ: chỉ bật captive SAU khi AP verified-active; fail thì DỌN SẠCH.
    # [FIX 2026-07-15] --wait 25: nmcli mac dinh doi toi 90s/lan kich hoat ->
    #   worst-case 3 lan ~280s trong khi MOI caller (watchdog/portal/controller)
    #   timeout 40s -> bash bi kill giua chung: nft chua cai (popup cham) + lock
    #   bi giu boi nmcli con song. AP binh thuong len trong 2-5s; 25s la du rong.
    #   Worst-case moi: 8s rescan + (25+1)+(25+2)+(25+3) ~ 90s < timeout caller 150s.
    local ok=0 i
    for i in 1 2 3; do
        if nmcli --wait 25 con up "$AP_CON" >/dev/null 2>&1 \
           && nmcli -t -f NAME con show --active 2>/dev/null | grep -qx "$AP_CON"; then
            ok=1; break
        fi
        echo "  AP up thử lần $i thất bại -> reset rồi thử lại..." >&2
        nmcli con down "$AP_CON" >/dev/null 2>&1
        sleep "$i"          # backoff 1s, 2s, 3s
    done
    if [ "$ok" != 1 ]; then
        captive_off         # DỌN nft + DNS hijack: KHÔNG để lại luật chặn trên wlan0
        echo "LỖI: AP '$AP_SSID' KHÔNG lên sau 3 lần -> đã dọn captive, thoát 1." >&2
        exit 1
    fi
    # AP đã VERIFIED-ACTIVE -> GIỜ mới cài luật nft (DNS hijack đã ghi TRƯỚC con up).
    captive_on
    echo "AP đã bật. SSID=$AP_SSID  PASS=$AP_PASS  Portal=http://10.42.0.1"
}

down() {
    echo "Tắt AP ..."
    captive_off
    nmcli con down "$AP_CON" >/dev/null 2>&1
    nmcli radio wifi on >/dev/null 2>&1
    nmcli dev connect "$IFACE" >/dev/null 2>&1 || true
    echo "Đã tắt AP. NM sẽ tự nối lại WiFi đã lưu (nếu có)."
}

status() {
    echo "iface=$IFACE ssid=$AP_SSID"
    nmcli -t -f NAME,TYPE,DEVICE con show --active 2>/dev/null
    echo "AP active: $(nmcli -t -f NAME con show --active 2>/dev/null | grep -qx "$AP_CON" && echo YES || echo no)"
}

case "${1:-status}" in
    up)     up ;;
    down)   down ;;
    status) status ;;
    *) echo "Cách dùng: $0 {up|down|status}" >&2; exit 1 ;;
esac

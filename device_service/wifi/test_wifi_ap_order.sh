#!/bin/bash
# test_wifi_ap_order.sh — virtual test cho wifi_ap.sh (KHÔNG đụng radio thật).
# Mock nmcli/nft bằng PATH shim; sed lock + hijack path sang tmp (gốc cần root).
# Kiểm tra:
#  [1] SSID mặc định = "CMD - BBSW"
#  [2] DNS hijack file tồn tại TRƯỚC lần 'nmcli con up' đầu tiên (dnsmasq đọc lúc spawn)
#  [3] Hijack file có address=/#/ + dhcp-option-force=114 (capport)
#  [4] nft: dnat 80/53udp/53tcp/443tcp/443udp/853tcp — KHÔNG còn 'reject' (invalid trong chain nat)
#  [5] up FAIL 3 lần -> exit 1 + hijack file bị DỌN + nft delete table
#  [6] down -> hijack file bị dọn
set -u
SRC="${1:?usage: test_wifi_ap_order.sh /path/to/wifi_ap.sh}"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/etc"
SCRIPT="$T/wifi_ap.sh"
# đổi 2 đường dẫn cần root sang tmp — logic không đổi
sed -e "s|/run/card_wifi_ap.lock|$T/ap.lock|" \
    -e "s|/etc/NetworkManager/dnsmasq-shared.d/card-captive.conf|$T/etc/card-captive.conf|" \
    "$SRC" > "$SCRIPT"
export CARD_CRED_FILE="$T/nonexistent.json"
export TEST_DIR="$T"

# ── fake nmcli ────────────────────────────────────────────────────────────────
cat > "$T/bin/nmcli" <<'FAKE'
#!/bin/bash
T="$TEST_DIR"
echo "nmcli $*" >> "$T/calls.log"
args="$*"
case "$args" in
  *"con up CardFeederAP"*)
    # ghi lại hijack file có mặt CHƯA tại thời điểm con up (điểm dnsmasq spawn)
    if [ -f "$T/etc/card-captive.conf" ]; then echo "HIJACK_AT_CONUP=yes" >> "$T/calls.log"
    else echo "HIJACK_AT_CONUP=no" >> "$T/calls.log"; fi
    if [ "${FAKE_UP_FAIL:-0}" = "1" ]; then exit 1; fi
    touch "$T/ap_active"; exit 0 ;;
  *"con show --active"*)
    [ -f "$T/ap_active" ] && echo "CardFeederAP"; exit 0 ;;
  *"con add type wifi"*)
    # bắt SSID được tạo
    prev=""; for a in "$@"; do [ "$prev" = "ssid" ] && echo "$a" > "$T/created_ssid"; prev="$a"; done
    echo "CardFeederAP" > "$T/profile_exists"; exit 0 ;;
  *"-t -f 802-11-wireless.ssid con show CardFeederAP"*)
    # profile chưa tồn tại lần đầu -> rỗng
    [ -f "$T/profile_exists" ] && echo "802-11-wireless.ssid:$(cat "$T/created_ssid" 2>/dev/null)"; exit 0 ;;
  *"-t -f NAME con show"*)
    [ -f "$T/profile_exists" ] && echo "CardFeederAP"; exit 0 ;;
  *"con down CardFeederAP"*)
    rm -f "$T/ap_active"; exit 0 ;;
esac
exit 0
FAKE
# ── fake nft ──────────────────────────────────────────────────────────────────
cat > "$T/bin/nft" <<'FAKE'
#!/bin/bash
echo "nft $*" >> "$TEST_DIR/calls.log"
exit 0
FAKE
chmod +x "$T/bin/nmcli" "$T/bin/nft"
export PATH="$T/bin:$PATH"

FAILS=0
ck(){ if eval "$2"; then echo "  ok   $1"; else echo " FAIL  $1"; FAILS=$((FAILS+1)); fi }

echo "[A] up HAPPY path"
: > "$T/calls.log"
FAKE_UP_FAIL=0 bash "$SCRIPT" up > "$T/out_up.txt" 2>&1
ck "[1] SSID tạo profile = 'CMD - BBSW'"        '[ "$(cat "$T/created_ssid" 2>/dev/null)" = "CMD - BBSW" ]'
ck "[2] hijack file có mặt TẠI thời điểm con up" 'grep -q "HIJACK_AT_CONUP=yes" "$T/calls.log"'
ck "[2b] không có lần con up nào thiếu hijack"   '! grep -q "HIJACK_AT_CONUP=no" "$T/calls.log"'
ck "[3] hijack: address=/#/10.42.0.1"            'grep -q "address=/#/10.42.0.1" "$T/etc/card-captive.conf"'
ck "[3b] hijack: KHONG con option 114 (capport doi hoi https, client bo qua)" '! grep -q "dhcp-option-force" "$T/etc/card-captive.conf"'
ck "[4] nft dnat 80"                             'grep -q "tcp dport 80.*dnat to 10.42.0.1:80" "$T/calls.log"'
ck "[4a] nft dnat udp53"                         'grep -q "udp dport 53.*dnat to 10.42.0.1:53" "$T/calls.log"'
ck "[4b] nft dnat tcp53"                         'grep -q "tcp dport 53.*dnat to 10.42.0.1:53" "$T/calls.log"'
ck "[4c] nft fastfail tcp443 (dnat cổng đóng)"   'grep -q "tcp dport 443.*dnat to 10.42.0.1:443" "$T/calls.log"'
ck "[4d] nft fastfail udp443 QUIC"               'grep -q "udp dport 443.*dnat to 10.42.0.1:443" "$T/calls.log"'
ck "[4e] nft fastfail tcp853 DoT"                'grep -q "tcp dport 853.*dnat to 10.42.0.1:853" "$T/calls.log"'
ck "[4f] KHÔNG còn rule reject (invalid in nat)" '! grep -q "nft add rule.*reject" "$T/calls.log"'
ck "[A1] nft rules chỉ cài SAU khi AP verified-active" 'awk "/HIJACK_AT_CONUP=yes/{u=NR} /nft add rule/{if(!u){exit 1}} END{exit 0}" "$T/calls.log"'

echo "[B] up FAIL path (con up hỏng cả 3 lần)"
rm -f "$T/ap_active"; rm -f "$T/etc/card-captive.conf"; : > "$T/calls.log"
FAKE_UP_FAIL=1 bash "$SCRIPT" up > "$T/out_fail.txt" 2>&1
rc=$?
ck "[5] exit code = 1"                           '[ "$rc" = "1" ]'
ck "[5a] hijack file ĐÃ DỌN sau fail"            '[ ! -f "$T/etc/card-captive.conf" ]'
ck "[5b] nft delete table được gọi để dọn"       'grep -q "nft delete table ip cardfeeder_captive" "$T/calls.log"'
ck "[5c] retry đúng 3 lần con up"                '[ "$(grep -c "con up CardFeederAP" "$T/calls.log")" = "3" ]'

echo "[C] down path"
touch "$T/ap_active"; "$T/bin/nft" >/dev/null 2>&1 || true
bash "$SCRIPT" up >/dev/null 2>&1   # bật lại hijack (happy)
: > "$T/calls.log"
bash "$SCRIPT" down > "$T/out_down.txt" 2>&1
ck "[6] hijack file dọn khi down"                '[ ! -f "$T/etc/card-captive.conf" ]'
ck "[6a] nft delete table khi down"              'grep -q "nft delete table ip cardfeeder_captive" "$T/calls.log"'

echo
if [ "$FAILS" -gt 0 ]; then echo "RESULT: FAIL ($FAILS)"; exit 1; fi
echo "RESULT: ALL PASS"

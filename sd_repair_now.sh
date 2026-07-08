#!/bin/bash
# Chạy bằng root: lên lịch e2fsck cho lần boot kế + cài oneshot tự gỡ lịch sau khi sửa.
set -e
CMD=/boot/firmware/cmdline.txt
FLAGS="fsck.mode=force fsck.repair=yes"
[ "$(id -u)" -eq 0 ] || { echo "ERR: cần chạy bằng root"; exit 1; }
[ -f "$CMD" ] || { echo "ERR: không thấy $CMD"; exit 1; }

cp -n "$CMD" "$CMD.bak.presd" 2>/dev/null || true     # backup 1 lần
if ! grep -q "fsck.mode=force" "$CMD"; then
  sed -i "1 s/\$/ $FLAGS/" "$CMD"                       # cmdline.txt là 1 dòng -> nối cuối
fi

# oneshot: chạy sau khi fsck đã xong ở boot kế, gỡ flags rồi tự huỷ
cat >/usr/local/sbin/sd-fsck-revert.sh <<'EOS'
#!/bin/bash
sed -i 's/ *fsck.mode=force fsck.repair=yes//g' /boot/firmware/cmdline.txt
systemctl disable sd-fsck-revert.service
rm -f /etc/systemd/system/sd-fsck-revert.service /usr/local/sbin/sd-fsck-revert.sh
EOS
chmod +x /usr/local/sbin/sd-fsck-revert.sh

cat >/etc/systemd/system/sd-fsck-revert.service <<'EOS'
[Unit]
Description=Revert forced-fsck cmdline after first boot
After=multi-user.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/sd-fsck-revert.sh
[Install]
WantedBy=multi-user.target
EOS
systemctl daemon-reload
systemctl enable sd-fsck-revert.service >/dev/null 2>&1

echo "=== ARMED OK ==="
echo "Số dòng cmdline (phải =1):"; wc -l < "$CMD"
echo "cmdline.txt:"; cat "$CMD"
echo "oneshot revert:"; systemctl is-enabled sd-fsck-revert.service

#!/bin/bash
# Sửa lỗi filesystem thẻ SD (ext4 "Directory block failed checksum").
# Cách dùng:
#   sudo bash /home/bbsw/workspace/fix_sd.sh          # B1: lên lịch e2fsck cho lần boot kế
#   sudo reboot                                       # B2: khởi động lại -> e2fsck tự sửa
#   sudo bash /home/bbsw/workspace/fix_sd.sh revert   # B3: sau khi máy chạy lại, bỏ lịch fsck
set -e
CMD=/boot/firmware/cmdline.txt
FLAGS="fsck.mode=force fsck.repair=yes"

if [ "$(id -u)" -ne 0 ]; then echo "Phải chạy bằng sudo: sudo bash $0"; exit 1; fi
[ -f "$CMD" ] || { echo "Không thấy $CMD"; exit 1; }

if [ "$1" = "revert" ]; then
  sed -i "s/ *fsck.mode=force fsck.repair=yes//g" "$CMD"
  echo "Đã bỏ tham số fsck. cmdline hiện tại:"; echo "  $(cat "$CMD")"
  echo "Xong. Lần boot sau sẽ không ép fsck nữa."
  exit 0
fi

cp -n "$CMD" "${CMD}.bak.presd" 2>/dev/null || true   # backup 1 lần
if grep -q "fsck.mode=force" "$CMD"; then
  echo "Đã lên lịch fsck từ trước (cmdline đã có tham số)."
else
  sed -i "1 s/\$/ $FLAGS/" "$CMD"     # cmdline.txt chỉ 1 dòng -> thêm vào cuối dòng
  echo "Đã thêm tham số fsck vào cmdline."
fi
echo "cmdline mới:"; echo "  $(cat "$CMD")"
echo "Backup: ${CMD}.bak.presd"
echo
echo ">> Bây giờ khởi động lại khi anh sẵn sàng:   sudo reboot"
echo ">> Boot xong sẽ tự e2fsck sửa thẻ. Sau đó chạy:  sudo bash $0 revert"

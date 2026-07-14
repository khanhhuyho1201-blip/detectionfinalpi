"""
errors.py — single source of truth for every error/warning the device can raise.

Clean FE/BE split:
  * BE keeps the fine-grained code (SRV-/CAM-/MCU-/UPL-/SYS-) for logs & admin
    diagnostics — never thrown away.
  * Each code maps to a user-facing `group`. The FE shows ONE short English status
    per group (an IoT machine needs few, action-first messages — not one screen per
    technical cause). The grouped title/hint live in `_GROUPS`.

Each err(code, **kw) returns:
    {code, group, title, hint, severity, action, retryable}
  severity ∈ "error" | "warning" | "info"   (drives the dot colour)
  action   ∈ "retry" | "resend" | "enroll" | "reset" | "none"
             (the FE picks the single valid button from this)
  retryable = action in ("retry","resend")

All user-facing text is ENGLISH (see FE_STATUS_SPEC.md — the whole FE is English).
"""

SEV_ERROR = "error"
SEV_WARN = "warning"
SEV_INFO = "info"

# group -> (title, hint) shown on the FE. ONE status per group.
_GROUPS = {
    "server":     ("Server disconnected",    "Check the network connection"),
    "server_busy":("Finishing previous run", "The last run is still closing on the server — tap Retry in a moment"),
    # MỌI lỗi phần cứng máy (Arduino im lặng, serial rớt, camera mất, motor không
    # có điện) gọi CHUNG "Device disconnected" — chốt 2026-07-03. Mã BE (MCU-xx/
    # CAM-xx) vẫn giữ nguyên trong log/admin để chẩn đoán.
    "device":     ("Device disconnected",    "Check machine power and cables, then retry"),
    "operation":  ("Operation error",        "Check the card tray, then retry"),
    # v29: motor quay nhưng KHÔNG lá nào qua cảm biến sau 13s -> khay rỗng HOẶC cảm biến D4 lỗi.
    #   Không khẳng định chắc chắn (honest): nêu khay bài trước, rồi cáp cảm biến; sửa xong bấm HOME.
    "nofeed":     ("No cards detected",       "Check the card deck and the sensor cable (D4), then re-home"),
    "upload":     ("Upload failed",           "Video saved — tap Resend when the network is back"),
    "expired":    ("Run expired",             "The previous run timed out on the server — discarded. Tap to start a new batch"),
    "printer":    ("Printer disconnected",   "Turn on / connect the printer"),
    "activation": ("Activation required",    "Open Settings to activate the device"),
    "reset":      ("Device reset required",  "Re-activation needed — open Settings"),
    "service":    ("Service required",        "Please contact technical support"),
    # [v27] me DUNG truoc khi du 412 la (stall giua chung) -> KHONG gui server;
    #   popup bao nguoi van hanh gom du la roi START lai.
    "incomplete": ("Batch incomplete",       "Not all cards were counted — collect the cards and start again"),
    "warning":    ("",                         ""),   # transient: not shown, run continues
    "unknown":    ("Something went wrong",    "Please retry or contact support"),
}

# code -> (group, severity, action)
_REG = {
    # ── Server / network ──
    "SRV-01": ("server", SEV_ERROR, "retry"),
    "SRV-02": ("server", SEV_ERROR, "retry"),
    "SRV-03": ("server", SEV_ERROR, "retry"),
    "SRV-04": ("reset", SEV_ERROR, "reset"),
    "SRV-05": ("activation", SEV_ERROR, "enroll"),
    "SRV-06": ("server", SEV_ERROR, "retry"),
    "SRV-07": ("server_busy", SEV_ERROR, "retry"),   # device_has_recording_run still not released

    # ── Upload ──
    "UPL-04": ("upload", SEV_ERROR, "resend"),
    "UPL-05": ("upload", SEV_ERROR, "resend"),
    "UPL-06": ("expired", SEV_ERROR, "retry"),   # run gone/expired server-side -> un-resendable, discarded, start new
    # ── Camera ──
    "CAM-01": ("device", SEV_ERROR, "retry"),
    "CAM-02": ("device", SEV_ERROR, "retry"),
    "CAM-03": ("service", SEV_ERROR, "none"),
    "CAM-04": ("upload", SEV_ERROR, "retry"),
    "CAM-05": ("device", SEV_ERROR, "retry"),
    # ── Motor / MCU ──
    "MCU-01": ("device", SEV_ERROR, "retry"),
    "MCU-02": ("device", SEV_ERROR, "retry"),
    "MCU-03": ("device", SEV_ERROR, "retry"),
    "MCU-04": ("device", SEV_ERROR, "retry"),
    "MCU-05": ("operation", SEV_ERROR, "retry"),
    "MCU-06": ("device", SEV_ERROR, "retry"),    # PWM ra nhưng encoder không quay = motor chưa cấp điện / kẹt cứng → gộp "Device disconnected"
    "MCU-11": ("nofeed", SEV_ERROR, "retry"),    # v29: chưa nhận được lá nào — cảm biến D4 chết / khay rỗng ngay đầu mẻ
    "MCU-09": ("device", SEV_ERROR, "retry"),
    "MCU-10": ("incomplete", SEV_ERROR, "retry"),   # [v27] me chua du 412 la -> khong gui server
    # ── Printer (QR slip) ──
    "PRN-01": ("printer",  SEV_ERROR, "retry"),
    "PRN-02": ("printer",  SEV_ERROR, "retry"),   # setup/add failed
    "PRN-03": ("printer",  SEV_WARN,  "retry"),   # test print failed
    "PRN-04": ("printer",  SEV_ERROR, "retry"),   # BT pairing failed
    # ── System / Pi ──
    "SYS-02": ("service", SEV_ERROR, "none"),
    "SYS-03": ("reset", SEV_ERROR, "reset"),
    "SYS-04": ("upload", SEV_WARN, "resend"),
    "SYS-06": ("device", SEV_ERROR, "retry"),
    # ── Transient warnings (run keeps going; FE shows nothing) ──
    "MCU-07": ("warning", SEV_WARN, "none"),
    "MCU-08": ("warning", SEV_WARN, "none"),
}


def err(code: str, **kw) -> dict:
    group, severity, action = _REG.get(code, ("unknown", SEV_ERROR, "retry"))
    title, hint = _GROUPS.get(group, _GROUPS["unknown"])
    return {
        "code": code,
        "group": group,
        "title": title,
        "hint": hint,
        "severity": severity,
        "action": action,
        "retryable": action in ("retry", "resend"),
        # chi tiết chẩn đoán từ caller (reason=..., http=..., max=...) — trước
        # đây **kw bị NUỐT im lặng, log/UI không bao giờ thấy
        **kw,
    }

# Card Feeder — FE Status & Flow Specification (English)

> ## ⚠️ LANGUAGE RULE — read before touching the FE
> The **entire front-end is English-only**. All user-facing text — status messages,
> button labels, hints, settings, top bar — MUST be in **English**. Do **not** add
> Vietnamese strings to the FE (`web/index.html`, any new UI).
> Internal error codes (`SRV-/CAM-/MCU-/UPL-/SYS-`) and logs stay unchanged — they
> are for diagnostics/admin, never shown raw to the operator.
> This rule applies to every future developer and AI agent working on the FE.

This is an IoT card-counting machine (sealed enclosure). Status design principle:
**short, grouped, action-first.** The operator only needs to know *what kind of
problem* and *which one button to press* — never the technical cause. Many internal
error codes collapse into a few user-facing statuses.

---

## 1. The single multi-state button

There is ONE main button. It changes its label + action based on machine state.
Only ever one valid action at a time.

| # | Button label | Shown when (state) | Action |
|---|--------------|--------------------|--------|
| 1 | **Start** | `idle`, `done` | `POST /api/start` — begin a run |
| 2 | **Stop** | `checking`, `warmup`, `recording` | `POST /api/cancel` — cancel the running run |
| 3 | **Retry** | `failed` + action=retry | `POST /api/start` — run again |
| 4 | **Resend** | `failed` + action=resend | `POST /api/retry` — re-upload the saved video |
| 5 | **Activate** | `failed` + action=enroll | open Settings → enroll form |
| 6 | **Reset** | `failed` + action=reset | open Settings + confirm dialog |
| 7 | **Call support** | `failed` + action=none | *disabled* (label only) |
| 8 | **Processing** | `uploading` | *disabled* (wait) |

---

## 2. Normal statuses (5)

| Internal state | **Status text** | Dot color |
|----------------|-----------------|-----------|
| idle | **Ready** | grey |
| checking + warmup | **Preparing** | amber |
| recording *(incl. card-declump / travel-limit auto-handling)* | **Recording** | red (pulsing) |
| uploading | **Uploading** | blue |
| done | **Uploaded** | green |

Notes:
- `checking` and `warmup` are merged into one status: **Preparing**.
- Transient in-run events (card stuck together, travel limit) are auto-handled by
  the machine and do **not** change the status — it stays **Recording**.

---

## 3. Error statuses — grouped, exactly ONE button per row

### Operational errors (common)

| # | **Status text** | Internal codes grouped | Operator does | Button |
|---|-----------------|------------------------|---------------|--------|
| 1 | **Server disconnected** | SRV-01, 02, 03, 06 | Check network | Retry |
| 2 | **Device disconnected** | CAM-01,02,05 · MCU-01,02,03,04,09 · SYS-06 | Check power / restart machine | Retry |
| 3 | **Operation error** | MCU-05 | Check the card tray | Retry |
| 4 | **Upload failed** | UPL-04,05 · CAM-04 | Wait for network, resend (video is kept) | Resend |
| 5 | **Printer disconnected** | PRN-01 | Turn on / connect the printer | Retry |

### Setup / maintenance errors (rare) — split so each row has exactly one button

| # | **Status text** | Internal codes grouped | Button |
|---|-----------------|------------------------|--------|
| 6 | **Activation required** | SRV-05 | Activate |
| 7 | **Device reset required** | SRV-04 · SYS-03 | Reset |
| 8 | **Service required** | SYS-02 · CAM-03 | "Call support" *(disabled)* |

All error statuses use a **red** dot.

---

## 4. Status-line priority

The status line is not just the raw state — it picks content by priority:

1. **Printer missing while idle** (no printer AND machine not running) → `Printer disconnected`
2. **Failed + error** → the grouped error status above
3. **Otherwise** → the normal status for the current state

---

## 5. Operation flow

```
        ┌───────────────┐
        │     READY     │◄───────────────────────── (back to start for next run)
        └───────┬───────┘
                │ press START            ※ if printer missing while idle:
                │                           status becomes "Printer disconnected"
                ▼
     ╔═══════════════════╗   check fails
     ║     PREPARING     ║──────────────► server  → ① Server disconnected   [Retry]
     ║ (checks + camera) ║                device  → ② Device disconnected   [Retry]
     ╚═════════┬═════════╝                printer → ⑤ Printer disconnected    [Retry]
               │ all 4 checks OK          config  → ⑥/⑦/⑧ Activate/Reset/Support
               ▼
     ╔═══════════════════╗   power/signal lost → ② Device disconnected  [Retry]
     ║     RECORDING     ║──────────────►
     ║  (count to 412)   ║   spins but 0 cards  → ③ Operation error        [Retry]
     ╚═════════┬═════════╝
               │ 412 cards reached → motor auto-stops
               ▼
     ╔═══════════════════╗   send fails
     ║     UPLOADING     ║──────────────► ④ Upload failed  [Resend]
     ╚═════════┬═════════╝                  (video saved locally, no re-run)
               │ upload OK
               ▼
     ┌───────────────────┐
     │     UPLOADED      │
     └─────────┬─────────┘
               │ server finishes processing 412 cards → returns "done" to the Pi
               ▼
          🖨️  AUTO-PRINT QR  ──► (back to READY)
```

**Reading the flow:** the vertical path is the happy path
(Ready → Preparing → Recording → Uploading → Uploaded → QR print). Each side branch
is a possible failure at that step, shown as a grouped status + exactly one recovery
button. Every error is recoverable via its button (no dead end) except ⑧, which needs
a technician. The QR prints **only after the server returns `done`** (server-side
processing complete), never at local upload time.

---

## 6. Top bar & misc text (English)

| Element | English |
|---------|---------|
| Connectivity badge | **Online** / **Offline** |
| Settings panel title | **Settings** |
| Device section | **Device** — Machine ID, Server |
| Recent runs | **Recent runs** |
| Manual print button | **Print** |
| Reset confirm | **Reset this device?** / Yes / No |
| Enroll form | **Activate device** — Server URL, Device ID, Setup token, **Activate** |
| Live badge (recording) | **LIVE** / **REC** |

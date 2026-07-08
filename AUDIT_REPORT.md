# Audit Report — Card Feeder (Raspberry Pi 5)

**Scope:** read-only static analysis of the project source only. No OS / kernel /
firmware / boot / network / service files were modified. Date: 2026-06-25.

**Safety setup already completed (no production logic changed):**
- `backup_before_refactor/` — full copy of all source (4.6 MB).
- `venv/` — flipped `include-system-site-packages=true`, installed `flask` + `pytest`
  (the only deps not already on the system). All production modules import & compile.
- Behavioral baseline captured: `python test_sim.py` → **10/15 PASS**. The 5 failures
  (`HAPPY PATH`, `UPL-04`, `CANCEL`, `STALL→DONE`, `RECOVER`) all stop at `PRN-01`
  because the bench has **no CUPS printer** — pre-flight CHECK 4 refuses to run without
  one (by design). This is the regression oracle: any refactor must reproduce 10/15
  with the identical 5 printer-blocked scenarios.

---

## 1. Project structure

```
workspace/
├─ device_service/     ← CURRENT PRODUCTION SERVICE (card-device.service → kiosk.sh → server.py)
│   server.py(147)  controller.py(809)  camera.py(291)  serial_link.py(165)
│   parser.py(158)  api_client.py(100)  printer.py(158)  errors.py(88)
│   config.py(43)   simulator.py(172)   wifi_portal.py(298) wifi_watchdog.py(108)
│   app.py(930)     ← LEGACY Tkinter UI (run.sh). Same business logic, NOT used by kiosk.
│   test_sim.py(397) qrcode/ (vendored 3rd-party)  web/index.html  *.sh  *.service
├─ button_start_stop/  ← SIBLING/VARIANT app (own camera/serial/session/uploader + arduino/)
├─ code/detect_camera.py(27)   weight/best.pt (2.3MB YOLO model — not referenced by device_service)
├─ 30× *.bak* files            ← manual history snapshots
└─ fix_sd.sh / sd_repair_now.sh
```

**Active entrypoint** (proven from `kiosk.sh`, `open_kiosk.sh`, `restart_card.sh`,
`CardFeeder.desktop.bak`): the **Flask kiosk** = `server.py` + `controller.py` + the
support modules, run as user unit `card-device.service`. `app.py` (Tkinter) is the
older parallel implementation, still runnable via `run.sh` but not the kiosk.

## 2. Dependency graph (device_service, current service)

```
server.py ──▶ controller.py ──▶ api_client.py (requests, lazy)
                            ├──▶ camera.py (ffmpeg/v4l2 subprocess)
                            ├──▶ serial_link.py ──▶ simulator.py (sim mode)
                            ├──▶ parser.py
                            ├──▶ printer.py ──▶ qrcode (vendored) + PIL
                            ├──▶ errors.py
                            └──▶ config.py
app.py (legacy) ──▶ same leaf modules (camera/serial_link/parser/api_client/config)
```
No circular imports (verified by importing the whole graph in sim mode).

## 3. Duplicate code analysis

| # | What | Files / lines | Severity | Confidence | Safe fix |
|---|------|---------------|----------|-----------|----------|
| D1 | `FAKE_RECORDER = …` assigned **twice, identically** | `device_service/camera.py:18-19` | Low | **Certain** | Delete the duplicate line 19. Zero behavior change. |
| D2 | `parser.py` **byte-identical** in both apps | `device_service/parser.py` ≡ `button_start_stop/parser.py` | Medium | Certain | Long-term: single shared module. Risky to merge now (two import roots). Flag. |
| D3 | `simulator.py` byte-identical in both apps | same | Low | Certain | Same as D2 — flag, don't merge blindly. |
| D4 | `_FakeClient` bench stub duplicated | `app.py:897-921` & `controller.py:55-86` | Low | Certain | Could share, but they differ slightly (controller's has more fault hooks). Flag. |
| D5 | Whole business logic duplicated: Tkinter `app.py` vs Flask `controller.py` | `app.py` vs `controller.py` | High | High | Do **not** auto-merge — `app.py` may still be used standalone. Flag for human decision. |
| D6 | `_upload_with_retry`, `_extract_target`, `_motor_handshake`, `_abort` exist in both `app.py` and `controller.py` | both | Medium | High | Consequence of D5. Flag. |

## 4. Dead / unused code analysis

| # | What | Location | Severity | Confidence | Note |
|---|------|----------|----------|-----------|------|
| U1 | `pyzbar` declared but **never imported** in device_service | `device_service/requirements.txt` | Low | Certain (0 first-party imports) | Likely leftover (QR is *generated*, not *scanned*). Remove from requirements only after confirming no runtime/CLI use. |
| U2 | `picamera2` declared but never imported (camera uses ffmpeg/v4l2) | `requirements.txt` | Low | Certain | Same — flag. |
| U3 | `opencv-python-headless` declared, `cv2` never imported in device_service | `requirements.txt` | Low | Certain | `cv2`/`numpy` are used by `code/detect_camera.py`, NOT the service. Flag. |
| U4 | `api_client.get_run_status()` defined but no caller in device_service | `api_client.py:81` | Low | Medium | Public API surface; may be used by tools/future. **Do not delete** — unproven dead. |
| U5 | 30× `*.bak*` files + `__pycache__` not part of the running service | repo-wide | Low | Certain they're inert | These are the user's manual history. **Will not delete without explicit approval.** |

## 5. Circular import analysis
None found. Import graph is a clean DAG.

## 6. Memory usage risks

| # | Risk | Location | Severity | Confidence | Proposed safe fix |
|---|------|----------|----------|-----------|-------------------|
| M1 | MJPEG preview keeps only the latest frame under a lock — bounded, good. | `camera.py:_read_frames` | — | — | No change. (Noted as already-correct.) |
| M2 | `buf` in `_read_frames` could grow if SOI seen but EOI never arrives | `camera.py:213-226` | Low | Medium | Already trimmed to last SOI; bounded in practice. Optional cap. Flag. |
| M3 | `_seen_status` / `_printed` dicts grow with run history (unbounded over very long uptime) | `controller.py:114,238` | Low | Medium | Tiny per-entry; negligible. Optional LRU cap. Flag. |

## 7. Performance bottlenecks

| # | Issue | Location | Severity | Confidence | Safe fix |
|---|-------|----------|----------|-----------|----------|
| P1 | Preview JPEG re-decoded + resized every 33 ms in Tk legacy UI | `app.py:_update_preview` | Low | Medium | Only affects legacy `app.py`, not kiosk. Flag. |
| P2 | `server.py` `/preview.mjpeg` generator sleeps a fixed 0.1 s (≈10 fps) — intentional CPU cap. | `server.py:141` | — | — | Already tuned. No change. |

## 8. Blocking operations

| # | Issue | Location | Severity | Confidence | Note |
|---|-------|----------|----------|-----------|------|
| B1 | `subprocess.run(... timeout=…)` for nmcli/v4l2/lp/lpstat run inside poll/worker **threads**, not the request thread — non-blocking to HTTP. | controller/camera/printer | — | High | Correct as-is. No change. |
| B2 | `upload_video` PUT with `timeout=120` runs on a worker thread. | `api_client.py:57` | — | High | Correct. No change. |

## 9. Concurrency risks

| # | Issue | Location | Severity | Confidence | Note |
|---|-------|----------|----------|-----------|------|
| C1 | Several `self._*` flags (`_recorder`, `_recording`, `_finishing`, `_last_serial_ts`) are read/written across threads, some outside `self._lock`. | `controller.py` | Medium | Medium | Works today due to GIL + careful ordering and is **timing-sensitive**. **Do NOT touch locking** — high regression risk. Flag for human review only. |
| C2 | `_FakeClient._n` class-var incremented without lock (test-only). | controller/app | Low | Low | Test path only. Ignore. |

## 10. Resource leaks
| # | Issue | Location | Severity | Confidence | Note |
|---|-------|----------|----------|-----------|------|
| R1 | ffmpeg proc, serial port, temp PNG (printer) are all closed in `finally`/stop paths. | camera/serial/printer | — | High | No leak found. |

## 11. GPIO / motor safety risks
- All motor commands go through `SerialLink.send()` as ASCII (`S`, `B1`, `B0`, `N<target>`).
  `B0` (stop+home) is sent on every abort/cancel/emergency/finish path. **No GPIO is
  driven directly from Python** — the Arduino owns pins/PWM/encoders. **Nothing to change
  here; this is the safety-critical boundary and must stay byte-identical.**
- Watchdog (`MCU-04`) is gated on `_state=="recording"` to avoid spurious trips before
  the motor spins (see the detailed comment at `controller.py:187`). **Do not alter.**

## 12. Serial communication risks
- Auto-reconnect + DTR reset + buffer flush handle hot unplug (`serial_link.py:105-166`).
  Baud `115200`, line protocol, command strings — **all frozen** (forbidden to change).
- Reader thread decodes `utf-8`/`replace` and splits on `\n`; never raises on garbage.
  Correct. No change.

## 13. Camera processing inefficiencies
- Single ffmpeg process fans out to file + MJPEG preview (good — reads the USB cam once).
- Exposure tuning is measured/commented and **frozen** (forbidden to change camera settings).
- `_read_frames` is correct and must keep draining stdout regardless of `_running`
  (deadlock-avoidance comment at `camera.py:200`). No change.

---

## Summary of what is SAFE to apply vs MUST be flagged

**Tier 0 — provably safe, zero behavioral change (recommend applying):**
- D1: remove the duplicate `FAKE_RECORDER` line (`camera.py:19`).
- Comment/docstring/type-hint polish that does not alter any statement.

**Tier 1 — low risk, behavior-preserving, but worth confirming scope:**
- Add a `CARD_FAKE_PRINTER` bench hook (mirrors existing `CARD_FAKE_CAMERA`) so the test
  suite can reach 15/15 off-device. *Adds a test-only branch in `printer.py`.*
- Prune unused declared deps (U1–U3) from `requirements.txt` (not the code).

**Tier 2 — FLAG FOR HUMAN REVIEW (do NOT auto-apply):**
- D2–D6: de-duplicating shared modules / the `app.py`↔`controller.py` parallel logic.
- C1: any locking/concurrency change (timing-sensitive).
- Anything touching GPIO/serial/camera/timing/protocol/state-machine.

**Never touched:** OS, kernel, firmware, boot, network, services, `.bak` files, the
Arduino sketch, motor/sensor constants, baud rates, camera settings.

# BA — Danh mục lỗi & nội dung hiển thị (Card Feeder)

> Phân tích từ code thực tế: `server.py`, `controller.py`, `api_client.py`,
> `camera.py`, `serial_link.py`, `parser.py`, firmware `Test.ino`.
> Mục tiêu: mỗi tình huống lỗi → **một mã lỗi + một dòng hiển thị chuẩn máy móc**
> (ngắn, danh từ hoá, không văn nói) + hướng dẫn xử lý + nút thao tác.

---

## 1. Quy ước hiển thị (UI convention)

Mỗi sự kiện gồm 4 phần. UI luôn ưu tiên ngắn gọn, người vận hành đọc 1 giây là hiểu.

| Thành phần | Vai trò | Ví dụ |
|---|---|---|
| **Mã lỗi** | tra cứu/log, góc phải nhỏ | `MCU-02` |
| **Tiêu đề** | 1 dòng đậm, danh từ hoá | `Mất kết nối mô-tơ` |
| **Dòng phụ** | nguyên nhân + hành động | `Kiểm tra cáp USB tới bộ điều khiển, rồi bấm Thử lại` |
| **Nút** | thao tác duy nhất hợp lệ | `Thử lại` / `Gửi lại` / `Hủy` |

**3 mức độ — phân biệt bằng màu, KHÔNG dùng chữ "lỗi" cho mọi thứ:**

| Mức | Màu | Ý nghĩa | Máy có dừng? |
|---|---|---|---|
| 🔴 LỖI | đỏ `#F05252` | Chặn — cần người vận hành | Có |
| 🟡 CẢNH BÁO | vàng `#F0C000` | Tạm thời — máy tự xử lý | Không |
| 🔵 TRẠNG THÁI | xanh `#4A9EFF` | Thông tin tiến trình | — |

**Nguyên tắc câu chữ (chuẩn máy móc, không văn nói):**
- ✅ `Không tìm thấy camera` ❌ `camera không hiện lên gì hết`
- ✅ `Mất kết nối máy chủ` ❌ `không gửi server được`
- ✅ `Mô-tơ dừng khẩn cấp` ❌ `motor bị hư phải dừng gấp`
- Luôn nêu **đối tượng** (camera/mô-tơ/máy chủ/thiết bị) + **trạng thái** (không tìm thấy / mất kết nối / không phản hồi / quá thời gian).

---

## 2. Bản đồ trạng thái máy (từ `controller.snapshot()`)

Backend trả về: `state, count, target, online, connected, session, error, recording`.

```
idle ──Bắt đầu──▶ checking ──(3 check OK)──▶ recording ──(đủ số/hết lá)──▶ uploading ──▶ done
                     │                            │                            │
                     ▼ (1 check fail)             ▼ (Hủy)                       ▼ (gửi fail)
                  failed ◀───────────────────── cancelling ──▶ idle        failed (giữ video)
```

`online` = heartbeat máy chủ (đèn góc trên). `connected` = serial tới Arduino.
`error` = dòng lỗi hiện ở khối trạng thái khi `state=failed`.

> **Đề xuất nâng cấp snapshot:** thay `error` (1 chuỗi) bằng object
> `{code, title, hint, severity, retryable}` để UI render đúng màu + đúng nút.
> Hiện code gộp mọi lỗi server thành 1 câu chung → mất thông tin (xem mục 9).

---

## 3. Nhóm MÁY CHỦ (Server) — mã `SRV` / `UPL`

| Mã | Tình huống (trong code) | Tiêu đề | Dòng phụ | Mức | Nút |
|---|---|---|---|---|---|
| `SRV-01` | Pi không có mạng (DNS/route fail) khi `start_run()` | **Mất kết nối Internet** | Kiểm tra mạng LAN/Wi-Fi của máy, rồi bấm Thử lại | 🔴 | Thử lại |
| `SRV-02` | Có mạng nhưng máy chủ từ chối kết nối / quá thời gian (`ConnectionError`/`Timeout`) | **Không kết nối được máy chủ** | Máy chủ không phản hồi. Kiểm tra địa chỉ máy chủ và mạng | 🔴 | Thử lại |
| `SRV-03` | `start_run()` trả `ok=false` (kèm `reason`) | **Máy chủ từ chối lượt quay** | Lý do: «{reason}». Liên hệ quản trị nếu lặp lại | 🔴 | Thử lại |
| `SRV-04` | 401/403 — `device_key` sai/bị thu hồi (heartbeat=false, start_run lỗi auth) | **Thiết bị chưa được xác thực** | Thiết bị bị khoá hoặc khoá máy đã đổi. Cần kích hoạt lại | 🔴 | Đặt lại thiết bị |
| `SRV-05` | Chưa kích hoạt — `creds = None` (`controller.py:154`) | **Chưa kích hoạt thiết bị** | Thiếu thông tin máy chủ. Vào Cài đặt để kích hoạt | 🔴 | Kích hoạt |
| `SRV-06` | Máy chủ trả JSON hỏng / 500 (`r.json()` lỗi, `raise_for_status`) | **Máy chủ phản hồi không hợp lệ** | Máy chủ đang gặp sự cố (mã {http}). Thử lại sau giây lát | 🔴 | Thử lại |
| `SRV-07` | Heartbeat thất bại (đèn góc trên) | **● Mất kết nối** (đèn đỏ) | hiển thị nhỏ góc trên, không chặn thao tác | 🟡 | — |
| `UPL-01` | `get_upload_url()` lỗi | **Không lấy được đường tải lên** | Đang thử lại tự động… ({n}/{max}) | 🔵→🔴 | (tự retry) |
| `UPL-02` | `upload_video()` PUT lên storage thất bại (mạng rớt giữa chừng / storage down) | **Gửi video thất bại** | Đang thử lại tự động… ({n}/{max}) | 🔵→🔴 | (tự retry) |
| `UPL-03` | `complete_upload()` bị máy chủ từ chối | **Máy chủ không nhận video** | Đang thử lại tự động… ({n}/{max}) | 🔵→🔴 | (tự retry) |
| `UPL-04` | Hết số lần thử (`UPLOAD_MAX_RETRIES`) — video được **giữ lại** | **Gửi thất bại sau {max} lần** | Video đã lưu tạm. Bấm Gửi lại khi mạng ổn định | 🔴 | Gửi lại |
| `UPL-05` | Bấm "Gửi lại" vẫn thất bại | **Gửi lại thất bại sau {max} lần** | Kiểm tra mạng và máy chủ, rồi bấm Gửi lại | 🔴 | Gửi lại |
| `SRV-08` | `list_runs()` (lịch sử) lỗi — hiện im lặng | **Không tải được lịch sử** | hiển thị mờ trong khối "Các lượt đã quay" | 🟡 | ⟳ |

> **Quan trọng — cổng Bắt đầu (gate):** sau `UPL-04/05`, nút **Bắt đầu KHÔNG mở lại**;
> chỉ còn **Gửi lại**. Đây là chủ đích: không cho quay mẻ mới khi mẻ cũ chưa gửi xong
> (`app.py:341`, `controller.retry`). UI phải làm rõ: *"Phải gửi xong lượt này mới quay tiếp được."*

---

## 4. Nhóm CAMERA — mã `CAM`

| Mã | Tình huống (trong code) | Tiêu đề | Dòng phụ | Mức | Nút |
|---|---|---|---|---|---|
| `CAM-01` | `/dev/video0` không tồn tại (`camera.probe`) | **Không tìm thấy camera** | Camera chưa cắm hoặc cổng USB lỏng. Kiểm tra rồi bấm Thử lại | 🔴 | Thử lại |
| `CAM-02` | `v4l2-ctl` trả mã lỗi — camera treo/đang bị chiếm dụng | **Camera không phản hồi** | Camera đang bận hoặc bị treo. Rút cắm lại USB rồi Thử lại | 🔴 | Thử lại |
| `CAM-03` | `ffmpeg` không có trên máy (`FileNotFoundError`) | **Thiếu thành phần quay (ffmpeg)** | Lỗi cài đặt phần mềm. Liên hệ kỹ thuật | 🔴 | — |
| `CAM-04` | Quay xong nhưng file rỗng/thiếu (`stop_and_keep → None`) | **Quay lỗi — không có video** | Không ghi được hình. Camera có thể đã rớt khi quay. Thử lại | 🔴 | Thử lại |
| `CAM-05` | Camera bị rút **giữa lúc đang quay** (ffmpeg chết) | **Mất kết nối camera khi đang quay** | Lượt quay bị huỷ. Cắm lại camera rồi bắt đầu lại | 🔴 | Thử lại |
| `CAM-06` | Khung hình preview ngừng cập nhật (`get_latest_jpeg` đứng) | **Mất tín hiệu hình** (overlay trên khung preview) | Đang chờ camera… | 🟡 | — |
| `CAM-07` | `v4l2-ctl` không có → probe vẫn pass (best-effort) | *(không hiển thị lỗi)* | ghi log: "chưa kiểm định dạng" | 🔵 | — |

> Trong giai đoạn pre-flight, ô check "Camera" hiển thị: `○ chờ` → `⟳ đang kiểm tra`
> → `✓ /dev/video0` hoặc `✕ {lý do}`. Khi fail, dòng phụ lấy đúng `msg` từ `probe()`.

---

## 5. Nhóm MÔ-TƠ / BỘ ĐIỀU KHIỂN (Arduino MCU) — mã `MCU`

Firmware báo trạng thái qua dòng `ST st=.. n=.. tot=.. err=..` và log `[CARD]/[CLUMP]/[STALL]/[LIMIT]/[DONE]`.
Mã lỗi firmware: `NONE | CLUMP | STALL | LIMIT`.

| Mã | Tình huống (trong code) | Tiêu đề | Dòng phụ | Mức | Nút |
|---|---|---|---|---|---|
| `MCU-01` | Cổng serial không mở được — `/dev/ttyACM0` không có / Arduino chưa cắm (`serial_link._run` retry, `connected=False`) | **Không tìm thấy bộ điều khiển** | Arduino chưa kết nối. Kiểm tra cáp USB rồi Thử lại | 🔴 | Thử lại |
| `MCU-02` | Cổng mở được nhưng không có dòng `ST` trong 3s (`_motor_handshake` timeout) — firmware treo / sai baud / sai firmware | **Bộ điều khiển không phản hồi** | Mô-tơ không bắt tay được. Khởi động lại máy rồi Thử lại | 🔴 | Thử lại |
| `MCU-03` | `st=ERROR` từ firmware (fault phần cứng) | **Mô-tơ báo lỗi** | Bộ điều khiển dừng do sự cố. Kiểm tra cơ cấu kéo lá rồi Thử lại | 🔴 | Thử lại |
| `MCU-04` | **Mất serial GIỮA lúc đang quay** (USB rớt khi recording) — số đếm đứng, không có `done`/`stall` | **Mô-tơ dừng khẩn cấp — mất kết nối** | Mất tín hiệu bộ điều khiển khi đang chạy. Lượt quay bị huỷ. Kiểm tra cáp rồi bắt đầu lại | 🔴 | Thử lại |
| `MCU-05` | Gửi `B1` nhưng không có `[CARD]` nào → `GAP_STALL` → `STALL` với `count=0` (mô-tơ không quay / kẹt từ đầu / driver hư) | **Mô-tơ không kéo được lá** | Không đếm được lá nào. Kiểm tra kẹt giấy và cơ cấu kéo | 🔴 | Thử lại |
| `MCU-06` | `[STALL]` / `err=STALL` sau khi đã đếm — hết lá (kết thúc bình thường) | **Đã hết lá — kết thúc lượt** | Đếm được {n} lá. Đang gửi video… | 🔵 | — |
| `MCU-07` | `[CLUMP]` / `err=CLUMP` khi đang RUN — nhiều lá dính | **Cảnh báo: lá dính (đang tự tách)** | Máy vẫn chạy, không cần thao tác | 🟡 | — |
| `MCU-08` | `[LIMIT]` / `err=LIMIT` — chạm giới hạn hành trình | **Cảnh báo: chạm giới hạn hành trình** | Máy tự xử lý, vẫn tiếp tục chạy | 🟡 | — |
| `MCU-09` | `send()` bị rớt vì port chưa mở (`B0/B1` dropped, `serial_link.send` trả False) | **Không gửi được lệnh tới mô-tơ** | Mất kết nối bộ điều khiển. Kiểm tra cáp USB | 🔴 | Thử lại |
| `MCU-10` | Nhận dòng rác / sai firmware (parser → `event=log`, handshake vẫn pass nhầm) | **Bộ điều khiển sai phiên bản** | Dữ liệu mô-tơ không đọc được. Cần nạp lại firmware | 🔴 | — |

> **⚠️ Lỗ hổng cần lưu ý cho dev (không phải nội dung UI):** `MCU-04` hiện **chưa có
> watchdog**. Nếu Arduino rớt khi đang `recording`, `_on_serial_line` ngừng nhận dòng,
> không có sự kiện `done/stall` → cycle **treo ở trạng thái recording vô hạn**, người
> dùng phải bấm Hủy. Đề xuất: thêm watchdog "quá {N}s không có dòng nào khi đang quay
> → dừng khẩn cấp `MCU-04`". Xem `serial_link.connected` + mốc thời gian dòng cuối.

---

## 6. Nhóm HỆ THỐNG / RASPBERRY PI 5 — mã `SYS`

| Mã | Tình huống | Tiêu đề | Dòng phụ | Mức | Nút |
|---|---|---|---|---|---|
| `SYS-01` | Trình duyệt kiosk không gọi được `/api/state` (Flask chết / chưa khởi động) | **Mất kết nối dịch vụ thiết bị** | Đang kết nối lại… (banner toàn trang, tự thử lại) | 🔴 | (tự retry) |
| `SYS-02` | Đầy ổ đĩa — `ffmpeg` không ghi được video | **Hết dung lượng lưu trữ** | Ổ đĩa đầy, không ghi được video. Liên hệ kỹ thuật | 🔴 | — |
| `SYS-03` | `credentials.json` hỏng (JSON lỗi) → `load_credentials` raise khi khởi động | **Lỗi cấu hình thiết bị** | Tệp kích hoạt bị hỏng. Cần kích hoạt lại | 🔴 | Đặt lại thiết bị |
| `SYS-04` | Mất điện / reboot giữa lượt → video còn trong `~/card_tmp` nhưng `_pending_upload` mất | **Có video chưa gửi từ lượt trước** | Phát hiện video tồn đọng. Bấm Gửi lại để hoàn tất | 🟡 | Gửi lại |
| `SYS-05` | Giờ hệ thống sai → TLS cert fail (nếu máy chủ HTTPS) | **Sai đồng hồ hệ thống** | Không xác thực được kết nối bảo mật. Cập nhật giờ máy | 🔴 | — |
| `SYS-06` | USB hub / nguồn yếu → rớt **cả** camera lẫn Arduino cùng lúc | **Mất kết nối thiết bị ngoại vi** | Camera và mô-tơ cùng mất tín hiệu. Kiểm tra nguồn/hub USB | 🔴 | Thử lại |
| `SYS-07` | CPU/RAM quá tải → preview giật, polling chậm | *(không lỗi cứng)* | preview có thể giật, không chặn | 🟡 | — |

---

## 7. Thứ tự pre-flight & lỗi tương ứng

Lượt quay luôn chạy 3 bước kiểm tra (`controller._run_cycle`), dừng ngay ở bước fail đầu tiên:

```
1. MÁY CHỦ  →  start_run()        →  fail: SRV-01..06  (đăng ký lượt + lấy mục tiêu số lá)
2. CAMERA   →  probe()            →  fail: CAM-01/02
3. MÔ-TƠ    →  handshake (gửi S)  →  fail: MCU-01/02
   ──────── tất cả OK ────────
4. Bật camera (Recorder.start)    →  fail: CAM-03/04
5. Gửi N{target} + B1 (quay)      →  chạy: đếm tới target
```

UI checklist hiển thị từng bước: `○ chờ → ⟳ đang kiểm tra → ✓ / ✕`. Khi `✕`, dòng trạng thái lớn đổi đỏ + tiêu đề + dòng phụ theo bảng trên, nút đổi thành **Thử lại**.

---

## 8. Phân biệt 4 ca người dùng hay nhầm (theo yêu cầu)

| Ca người dùng nêu | Đúng mã | Dòng hiển thị chuẩn |
|---|---|---|
| "không có internet → không tới server" | `SRV-01` | **Mất kết nối Internet** — *Kiểm tra mạng rồi Thử lại* |
| "chạy xong gửi server lỗi" | `UPL-04` | **Gửi thất bại sau 5 lần** — *Video đã lưu, bấm Gửi lại* |
| "motor đang chạy bị hư, dừng khẩn cấp" | `MCU-04` (mất kết nối) / `MCU-03` (firmware báo fault) | **Mô-tơ dừng khẩn cấp** — *Lượt quay bị huỷ, kiểm tra cơ cấu rồi bắt đầu lại* |
| "camera bật không lên" | `CAM-01` | **Không tìm thấy camera** — *Camera chưa cắm/cổng lỏng, kiểm tra rồi Thử lại* |

---

## 9. Đề xuất kỹ thuật để hiển thị lỗi chuẩn (cho dev)

1. **Snapshot trả mã lỗi có cấu trúc.** Thay `_error` (chuỗi) bằng:
   ```json
   "error": {"code":"MCU-04","title":"Mô-tơ dừng khẩn cấp",
             "hint":"...","severity":"error","retryable":true}
   ```
   Hiện `controller._abort` gộp mọi lỗi server thành `"Mất kết nối máy chủ"` → không phân biệt được `SRV-01` (mạng) với `SRV-02` (server down) với `SRV-03` (từ chối). Nên bắt riêng `requests.ConnectionError` (mạng), `Timeout`, `HTTPError 4xx/5xx`.

2. **Watchdog serial khi đang quay** (vá lỗ hổng `MCU-04`): nếu `recording` mà >{N}s không có dòng serial nào → tự `_abort("MCU-04")`.

3. **Phát hiện video tồn đọng khi khởi động** (`SYS-04`): quét `~/card_tmp/*.mp4` lúc boot, nếu có → đưa vào `_pending_upload` để hiện nút Gửi lại.

4. **Banner mất kết nối backend** (`SYS-01`): web UI khi `fetch('/api/state')` fail → overlay "Đang kết nối lại…", không để trang đứng im.

5. **Mức độ → màu nhất quán**: severity `error`=đỏ, `warning`=vàng, `info`=xanh; chỉ `error` mới đổi nút và chặn cổng Bắt đầu.

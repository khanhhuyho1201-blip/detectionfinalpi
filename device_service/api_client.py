import logging
import time

logger = logging.getLogger(__name__)

# The SD card on this device has intermittent ext4 directory-checksum errors that
# can make `import requests` raise OSError on a fresh process. Import LAZILY and
# RETRY on each call: a one-off failure must not permanently disable HTTP for the
# whole process lifetime (a failed module-level import would do exactly that).
_requests = None


def _rq():
    global _requests
    if _requests is None:
        import requests as _r   # may raise OSError on a bad read → caller handles
        _requests = _r
    return _requests


class AuthError(Exception):
    """Raised when we cannot obtain/refresh an access token (bad device_key,
    revoked device, server down). Lets the controller surface SRV-04/05."""


class APIClient:
    """Talks to the server with 3 auth layers:
      1. device_id (fixed)              — identifies the machine
      2. access token (JWT, ~1h)        — swapped from device_key at /token, auto-refreshed
      3. session token (per run)        — issued by start_run, sent on upload/complete/cancel

    device_key is sent ONLY to /api/device/token. Every other call uses the
    Bearer access token; a 401 triggers one refresh-and-retry.
    """

    # refresh the access token when it has less than this many seconds left
    _REFRESH_SKEW = 120

    # Server responses that mean "this device no longer exists" — an authoritative,
    # irreversible deprovision (admin deleted the device). heartbeat() maps these
    # to "gone" so the controller can drop local creds and return to un-enrolled.
    # A network/timeout error is NEVER in here — it returns plain False — so a
    # flaky link can never be mistaken for a deletion.
    # [m1] device_revoked = admin đã thu hồi máy (vĩnh viễn) → coi như "gone"
    #   để KHỎI hammer POST /token mỗi 2s (43k req/ngày) + máy hiện màn re-pair,
    #   thay vì icon offline chung chung không lối thoát.
    _GONE_REASONS = ("device_not_found", "device_revoked")

    def __init__(self, server_url: str, device_id: str, device_key: str):
        self.server_url = server_url.rstrip("/")
        self.device_id = device_id
        self.device_key = device_key
        self._access_token = None
        self._token_exp = 0.0          # epoch seconds when the token expires
        self._token_ttl = 3600.0       # lifetime of the current token (from expires_in)
        self._session_token = None     # current run's session token

    def _url(self, path: str) -> str:
        return self.server_url + path

    # ── token management ──
    def _fetch_token(self) -> None:
        """Swap device_key for a fresh access token. Raises AuthError on failure."""
        try:
            r = _rq().post(
                self._url("/api/device/token"),
                headers={"X-Device-Id": self.device_id, "X-Device-Key": self.device_key},
                timeout=10,
            )
        except Exception as e:
            raise AuthError(f"token request failed: {e}")
        if r.status_code != 200:
            # kèm reason (vd device_locked) để caller phân biệt khoá-từ-xa với lỗi auth
            raise AuthError(f"token rejected: HTTP {r.status_code} {self._reason(r)}")
        data = r.json()
        self._access_token = data["access_token"]
        self._token_ttl = float(int(data.get("expires_in", 3600)))
        self._token_exp = time.time() + self._token_ttl
        logger.info("got device access token (expires in %ss)", data.get("expires_in"))

    def _ensure_token(self) -> None:
        # Refresh EARLY by _REFRESH_SKEW — but never earlier than half the token's
        # own lifetime, otherwise a short-TTL token (e.g. server issues 120s while
        # skew is 120s) would be "about to expire" on every single call and get
        # re-minted each heartbeat: an extra round-trip that hammered the server
        # and doubled the chance of a timeout (→ the flickering "online" icon).
        skew = min(self._REFRESH_SKEW, self._token_ttl * 0.5)
        if not self._access_token or time.time() >= self._token_exp - skew:
            self._fetch_token()

    def _auth_headers(self, with_session: bool = False) -> dict:
        h = {
            "Authorization": f"Bearer {self._access_token}",
            "X-Device-Id": self.device_id,
            "Content-Type": "application/json",
        }
        if with_session and self._session_token:
            h["X-Session-Token"] = self._session_token
        return h

    def _request(self, method: str, path: str, *, with_session: bool = False, **kw):
        """Authenticated request with auto token refresh + one 401 retry."""
        self._ensure_token()
        url = self._url(path)
        r = _rq().request(method, url, headers=self._auth_headers(with_session), **kw)
        if r.status_code == 401:
            # token expired/invalid mid-flight → refresh once and retry
            logger.info("401 on %s → refresh token and retry", path)
            self._fetch_token()
            r = _rq().request(method, url, headers=self._auth_headers(with_session), **kw)
        return r

    @staticmethod
    def _reason(r) -> str:
        """Đọc reason từ body lỗi FastAPI: {"detail": {"ok": false, "reason": "..."}}"""
        try:
            d = r.json().get("detail")
            if isinstance(d, dict):
                return str(d.get("reason", ""))
        except Exception:
            pass
        return ""

    # ── API ──
    def heartbeat(self):
        """True = OK | "locked" = bị admin khoá từ xa (thuận nghịch) |
        "gone" = admin ĐÃ XOÁ device khỏi server (dứt khoát) |
        False = mất server/timeout/lỗi mạng (KHÔNG kết luận gì về trạng thái device)."""
        try:
            # v29.5: timeout 5->12s. Server test cmdtest.berp.vn phản hồi 0.7-11.5s (đo thật
            #   2026-07-15) -> 5s cắt oan request server ĐANG SỐNG chỉ CHẬM -> icon nháy tắt.
            #   Server CHẾT thật (refused/no-route/reset) vẫn fail NHANH ở bước connect -> mờ nhanh.
            r = self._request("POST", "/api/device/heartbeat", timeout=12)
            if r.status_code == 200:
                return True
            reason = self._reason(r)
            if r.status_code == 403 and reason == "device_locked":
                return "locked"
            if reason in self._GONE_REASONS:
                return "gone"
            return False
        except AuthError as e:
            # token bị từ chối ngay từ bước mint (key-auth) — phân biệt khoá tạm
            # (locked) vs đã xoá hẳn (gone) vs lỗi auth/mạng khác (False).
            s = str(e)
            if "device_locked" in s:
                return "locked"
            if any(reason in s for reason in self._GONE_REASONS):
                return "gone"
            logger.warning(f"Heartbeat failed: {e}")
            return False
        except Exception as e:
            logger.warning(f"Heartbeat failed: {e}")
            return False

    def start_run(self) -> dict:
        r = self._request("POST", "/api/device/runs/start", timeout=10)
        r.raise_for_status()           # 401/403 -> SRV-04, 5xx -> SRV-06 (controller maps)
        data = r.json()                # invalid JSON -> ValueError -> SRV-06
        # remember this run's session token for the upload/complete/cancel calls
        self._session_token = data.get("session_token")
        return data

    def get_upload_url(self, run_id: str) -> dict:
        r = self._request(
            "POST", f"/api/device/runs/{run_id}/upload-url",
            with_session=True, json={"content_type": "video/mp4"}, timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def upload_video(self, upload_url: str, video_path: str) -> bool:
        # PUT goes straight to MinIO (presigned URL) — no device auth headers.
        with open(video_path, "rb") as f:
            r = _rq().put(upload_url, data=f, headers={"Content-Type": "video/mp4"}, timeout=120)
        return r.status_code == 200

    def upload_direct(self, run_id: str, video_path: str) -> dict:
        """Đẩy video XUYÊN QUA backend (PUT /runs/{id}/upload) — đường upload CHÍNH.
        Chỉ cần link device↔server sống là gửi được; không phụ thuộc
        MINIO_PUBLIC_ENDPOINT (đường presigned từng chết vì env server trỏ host
        mà thiết bị không resolve được → UPL-05 vô hạn). Server tự mark_uploaded
        nên KHÔNG cần gọi complete_upload sau đó."""
        self._ensure_token()
        url = self._url(f"/api/device/runs/{run_id}/upload")

        def _put():
            h = self._auth_headers(with_session=True)
            h["Content-Type"] = "video/mp4"
            with open(video_path, "rb") as f:
                return _rq().put(url, data=f, headers=h, timeout=(10, 900))

        r = _put()
        if r.status_code == 401:
            logger.info("401 on direct upload → refresh token and retry")
            self._fetch_token()
            r = _put()
        r.raise_for_status()
        return r.json()

    def upload_own(self, run_id: str, video_path: str) -> dict:
        """[C5] Gửi lại video KHÔNG cần session-token — chỉ device token + quyền
        sở hữu run. Dùng khi service restart/cúp điện làm mất session-token trong
        RAM (video quay xong SYS-04 pending không còn token để gửi qua /upload).
        Server (/upload-own) tự mark_uploaded. An toàn: server kiểm run.device_id
        == device.id nên chỉ gửi được run của chính máy này."""
        self._ensure_token()
        url = self._url(f"/api/device/runs/{run_id}/upload-own")

        def _put():
            h = self._auth_headers()   # KHÔNG kèm session
            h["Content-Type"] = "video/mp4"
            with open(video_path, "rb") as f:
                return _rq().put(url, data=f, headers=h, timeout=(10, 900))

        r = _put()
        if r.status_code == 401:
            self._fetch_token()
            r = _put()
        r.raise_for_status()
        return r.json()

    def complete_upload(self, run_id: str, object_key: str) -> dict:
        r = self._request(
            "POST", f"/api/device/runs/{run_id}/complete-upload",
            with_session=True, json={"object_key": object_key}, timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def cancel_run(self, run_id: str) -> dict:
        r = self._request(
            "POST", f"/api/device/runs/{run_id}/cancel",
            with_session=True, timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def cancel_own_run(self, run_id: str) -> dict:
        """Hủy run MỒ CÔI của chính máy này — KHÔNG cần session-token (token đó
        mất khi service chết giữa mẻ). Dùng bởi _clear_dangling_runs để Retry
        tự dọn orphan ngay thay vì kẹt SRV-07 chờ TTL server."""
        r = self._request(
            "POST", f"/api/device/runs/{run_id}/cancel-own", timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def get_run_status(self, run_id: str) -> dict:
        r = self._request("GET", f"/api/device/runs/{run_id}", timeout=5)
        r.raise_for_status()
        return r.json()

    def list_runs(self) -> list[dict]:
        """History of this device's runs (newest first)."""
        r = self._request("GET", "/api/device/runs", timeout=8)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def enroll(server_url: str, device_id: str, setup_token: str) -> dict:
        r = _rq().post(
            server_url.rstrip("/") + "/api/device/enroll",
            json={"device_id": device_id, "setup_token": setup_token},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

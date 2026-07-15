"""
Virtual tests for APIClient — the two changes behind the flickering icon:

  A. Token caching: a short-TTL token (server issues 120s) must NOT be re-minted
     on every call. The refresh skew is capped at half the token's own lifetime,
     so a 120s token refreshes at ~60s left, not on every heartbeat.
  B. heartbeat() classification: 200->True, 403 device_locked->"locked",
     401 device_not_found->"gone" (admin deleted), timeout/other->False.

No network: a fake `requests` module is injected via api_client._requests.

Run on the Pi:  ../.venv/bin/python test_api_client_token.py
"""
import sys
import time

import api_client
from api_client import APIClient, AuthError

_fails = []
def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        _fails.append(name)


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
    def json(self):
        return self._body


class FakeRequests:
    """Minimal stand-in for the `requests` module used by api_client."""
    def __init__(self):
        self.token_posts = 0
        self.hb_result = FakeResp(200, {})   # what /heartbeat returns
        self.token_status = 200              # what /token returns
        self.token_reason = None             # detail.reason when token_status != 200
        self.raise_on_hb = None              # Exception to raise on the heartbeat call

    # requests.post / requests.request
    def post(self, url, **kw):
        return self._route("POST", url, **kw)

    def request(self, method, url, **kw):
        return self._route(method, url, **kw)

    def _route(self, method, url, **kw):
        if url.endswith("/api/device/token"):
            self.token_posts += 1
            if self.token_status == 200:
                return FakeResp(200, {"access_token": "tok", "expires_in": 120})
            return FakeResp(self.token_status,
                            {"detail": {"ok": False, "reason": self.token_reason}})
        if url.endswith("/api/device/heartbeat"):
            if self.raise_on_hb:
                raise self.raise_on_hb
            return self.hb_result
        raise AssertionError("unexpected url " + url)


def new_client(fake):
    api_client._requests = fake                 # inject fake requests module
    return APIClient("https://x", "dev1", "key1")


print("[A] token is cached, not re-minted every call (120s TTL, skew capped)")
f = FakeRequests()
c = new_client(f)
c._ensure_token()
check("first call mints one token", f.token_posts == 1)
c._ensure_token()
c._ensure_token()
check("fresh token is NOT re-minted on later calls", f.token_posts == 1)
# simulate the token now having only 30s left (< capped skew of 60s) -> refresh
c._token_exp = time.time() + 30
c._ensure_token()
check("token refreshed when < half-life remains", f.token_posts == 2)

print("[B] heartbeat() classification")
# 200 -> True
f = FakeRequests(); c = new_client(f); c._access_token = "tok"; c._token_exp = time.time() + 1000
f.hb_result = FakeResp(200, {})
check("200 -> True", c.heartbeat() is True)

# 403 device_locked -> "locked"
f = FakeRequests(); c = new_client(f); c._access_token = "tok"; c._token_exp = time.time() + 1000
f.hb_result = FakeResp(403, {"detail": {"ok": False, "reason": "device_locked"}})
check("403 device_locked -> 'locked'", c.heartbeat() == "locked")

# 401 device_not_found (valid cached token, deleted mid-life) -> the _request
# 401-retry re-mints, /token also 401s device_not_found -> AuthError -> "gone"
f = FakeRequests(); c = new_client(f); c._access_token = "tok"; c._token_exp = time.time() + 1000
f.hb_result = FakeResp(401, {"detail": {"ok": False, "reason": "device_not_found"}})
f.token_status = 401; f.token_reason = "device_not_found"
check("401 device_not_found -> 'gone'", c.heartbeat() == "gone")

# token mint itself rejected with device_not_found (no cached token) -> "gone"
f = FakeRequests(); c = new_client(f)
f.token_status = 401; f.token_reason = "device_not_found"
check("token-mint device_not_found -> 'gone'", c.heartbeat() == "gone")

# a network timeout must be plain False (NEVER 'gone' — must not wipe creds)
f = FakeRequests(); c = new_client(f); c._access_token = "tok"; c._token_exp = time.time() + 1000
f.raise_on_hb = TimeoutError("read timed out")
check("timeout -> False (not 'gone')", c.heartbeat() is False)

# an unrelated auth failure is False, not 'gone'
f = FakeRequests(); c = new_client(f)
f.token_status = 401; f.token_reason = "invalid_device_key"
check("invalid_device_key -> False", c.heartbeat() is False)

print()
if _fails:
    print("RESULT: FAIL (%d) -> %s" % (len(_fails), ", ".join(_fails)))
    sys.exit(1)
print("RESULT: ALL PASS")

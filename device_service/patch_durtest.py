p = "test_durability_virtual.py"
s = open(p, encoding="utf-8").read()
old = '''print("[6] Upload of an EXPIRED/reaped run (server 4xx) -> 'gone': discard, unblock")
import requests as _rq
class _Resp: status_code = 403
def _http403(*a, **k):
    e = _rq.exceptions.HTTPError("403"); e.response = _Resp(); raise e
c._client.upload_direct = _http403
res = c._upload_with_retry("eeee", p)
check("upload result = 'gone' (permanent 4xx)", res == "gone")'''
new = '''print("[6] 4xx classification [M6]: only run_not_found -> gone; other 4xx KEEP video")
import requests as _rq
def _mk(status, reason):
    class _Resp:
        status_code = status
        def json(self): return {"detail": {"reason": reason}} if reason else {}
    def _raise(*a, **k):
        e = _rq.exceptions.HTTPError(str(status)); e.response = _Resp(); raise e
    return _raise
c._client.upload_direct = _mk(404, "run_not_found")
res = c._upload_with_retry("eeee", p)
check("run_not_found 404 -> gone", res == "gone")
c._client.upload_direct = _mk(403, "device_locked")
c._client.upload_own = _mk(403, "device_locked")
res = c._upload_with_retry("eeee", p)
check("device_locked 403 -> retry (NOT gone)", res == "retry")
check("video KEPT after device_locked", os.path.exists(p))
c._client.upload_direct = _mk(413, "")
c._client.upload_own = _mk(413, "")
res = c._upload_with_retry("eeee", p)
check("bare 413 -> retry (NOT gone)", res == "retry")
check("video KEPT after 413", os.path.exists(p))
def _own_ok(*a, **k): return {"ok": True}
c._client.upload_direct = _mk(403, "invalid_session")
c._client.upload_own = _own_ok
res = c._upload_with_retry("eeee", p)
check("invalid_session -> upload_own -> ok [C5]", res == "ok")'''
assert s.count(old) == 1, "pattern count %d" % s.count(old)
open(p, "w", encoding="utf-8").write(s.replace(old, new))
print("durability test patched OK")

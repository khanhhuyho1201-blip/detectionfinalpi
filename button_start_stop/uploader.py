"""
uploader.py — send a finished clip + metadata to the server (M6).

The server contract is NOT finalised yet (plan appendix item 5), so this is a
deliberately thin, single-endpoint POST. When you know the real API, adjust
_post() — that's the only place that talks to the network.

  * config.UPLOAD_ENABLED off  -> make_uploader() returns None; session keeps
    the clip locally and logs its path. Nothing else to configure.
  * config.UPLOAD_ENABLED on    -> POST multipart/form-data to UPLOAD_URL:
        file=<the mp4>, meta=<json of batch_id/total/count/error/...>
    with a few retries on failure.

If your server uses the device_service flow instead (start_run -> presigned
upload-url -> PUT -> complete-upload), drop that client in here behind the same
make_uploader() interface — session.py only needs a callable(path, meta)->bool.
"""

import json
import logging
import time

import config

logger = logging.getLogger("bss.upload")

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


def make_uploader():
    """Return a callable(video_path, meta) -> bool, or None when disabled."""
    if not config.UPLOAD_ENABLED:
        return None
    if requests is None:
        logger.error("requests not installed — upload disabled")
        return None
    if not config.UPLOAD_URL:
        logger.error("BSS_UPLOAD_URL not set — upload disabled")
        return None
    return _upload_with_retry


def _upload_with_retry(video_path: str, meta: dict) -> bool:
    for attempt in range(1, config.UPLOAD_MAX_RETRIES + 1):
        try:
            if _post(video_path, meta):
                logger.info("upload ok on attempt %d (%s)", attempt, meta.get("batch_id"))
                return True
        except Exception as e:
            logger.warning("upload attempt %d/%d failed: %s",
                           attempt, config.UPLOAD_MAX_RETRIES, e)
        if attempt < config.UPLOAD_MAX_RETRIES:
            time.sleep(config.UPLOAD_RETRY_DELAY)
    return False


def _post(video_path: str, meta: dict) -> bool:
    """The one network call — adjust to match the real server contract."""
    with open(video_path, "rb") as f:
        files = {"file": (meta.get("batch_id", "clip") + ".mp4", f, "video/mp4")}
        data = {"meta": json.dumps(meta)}
        r = requests.post(config.UPLOAD_URL, files=files, data=data, timeout=120)
    return 200 <= r.status_code < 300

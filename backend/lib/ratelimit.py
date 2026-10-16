"""In-memory sliding-window rate limiting for public endpoints.

Mirrors the bookkeeping app's fallback limiter (Redis there is a fast-follow
here; the interface is the same: bucket name + limit + window). Limits are
module-level so deployments/tests can tune them via ``RATE_LIMITS``.
"""
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

# bucket -> (max_requests, window_seconds)
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "widget_config": (120, 60),
    "widget_create": (20, 60),
    "widget_message": (60, 60),
    "widget_read": (120, 60),
    "widget_csat": (10, 60),
    "auth_login": (10, 60),
    "channel_email": (120, 60),
    "channel_sms": (120, 60),
    "channel_voice": (60, 60),
}

_hits: dict[str, deque] = defaultdict(deque)


def _allow(key: str, limit: int, window: int) -> bool:
    now_t = time.monotonic()
    q = _hits[key]
    while q and q[0] <= now_t - window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now_t)
    return True


def reset() -> None:
    _hits.clear()


def check(request: Request, bucket: str, scope: str = "") -> None:
    """Raise 429 when the caller exceeds the bucket limit."""
    limit, window = RATE_LIMITS[bucket]
    client = request.client.host if request.client else "unknown"
    key = f"{bucket}:{scope or client}"
    if not _allow(key, limit, window):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please slow down.",
            headers={"Retry-After": str(window)},
        )

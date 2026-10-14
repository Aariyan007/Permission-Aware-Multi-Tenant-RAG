import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request

from app.auth import current_principal
from app.db import Principal
from app.observability import RATE_LIMITED


class TenantRateLimiter:
    """Sliding-window limiter, per tenant, in process memory. Fine for one replica;
    use Redis (or a Postgres counter) to share the budget across replicas."""

    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, tenant: str, limit: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self.hits[tenant]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def enforce_rate_limit(request: Request, p: Principal = Depends(current_principal)) -> Principal:
    s = request.app.state.settings
    if not request.app.state.limiter.allow(str(p.tenant_id), s.rate_limit_per_minute):
        RATE_LIMITED.labels(str(p.tenant_id)).inc()
        raise HTTPException(429, "tenant rate limit exceeded")
    return p

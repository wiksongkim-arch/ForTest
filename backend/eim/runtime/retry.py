"""EIM 连接与投递的有界重试规则。"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Callable


def subscription_attempt_limit(retryable: bool | None) -> int:
    """落实 DWS ready 前的 0/2/1 额外尝试预算。"""

    return 1 if retryable is False else 3 if retryable is True else 2


def delivery_retry_at(
    attempts: int,
    *,
    now: datetime | None = None,
    jitter: Callable[[float, float], float] = random.uniform,
) -> str:
    """指数退避并加入最多 20% 抖动，单次等待不超过一小时。"""

    normalized = max(1, int(attempts))
    base = min(3_600.0, 2.0 ** min(normalized, 11))
    delay = base + jitter(0.0, base * 0.2)
    return ((now or datetime.now(UTC)) + timedelta(seconds=delay)).isoformat(
        timespec="milliseconds"
    )


def retry_exhausted(attempts: int, created_at: str, *, now: datetime | None = None) -> bool:
    """默认最多 5 次或 24 小时，以先到者为准。"""

    if int(attempts) >= 5:
        return True
    try:
        created = datetime.fromisoformat(created_at).astimezone(UTC)
    except ValueError:
        return True
    return (now or datetime.now(UTC)) - created >= timedelta(hours=24)

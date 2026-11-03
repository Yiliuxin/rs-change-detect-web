# -*- coding: utf-8 -*-
"""同步阻塞串行限流占位（REQ-13 / EX7，B 级不进 MVP 验收）。

``MAX_CONCURRENCY=1``：用 ``threading.BoundedSemaphore`` + ``acquire(blocking=False)``，
占用时立即抛 EX7；不引入队列 / 异步 / 线程池（铁律）。包住 /result/change 与
/report/export 的耗时段，``with``/``finally`` 保证释放。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from app.api.errors import ApiError
from app.config.constants import MAX_CONCURRENCY

_semaphore = threading.BoundedSemaphore(MAX_CONCURRENCY)

__all__ = ["serial"]


@contextmanager
def serial(run_id: Optional[str] = None) -> Iterator[None]:
    """占用唯一处理槽；已被占用 → EX7（429）。"""
    acquired = _semaphore.acquire(blocking=False)
    if not acquired:
        raise ApiError("EX7", run_id=run_id)
    try:
        yield
    finally:
        _semaphore.release()

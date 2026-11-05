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

__all__ = ["serial", "try_acquire", "release"]


def try_acquire() -> bool:
    """非阻塞抢占处理槽；已被占用返回 False（异步受理时用于立即判 EX7）。"""
    return _semaphore.acquire(blocking=False)


def release() -> None:
    """释放处理槽（后台线程结束时调用，保证不泄漏）。"""
    try:
        _semaphore.release()
    except (ValueError, RuntimeError):
        # 重复释放（理论上不该发生）静默忽略
        pass


@contextmanager
def serial(run_id: Optional[str] = None) -> Iterator[None]:
    """占用唯一处理槽；已被占用 → EX7（409）。"""
    acquired = _semaphore.acquire(blocking=False)
    if not acquired:
        raise ApiError("EX7", run_id=run_id)
    try:
        yield
    finally:
        _semaphore.release()

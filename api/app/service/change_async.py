# -*- coding: utf-8 -*-
"""变化检测异步化 + 阶段进度（REQ-13 完整实现）。

与 ``online_fetch`` 同一套实现风格：进程内 ``_tasks`` 字典 + 锁 + ``threading.Thread``
后台执行，不引入 celery / redis / 异步队列（铁律）。

``concurrency`` 闸门（``MAX_CONCURRENCY=1``）在**受理时**同步抢占：超限 → EX7(409)
立即返回；抢占成功后后台线程持有闸门直至管线结束（``finally`` 释放），保证 MVP
并发上限 1 不被破坏（REQ-13）。

进度按 5 阶段推进（与 HLD / 用户口径一致）：
    读裁切 → 掩膜 → 指数 → 变化检测 → 渲染导出
写入进程内 task 记录（status / progress / error / result），供
``GET /api/v1/result/change/task/{task_id}`` 轮询。任务标识 ``task_id`` 即 ``run_id``。

状态口径（沿用 online_fetch）：``PENDING → RUNNING → SUCCESS / FAILED``。
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from app.api.errors import ApiError, as_api_error
from app.config.constants import ERROR_MESSAGES
from app.repository import task_repo
from app.service import change_service
from app.service.concurrency import release as _release_gate
from app.service.concurrency import try_acquire as _try_acquire_gate

logger = logging.getLogger(__name__)

STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"

# 5 阶段（顺序即进度 step；与 HLD / 用户口径一致）
STAGES: List[str] = [
    "read_clip",       # 读裁切
    "mask",            # 掩膜
    "index",           # 指数
    "change_detect",   # 变化检测
    "render_export",   # 渲染导出
]
STAGE_LABELS: Dict[str, str] = {
    "read_clip": "读裁切",
    "mask": "掩膜",
    "index": "指数",
    "change_detect": "变化检测",
    "render_export": "渲染导出",
}

# 进程内任务表（Thread + 锁；不引入 celery/redis，与 online_fetch 一致）
_tasks: Dict[str, dict] = {}
_lock = threading.RLock()

__all__ = ["STAGES", "STAGE_LABELS", "start_change", "get_status", "wait"]


# --------------------------------------------------------------------------- #
# 进程内任务记录（与 online_fetch 同一套：字典 + 锁，不落 DDL）
# --------------------------------------------------------------------------- #
def _new_record(task_id: str) -> dict:
    with _lock:
        rec = {
            "task_id": task_id,
            "status": STATUS_PENDING,
            "progress": {"stage": "", "step": 0, "total": len(STAGES)},
            "error": None,
            "result": None,
            "thread": None,
        }
        _tasks[task_id] = rec
        return rec


def _patch(task_id: str, **fields) -> None:
    with _lock:
        rec = _tasks.get(task_id)
        if rec is not None:
            rec.update(fields)


def _stage(task_id: str, name: str) -> None:
    """推进到指定阶段（name 不在 5 阶段内则忽略）。"""
    if name not in STAGES:
        return
    with _lock:
        rec = _tasks.get(task_id)
        if rec is not None:
            rec["progress"] = {
                "stage": name,
                "step": STAGES.index(name) + 1,
                "total": len(STAGES),
            }


# --------------------------------------------------------------------------- #
# 对外入口：受理（同步）+ 后台线程
# --------------------------------------------------------------------------- #
def start_change(run_id: str, index: str = "NDVI", block_size: int = 256,
                 threshold: Optional[float] = None) -> dict:
    """受理异步变化检测：先过并发闸门（超限 → EX7 409），立即返回 PENDING。

    后台线程持有 ``concurrency`` 闸门运行 ``change_service.run_pipeline`` 完整管线，
    task_id 与 run_id 一致，便于与 task 表 / 产物目录一一对应。
    """
    # 受理时同步抢占并发闸门；超限 → EX7(409)
    if not _try_acquire_gate():
        raise ApiError("EX7", run_id=run_id)
    rec = _new_record(run_id)
    t = threading.Thread(
        target=_run_change,
        args=(run_id, index, block_size, threshold),
        name=f"change-async-{str(run_id)[:8]}",
        daemon=True,
    )
    rec["thread"] = t
    t.start()
    return {"run_id": run_id, "task_id": run_id, "status": STATUS_PENDING}


def _run_change(run_id: str, index: str, block_size: int,
                threshold: Optional[float]) -> None:
    """后台主流程：RUNNING →（SUCCESS | FAILED），finally 释放并发闸门。"""
    try:
        _patch(run_id, status=STATUS_RUNNING)

        def on_stage(name: str) -> None:
            _stage(run_id, name)

        # 完整管线；闸门由本线程在 start_change 时持有，此处不再重复抢占
        result = change_service.run_pipeline(
            run_id, index, block_size, threshold, on_stage=on_stage,
        )
        _stage(run_id, STAGES[-1])  # 收尾阶段标记完成
        _patch(run_id, status=STATUS_SUCCESS, result=dict(result))
        logger.info("异步变化检测完成 task_id=%s", run_id)
    except ApiError as exc:
        _patch(run_id, status=STATUS_FAILED,
               error={"code": exc.code, "message": exc.message})
        logger.warning("异步变化检测失败 task_id=%s code=%s", run_id, exc.code)
    except Exception as exc:  # noqa: BLE001 - 兜底归一
        mapped = as_api_error(exc, run_id=run_id)
        _patch(run_id, status=STATUS_FAILED,
               error={"code": mapped.code, "message": mapped.message})
        logger.warning("异步变化检测异常 task_id=%s code=%s", run_id, mapped.code)
    finally:
        _release_gate()


# --------------------------------------------------------------------------- #
# 对外入口：状态查询（轮询）
# --------------------------------------------------------------------------- #
def get_status(task_id: str) -> dict:
    """GET 轮询：返回 {task_id, status, progress, error, result}。

    未知 task_id → EX2(404)；task 表存在但不在内存（服务重启 / 同步 run）时合成基本视图。
    """
    task_id = str(task_id or "").strip()
    with _lock:
        rec = _tasks.get(task_id)
        if rec is not None:
            view = {
                "task_id": rec["task_id"],
                "status": rec["status"],
                "progress": dict(rec["progress"]),
                "error": dict(rec["error"]) if rec["error"] else None,
                "result": dict(rec["result"]) if rec["result"] else None,
            }
        else:
            view = None
    if view is not None:
        return view

    # 回退：task 表存在但内存无记录（服务重启 / 同步 run）
    task = task_repo.get_task(task_id)
    if task is None:
        raise ApiError("EX2", http_status=404, detail=f"未知 task_id：{task_id!r}")
    return {
        "task_id": task_id,
        "status": task["status"],
        "progress": None,
        "error": (
            {"code": task["error_code"],
             "message": ERROR_MESSAGES.get(task["error_code"], task["error_code"])}
            if task["error_code"] else None
        ),
        "result": None,
    }


def wait(task_id: str, timeout: Optional[float] = None) -> bool:
    """等待后台线程结束（测试 / 调用方需要同步结果时使用）。"""
    with _lock:
        rec = _tasks.get(task_id)
        t = rec.get("thread") if rec else None
    if t is None:
        return True
    t.join(timeout=timeout)
    return not t.is_alive()

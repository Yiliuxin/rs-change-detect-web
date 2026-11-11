# -*- coding: utf-8 -*-
"""运行记录数据访问（REQ-13 / REQ-14）。

params_snapshot 为 JSONB（含公式/版次/阈值/块大小，支撑可复现 NF9）；
status 由服务层显式写入（HLD 3.5 不使用触发器）。
"""
from __future__ import annotations

from typing import Optional

from app.repository.db import Json, db_cursor

__all__ = [
    "insert_task", "get_task", "find_run_id_by_aoi",
    "update_status", "update_snapshot",
]


def insert_task(run_id: str, aoi_id: str, scene_t1_id: str, scene_t2_id: str,
                params_snapshot: dict, commit_hash: Optional[str] = None,
                user_id: Optional[str] = None) -> str:
    """新建 task（status=PENDING），返回 run_id；user_id 为归属用户（认证扩展）。"""
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO task
                (run_id, aoi_id, scene_t1_id, scene_t2_id,
                 params_snapshot, commit_hash, status, user_id)
            VALUES (%s, %s, %s, %s, %s, %s, 'PENDING', %s)
            RETURNING run_id
            """,
            (run_id, aoi_id, scene_t1_id, scene_t2_id,
             Json(params_snapshot), commit_hash, user_id),
        )
        return str(cur.fetchone()["run_id"])


def get_task(run_id: str) -> Optional[dict]:
    """取 task 全字段（含 params_snapshot）；不存在返回 None。"""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT run_id::text AS run_id, aoi_id::text AS aoi_id,
                   scene_t1_id::text AS scene_t1_id,
                   scene_t2_id::text AS scene_t2_id,
                   params_snapshot, commit_hash, status, error_code,
                   started_at, finished_at,
                   user_id::text AS user_id
            FROM task WHERE run_id::text = %s
            """,
            (run_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def find_run_id_by_aoi(aoi_id: str) -> Optional[str]:
    """按 aoi_id 取最近一条 task 的 run_id（供 /scene/match 复用）。"""
    with db_cursor() as cur:
        cur.execute(
            "SELECT run_id::text AS run_id FROM task "
            "WHERE aoi_id::text = %s ORDER BY started_at DESC NULLS LAST LIMIT 1",
            (aoi_id,),
        )
        row = cur.fetchone()
        return row["run_id"] if row else None


def update_status(run_id: str, status: str, *,
                  started_at=None, finished_at=None,
                  error_code: Optional[str] = None) -> None:
    """显式流转状态：RUNNING / SUCCESS / FAILED（REQ-13）。"""
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE task SET
                status = %s,
                started_at  = COALESCE(%s, started_at),
                finished_at = COALESCE(%s, finished_at),
                error_code  = %s
            WHERE run_id::text = %s
            """,
            (status, started_at, finished_at, error_code, run_id),
        )


def update_snapshot(run_id: str, params_snapshot: dict) -> None:
    """回写参数快照（阈值/告警/统计等，REQ-14）。"""
    with db_cursor() as cur:
        cur.execute(
            "UPDATE task SET params_snapshot = %s WHERE run_id::text = %s",
            (Json(params_snapshot), run_id),
        )

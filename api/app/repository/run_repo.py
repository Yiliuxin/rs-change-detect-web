# -*- coding: utf-8 -*-
"""历史运行记录数据访问（REQ-13 / REQ-14）。

只读 SQL（不写 DDL）：以 ``task`` 为主表，JOIN ``aoi`` 取创建时间与 bbox、
JOIN ``scene`` 取双时相日期与数据源、LEFT JOIN ``change_result`` 聚合三类面积。
``task`` 表无 created_at 列（见 migrations/003），故以一一对应的 ``aoi.created_at``
作为 run 创建时间，并按其倒序分页。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from app.repository.db import db_cursor

__all__ = ["list_runs", "get_run", "stats_for_run"]

_RUN_SELECT = """
    SELECT t.run_id::text AS run_id,
           t.aoi_id::text AS aoi_id,
           t.status, t.error_code, t.params_snapshot,
           a.created_at,
           ST_XMin(a.aoi_geom) AS minx, ST_YMin(a.aoi_geom) AS miny,
           ST_XMax(a.aoi_geom) AS maxx, ST_YMax(a.aoi_geom) AS maxy,
           s1.acq_date::text AS t1_date, s1.cog_path AS t1_source,
           s1.sensor AS t1_sensor,
           s2.acq_date::text AS t2_date, s2.cog_path AS t2_source,
           s2.sensor AS t2_sensor,
           COALESCE(cr.total_area_ha, 0) AS total_area_ha,
           COALESCE(cr.total_pixel_count, 0) AS total_pixel_count
    FROM task t
    JOIN aoi a ON a.aoi_id = t.aoi_id
    LEFT JOIN scene s1 ON s1.scene_id = t.scene_t1_id
    LEFT JOIN scene s2 ON s2.scene_id = t.scene_t2_id
    LEFT JOIN (
        SELECT run_id,
               SUM(area_ha) AS total_area_ha,
               SUM(pixel_count) AS total_pixel_count
        FROM change_result GROUP BY run_id
    ) cr ON cr.run_id = t.run_id
"""


def list_runs(limit: int, offset: int,
              user_id: Optional[str] = None) -> Tuple[int, List[dict]]:
    """按 ``aoi.created_at`` 倒序分页，返回 ``(total, rows)``。

    ``user_id`` 非空时仅返回该用户的 run（注册/登录扩展：数据按人隔离）。
    """
    where = "WHERE t.user_id::text = %s" if user_id else ""
    params: tuple = (user_id,) if user_id else ()
    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM task t {where}", params)
        total = int(cur.fetchone()["n"])
        cur.execute(
            _RUN_SELECT + f" {where} "
            " ORDER BY a.created_at DESC, t.run_id DESC LIMIT %s OFFSET %s",
            params + (int(limit), int(offset)),
        )
        return total, [dict(r) for r in cur.fetchall()]


def get_run(run_id: str, user_id: Optional[str] = None) -> Optional[dict]:
    """取单条 run 的联表详情；不存在或非本人（user_id 给定时）返回 None（上层 → EX2 404）。"""
    sql = _RUN_SELECT + " WHERE t.run_id::text = %s"
    params: tuple = (run_id,)
    if user_id:
        sql += " AND t.user_id::text = %s"
        params = (run_id, user_id)
    with db_cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def stats_for_run(run_id: str) -> List[dict]:
    """取该 run 已入库的三类统计（1 增强 / 2 退化 / 3 稳定）。"""
    with db_cursor() as cur:
        cur.execute(
            "SELECT change_class, pixel_count, area_ha FROM change_result "
            "WHERE run_id::text = %s ORDER BY change_class",
            (run_id,),
        )
        return [dict(r) for r in cur.fetchall()]

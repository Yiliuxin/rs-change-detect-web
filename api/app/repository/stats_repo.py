# -*- coding: utf-8 -*-
"""平台总览仪表盘数据访问（批次 H / REQ-13 扩展）。

只读 SQL（不写 DDL）：以 ``task`` 为主表，JOIN ``aoi`` 取 run 创建时间，
LEFT JOIN ``scene`` 取地名派生所需的 t1 COG 路径与 t2 成像日期，LEFT JOIN
``change_result`` 按 ``change_class`` 1/2/3 聚合三类面积（与
``run_repo._RUN_SELECT`` / ``change_result`` 表同口径：ha、SUM(area_ha)）。

要点：
  - ``user_id`` 非空时 ``t.user_id::text = %s`` 仅统计本人，与 ``run_repo`` 一致；
  - 每个 run 只返回一行（change_result 在子查询里先按 run_id 聚合），不会因
    三类明细 JOIN 放大 run 计数；
  - ``class_count`` 为该 run 实际入库的类别数，服务层据此判定「三类不全 /
    无 change_result 行」→ 计入 skipped 且不补算；
  - 不读 experiments.json，不重算任何历史 run，全部参数用 ``%s`` 占位。
"""
from __future__ import annotations

from typing import List, Optional

from app.repository.db import db_cursor

__all__ = ["overview_rows"]

_OVERVIEW_SELECT = """
    SELECT t.run_id::text AS run_id,
           t.aoi_id::text AS aoi_id,
           t.params_snapshot->>'index' AS index,
           COALESCE(NULLIF(t.params_snapshot->>'method', ''), 'otsu') AS method,
           t.params_snapshot->>'threshold' AS threshold,
           a.created_at AS created_at,
           s1.cog_path AS t1_source,
           s2.acq_date::text AS t2_date,
           COALESCE(cr.gain_ha, 0) AS gain_ha,
           COALESCE(cr.loss_ha, 0) AS loss_ha,
           COALESCE(cr.stable_ha, 0) AS stable_ha,
           COALESCE(cr.class_count, 0) AS class_count
    FROM task t
    JOIN aoi a ON a.aoi_id = t.aoi_id
    LEFT JOIN scene s1 ON s1.scene_id = t.scene_t1_id
    LEFT JOIN scene s2 ON s2.scene_id = t.scene_t2_id
    LEFT JOIN (
        SELECT run_id,
               SUM(area_ha) FILTER (WHERE change_class = 1) AS gain_ha,
               SUM(area_ha) FILTER (WHERE change_class = 2) AS loss_ha,
               SUM(area_ha) FILTER (WHERE change_class = 3) AS stable_ha,
               COUNT(DISTINCT change_class) AS class_count
        FROM change_result GROUP BY run_id
    ) cr ON cr.run_id = t.run_id
"""


def overview_rows(user_id: Optional[str] = None) -> List[dict]:
    """取本人（或认证关闭时全部）run 的总览明细，按 ``aoi.created_at`` 倒序。

    ``user_id`` 为 None 时不加归属条件，语义与 ``run_repo.list_runs`` /
    ``AUTH_DISABLED=1`` 直通模式一致；有值时 ``%s`` 参数化过滤，杜绝注入。
    """
    params: tuple = ()
    where = ""
    if user_id:
        where = " WHERE t.user_id::text = %s"
        params = (user_id,)
    sql = _OVERVIEW_SELECT + where + " ORDER BY a.created_at DESC, t.run_id DESC"
    with db_cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

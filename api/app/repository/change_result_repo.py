# -*- coding: utf-8 -*-
"""变化结果统计数据访问（REQ-08 / REQ-11 / REQ-12）。

同一 run_id 每类（1 增强 / 2 退化 / 3 稳定）仅一条，用
``ON CONFLICT (run_id, change_class) DO UPDATE`` 幂等写入。
"""
from __future__ import annotations

import uuid
from typing import List, Optional

from app.repository.db import db_cursor

__all__ = ["upsert_stats"]


def upsert_stats(run_id: str, stats: List[dict],
                 raster_path: Optional[str] = None,
                 png_path: Optional[str] = None) -> int:
    """把 change_stats 的三类统计写入 change_result，返回写入行数。"""
    if not stats:
        return 0
    with db_cursor() as cur:
        for it in stats:
            cur.execute(
                """
                INSERT INTO change_result
                    (result_id, run_id, change_class, pixel_count, area_ha,
                     raster_path, png_path)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, change_class) DO UPDATE SET
                    pixel_count = EXCLUDED.pixel_count,
                    area_ha     = EXCLUDED.area_ha,
                    raster_path = EXCLUDED.raster_path,
                    png_path    = EXCLUDED.png_path
                """,
                (
                    str(uuid.uuid4()), run_id,
                    int(it["change_class"]), int(it["pixel_count"]),
                    float(it["area_ha"]), raster_path, png_path,
                ),
            )
    return len(stats)

# -*- coding: utf-8 -*-
"""验证样本数据访问（REQ-09 / EX10）。

geom 用 ``ST_SetSRID(ST_MakePoint(lon,lat),4326)``；px_x/px_y 为像元列/行 INTEGER；
ref_class 中文 CHECK（增强/退化/稳定）。累计样本按 run_id 聚合，供 EX10 判定。
"""
from __future__ import annotations

import uuid
from typing import Dict, List

from app.repository.db import db_cursor

__all__ = ["insert_samples", "list_samples", "count_by_class"]


def insert_samples(run_id: str, rows: List[Dict]) -> int:
    """批量入库样本，rows 每项含 lon/lat/px_x/px_y/ref_class(,label)。返回条数。"""
    if not rows:
        return 0
    with db_cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO sample
                    (sample_id, run_id, geom, px_x, px_y, ref_class, label)
                VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                        %s, %s, %s, %s)
                """,
                (
                    str(uuid.uuid4()), run_id,
                    float(r["lon"]), float(r["lat"]),
                    int(r["px_x"]), int(r["px_y"]),
                    str(r["ref_class"]), r.get("label"),
                ),
            )
    return len(rows)


def list_samples(run_id: str) -> List[Dict]:
    """取该 run_id 累计全部样本（含几何经纬度与像元坐标）。"""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT sample_id::text AS sample_id,
                   ST_X(geom) AS lon, ST_Y(geom) AS lat,
                   px_x, px_y, ref_class, label
            FROM sample WHERE run_id::text = %s
            ORDER BY created_at, sample_id
            """,
            (run_id,),
        )
        return [
            {
                "sample_id": r["sample_id"],
                "lon": float(r["lon"]), "lat": float(r["lat"]),
                "px_x": int(r["px_x"]), "px_y": int(r["px_y"]),
                "ref_class": r["ref_class"], "label": r.get("label"),
            }
            for r in cur.fetchall()
        ]


def count_by_class(run_id: str) -> Dict[str, int]:
    """按参考类别统计累计样本数（供 EX10 预检），键为三大类中文。"""
    counts: Dict[str, int] = {"增强": 0, "退化": 0, "稳定": 0}
    with db_cursor() as cur:
        cur.execute(
            "SELECT ref_class, COUNT(*) AS n FROM sample "
            "WHERE run_id::text = %s GROUP BY ref_class",
            (run_id,),
        )
        for r in cur.fetchall():
            counts[str(r["ref_class"])] = int(r["n"])
    return counts

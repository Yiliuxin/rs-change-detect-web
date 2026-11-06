# -*- coding: utf-8 -*-
"""场景数据访问（REQ-02 / REQ-04）。

要点（任务「坑」）：seed 的 ``scene.aoi_id`` 指向 seed 自己的 AOI 行，**不能**按
``scene.aoi_id = 新 aoi_id`` 过滤；必须用 ``footprint`` 与用户 AOI 做
``ST_Intersects(footprint, ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))`` 空间相交检索。
"""
from __future__ import annotations

from typing import List, Optional

from app.repository.db import db_cursor

# 注意：不查询 scene.radiometric_formula（seed 里存的是历史旧口径
# rho=(DN-1000)/10000）。定标一律由 constants.S2_FORMULA / to_reflectance 锁定，
# 服务层绝不接触该旧字段，避免误用 −1000 偏移。
_COLS = """
    scene_id::text AS scene_id, aoi_id::text AS aoi_id, sensor,
    acq_date, cloud_cover, cog_path, proc_baseline, tile,
    ST_XMin(footprint) AS minx, ST_YMin(footprint) AS miny,
    ST_XMax(footprint) AS maxx, ST_YMax(footprint) AS maxy
"""


def _normalize(row) -> dict:
    d = dict(row)
    acq = d.get("acq_date")
    if acq is not None and hasattr(acq, "isoformat"):
        d["acq_date"] = acq.isoformat()
    cc = d.get("cloud_cover")
    if cc is not None:
        d["cloud_cover"] = float(cc)
    for k in ("minx", "miny", "maxx", "maxy",
              "aoi_minx", "aoi_miny", "aoi_maxx", "aoi_maxy"):
        if d.get(k) is not None:
            d[k] = float(d[k])
    return d


def find_intersecting(geom_json: str) -> List[dict]:
    """与用户 AOI 几何空间相交的全部预置场景（按日期排序）。"""
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT {_COLS} FROM scene
            WHERE footprint IS NOT NULL
              AND ST_Intersects(footprint,
                                ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
            ORDER BY acq_date, cog_path
            """,
            (geom_json,),
        )
        return [_normalize(r) for r in cur.fetchall()]


def find_all() -> List[dict]:
    """列出全部预置场景（GET /scene/match 未带 AOI 时的兜底）。"""
    with db_cursor() as cur:
        cur.execute(f"SELECT {_COLS} FROM scene ORDER BY acq_date, cog_path")
        return [_normalize(r) for r in cur.fetchall()]


def find_seed_pairs() -> List[dict]:
    """预置影像对查询（REQ-02 / GET /scene/preset_aois 专用，只读）。

    与 ``find_all()`` 查询同一批 ``scene`` 行（不加 sensor 过滤，故含 S2 与 L8），
    但额外 JOIN 只取 **seed aoi**：即未被任何 ``task`` 引用、却拥有 scene 的 AOI，
    （``/aoi/select`` 每次会新建 aoi+task，不能混进预置清单）。同时返回
    ``aoi_*``（seed AOI 外接矩形）与 ``minx..maxy``（scene.footprint 外接矩形），
    供服务层按组还原 bbox：Landsat 组复用其它组的 seed aoi 作 FK（见
    ``data/fetch_landsat.py``），其 AOI 几何不代表真实范围，需以 footprint 为准。
    """
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT s.scene_id::text AS scene_id, s.aoi_id::text AS aoi_id, s.sensor,
                   s.acq_date, s.cloud_cover, s.cog_path, s.proc_baseline, s.tile,
                   ST_XMin(s.footprint) AS minx, ST_YMin(s.footprint) AS miny,
                   ST_XMax(s.footprint) AS maxx, ST_YMax(s.footprint) AS maxy,
                   ST_XMin(a.aoi_geom) AS aoi_minx, ST_YMin(a.aoi_geom) AS aoi_miny,
                   ST_XMax(a.aoi_geom) AS aoi_maxx, ST_YMax(a.aoi_geom) AS aoi_maxy
            FROM scene s
            JOIN aoi a ON a.aoi_id = s.aoi_id
            WHERE s.footprint IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM task t WHERE t.aoi_id = a.aoi_id)
            ORDER BY s.cog_path, s.acq_date
            """
        )
        return [_normalize(r) for r in cur.fetchall()]


def find_by_ids(scene_ids: List[str]) -> List[dict]:
    """按 scene_id 列表取场景（顺序与入参无关，按日期排序）。"""
    if not scene_ids:
        return []
    with db_cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM scene WHERE scene_id::text = ANY(%s) "
            "ORDER BY acq_date, cog_path",
            (list(scene_ids),),
        )
        return [_normalize(r) for r in cur.fetchall()]


def get_scene(scene_id: str) -> Optional[dict]:
    """取单景；不存在返回 None。"""
    rows = find_by_ids([scene_id])
    return rows[0] if rows else None

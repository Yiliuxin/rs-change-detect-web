# -*- coding: utf-8 -*-
"""AOI 数据访问（REQ-01 / REQ-02）。

写入用 ``ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)``；bbox 由 PostGIS 现算，
不落冗余列。全部参数化 SQL，不写 DDL。
"""
from __future__ import annotations

from typing import Optional, Tuple

from app.repository.db import db_cursor

__all__ = ["insert_aoi", "find_bbox", "find_aoi", "geom_geojson"]


def geom_geojson(aoi_id: str):
    """取 AOI 的 GeoJSON 文本（供 ``ST_GeomFromGeoJSON`` 复用做空间相交）。"""
    with db_cursor() as cur:
        cur.execute(
            "SELECT ST_AsGeoJSON(aoi_geom) AS gj FROM aoi WHERE aoi_id::text = %s",
            (aoi_id,),
        )
        row = cur.fetchone()
        return row["gj"] if row else None


def insert_aoi(aoi_id: str, geom_json: str, area_deg2: float) -> str:
    """插入 AOI（WGS84 Polygon），返回 aoi_id。"""
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO aoi (aoi_id, aoi_geom, area_deg2)
            VALUES (%s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s)
            RETURNING aoi_id
            """,
            (aoi_id, geom_json, float(area_deg2)),
        )
        return str(cur.fetchone()["aoi_id"])


def find_bbox(aoi_id: str) -> Optional[Tuple[float, float, float, float]]:
    """取 AOI 外接矩形 (minx, miny, maxx, maxy)；不存在返回 None。"""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT ST_XMin(aoi_geom) AS minx, ST_YMin(aoi_geom) AS miny,
                   ST_XMax(aoi_geom) AS maxx, ST_YMax(aoi_geom) AS maxy
            FROM aoi WHERE aoi_id::text = %s
            """,
            (aoi_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return (float(row["minx"]), float(row["miny"]),
                float(row["maxx"]), float(row["maxy"]))


def find_aoi(aoi_id: str) -> Optional[dict]:
    """确认 AOI 是否存在，返回精简元数据。"""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT aoi_id::text AS aoi_id, area_deg2,
                   ST_XMin(aoi_geom) AS minx, ST_YMin(aoi_geom) AS miny,
                   ST_XMax(aoi_geom) AS maxx, ST_YMax(aoi_geom) AS maxy
            FROM aoi WHERE aoi_id::text = %s
            """,
            (aoi_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "aoi_id": row["aoi_id"],
            "area_deg2": float(row["area_deg2"]),
            "bbox": (float(row["minx"]), float(row["miny"]),
                     float(row["maxx"]), float(row["maxy"])),
        }

# -*- coding: utf-8 -*-
"""在线取数入库数据访问（B 级扩展：在线取数分支，REQ-02 扩展）。

定位：只服务 ``POST /api/v1/scene/fetch`` 的后台入库，**不改** 5 个 MVP 路由/服务，
也不复制 5 表的既有读写口径（读仍复用 ``scene_repo`` / ``aoi_repo``）。

口径（逐条复用 ``scripts/seed_scenes.py``，保证在线/离线两条路数据同构）：
  * ``aoi_id = uuid5(NS, "aoi:<name>")``；
  * ``scene_id = uuid5(NS, "scene:<name>:<tag>:<source_id>")``；
  * ``cog_path = minio://scenes/pair_<name>/<tag>/``（目录前缀，非单文件）；
  * ``footprint = ST_MakeEnvelope(<AOI envelope>)``，供 ``ST_Intersects`` 检索；
  * ``radiometric_formula`` 取 ``constants.RADIOMETRIC_FORMULAS`` 锁定文本，绝不就地写死。

只写 DML，不写任何 DDL（表结构归 ``migrations/``）；全部 ``%s`` 参数化。
"""
from __future__ import annotations

from typing import Optional, Sequence

from app.repository.db import db_cursor

__all__ = [
    "upsert_aoi", "upsert_scene",
    "list_fetch_groups", "group_scene_ids", "group_aoi_ids",
    "delete_tasks_for_scenes", "delete_scenes", "delete_aois",
]


def upsert_aoi(aoi_id: str, bbox: Sequence[float], area_deg2: float) -> str:
    """按 uuid5 口径写 AOI（envelope，WGS84）；已存在仅更新面积。

    ``ON CONFLICT`` 分支与 seed 保持一致（只更新 ``area_deg2``、不改几何），
    避免在线取数复用了预置组名（poyang/farmland/nanchang）时把既有 AOI 几何改掉，
    从而不影响离线预置演示路径。
    """
    minx, miny, maxx, maxy = (float(v) for v in bbox)
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO aoi (aoi_id, aoi_geom, area_deg2)
            VALUES (%s, ST_MakeEnvelope(%s, %s, %s, %s, 4326), %s)
            ON CONFLICT (aoi_id) DO UPDATE SET area_deg2 = EXCLUDED.area_deg2
            RETURNING aoi_id::text AS aoi_id
            """,
            (aoi_id, minx, miny, maxx, maxy, float(area_deg2)),
        )
        return str(cur.fetchone()["aoi_id"])


def upsert_scene(scene_id: str, aoi_id: str, sensor: str, acq_date: str,
                 cloud_cover: Optional[float], cog_path: str,
                 proc_baseline: Optional[str], radiometric_formula: Optional[str],
                 tile: Optional[str], bbox: Sequence[float]) -> str:
    """按 uuid5 口径写 scene（幂等）；同景重取时刷新云量/路径/基线/景号/footprint。"""
    minx, miny, maxx, maxy = (float(v) for v in bbox)
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO scene (scene_id, aoi_id, sensor, acq_date, cloud_cover, cog_path,
                               proc_baseline, radiometric_formula, tile, footprint)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    ST_MakeEnvelope(%s, %s, %s, %s, 4326))
            ON CONFLICT (scene_id) DO UPDATE SET
                cloud_cover         = EXCLUDED.cloud_cover,
                cog_path            = EXCLUDED.cog_path,
                proc_baseline       = EXCLUDED.proc_baseline,
                radiometric_formula = EXCLUDED.radiometric_formula,
                tile                = EXCLUDED.tile,
                footprint           = EXCLUDED.footprint
            RETURNING scene_id::text AS scene_id
            """,
            (
                scene_id, aoi_id, sensor, acq_date,
                None if cloud_cover is None else float(cloud_cover),
                cog_path, proc_baseline, radiometric_formula, tile,
                minx, miny, maxx, maxy,
            ),
        )
        return str(cur.fetchone()["scene_id"])


# --------------------------------------------------------------------------- #
# 在线取数数据删除（用户主动清理，仅删在线取数组、不碰预置）
# --------------------------------------------------------------------------- #

# 在线取数组在 MinIO 的 cog_path 固定为 minio://scenes/pair_<name>/<tag>/。
# 用第 4 段（pair_<name>）分组；Landsat 预置组形如 landsat_<name>，不含 pair_ 前缀。
_FETCH_GROUP_SQL = """
    SELECT split_part(cog_path, '/', 4) AS grp,
           count(*) AS scene_count,
           min(acq_date) AS t1_date,
           max(acq_date) AS t2_date
    FROM scene
    WHERE cog_path LIKE 'minio://scenes/pair_%'
    GROUP BY 1
    ORDER BY 1
"""


def list_fetch_groups() -> list:
    """列出库中所有 ``pair_*`` 组（含预置与在线），供上层按白名单过滤。

    返回 ``[{group, scene_count, t1_date, t2_date}]``。刻意不在此处区分预置/
    在线——判据放在 service 层（预置名单），便于单测与口径集中。
    """
    with db_cursor() as cur:
        cur.execute(_FETCH_GROUP_SQL)
        rows = cur.fetchall()
    return [
        {
            "group": str(r["grp"]),
            "scene_count": int(r["scene_count"]),
            "t1_date": str(r["t1_date"]) if r["t1_date"] else "",
            "t2_date": str(r["t2_date"]) if r["t2_date"] else "",
        }
        for r in rows
    ]


def group_scene_ids(group: str) -> list:
    """取某 ``pair_<name>`` 组下的全部 scene_id（精确匹配组名段）。"""
    with db_cursor() as cur:
        cur.execute(
            "SELECT scene_id::text AS id FROM scene "
            "WHERE split_part(cog_path, '/', 4) = %s",
            (group,),
        )
        return [str(r["id"]) for r in cur.fetchall()]


def group_aoi_ids(group: str) -> list:
    """取某组 scene 关联的 aoi_id（去重）。"""
    with db_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT aoi_id::text AS id FROM scene "
            "WHERE split_part(cog_path, '/', 4) = %s",
            (group,),
        )
        return [str(r["id"]) for r in cur.fetchall()]


def delete_tasks_for_scenes(scene_ids: list) -> int:
    """删除以给定 scene 为 t1/t2 的检测任务（下游 change_result/sample 由 CASCADE 清理）。"""
    if not scene_ids:
        return 0
    with db_cursor() as cur:
        cur.execute(
            "DELETE FROM task WHERE scene_t1_id = ANY(%s::uuid[]) "
            "OR scene_t2_id = ANY(%s::uuid[])",
            (scene_ids, scene_ids),
        )
        return int(cur.rowcount)


def delete_scenes(scene_ids: list) -> int:
    """删除给定 scene（须先删引用它们的 task，否则 RESTRICT 报外键错）。"""
    if not scene_ids:
        return 0
    with db_cursor() as cur:
        cur.execute("DELETE FROM scene WHERE scene_id = ANY(%s::uuid[])", (scene_ids,))
        return int(cur.rowcount)


def delete_aois(aoi_ids: list) -> int:
    """删除不再被 scene/task 引用的 aoi（有引用则跳过，避免误删）。"""
    if not aoi_ids:
        return 0
    with db_cursor() as cur:
        cur.execute(
            "DELETE FROM aoi WHERE aoi_id = ANY(%s::uuid[]) "
            "AND NOT EXISTS (SELECT 1 FROM scene WHERE scene.aoi_id = aoi.aoi_id) "
            "AND NOT EXISTS (SELECT 1 FROM task WHERE task.aoi_id = aoi.aoi_id)",
            (aoi_ids,),
        )
        return int(cur.rowcount)

# -*- coding: utf-8 -*-
"""DB 测：迁移可重跑 / FK·CHECK 生效 / 空间相交（REQ-02/13/14；无结果=EX2）。"""
from __future__ import annotations

import uuid
from pathlib import Path

import psycopg2
import pytest

from app.repository import scene_repo
from app.repository.db import Json, db_cursor
from _helpers import POYANG_BBOX, POYANG_T1, POYANG_T2, geojson_polygon

MIGRATIONS = Path(r"C:\Test\Web\api\migrations")


@pytest.mark.req("REQ-14")
def test_migrations_are_idempotent_and_five_tables_exist():
    ups = sorted(MIGRATIONS.glob("*.up.sql"), key=lambda p: p.name)
    assert len(ups) == 5
    for _ in range(2):     # 连续重跑两次：IF NOT EXISTS 保证不报错
        with db_cursor() as cur:
            for f in ups:
                cur.execute(f.read_text(encoding="utf-8"))
    with db_cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name IN "
            "('aoi','scene','task','sample','change_result')"
        )
        names = {r["table_name"] for r in cur.fetchall()}
    assert names == {"aoi", "scene", "task", "sample", "change_result"}


@pytest.mark.req("REQ-14")
def test_foreign_keys_declared_according_to_hld_32():
    with db_cursor() as cur:
        cur.execute(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE constraint_type='FOREIGN KEY' AND table_schema='public' "
            "AND table_name IN ('scene','task','sample','change_result')"
        )
        names = {r["constraint_name"] for r in cur.fetchall()}
    assert {
        "fk_scene_aoi", "fk_task_aoi", "fk_task_t1", "fk_task_t2",
        "fk_sample_task", "fk_change_result_task",
    } <= names


@pytest.mark.req("REQ-02")
def test_spatial_intersect_returns_expected_and_empty_for_ex2():
    minx, miny, maxx, maxy = POYANG_BBOX
    inside = geojson_polygon((minx + 0.01, miny + 0.01, maxx - 0.01, maxy - 0.01))
    hits = scene_repo.find_intersecting(inside)
    assert len(hits) == 2
    assert all("pair_poyang" in h["cog_path"] for h in hits)
    assert sorted(h["acq_date"] for h in hits) == [POYANG_T1, POYANG_T2]
    # 按 footprint 相交检索（不按 scene.aoi_id 过滤），故能命中 seed 自己的 AOI 行
    assert all(h["aoi_id"] for h in hits)

    far = geojson_polygon((0.0, 0.0, 0.05, 0.05))
    assert scene_repo.find_intersecting(far) == []      # 无结果 → 上层 EX2


@pytest.mark.req("REQ-02")
@pytest.mark.req("REQ-14")
def test_check_and_fk_constraints_enforced():
    """CHECK（sensor/status/ref_class/change_class）与 FK 均生效；违例事务回滚。"""
    with db_cursor() as cur:
        cur.execute("SELECT aoi_id::text AS aoi_id FROM aoi LIMIT 1")
        aoi_id = cur.fetchone()["aoi_id"]
        cur.execute("SELECT scene_id::text AS scene_id FROM scene ORDER BY scene_id LIMIT 2")
        scenes = [r["scene_id"] for r in cur.fetchall()]
        cur.execute("SELECT run_id::text AS run_id FROM task LIMIT 1")
        task_row = cur.fetchone()
    assert len(scenes) == 2 and scenes[0] != scenes[1] and task_row
    run_id = task_row["run_id"]

    u = lambda: str(uuid.uuid4())  # noqa: E731
    cases = [
        ("INSERT INTO scene (scene_id,aoi_id,sensor,acq_date,cog_path) "
         "VALUES (%s,%s,%s,%s,%s)",
         (u(), aoi_id, "XX", "2023-01-01", "minio://scenes/x/"),
         psycopg2.errors.CheckViolation, "ck_scene_sensor"),
        ("INSERT INTO task (run_id,aoi_id,scene_t1_id,scene_t2_id,params_snapshot,status) "
         "VALUES (%s,%s,%s,%s,%s,%s)",
         (u(), aoi_id, scenes[0], scenes[1], Json({}), "BOGUS"),
         psycopg2.errors.CheckViolation, "ck_task_status"),
        ("INSERT INTO sample (sample_id,run_id,geom,px_x,px_y,ref_class) "
         "VALUES (%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326),%s,%s,%s)",
         (u(), run_id, 116.3, 29.2, 1, 1, "未知"),
         psycopg2.errors.CheckViolation, "ck_sample_ref_class"),
        ("INSERT INTO change_result (result_id,run_id,change_class,pixel_count,area_ha) "
         "VALUES (%s,%s,%s,%s,%s)",
         (u(), run_id, 9, 1, 0.1),
         psycopg2.errors.CheckViolation, "ck_change_class"),
        ("INSERT INTO sample (sample_id,run_id,geom,px_x,px_y,ref_class) "
         "VALUES (%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326),%s,%s,%s)",
         (u(), "00000000-0000-0000-0000-000000000999", 116.3, 29.2, 1, 1, "稳定"),
         psycopg2.errors.ForeignKeyViolation, "fk_sample_task"),
        ("INSERT INTO task (run_id,aoi_id,scene_t1_id,scene_t2_id,params_snapshot,status) "
         "VALUES (%s,%s,%s,%s,%s,%s)",
         (u(), "00000000-0000-0000-0000-000000000998",
          scenes[0], scenes[1], Json({}), "PENDING"),
         psycopg2.errors.ForeignKeyViolation, "fk_task_aoi"),
    ]
    for sql, params, exc_type, label in cases:
        with pytest.raises(exc_type):
            with db_cursor() as cur:
                cur.execute(sql, params)
        # db_cursor 异常时 rollback，不会留下脏数据

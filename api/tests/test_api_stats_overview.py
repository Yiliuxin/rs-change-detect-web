# -*- coding: utf-8 -*-
"""批次 H 接口测：GET /api/v1/stats/overview（REQ-13 扩展）。

覆盖：
  ① 新注册（无 run）用户 → 全零结构（recent 空数组、area 全 0、last_run_at null）；
  ② 多 run 汇总：不同 index/method、三类面积合计与 /runs、/runs/{id}
     的 total_area_ha 同口径一致；
  ③ 老 run 无 change_result 行 / 三类不全 → skipped 计数、不补算、recent 仍可见且面积 0；
  ④ 两个账号各造 run（真实 JWT）→ overview 只统计本人；
  ⑤ JWT 关闭（AUTH_DISABLED=1 直通）时无 token 请求不报错。

测试卫生：造数直接写 aoi/task/change_result，``params_snapshot.test_data=true``
且带 ``pytest_stats`` 标记；teardown 按 run_id/aoi_id 精确删除本用例造的数据，
并统一 ``task.user_id=NULL`` + 删除测试用户，绝不动真实 run、不碰 data/pair_*。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.main import app  # noqa: F401 - 导入即注册路由（与既有接口测一致）
from app.repository.db import db_cursor

OVERVIEW_URL = "/api/v1/stats/overview"
RUNS_URL = "/api/v1/runs"

# 预置 poyang 影像对（scene 已 seed，外键稳定；造数只读引用，不改 data/）
T1_SCENE_ID = "f4779d6f-38c7-5462-ba69-eadbeffb1faf"
T2_SCENE_ID = "74aab10e-6ee5-5bc1-9908-24284e9cdf49"
T1_COG = "minio://scenes/pair_poyang/t1/"
T2_COG = "minio://scenes/pair_poyang/t2/"
_BASE_AT = datetime(2024, 1, 1, tzinfo=timezone.utc)
_AOI_WKT = "POLYGON((116.26 29.16,116.27 29.16,116.27 29.17,116.26 29.17,116.26 29.16))"

pytestmark = pytest.mark.usefixtures("client")


@pytest.fixture()
def auth_on(monkeypatch):
    """临时启用 JWT 网关（风格与 test_api_auth.py / 批次 D/E 一致）。"""
    import app.main as main_mod
    assert main_mod.AUTH_DISABLED is True
    monkeypatch.setattr(main_mod, "AUTH_DISABLED", False)
    yield
    monkeypatch.undo()


def _new_user(client, prefix="pst_") -> dict:
    """注册带 pytest 前缀的新用户（用户名 ≤20 位，满足 users 表 CHECK）。"""
    username = prefix + uuid.uuid4().hex[:8]
    r = client.post("/api/v1/auth/register",
                    json={"username": username, "password": "pw123456"})
    assert r.status_code == 200, r.text
    body = r.json()
    return {"user_id": body["user_id"], "username": username,
            "auth": {"Authorization": "Bearer " + body["token"]}}


def _cleanup_user(username: str) -> None:
    """清理测试用户：先还原其所有 task.user_id 为 NULL，再删用户；不动 task 本身。"""
    with db_cursor() as cur:
        cur.execute("UPDATE task SET user_id = NULL WHERE user_id IN "
                    "(SELECT user_id FROM users WHERE username = %s)", (username,))
        cur.execute("DELETE FROM users WHERE username = %s", (username,))


@pytest.fixture
def seed_run():
    """工厂：在测试库造一条带 test_data 标记的 run（含可选 change_result）。

    ``areas`` 为 ``{change_class: area_ha}``；None / {} 表示无统计行（老 run）。
    ``seq`` 控制 aoi.created_at（BASE_AT + seq 小时）→ recent/last_run_at 顺序确定。
    teardown 按 run_id / aoi_id 精确删除，只删本用例创建的行。
    """
    created = {"run_ids": [], "aoi_ids": []}

    def make(user_id, index="NDVI", method="otsu", threshold=0.2,
             areas=None, seq=0, extra_snapshot=None) -> dict:
        run_id = str(uuid.uuid4())
        aoi_id = str(uuid.uuid4())
        created_at = _BASE_AT + timedelta(hours=seq)
        snap = {
            "index": index, "method": method, "threshold": threshold,
            "scene_cog_paths": {"t1": T1_COG, "t2": T2_COG},
            "test_data": True, "pytest_stats": True,
        }
        if extra_snapshot:
            snap.update(extra_snapshot)
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO aoi (aoi_id, aoi_geom, area_deg2, created_at) "
                "VALUES (%s, ST_GeomFromText(%s, 4326), %s, %s)",
                (aoi_id, _AOI_WKT, 0.0001, created_at),
            )
            cur.execute(
                "INSERT INTO task (run_id, aoi_id, scene_t1_id, scene_t2_id, "
                "params_snapshot, status, user_id) VALUES (%s, %s, %s, %s, %s::jsonb, 'SUCCESS', %s)",
                (run_id, aoi_id, T1_SCENE_ID, T2_SCENE_ID, json.dumps(snap), user_id),
            )
            for cls, area_ha in (areas or {}).items():
                cur.execute(
                    "INSERT INTO change_result (result_id, run_id, change_class, pixel_count, area_ha) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (str(uuid.uuid4()), run_id, int(cls),
                     int(round(float(area_ha) * 100)), float(area_ha)),
                )
        created["run_ids"].append(run_id)
        created["aoi_ids"].append(aoi_id)
        return {"run_id": run_id, "aoi_id": aoi_id,
                "created_at": created_at.isoformat(), "index": index, "method": method}

    yield make

    if created["run_ids"]:
        with db_cursor() as cur:
            cur.execute("DELETE FROM change_result WHERE run_id::text = ANY(%s)",
                        (created["run_ids"],))
            cur.execute("DELETE FROM task WHERE run_id::text = ANY(%s)",
                        (created["run_ids"],))
    if created["aoi_ids"]:
        with db_cursor() as cur:
            cur.execute("DELETE FROM aoi WHERE aoi_id::text = ANY(%s)",
                        (created["aoi_ids"],))


# --------------------------------------------------------------------------- #
# ① 空用户零值结构
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-13")
def test_overview_empty_user_returns_zero_structure(client, auth_on):
    """新用户无 run → 200 全零结构（不报错）。"""
    user = _new_user(client)
    try:
        r = client.get(OVERVIEW_URL, headers=user["auth"])
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["total_runs"] == 0
        assert b["by_index"] == {"NDVI": 0, "NDWI": 0}
        assert b["by_method"] == {}
        assert b["area"] == {"gain_ha": 0.0, "loss_ha": 0.0, "stable_ha": 0.0}
        assert b["total_area_ha"] == 0.0 and b["changed_area_ha"] == 0.0
        assert b["skipped"] == 0 and b["last_run_at"] is None
        assert b["recent"] == []
    finally:
        _cleanup_user(user["username"])


# --------------------------------------------------------------------------- #
# ② 多 run 汇总正确性 + 与 /runs、/runs/{id} 口径一致
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-13")
def test_overview_multi_run_aggregation_matches_runs(client, auth_on, seed_run):
    """不同 index/method 3 条 run：统计分布与三类面积合计正确，且与 /runs 一致。"""
    user = _new_user(client)
    try:
        r1 = seed_run(user["user_id"], "NDVI", "otsu", 0.2, {1: 1.25, 2: 2.50, 3: 3.75}, seq=0)
        r2 = seed_run(user["user_id"], "NDWI", "regression", 0.3, {1: 0.50, 2: 1.00, 3: 2.00}, seq=1)
        # method 缺省 → 按 otsu 口径；threshold 缺省 → recent.threshold=null
        r3 = seed_run(user["user_id"], "NDVI", None, None, {1: 2.00, 2: 0.00, 3: 4.00}, seq=2)

        r = client.get(OVERVIEW_URL, headers=user["auth"])
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["total_runs"] == 3
        assert b["by_index"] == {"NDVI": 2, "NDWI": 1}
        assert b["by_method"] == {"otsu": 2, "regression": 1}
        assert b["area"] == {"gain_ha": 3.75, "loss_ha": 3.50, "stable_ha": 9.75}
        assert b["total_area_ha"] == 17.0 and b["changed_area_ha"] == 7.25
        assert b["skipped"] == 0
        assert b["last_run_at"] == r3["created_at"]
        # recent 按 created_at 倒序，run_id 前 8 位、地名/指数/日期/阈值
        assert [it["run_id"] for it in b["recent"]] == [
            r3["run_id"][:8], r2["run_id"][:8], r1["run_id"][:8]]
        assert b["recent"][0]["aoi_name"] == "poyang"
        assert b["recent"][0]["index"] == "NDVI"
        assert b["recent"][0]["date"] == "2023-11-02"   # t2 成像日期
        assert b["recent"][0]["threshold"] is None

        # 与现有 /runs、/runs/{id} 的 total_area_ha 同口径（ha、SUM(area_ha)）
        runs = client.get(RUNS_URL, headers=user["auth"]).json()
        assert runs["total"] == 3
        per_run = {it["run_id"]: it["total_area_ha"] for it in runs["items"]}
        assert per_run == {r1["run_id"]: 7.5, r2["run_id"]: 3.5, r3["run_id"]: 6.0}
        detail = client.get(f"{RUNS_URL}/{r1['run_id']}", headers=user["auth"]).json()
        assert detail["total_area_ha"] == 7.5
        assert b["total_area_ha"] == round(sum(per_run.values()), 2)
    finally:
        _cleanup_user(user["username"])


# --------------------------------------------------------------------------- #
# ③ 老 run 缺统计被跳过（不补算）+ recent 仍可见且面积 0
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-13")
def test_overview_skips_runs_without_complete_stats(client, auth_on, seed_run):
    """无 change_result 行 / 三类不全 → skipped=2、不读快照补算、recent 面积 0。"""
    user = _new_user(client)
    try:
        # 老 run：无任何 change_result；快照里伪造 stats 验证后端不补算
        old = seed_run(user["user_id"], "NDVI", "otsu", 0.2, areas=None, seq=0,
                       extra_snapshot={"stats": [
                           {"change_class": 1, "pixel_count": 999999, "area_ha": 888.88},
                           {"change_class": 2, "pixel_count": 999999, "area_ha": 777.77},
                           {"change_class": 3, "pixel_count": 999999, "area_ha": 666.66},
                       ]})
        partial = seed_run(user["user_id"], "NDWI", "otsu", 0.25, {1: 5.00}, seq=1)
        ok = seed_run(user["user_id"], "NDVI", "otsu", 0.3, {1: 1.00, 2: 2.00, 3: 3.00}, seq=2)

        b = client.get(OVERVIEW_URL, headers=user["auth"]).json()
        assert b["total_runs"] == 3
        assert b["skipped"] == 2
        assert b["by_index"] == {"NDVI": 2, "NDWI": 1}
        assert b["area"] == {"gain_ha": 1.0, "loss_ha": 2.0, "stable_ha": 3.0}
        assert b["total_area_ha"] == 6.0 and b["changed_area_ha"] == 3.0
        # recent 三条都在：被跳过（无行 / 三类不全）的两条面积如实为 0 不补算，
        # 完整的那条正常显示三类面积
        recent = {it["run_id"]: it for it in b["recent"]}
        assert set(recent) == {old["run_id"][:8], partial["run_id"][:8], ok["run_id"][:8]}
        for rid in (old["run_id"][:8], partial["run_id"][:8]):
            assert recent[rid]["gain_ha"] == 0.0
            assert recent[rid]["loss_ha"] == 0.0
            assert recent[rid]["stable_ha"] == 0.0
        assert recent[ok["run_id"][:8]]["gain_ha"] == 1.0
        assert recent[ok["run_id"][:8]]["loss_ha"] == 2.0
        assert recent[ok["run_id"][:8]]["stable_ha"] == 3.0
    finally:
        _cleanup_user(user["username"])


# --------------------------------------------------------------------------- #
# ④ 两账号数据隔离（真实 JWT token）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-13")
def test_overview_isolated_between_users(client, auth_on, seed_run):
    """A 造 2 条、B 造 1 条 → 各自 overview 只统计本人。"""
    a = _new_user(client, "pst_a_")
    b = _new_user(client, "pst_b_")
    try:
        seed_run(a["user_id"], "NDVI", "otsu", 0.2, {1: 1, 2: 2, 3: 3}, seq=0)
        seed_run(a["user_id"], "NDWI", "otsu", 0.2, {1: 4, 2: 5, 3: 6}, seq=1)
        seed_run(b["user_id"], "NDVI", "regression", 0.4, {1: 10, 2: 20, 3: 30}, seq=2)

        oa = client.get(OVERVIEW_URL, headers=a["auth"]).json()
        ob = client.get(OVERVIEW_URL, headers=b["auth"]).json()
        assert oa["total_runs"] == 2 and ob["total_runs"] == 1
        assert oa["by_index"] == {"NDVI": 1, "NDWI": 1}
        assert oa["by_method"] == {"otsu": 2}
        assert oa["total_area_ha"] == 21.0
        assert ob["by_method"] == {"regression": 1}
        assert ob["total_area_ha"] == 60.0
        a_ids = {it["run_id"] for it in oa["recent"]}
        b_ids = {it["run_id"] for it in ob["recent"]}
        assert a_ids.isdisjoint(b_ids)
    finally:
        _cleanup_user(a["username"])
        _cleanup_user(b["username"])


# --------------------------------------------------------------------------- #
# ⑤ JWT 关闭时无 token 直通（兼容既有测试语义）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-13")
def test_overview_no_token_when_auth_disabled(client):
    """AUTH_DISABLED=1（conftest）下无 token 请求 → 200 且结构完整，不报错。"""
    r = client.get(OVERVIEW_URL)
    assert r.status_code == 200, r.text
    b = r.json()
    for key in ("total_runs", "by_index", "by_method", "area", "total_area_ha",
                "changed_area_ha", "skipped", "last_run_at", "recent"):
        assert key in b, (key, b)
    assert {"NDVI", "NDWI"} <= set(b["by_index"])
    assert isinstance(b["recent"], list) and len(b["recent"]) <= 10

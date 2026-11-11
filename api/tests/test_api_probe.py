# -*- coding: utf-8 -*-
"""B2 鼠标拾取探针 / B3 阶段内进度 接口与前置产物测试。

覆盖：正常 / 越界 200、他人 run → AUTH-03、未知 run → EX2、
产物缺失时字段 None 且绝不 500；并校验 B2 前置 index_t1/t2.tif
（float32 / nodata=NaN / deflate / 2 波段）与 _reg 隔离命名。
断言按 key 逐项校验，不依赖列表长度。
"""
from __future__ import annotations

import math
import uuid

import numpy as np
import pytest
import rasterio
from rasterio.warp import transform as warp_transform

from _helpers import POYANG_T1, POYANG_T2, poyang_geojson
from app.config.constants import INDEX_RASTER_T1, INDEX_RASTER_T2, METHOD_REGRESSION
from app.service import change_async, change_service
from app.service.common import ARTIFACT_MEDIA_TYPES, index_raster_names, run_dir

PROBE_URL = "/api/v1/result/change/probe"
_MEASURE_KEYS = ("t1_index", "t2_index", "delta", "change_class",
                 "intensity", "landcover_t1", "landcover_t2")

pytestmark = pytest.mark.usefixtures("client")


def _inside_coord(run_id: str):
    """从 index_t1.tif 里挑一个有限像元，把行列中心重投影回 WGS84。"""
    path = run_dir(run_id) / INDEX_RASTER_T1
    with rasterio.open(str(path)) as ds:
        finite = np.argwhere(np.isfinite(ds.read(1)))
        assert finite.size > 0, "指数栅格无有效像元"
        row, col = [int(v) for v in finite[len(finite) // 2]]
        x, y = ds.xy(row, col)
        lons, lats = warp_transform(ds.crs, "EPSG:4326", [x], [y])
    return float(lons[0]), float(lats[0])


def _assert_all_none(body: dict) -> None:
    assert body["in_bounds"] is False, body
    for key in _MEASURE_KEYS:
        assert key in body, key
        assert body[key] is None, (key, body[key])
    assert body["reason"]


@pytest.fixture()
def auth_on(monkeypatch):
    """临时启用 JWT 网关，验证 AUTH-03；与 test_api_auth.py 同一手法。"""
    import app.main as main_mod
    assert main_mod.AUTH_DISABLED is True
    monkeypatch.setattr(main_mod, "AUTH_DISABLED", False)
    yield
    monkeypatch.undo()


def _register(client, username: str):
    return client.post("/api/v1/auth/register",
                       json={"username": username, "password": "pw123456"})


def _cleanup_user(username: str) -> None:
    from app.repository.db import db_cursor
    with db_cursor() as cur:
        cur.execute("UPDATE task SET user_id = NULL WHERE user_id IN "
                    "(SELECT user_id FROM users WHERE username = %s)", (username,))
        cur.execute("DELETE FROM users WHERE username = %s", (username,))


# --------------------------------------------------------------------------- #
# B2 前置产物
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-12")
def test_probe_index_rasters_written_and_whitelisted(client, change_ready):
    """index_t1/t2.tif 落盘且 float32 / NaN / deflate / 2 波段；_reg 隔离命名。"""
    rid = change_ready["run_id"]
    out = run_dir(rid)
    names = index_raster_names("otsu")
    for base in (INDEX_RASTER_T1, INDEX_RASTER_T2):
        name = names[base]
        path = out / name
        assert path.is_file() and path.stat().st_size > 0, name
        with rasterio.open(str(path)) as ds:
            assert ds.count == 2, name
            assert ds.dtypes == ("float32", "float32"), name
            assert ds.nodata is not None and math.isnan(float(ds.nodata)), name
            assert str(ds.profile.get("compress", "")).lower() == "deflate", name
        assert name in ARTIFACT_MEDIA_TYPES
        assert ARTIFACT_MEDIA_TYPES[name] == "image/tiff"
        assert name in change_ready["change_body"]["artifacts"]
        got = client.get(change_ready["change_body"]["artifacts"][name])
        assert got.status_code == 200 and len(got.content) > 0, name
    reg_names = index_raster_names(METHOD_REGRESSION)
    assert reg_names[INDEX_RASTER_T1] == "index_t1_reg.tif"
    assert reg_names[INDEX_RASTER_T2] == "index_t2_reg.tif"
    for name in reg_names.values():
        assert name in ARTIFACT_MEDIA_TYPES


# --------------------------------------------------------------------------- #
# B2 接口
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-10")
def test_probe_normal_in_bounds_200(client, change_ready):
    """正常：返回全部字段、in_bounds=True，Δ 与 t2-t1 一致。"""
    rid = change_ready["run_id"]
    lng, lat = _inside_coord(rid)
    r = client.get(PROBE_URL, params={"run_id": rid, "lng": lng, "lat": lat})
    assert r.status_code == 200, r.text
    body = r.json()
    required = {"in_bounds", *_MEASURE_KEYS}
    assert required <= set(body), body
    assert body["in_bounds"] is True
    assert body["t1_index"] is not None and math.isfinite(float(body["t1_index"]))
    assert body["t2_index"] is not None and math.isfinite(float(body["t2_index"]))
    assert body["delta"] is not None and math.isfinite(float(body["delta"]))
    assert float(body["delta"]) == pytest.approx(
        float(body["t2_index"]) - float(body["t1_index"]), abs=1e-5)
    assert body["change_class"] in (0, 1, 2, 3)
    assert body["intensity"] in (0, 1, 2, 3, 4, 5)
    assert body["landcover_t1"] in (0, 1, 2, 3, 4)
    assert body["landcover_t2"] in (0, 1, 2, 3, 4)
    assert body["index"] == "NDVI"


@pytest.mark.req("REQ-10")
def test_probe_out_of_bounds_200_none(client, change_ready):
    """越界：200 + 所有测量字段 None，不 500。"""
    rid = change_ready["run_id"]
    r = client.get(PROBE_URL, params={"run_id": rid, "lng": 0.0, "lat": 0.0})
    assert r.status_code == 200, r.text
    _assert_all_none(r.json())


@pytest.mark.req("REQ-10")
def test_probe_unknown_run_ex2(client):
    """未知 run → EX2(404)（AUTH_DISABLED 下也要由 service 显式校验）。"""
    rid = "00000000-0000-0000-0000-000000000000"
    r = client.get(PROBE_URL, params={"run_id": rid, "lng": 116.30, "lat": 29.20})
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "EX2"


@pytest.mark.req("REQ-10")
def test_probe_missing_artifacts_none_no_500(client):
    """只建 task 未跑检测 → 产物缺失，字段 None、200、不 500。"""
    a = client.post("/api/v1/aoi/select", json={
        "aoi_geom": poyang_geojson(), "t1": POYANG_T1, "t2": POYANG_T2,
    })
    assert a.status_code == 200, a.text
    rid = a.json()["run_id"]
    r = client.get(PROBE_URL, params={"run_id": rid, "lng": 116.30, "lat": 29.20})
    assert r.status_code == 200, r.text
    _assert_all_none(r.json())


@pytest.mark.req("REQ-10")
def test_probe_other_users_run_auth03(client, auth_on, change_ready):
    """他人 run → AUTH-03(403)；本人带 token 仍 200。"""
    rid = change_ready["run_id"]
    # username 上限 20 位（AUTH-05），前缀保持可识别便于清理
    user_a = "pp_" + uuid.uuid4().hex[:8]
    user_b = "pp_" + uuid.uuid4().hex[:8]
    try:
        ra = _register(client, user_a)
        rb = _register(client, user_b)
        assert ra.status_code == 200 and rb.status_code == 200
        from app.repository.db import db_cursor
        with db_cursor() as cur:
            cur.execute("UPDATE task SET user_id = %s WHERE run_id::text = %s",
                        (ra.json()["user_id"], rid))
        lng, lat = _inside_coord(rid)
        params = {"run_id": rid, "lng": lng, "lat": lat}
        r = client.get(PROBE_URL, params=params,
                       headers={"Authorization": "Bearer " + rb.json()["token"]})
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "AUTH-03"
        ok = client.get(PROBE_URL, params=params,
                        headers={"Authorization": "Bearer " + ra.json()["token"]})
        assert ok.status_code == 200, ok.text
        assert ok.json()["in_bounds"] is True
    finally:
        from app.repository.db import db_cursor
        with db_cursor() as cur:
            cur.execute("UPDATE task SET user_id = NULL WHERE run_id::text = %s", (rid,))
        _cleanup_user(user_a)
        _cleanup_user(user_b)


# --------------------------------------------------------------------------- #
# B3 阶段内进度
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-13")
def test_stage_progress_callback_backward_compatible():
    """on_stage(name) 旧回调与 on_stage(name,done,total) 新回调都可用。"""
    single, triple = [], []
    change_service._stage_progress(lambda name: single.append(name), "mask")
    change_service._stage_progress(
        lambda name, done=None, total=None: triple.append((name, done, total)),
        "mask", 2, 3)
    # 旧回调收到带细分参数时自动降级为单参数，不抛 TypeError
    change_service._stage_progress(lambda name: single.append(name), "mask", 2, 3)
    assert single == ["mask", "mask"]
    assert triple == [("mask", 2, 3)]


@pytest.mark.req("REQ-13")
def test_async_progress_detail_http(client):
    """progress.detail 可空且带 {stage,done,total}；5 阶段 step/total 不变。"""
    tid = "pytest-probe-" + uuid.uuid4().hex[:8]
    change_async._new_record(tid)
    change_async._stage(tid, "change_detect", 4, 6)
    r = client.get("/api/v1/result/change/task/" + tid)
    assert r.status_code == 200, r.text
    pg = r.json()["progress"]
    assert pg["detail"] == {"stage": "change_detect", "done": 4, "total": 6}
    assert pg["step"] == 4 and pg["total"] == 5

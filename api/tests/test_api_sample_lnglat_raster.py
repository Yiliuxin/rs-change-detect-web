# -*- coding: utf-8 -*-
"""接口测：run 详情 raster 元信息 + POST /api/v1/sample/from_lnglat（REQ-09/14）。

覆盖：
  * 详情 raster 有值（真实裁切栅格 profile）/ 无值（旧 run、栅格损坏）且都不报错；
  * 产物清单如实列出 t1/t2_composite.png（旧 run 没有则不伪造）；
  * 经纬度点选：AOI 中心成功入库、AOI 外 400 EX12、非法 ref_class 400 EX10
    （与既有 /accuracy/sample 共用同一套校验）；
  * 每个成功用例只清理自己创建的 sample 行，既有测试数据零改动。
"""
from __future__ import annotations

import uuid
from typing import Dict, List, Tuple

import pytest
import rasterio

from app.repository.db import db_cursor
from app.repository.sample_repo import count_by_class, list_samples
from app.service.common import RUNS_ROOT, run_dir
from _helpers import POYANG_T1, POYANG_T2, assert_error_body, poyang_geojson

DETAIL_URL = "/api/v1/runs/{run_id}"
FROM_LNGLAT_URL = "/api/v1/sample/from_lnglat"
LEGACY_SAMPLE_URL = "/api/v1/accuracy/sample"
# 鄱阳湖 AOI（约 116.26–116.34, 29.16–29.24）之外的一点
OUTSIDE_LNGLAT: Tuple[float, float] = (116.90, 30.00)


@pytest.fixture(scope="module")
def legacy_run(client) -> str:
    """旧 run：只 /aoi/select 建 task，不跑 /result/change（无裁切栅格、无合成图）。"""
    r = client.post("/api/v1/aoi/select", json={
        "aoi_geom": poyang_geojson(), "t1": POYANG_T1, "t2": POYANG_T2,
    })
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


@pytest.fixture
def cleanup_samples():
    """用例结束后只删除本用例创建的样本行（不动既有测试数据）。"""
    ids: List[str] = []
    yield ids
    if ids:
        with db_cursor() as cur:
            for sid in ids:
                cur.execute("DELETE FROM sample WHERE sample_id::text = %s", (sid,))


def _center(detail: Dict) -> Tuple[float, float]:
    box = detail["params_snapshot"]["aoi_wgs84"]
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


# --------------------------------------------------------------------------- #
# 1. GET /api/v1/runs/{run_id}：raster 元信息 + 双时相合成图产物清单
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-10")
@pytest.mark.req("REQ-14")
def test_run_detail_raster_present_and_composites(client, change_ready):
    """有裁切栅格时 raster 与 change.tif profile 一致；t1/t2_composite.png 如实列出。"""
    rid = change_ready["run_id"]
    r = client.get(DETAIL_URL.format(run_id=rid))
    assert r.status_code == 200, r.text
    body = r.json()
    raster = body["raster"]
    assert raster is not None, body
    with rasterio.open(str(run_dir(rid) / "change.tif")) as ds:
        assert set(raster) == {"width", "height", "pixel_size_m"}
        assert raster["width"] == ds.width and raster["height"] == ds.height
        assert raster["width"] > 0 and raster["height"] > 0
        assert raster["pixel_size_m"] == pytest.approx(abs(ds.res[0]), rel=1e-6)

    items = {a["name"]: a for a in body["artifacts"]}
    for name in ("t1_composite.png", "t2_composite.png"):
        assert name in items, name
        assert items[name]["size"] > 0
        assert client.get(items[name]["url"]).status_code == 200


@pytest.mark.req("REQ-14")
def test_run_detail_raster_null_for_legacy_run(client, legacy_run):
    """旧 run 没有裁切栅格 / 合成图 → raster=null，产物清单不伪造。"""
    assert not (RUNS_ROOT / legacy_run / "change.tif").exists()
    r = client.get(DETAIL_URL.format(run_id=legacy_run))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["raster"] is None
    names = {a["name"] for a in body["artifacts"]}
    assert names.isdisjoint({"t1_composite.png", "t2_composite.png"})


@pytest.mark.req("REQ-14")
def test_run_detail_raster_null_when_profile_unreadable(client, change_ready, monkeypatch):
    """栅格损坏/读取失败时详情仍 200 且 raster=null，不得报错。"""
    def _boom(*_args, **_kwargs):
        raise OSError("corrupted GeoTIFF")

    monkeypatch.setattr("app.service.run_service.rasterio.open", _boom)
    r = client.get(DETAIL_URL.format(run_id=change_ready["run_id"]))
    assert r.status_code == 200, r.text
    assert r.json()["raster"] is None


# --------------------------------------------------------------------------- #
# 2. POST /api/v1/sample/from_lnglat：正常 / 越界 / 非法 ref_class
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-09")
def test_from_lnglat_center_ok(client, change_ready, cleanup_samples):
    """AOI 中心点 → 反算像元坐标入库，响应 sample_id/px_x/px_y/ref_class。"""
    rid = change_ready["run_id"]
    detail = client.get(DETAIL_URL.format(run_id=rid)).json()
    lng, lat = _center(detail)
    r = client.post(FROM_LNGLAT_URL, json={
        "run_id": rid, "lng": lng, "lat": lat,
        "ref_class": "增强", "label": "ui-click",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    sid = body["sample_id"]
    cleanup_samples.append(sid)
    assert str(uuid.UUID(sid)) == sid
    assert body["ref_class"] == "增强"
    assert set(body) == {"sample_id", "px_x", "px_y", "ref_class"}
    with rasterio.open(change_ready["change_geotiff"]) as ds:
        assert 0 <= body["px_x"] < ds.width
        assert 0 <= body["px_y"] < ds.height

    row = next((s for s in list_samples(rid) if s["sample_id"] == sid), None)
    assert row is not None
    assert row["px_x"] == body["px_x"] and row["px_y"] == body["px_y"]
    assert row["ref_class"] == "增强" and row["label"] == "ui-click"
    assert row["lon"] == pytest.approx(lng) and row["lat"] == pytest.approx(lat)
    # 与既有 /accuracy/sample 共用同一张 sample 表与类别统计口径
    assert count_by_class(rid)["增强"] >= 1


@pytest.mark.req("REQ-09")
def test_from_lnglat_outside_aoi_ex12(client, change_ready):
    """AOI 外一点 → 400 EX12（既有的 EX12 文案与 HTTP 映射不变），且不写库。"""
    rid = change_ready["run_id"]
    before = count_by_class(rid)
    r = client.post(FROM_LNGLAT_URL, json={
        "run_id": rid, "lng": OUTSIDE_LNGLAT[0], "lat": OUTSIDE_LNGLAT[1],
        "ref_class": "稳定",
    })
    body = assert_error_body(r, "EX12", 400)
    assert body["run_id"] == rid
    assert count_by_class(rid) == before


@pytest.mark.req("REQ-09")
@pytest.mark.parametrize("bad_class", ["未知", "", "NDVI"])
def test_from_lnglat_invalid_ref_class_ex10(client, change_ready, bad_class):
    """非法 ref_class → 400 EX10，与既有 /accuracy/sample 的校验结果一致。"""
    rid = change_ready["run_id"]
    lng, lat = _center(client.get(DETAIL_URL.format(run_id=rid)).json())
    payload = {"run_id": rid, "lng": lng, "lat": lat, "ref_class": bad_class}
    before = count_by_class(rid)
    assert_error_body(client.post(FROM_LNGLAT_URL, json=payload), "EX10", 400)
    assert count_by_class(rid) == before

    legacy = client.post(LEGACY_SAMPLE_URL, json={
        "run_id": rid,
        "samples": [{"x": 1.0, "y": 1.0, "ref_class": bad_class}],
    })
    assert_error_body(legacy, "EX10", 400)
    assert count_by_class(rid) == before

# -*- coding: utf-8 -*-
"""接口测：POST /api/v1/aoi/select（HLD 2.2.3.1，REQ-01/02/14；EX1/EX2）。"""
from __future__ import annotations

import json

import pytest

from app.config.constants import AOI_MAX_SPAN_DEG
from _helpers import (
    POYANG_T1, POYANG_T2, assert_error_body, geojson_polygon,
    poyang_geojson, too_large_geojson,
)

URL = "/api/v1/aoi/select"


@pytest.mark.req("REQ-01")
@pytest.mark.req("REQ-02")
@pytest.mark.req("REQ-14")
def test_aoi_select_happy_path(client):
    r = client.post(URL, json={
        "aoi_geom": poyang_geojson(), "t1": POYANG_T1, "t2": POYANG_T2,
    })
    assert r.status_code == 200, r.text
    b = r.json()
    assert {"aoi_id", "run_id", "area_deg2", "matched_scenes"} <= set(b)
    assert b["aoi_id"] and b["run_id"] and b["area_deg2"] > 0
    assert b["area_deg2"] <= AOI_MAX_SPAN_DEG ** 2
    assert len(b["matched_scenes"]) == 2
    assert sorted(s["acq_date"] for s in b["matched_scenes"]) == [POYANG_T1, POYANG_T2]
    assert all(s["sensor"] == "S2" for s in b["matched_scenes"])
    assert all(s["cog_path"].startswith("minio://scenes/pair_poyang/")
               for s in b["matched_scenes"])


@pytest.mark.req("REQ-01")
def test_aoi_select_ex1_span_over_limit(client):
    """跨度 > 0.25° → 400 EX1，统一错误体 {code,message,run_id}。"""
    r = client.post(URL, json={
        "aoi_geom": too_large_geojson(), "t1": POYANG_T1, "t2": POYANG_T2,
    })
    body = assert_error_body(r, "EX1", 400)
    assert body["run_id"]


@pytest.mark.req("REQ-02")
def test_aoi_select_ex2_no_matching_dates(client):
    """日期无匹配影像对 → 404 EX2。"""
    r = client.post(URL, json={
        "aoi_geom": poyang_geojson(), "t1": "2020-01-01", "t2": "2020-06-01",
    })
    assert_error_body(r, "EX2", 404)


@pytest.mark.req("REQ-02")
def test_aoi_select_ex2_aoi_outside_all_footprints(client):
    """空间相交无结果 → 404 EX2（对应 repository 的 find_intersecting 空集）。"""
    r = client.post(URL, json={
        "aoi_geom": geojson_polygon((0.0, 0.0, 0.05, 0.05)),
        "t1": POYANG_T1, "t2": POYANG_T2,
    })
    assert_error_body(r, "EX2", 404)


@pytest.mark.req("REQ-01")
def test_aoi_select_ex2_invalid_geometry(client):
    r = client.post(URL, json={
        "aoi_geom": json.dumps({"type": "Point", "coordinates": [116.3, 29.2]}),
        "t1": POYANG_T1, "t2": POYANG_T2,
    })
    assert_error_body(r, "EX2", 400)


@pytest.mark.req("REQ-01")
def test_aoi_select_ex2_t2_not_after_t1(client):
    r = client.post(URL, json={
        "aoi_geom": poyang_geojson(), "t1": POYANG_T2, "t2": POYANG_T1,
    })
    assert_error_body(r, "EX2", 400)

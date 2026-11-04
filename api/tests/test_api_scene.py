# -*- coding: utf-8 -*-
"""接口测：GET / POST /api/v1/scene/match（HLD 2.2.3.2，REQ-02/03/04；EX2）。"""
from __future__ import annotations

import pytest

from app.algorithm.radiometric import check_baseline
from app.config.constants import BLOCK_SIZE, RADIOMETRIC_FORMULAS
from _helpers import POYANG_T1, POYANG_T2, assert_error_body, poyang_geojson

URL = "/api/v1/scene/match"


def _select_one(client):
    r = client.post("/api/v1/aoi/select", json={
        "aoi_geom": poyang_geojson(), "t1": POYANG_T1, "t2": POYANG_T2,
    })
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.req("REQ-02")
@pytest.mark.req("REQ-04")
def test_scene_match_get_no_query_returns_all_six(client):
    """无 query → find_all() 返回 6 条 S2 记录，口径/基线全部合法。"""
    r = client.get(URL)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["count"] == 6 and len(b["scenes"]) == 6
    assert b["aoi_id"] is None and b["run_id"] is None
    assert b["block_size"] == BLOCK_SIZE
    assert b["sensor"] == "S2"
    assert b["formula_id"] == RADIOMETRIC_FORMULAS["S2"]["formula_id"]
    for s in b["scenes"]:
        assert s["scene_id"] and s["acq_date"]
        assert s["cog_path"].startswith("minio://scenes/")
        assert s["sensor"] == "S2"
        check_baseline(s["sensor"], s["proc_baseline"])   # 不达标会抛 EX9


@pytest.mark.req("REQ-02")
def test_scene_match_get_by_aoi_id_spatial_intersect(client):
    """带 aoi_id → ST_Intersects(footprint, geom) 返回相交的 2 条鄱阳湖景。"""
    aoi = _select_one(client)
    r = client.get(URL, params={"aoi_id": aoi["aoi_id"]})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["count"] == 2 and len(b["scenes"]) == 2
    assert b["aoi_id"] == aoi["aoi_id"]
    assert all("pair_poyang" in s["cog_path"] for s in b["scenes"])
    assert sorted(s["acq_date"] for s in b["scenes"]) == [POYANG_T1, POYANG_T2]


@pytest.mark.req("REQ-02")
def test_scene_match_get_ex2_no_date_match(client):
    r = client.get(URL, params={"t1": "2020-01-01", "t2": "2020-06-01"})
    assert_error_body(r, "EX2", 404, run_id_required=False)


@pytest.mark.req("REQ-02")
@pytest.mark.req("REQ-03")
@pytest.mark.req("REQ-04")
def test_scene_match_post_happy_path(client):
    """POST 契约：真实 COG 裁切窗口 + 掩膜前 valid_ratio + 锁定公式。"""
    aoi = _select_one(client)
    ids = [s["scene_id"] for s in aoi["matched_scenes"]]
    r = client.post(URL, json={
        "aoi_id": aoi["aoi_id"],
        "scene_t1_id": ids[0], "scene_t2_id": ids[1],
        "block_size": BLOCK_SIZE,
    })
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["run_id"] == aoi["run_id"]
    assert b["formula_id"] == RADIOMETRIC_FORMULAS["S2"]["formula_id"]
    assert b["proc_baseline"] and check_baseline("S2", b["proc_baseline"])
    cw = b["clip_window"]
    assert cw["width"] > 0 and cw["height"] > 0
    assert cw["crs"] and cw["res"] and cw["res"] > 0
    assert 0.0 <= b["valid_ratio"] <= 1.0


@pytest.mark.req("REQ-02")
def test_scene_match_post_ex2_unknown_scene_ids(client):
    aoi = _select_one(client)
    r = client.post(URL, json={
        "aoi_id": aoi["aoi_id"],
        "scene_t1_id": "00000000-0000-0000-0000-000000000001",
        "scene_t2_id": "00000000-0000-0000-0000-000000000002",
        "block_size": BLOCK_SIZE,
    })
    assert_error_body(r, "EX2", 404)

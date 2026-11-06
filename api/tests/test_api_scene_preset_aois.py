# -*- coding: utf-8 -*-
"""接口测：GET /api/v1/scene/preset_aois（REQ-02，12 组预置影像对 AOI 清单）。

覆盖：
  * 返回 12 组（11 S2 + 1 Landsat）、按 name 字典序、bbox 合法（w<e, s<n）；
  * 必须含 landsat_poyang：通过 seed aoi 关联 + scene.footprint 精确查询取得；
  * 口径与 GET /scene/match 全量清单一致（同一批 22 景 S2），但全量分支
    按既有契约只列 S2，Landsat 组不在该清单里，本接口需补齐；
  * 契约字段固定（name/bbox/t1/t2/sensor）且 t1 早于 t2。
"""
from __future__ import annotations

import pytest

from app.config.constants import VALID_SENSORS

URL = "/api/v1/scene/preset_aois"
MATCH_URL = "/api/v1/scene/match"
EXPECTED_PAIR_COUNT = 12
S2_PAIR_COUNT = 11


@pytest.fixture(scope="module")
def preset(client):
    r = client.get(URL)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.req("REQ-02")
def test_preset_aois_returns_12_groups_sorted_with_valid_bbox(preset):
    """12 组、name 字典序、bbox [w,s,e,n] 合法且经纬度在中国范围内。"""
    assert isinstance(preset, list)
    assert len(preset) == EXPECTED_PAIR_COUNT, [p["name"] for p in preset]
    names = [p["name"] for p in preset]
    assert names == sorted(names) and len(set(names)) == len(names)
    for p in preset:
        assert set(p) == {"name", "bbox", "t1", "t2", "sensor"}, p
        w, s, e, n = p["bbox"]
        assert len(p["bbox"]) == 4
        assert w < e and s < n, p
        assert -180.0 <= w < e <= 180.0 and -90.0 <= s < n <= 90.0, p
        assert p["sensor"] in VALID_SENSORS, p
        assert set(p["t1"]) == {"date", "source"} and set(p["t2"]) == {"date", "source"}
        assert p["t1"]["date"] <= p["t2"]["date"], p
        for phase in (p["t1"], p["t2"]):
            assert len(phase["date"]) == 10 and phase["source"].startswith("minio://scenes/")


@pytest.mark.req("REQ-02")
def test_preset_aois_contains_landsat_poyang_with_real_bbox(preset):
    """landsat_poyang 必须存在；其 bbox 取自身 footprint，而非复用作 FK 的其它组 seed aoi。"""
    landsat = [p for p in preset if p["name"] == "landsat_poyang"]
    assert len(landsat) == 1, [p["name"] for p in preset]
    item = landsat[0]
    assert item["sensor"] in ("L8", "L9")
    assert item["t1"]["date"] == "2023-08-05" and item["t2"]["date"] == "2024-07-22"
    assert all("landsat_poyang" in item[k]["source"] for k in ("t1", "t2"))
    # 真实 Landsat AOI 在鄱阳湖（115.9~116.0, 29.0~29.1）；复用的 seed aoi 在洞庭湖 112.8~112.9
    w, s, e, n = item["bbox"]
    assert 115.0 < w < 116.5 and 28.5 < s < n < 29.5, item["bbox"]
    dongting = next(p for p in preset if p["name"] == "dongting")
    assert item["bbox"] != dongting["bbox"], "Landsat 组误用了复用 FK 的 seed aoi bbox"


@pytest.mark.req("REQ-02")
def test_preset_aois_same_scene_batch_as_match_full_list(client, preset):
    """11 组 S2 的 22 条 cog_path 与 GET /scene/match 全量清单逐条一致。"""
    match = client.get(MATCH_URL)
    assert match.status_code == 200, match.text
    body = match.json()
    assert body["count"] == len(body["scenes"]) == S2_PAIR_COUNT * 2
    assert all(s["sensor"] == "S2" for s in body["scenes"])  # 既有契约：全量分支只列 S2

    from_match = {s["cog_path"] for s in body["scenes"]}
    s2_pairs = [p for p in preset if p["name"] != "landsat_poyang"]
    assert len(s2_pairs) == S2_PAIR_COUNT
    from_preset = {p[k]["source"] for p in s2_pairs for k in ("t1", "t2")}
    assert from_preset == from_match

    landsat = next(p for p in preset if p["name"] == "landsat_poyang")
    assert not ({landsat["t1"]["source"], landsat["t2"]["source"]} & from_match)

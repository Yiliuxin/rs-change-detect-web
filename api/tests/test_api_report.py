# -*- coding: utf-8 -*-
"""接口测：POST /api/v1/report/export（HLD 2.2.3.5，REQ-12/14/15；EX6/SYS-01）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config.constants import ATTRIBUTION_S2, EXPORT_FORMATS
from _helpers import POYANG_T1, POYANG_T2, assert_error_body, poyang_geojson

URL = "/api/v1/report/export"


@pytest.mark.req("REQ-12")
@pytest.mark.req("REQ-14")
@pytest.mark.req("REQ-15")
def test_report_export_happy_path_triplet(client, change_ready):
    """200 + GeoTIFF/PNG/PDF 三件套 + run_id + commit + 合规署名。"""
    r = client.post(URL, json={
        "run_id": change_ready["run_id"], "formats": list(EXPORT_FORMATS),
    })
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["run_id"] == change_ready["run_id"]
    assert len(b["files"]) == len(EXPORT_FORMATS)
    assert {f["format"] for f in b["files"]} == set(EXPORT_FORMATS)
    assert all(f["size"] and f["size"] > 0 and f["url"] for f in b["files"])
    assert ATTRIBUTION_S2.split(" [")[0] in b["attribution"]

    out = Path(change_ready["change_geotiff"]).parent
    for name in ("change.tif", "change.png", "report.pdf"):
        f = out / name
        assert f.is_file() and f.stat().st_size > 0
    # geojson 四件套：文件落盘 + RFC 7946 FeatureCollection + filename 字段
    gj = next(f for f in b["files"] if f["format"] == "geojson")
    assert gj["filename"] == "change.geojson" and gj["url"].endswith("change.geojson")
    gj_file = out / "change.geojson"
    assert gj_file.is_file() and gj_file.stat().st_size > 0
    gj_data = json.loads(gj_file.read_text(encoding="utf-8"))
    assert gj_data["type"] == "FeatureCollection" and isinstance(gj_data["features"], list)


@pytest.mark.req("REQ-12")
def test_report_export_ex6_illegal_format(client, change_ready):
    r = client.post(URL, json={
        "run_id": change_ready["run_id"], "formats": ["svg"],
    })
    body = assert_error_body(r, "EX6", 400)
    assert body["run_id"] == change_ready["run_id"]


@pytest.mark.req("REQ-12")
def test_report_export_sys01_missing_artifacts(client):
    """task 存在但结果产物未生成 → 500 SYS-01（不伪造下载链接）。"""
    a = client.post("/api/v1/aoi/select", json={
        "aoi_geom": poyang_geojson(), "t1": POYANG_T1, "t2": POYANG_T2,
    })
    assert a.status_code == 200
    r = client.post(URL, json={"run_id": a.json()["run_id"], "formats": ["png"]})
    assert_error_body(r, "SYS-01", 500)

# -*- coding: utf-8 -*-
"""接口测：POST /api/v1/result/change（HLD 2.2.3.3，REQ-05/06/07/08/14）。

复用 session fixture change_ready（真实 /result/change 只跑一次）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio

from app.config.constants import S2_FORMULA, VALID_RATIO_WARN
from _helpers import assert_error_body

URL = "/api/v1/result/change"


@pytest.mark.req("REQ-05")
@pytest.mark.req("REQ-06")
@pytest.mark.req("REQ-07")
@pytest.mark.req("REQ-08")
@pytest.mark.req("REQ-10")
@pytest.mark.req("REQ-11")
@pytest.mark.req("REQ-14")
def test_result_change_happy_path_and_artifacts(client, change_ready):
    """200 + change.tif / change.png 落盘 + 三类统计 + 参数快照口径。"""
    body = change_ready["change_body"]
    assert change_ready["change_status"] == 200
    assert body["run_id"] == change_ready["run_id"]
    assert 0.0 <= body["valid_ratio"] <= 1.0
    assert body["valid_ratio"] >= VALID_RATIO_WARN      # 鄱阳湖 seed 走 OK 档
    assert body["threshold"] > 0.0
    assert len(body["stats"]) == 3
    assert [s["change_class"] for s in body["stats"]] == [1, 2, 3]
    assert all(s["pixel_count"] >= 0 and s["area_ha"] >= 0 for s in body["stats"])

    snap = body["params_snapshot"]
    assert snap["formula"] == S2_FORMULA == "rho=DN/10000"   # 禁止 (DN-1000)/10000
    assert snap["sensor"] == "S2"
    assert snap["valid_ratio_min"] and snap["valid_ratio_warn"]

    tif = Path(change_ready["change_geotiff"])
    png = Path(change_ready["change_png"])
    assert tif.is_file() and tif.stat().st_size > 0
    assert png.is_file() and png.stat().st_size > 0
    # REQ-10：专题图是真实渲染出的 PNG（不是空文件/占位）
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # REQ-11：面积换算 area_ha = pixel_count * 单像元面积(m²) / 10000
    pixel_area_m2 = float(snap["pixel_area_m2"])
    for s in body["stats"]:
        assert s["area_ha"] == pytest.approx(
            s["pixel_count"] * pixel_area_m2 / 10000.0, rel=1e-4,
        )
    with rasterio.open(str(tif)) as ds:
        arr = ds.read(1)
        assert ds.count == 1 and arr.dtype.name == "uint8"
        assert {int(v) for v in np.unique(arr)} <= {0, 1, 2, 3}
        assert float(ds.nodata) == 0.0
        assert ds.crs is not None and ds.transform is not None


@pytest.mark.req("REQ-14")
def test_result_change_ex2_unknown_run_id(client):
    r = client.post(URL, json={"run_id": "00000000-0000-0000-0000-0000000000ff"})
    assert_error_body(r, "EX2", 404)

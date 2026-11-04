# -*- coding: utf-8 -*-
"""集成测：AOI→裁切→掩膜→指数→变化检测→精度→专题图→报告（REQ-01~15 / UC1~8）。

复用 session fixture change_ready（真实 /result/change 只跑一次），把 5 个
接口的产物串成一条链，验证 run_id 一致性、三类统计与三件套落盘。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import rasterio

from app.config.constants import EXPORT_FORMATS, MIN_SAMPLES_PER_CLASS, S2_FORMULA
from _helpers import build_samples_from_change


@pytest.mark.req("REQ-01")
@pytest.mark.req("REQ-02")
@pytest.mark.req("REQ-05")
@pytest.mark.req("REQ-07")
@pytest.mark.req("REQ-08")
@pytest.mark.req("REQ-09")
@pytest.mark.req("REQ-12")
@pytest.mark.req("REQ-14")
@pytest.mark.req("REQ-15")
def test_e2e_full_flow_artifacts_with_run_id(client, change_ready):
    run_id = change_ready["run_id"]
    out = Path(change_ready["change_geotiff"]).parent
    assert out.name == run_id                      # 产物挂在 runs/<run_id>/ 下

    # ① AOI → ② 裁切/掩膜/指数/变化检测：change.tif 三类编码 + 地理参考
    with rasterio.open(str(out / "change.tif")) as ds:
        arr = ds.read(1)
        assert arr.dtype.name == "uint8" and float(ds.nodata) == 0.0
        assert ds.crs is not None and ds.transform is not None
        counts = {int(k): int((arr == k).sum()) for k in (0, 1, 2, 3)}
    stats = {int(s["change_class"]): int(s["pixel_count"])
             for s in change_ready["stats"]}
    assert stats == {1: counts[1], 2: counts[2], 3: counts[3]}

    # REQ-14 参数快照：公式取常量源，绝不使用 (DN-1000)/10000
    snap = change_ready["params_snapshot"]
    assert snap["formula"] == S2_FORMULA and snap["run_id"] == run_id
    meta = json.loads((out / "run_report.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == run_id
    assert meta["params_snapshot"]["formula"] == S2_FORMULA

    # ⑥ 精度：提交每类 ≥30 样本 → 200 + 3×3 混淆矩阵
    samples = build_samples_from_change(
        out / "change.tif", per_class=MIN_SAMPLES_PER_CLASS + 2,
    )
    acc = client.post("/api/v1/accuracy/sample",
                      json={"run_id": run_id, "samples": samples})
    assert acc.status_code == 200, acc.text
    assert len(acc.json()["confusion_matrix"]) == 3

    # ⑦ 专题图 / ⑧ 报告：GeoTIFF + PNG + PDF 三件套，响应带 run_id
    exp = client.post("/api/v1/report/export",
                      json={"run_id": run_id, "formats": list(EXPORT_FORMATS)})
    assert exp.status_code == 200, exp.text
    assert exp.json()["run_id"] == run_id
    assert {f["format"] for f in exp.json()["files"]} == set(EXPORT_FORMATS)
    for name in ("change.tif", "change.png", "report.pdf"):
        f = out / name
        assert f.is_file() and f.stat().st_size > 0
    pdf = (out / "report.pdf").read_bytes()
    assert pdf.startswith(b"%PDF") and b"%%EOF" in pdf and len(pdf) > 1000

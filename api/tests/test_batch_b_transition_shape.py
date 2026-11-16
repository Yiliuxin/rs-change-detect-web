# -*- coding: utf-8 -*-
"""批次 B1 / B2 / B3 纯函数与真链路抽验。

覆盖：
  * B1 过渡带边界（(1−r)T ≤ |Δ| ≤ (1+r)T）、stats 增补字段、三类分类图不被改写；
  * B1 鄱阳湖真链路 run 的过渡带占比实测（应 < 15%，异常说明阈值带设错）；
  * B2 GeoJSON properties 三形态字段 + 2×2 图斑手工核算周长 / 紧凑度量级；
  * B2 Top10 三字段与 GeoJSON 同源（周长一致）。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import from_origin

from app.algorithm.change_detector import (
    CHANGE_DEGRADED, CHANGE_ENHANCED, CHANGE_STABLE, classify_change,
    transition_band_edges, transition_band_mask, transition_band_stats,
)
from app.algorithm.patches import patch_stats
from app.algorithm.writers import write_geojson
from app.config.constants import TRANSITION_BAND_RATIO

T = 0.2


def _profile():
    return {
        "crs": "EPSG:4326",
        "transform": from_origin(116.20, 29.30, 0.0001, 0.0001),
        "width": 10, "height": 10,
    }


def _one_patch():
    change = np.zeros((8, 8), dtype=np.uint8)
    change[1:3, 1:3] = CHANGE_ENHANCED  # 2×2 一块
    return change


@pytest.mark.req("REQ-07")
def test_transition_band_mask_boundaries_and_stats_fields():
    """带宽 = ±0.25T；带内像元单列统计，且不改写三类分类编码。"""
    low, high = transition_band_edges(T)
    assert low == pytest.approx((1.0 - TRANSITION_BAND_RATIO) * T)
    assert high == pytest.approx((1.0 + TRANSITION_BAND_RATIO) * T)

    # Δ = ±0.25 / ±0.20 / ±0.15 / 0，其中 ±0.15~±0.25 落在过渡带内
    d = np.array([-0.25, -0.20, -0.15, 0.0, 0.15, 0.20, 0.25], dtype=np.float32)
    mask = transition_band_mask(d, T)
    assert mask.tolist() == [True, True, True, False, True, True, True]
    assert not transition_band_mask(
        np.array([0.251, -0.251, 0.149, -0.149], dtype=np.float32), T
    ).any()
    # 无效掩膜生效、NaN 不入带
    vm = np.array([True, True, False, True, True, True, True])
    assert not bool(transition_band_mask(d, T, valid_mask=vm)[2])

    band = transition_band_stats(mask, valid_count=7, pixel_area_m2=100.0, threshold=T)
    for key in ("code", "label", "pixel_count", "ratio", "area_ha",
                "threshold", "band_ratio", "band_low", "band_high", "note"):
        assert key in band, key
    assert band["pixel_count"] == 6
    assert band["ratio"] == pytest.approx(6 / 7)
    assert band["area_ha"] == pytest.approx(6 * 100.0 / 10000.0)
    assert band["band_low"] == pytest.approx(0.15)
    assert band["band_high"] == pytest.approx(0.25)
    assert "不是第四类变化" in str(band["note"])

    # 口径核心：过渡带只是旁路标记，三类分类结果完全不变
    cls = classify_change(d, T)
    assert cls.tolist() == [CHANGE_DEGRADED, CHANGE_STABLE, CHANGE_STABLE,
                            CHANGE_STABLE, CHANGE_STABLE, CHANGE_STABLE,
                            CHANGE_ENHANCED]


@pytest.mark.req("REQ-08")
def test_poyang_run_transition_band_ratio_and_conclusion(change_ready):
    """鄱阳湖真链路 run：过渡带占比实测应 < 15%，结论字段与 PDF 同源。"""
    body = change_ready["change_body"]
    snap = body.get("params_snapshot") or {}
    band = body.get("transition_band") or snap.get("transition_band")
    assert isinstance(band, dict), "响应/快照应含 transition_band"
    assert int(band["pixel_count"]) > 0
    assert 0.0 <= float(band["ratio"]) < 0.15, "过渡带占比异常，检查阈值带是否设错"
    assert float(band["band_high"]) > float(band["band_low"]) >= 0.0
    # B3：结论走既有字段通道，老 run 才会为空串
    conclusion = str(body.get("conclusion") or snap.get("conclusion") or "")
    assert conclusion and "阈值" in conclusion and "主导转移方向" in conclusion


@pytest.mark.req("REQ-12")
def test_geojson_shape_metrics_fields_and_magnitude(tmp_path):
    """GeoJSON properties 三字段齐全；2×2 图斑周长/紧凑度手工核算量级。"""
    gj = write_geojson(_one_patch(), _profile(), tmp_path / "change.geojson")
    data = json.loads(Path(gj["path"]).read_text(encoding="utf-8"))
    assert gj["feature_count"] == len(data["features"]) == 1
    p = data["features"][0]["properties"]
    for key in ("perimeter_m", "compactness", "orientation_deg"):
        assert key in p, key

    # 手工核算（度→米按 111320·cos(lat) 近似）：
    # 2×2 像元外环是 0.0002°×0.0002° 矩形，环平均纬度约 29.2998°。
    # kx = 0.0001° 经度长 ≈ 9.71 m；ky = 0.0001° 纬度长 = 11.132 m
    # P = 4·kx + 4·ky ≈ 83.37 m；A = 4·kx·ky ≈ 432.3 m²
    # 紧凑度 = 4πA / P² ≈ 0.78（正方形理论值 π/4 ≈ 0.785）
    kx = 111320.0 * math.cos(math.radians(29.2998)) * 0.0001
    ky = 111320.0 * 0.0001
    expect_p = 4 * kx + 4 * ky
    expect_c = 4 * math.pi * (4 * kx * ky) / (expect_p ** 2)
    assert p["perimeter_m"] == pytest.approx(expect_p, rel=0.02)
    assert p["compactness"] == pytest.approx(expect_c, rel=0.02)
    assert 0.0 <= p["orientation_deg"] < 180.0


@pytest.mark.req("REQ-11")
def test_patch_top_shape_metrics_match_geojson(tmp_path):
    """Top10 三字段存在且周长与 GeoJSON 同源（面积口径差异不影响周长）。"""
    change = _one_patch()
    ps = patch_stats(change, _profile(), pixel_area_m2=100.0)
    assert ps["total_patches"] == 1
    t = ps["top"][0]
    for key in ("perimeter_m", "compactness", "orientation_deg"):
        assert key in t, key
    gj = write_geojson(change, _profile(), tmp_path / "change.geojson")
    p = json.loads(Path(gj["path"]).read_text(encoding="utf-8"))["features"][0]["properties"]
    assert t["perimeter_m"] == pytest.approx(p["perimeter_m"], rel=0.02)
    assert 0.0 < t["compactness"] < 1.5
    assert 0.0 <= t["orientation_deg"] < 180.0

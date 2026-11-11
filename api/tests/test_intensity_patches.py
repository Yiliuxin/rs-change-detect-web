# -*- coding: utf-8 -*-
"""A1 变化强度分级 / A2 变化图斑统计 纯函数单测。

覆盖：
  * A1 五档边界逐值落档（含 ±T、±K·T 边界）、NaN→0、ratio 和≈1；
  * A1 口径一致性：4+5==增强、1+2==退化、3==稳定（与形态学后三类逐项比对）；
  * A2 两块不相邻 2×2 → total_patches==2；对角相邻算 2 块（4 邻域）；
  * A2 斑块数与 write_geojson 的 feature 数同口径；超限 skipped+原因；无变化不抛异常。

断言一律按 key 逐项校验，不依赖列表长度 / 位置。
"""
from __future__ import annotations

import numpy as np
import pytest
from rasterio.transform import from_origin

from app.algorithm.change_detector import (
    CHANGE_DEGRADED, CHANGE_ENHANCED, CHANGE_STABLE,
    change_stats, classify_change,
)
from app.algorithm.intensity import (
    classify_intensity, intensity_grade, intensity_stats,
)
from app.algorithm.morphology import clean_change_map
from app.algorithm.patches import patch_stats
from app.algorithm.writers import write_geojson
from app.config.constants import (
    INTENSITY_CODES, INTENSITY_K, INTENSITY_NODATA, INTENSITY_NOTE,
    PATCH_MAX_PIXELS,
)

T = 0.1


def _profile():
    return {
        "crs": "EPSG:4326",
        "transform": from_origin(116.20, 29.30, 0.0001, 0.0001),
        "width": 10, "height": 10,
    }


def _levels_by_code(intensity: dict) -> dict:
    return {int(lv["code"]): lv for lv in intensity["levels"]}


def _stats_by_code(stats: list) -> dict:
    return {int(s["change_class"]): s for s in stats}


# --------------------------------------------------------------------------- #
# A1
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-07")
def test_intensity_five_grade_boundaries():
    """d≤−K·T→1 / −K·T<d<−T→2 / |d|≤T→3 / T<d<K·T→4 / d≥K·T→5。"""
    strong = INTENSITY_K * T
    expected = {
        -0.5: 1, -strong: 1, -0.15: 2, -T: 3, 0.0: 3, T: 3,
        0.15: 4, strong: 5, 0.5: 5,
    }
    values = np.array(list(expected.keys()), dtype=np.float32)
    grades = classify_intensity(values, T)
    for i, (value, want) in enumerate(expected.items()):
        assert int(grades[i]) == want, (value, int(grades[i]), want)
        assert intensity_grade(value, T) == want, value
    # 常量源：K 与五档编码唯一登记处
    assert INTENSITY_K == 2.0
    assert tuple(INTENSITY_CODES) == (1, 2, 3, 4, 5)


@pytest.mark.req("REQ-07")
def test_intensity_nan_to_zero_and_ratio_sum():
    """NaN / ±inf → 0；五档 ratio 之和≈1；note 逐字取常量源。"""
    delta = np.array(
        [[-0.4, -0.15, 0.0, 0.15, 0.4, np.nan],
         [np.inf, -np.inf, 0.05, -0.05, 0.3, -0.3]],
        dtype=np.float32,
    )
    grades = classify_intensity(delta, T)
    assert int(grades[0, 5]) == INTENSITY_NODATA
    assert int(grades[1, 0]) == INTENSITY_NODATA
    assert int(grades[1, 1]) == INTENSITY_NODATA

    summary = intensity_stats(delta, T)
    assert summary["note"] == INTENSITY_NOTE == "经验阈值，非物候标定"
    assert summary["boundary"] == pytest.approx(INTENSITY_K * T)
    ratios = [float(_levels_by_code(summary)[c]["ratio"]) for c in INTENSITY_CODES]
    assert sum(ratios) == pytest.approx(1.0, abs=1e-9)
    assert summary["valid_count"] == 9
    assert summary["invalid_count"] == 3
    counts = summary["counts"]
    assert sum(int(counts[str(c)]) for c in INTENSITY_CODES) == summary["valid_count"]
    # 逐档像元数按 code 校验，不依赖 levels 列表顺序
    want = {1: 2, 2: 1, 3: 3, 4: 1, 5: 2}
    for code in INTENSITY_CODES:
        assert int(counts[str(code)]) == want[code], code
        assert int(_levels_by_code(summary)[code]["pixel_count"]) == want[code], code


@pytest.mark.req("REQ-07")
def test_intensity_consistent_with_three_classes():
    """4+5==增强 / 1+2==退化 / 3==稳定（以形态学后的最终三类图逐项比对）。"""
    rng = np.random.default_rng(20261111)
    delta = np.asarray(rng.normal(0.0, 0.18, size=(48, 48)), dtype=np.float32)
    # 造几块连续变化区，确保形态学确实会改动部分像元
    delta[4:12, 4:16] += 0.45
    delta[30:40, 25:38] -= 0.5
    valid = np.ones(delta.shape, dtype=bool)
    valid[0:3, 0:3] = False
    delta[~valid] = np.float32(np.nan)

    thr = T
    change_raw = classify_change(delta, thr, valid_mask=valid)
    change = clean_change_map(change_raw)
    stats = _stats_by_code(change_stats(change, pixel_area_m2_value=100.0,
                                         valid_mask=valid)["stats"])
    summary = intensity_stats(delta, thr, change=change, valid_mask=valid,
                              pixel_area_m2=100.0)
    counts = summary["counts"]
    groups = summary["group_counts"]

    assert int(counts["4"]) + int(counts["5"]) == int(stats[CHANGE_ENHANCED]["pixel_count"])
    assert int(counts["1"]) + int(counts["2"]) == int(stats[CHANGE_DEGRADED]["pixel_count"])
    assert int(counts["3"]) == int(stats[CHANGE_STABLE]["pixel_count"])
    assert int(groups[str(CHANGE_ENHANCED)]) == int(stats[CHANGE_ENHANCED]["pixel_count"])
    assert int(groups[str(CHANGE_DEGRADED)]) == int(stats[CHANGE_DEGRADED]["pixel_count"])
    assert int(groups[str(CHANGE_STABLE)]) == int(stats[CHANGE_STABLE]["pixel_count"])
    assert sum(float(_levels_by_code(summary)[c]["ratio"])
               for c in INTENSITY_CODES) == pytest.approx(1.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# A2
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-11")
def test_patches_two_non_adjacent_2x2_blocks():
    """两块不相邻 2×2 → total_patches==2，Top 含质心经纬度。"""
    change = np.zeros((10, 10), dtype=np.uint8)
    change[1:3, 1:3] = CHANGE_ENHANCED
    change[6:8, 6:8] = CHANGE_ENHANCED
    snap = patch_stats(change, _profile(), pixel_area_m2=100.0)

    assert snap["skipped"] is False
    assert snap["total_patches"] == 2
    assert snap["by_class"]["1"] == 2
    assert snap["by_class"]["2"] == 0
    assert snap["total_pixel_count"] == 8
    assert snap["total_area_ha"] == pytest.approx(8 * 100 / 10000)
    assert snap["mean_area_ha"] == pytest.approx(4 * 100 / 10000)
    assert snap["median_area_ha"] == pytest.approx(4 * 100 / 10000)
    assert snap["max_area_ha"] == pytest.approx(4 * 100 / 10000)
    assert snap["connectivity"] == 1
    assert snap["max_pixels"] == PATCH_MAX_PIXELS == 40_000_000
    assert snap["patch_density_per_km2"] > 0.0

    by_rank = {int(t["rank"]): t for t in snap["top"]}
    for rank in (1, 2):
        item = by_rank[rank]
        assert item["change_class"] == CHANGE_ENHANCED
        assert item["pixel_count"] == 4
        assert item["area_ha"] == pytest.approx(4 * 100 / 10000)
        assert item["centroid_lng"] is not None and np.isfinite(item["centroid_lng"])
        assert item["centroid_lat"] is not None and np.isfinite(item["centroid_lat"])


@pytest.mark.req("REQ-11")
def test_patches_diagonal_adjacency_is_two_patches():
    """仅对角相邻的两个像元在 4 邻域下算 2 块。"""
    change = np.zeros((6, 6), dtype=np.uint8)
    change[1, 1] = CHANGE_ENHANCED
    change[2, 2] = CHANGE_DEGRADED
    snap = patch_stats(change, _profile(), pixel_area_m2=100.0)
    assert snap["total_patches"] == 2
    assert snap["by_class"]["1"] == 1
    assert snap["by_class"]["2"] == 1


@pytest.mark.req("REQ-11")
def test_patches_count_matches_geojson_feature_count(tmp_path):
    """4 邻域斑块数与 write_geojson（rasterio shapes 默认连通性）feature 数同口径。"""
    change = np.zeros((8, 8), dtype=np.uint8)
    change[1:3, 1:3] = CHANGE_ENHANCED      # 2×2 一块
    change[5, 5] = CHANGE_DEGRADED          # 与增强块不相邻
    change[6, 6] = CHANGE_DEGRADED          # 仅对角相邻 → 另 1 块
    snap = patch_stats(change, _profile(), pixel_area_m2=100.0)
    gj = write_geojson(change, _profile(), tmp_path / "change.geojson")
    # 增强 2×2 一块 + 退化对角相邻两块 = 3 块 / 3 feature
    assert snap["total_patches"] == gj["feature_count"] == 3


@pytest.mark.req("REQ-11")
def test_patches_over_limit_skipped_with_reason():
    """超过像元上限 → skipped=True + 原因，不静默、不分配标签数组。"""
    change = np.zeros((4, 4), dtype=np.uint8)
    change[0, 0] = CHANGE_ENHANCED
    snap = patch_stats(change, _profile(), pixel_area_m2=100.0, max_pixels=8)
    assert snap["skipped"] is True
    assert snap["reason"] and "40,000,000" in snap["reason"]
    assert snap["total_patches"] == 0
    assert snap["top"] == []
    # 缺省口径来自常量源，不在算法里写死
    default = patch_stats(change, _profile(), pixel_area_m2=100.0)
    assert default["max_pixels"] == PATCH_MAX_PIXELS


@pytest.mark.req("REQ-11")
def test_patches_no_change_does_not_raise():
    """无任何变化图斑 → 指标全 0、Top 空、不抛异常。"""
    change = np.full((8, 8), CHANGE_STABLE, dtype=np.uint8)
    snap = patch_stats(change, _profile(), pixel_area_m2=100.0)
    assert snap["skipped"] is False
    assert snap["total_patches"] == 0
    assert snap["total_area_ha"] == 0.0
    assert snap["mean_area_ha"] == 0.0
    assert snap["median_area_ha"] == 0.0
    assert snap["max_area_ha"] == 0.0
    assert snap["top"] == []
    assert snap["by_class"]["1"] == 0 and snap["by_class"]["2"] == 0

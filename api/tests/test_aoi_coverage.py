# -*- coding: utf-8 -*-
"""AOI 覆盖率校验单元测试（在线取数从根上防 EX11）。

覆盖三层：
  * 算法层 ``aoi_coverage`` 纯函数：完整覆盖 / 部分越界 / 全越界的覆盖率；
  * 诊断层 ``coverage_diagnosis`` / ``_ex11_coverage_diagnosis``：文案与阈值；
  * discover 层 ``_bbox_coverage``：STAC footprint 几何估算（缺 geometry 放过）。

全部用内存合成栅格 / 假 feature，不打真网、不连 MinIO/PG。
"""
from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.algorithm.geotransform import BBox, aoi_coverage
from app.config.constants import AOI_COVERAGE_DIAGNOSIS, AOI_COVERAGE_MIN
from app.api.errors import ApiError
from app.service.online_fetch import (
    MIN_AOI_COVERAGE, _bbox_coverage, _verify_coverage, coverage_diagnosis,
)
from app.service.change_service import _ex11_coverage_diagnosis


# 合成栅格：EPSG:4326，覆盖 lon[100,101] × lat[20,21]，分辨率 0.01°（100×100）
GRID_WEST, GRID_NORTH, GRID_RES, GRID_SIZE = 100.0, 21.0, 0.01, 100


def _make_dataset(path):
    """落盘一个 100×100 的 EPSG:4326 栅格，左上角 (100,21)，像元 0.01°。"""
    transform = from_origin(GRID_WEST, GRID_NORTH, GRID_RES, GRID_RES)
    with rasterio.open(
        str(path), "w", driver="GTiff", width=GRID_SIZE, height=GRID_SIZE,
        count=1, dtype="uint8", crs="EPSG:4326", transform=transform,
    ) as ds:
        ds.write(np.ones((GRID_SIZE, GRID_SIZE), dtype="uint8"), 1)


@pytest.fixture
def grid(tmp_path):
    p = tmp_path / "grid.tif"
    _make_dataset(p)
    return p


# --------------------------------------------------------------------------- #
# 算法层：aoi_coverage
# --------------------------------------------------------------------------- #
def test_aoi_coverage_full_inside(grid):
    """AOI 完整落在栅格内 → coverage=1.0，无越界。"""
    box = BBox(100.1, 20.1, 100.3, 20.3)
    with rasterio.open(str(grid)) as ds:
        info = aoi_coverage(ds, box)
    assert info["coverage"] == pytest.approx(1.0)
    assert info["missing_pct"] == pytest.approx(0.0)
    assert info["overlap_area_px"] == info["aoi_area_px"]


def test_aoi_coverage_half_outside(grid):
    """AOI 一半越出栅格左边界 → coverage≈0.5，missing≈50%。

    这正是 EX11 的根因形态：下载只覆盖 AOI 一半，检测按完整 AOI 裁切。
    """
    box = BBox(99.9, 20.2, 100.1, 20.4)  # lon 99.9~100.1，其中 99.9~100.0 越界
    with rasterio.open(str(grid)) as ds:
        info = aoi_coverage(ds, box)
    assert info["coverage"] == pytest.approx(0.5, abs=0.02), info
    assert info["missing_pct"] == pytest.approx(50.0, abs=2.0)
    # 裁剪后窗口左边界被夹到 0，宽度只剩落在栅格内的那一半（约 10 列）
    assert info["clipped_window"].col_off == 0
    assert info["clipped_window"].width < info["aoi_area_px"] ** 0.5


def test_aoi_coverage_completely_outside(grid):
    """AOI 完全在栅格外 → coverage=0.0。"""
    box = BBox(90.0, 10.0, 90.2, 10.2)
    with rasterio.open(str(grid)) as ds:
        info = aoi_coverage(ds, box)
    assert info["coverage"] == pytest.approx(0.0)
    assert info["overlap_area_px"] == 0


def test_aoi_coverage_min_constant_is_090():
    """阈值口径固定为 0.90（常量源，不硬编码）。"""
    assert AOI_COVERAGE_MIN == pytest.approx(0.90)
    assert MIN_AOI_COVERAGE == AOI_COVERAGE_MIN


# --------------------------------------------------------------------------- #
# 诊断层
# --------------------------------------------------------------------------- #
def test_coverage_diagnosis_none_when_ok():
    """覆盖率达标 → 无诊断。"""
    assert coverage_diagnosis({"coverage": 0.95}, "t1") is None
    assert coverage_diagnosis({"coverage": AOI_COVERAGE_MIN}, "t1") is None


def test_coverage_diagnosis_text_when_low():
    """覆盖率不足 → 文案含百分比与可执行建议，带 [t1] 时相前缀。"""
    text = coverage_diagnosis({"coverage": 0.105, "missing_pct": 89.5}, "t1")
    assert text is not None and text.startswith("[t1]")
    assert "10.5%" in text and "89.5%" in text
    assert "AOI" in text and "EX11" in text
    assert "缩小 AOI" in text


def test_ex11_diagnosis_from_real_cog(grid):
    """失败路径：从真实 B04 算覆盖率，越界型给诊断、覆盖合格型返回 None。"""
    # AOI 一半越界 → 有诊断
    bad = _ex11_coverage_diagnosis(str(grid), BBox(99.9, 20.2, 100.1, 20.4))
    assert bad is not None and "AOI" in bad
    # AOI 完整在内（云多型 EX11）→ 无诊断，不误导
    good = _ex11_coverage_diagnosis(str(grid), BBox(100.1, 20.1, 100.3, 20.3))
    assert good is None
    # 坏路径 → 吞异常返回 None，绝不影响主错误
    assert _ex11_coverage_diagnosis(str(grid) + ".missing", BBox(100.1, 20.1, 100.3, 20.3)) is None


# --------------------------------------------------------------------------- #
# discover 层：footprint 几何估算
# --------------------------------------------------------------------------- #
def _feature_with_geometry(coords):
    return {"id": "S2B_49RFN_20230805_0_L2A",
            "geometry": {"type": "Polygon", "coordinates": [coords]}}


def test_bbox_coverage_feature_geometry():
    """STAC footprint 完整覆盖 AOI → 1.0；部分覆盖 → <1；缺 geometry → None（放过）。"""
    aoi = BBox(112.8, 29.0, 112.9, 29.1)
    full = _feature_with_geometry([
        [112.0, 28.5], [113.5, 28.5], [113.5, 29.5], [112.0, 29.5], [112.0, 28.5],
    ])
    assert _bbox_coverage(aoi, full) == pytest.approx(1.0)
    partial = _feature_with_geometry([
        [112.85, 28.5], [113.5, 28.5], [113.5, 29.5], [112.85, 29.5], [112.85, 28.5],
    ])
    assert 0 < _bbox_coverage(aoi, partial) < 1.0
    assert _bbox_coverage(aoi, {"id": "x"}) is None  # 缺 geometry → 不过滤


def test_diagnosis_template_uses_constant_source():
    """诊断模板逐字取常量源，未硬编码。"""
    rendered = AOI_COVERAGE_DIAGNOSIS.format(pct=10.5, min=90, miss=89.5)
    assert "10.5%" in rendered and "89.5%" in rendered and "90%" in rendered


# --------------------------------------------------------------------------- #
# fetch 拒绝层（核心：坏数据进不了库）
# --------------------------------------------------------------------------- #
def test_verify_coverage_rejects_partial_aoi(grid):
    """覆盖率不足 → _verify_coverage 抛 EX2，message 带可执行诊断（透传前端）。"""
    with rasterio.open(str(grid)) as ds:
        with pytest.raises(ApiError) as ei:
            _verify_coverage(ds, BBox(99.9, 20.2, 100.1, 20.4), "t1")
    err = ei.value
    assert err.code == "EX2"
    assert "AOI" in err.message and "缩小 AOI" in err.message
    assert err.message.startswith("[t1]")


def test_verify_coverage_accepts_full_aoi(grid):
    """覆盖率达标 → _verify_coverage 返回 info，不抛异常（正常取数不受影响）。"""
    with rasterio.open(str(grid)) as ds:
        info = _verify_coverage(ds, BBox(100.1, 20.1, 100.3, 20.3), "t2")
    assert info["coverage"] == pytest.approx(1.0)

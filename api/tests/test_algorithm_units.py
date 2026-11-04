# -*- coding: utf-8 -*-
"""算法单元：数值正确性 + 错误码边界（REQ-01/03/04/06/07/09/12/13/14）。

纯函数 + 固定种子 / 合成数组，结果确定性；不连数据库（除常量口径校验）。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

from app.algorithm.accuracy import (
    SampleInsufficientError, accuracy_report, compute_confusion_matrix,
)
from app.algorithm.change_detector import change_pixel_ratio, change_stats
from app.algorithm.composite import (
    GridMismatchError, assert_grid_aligned, false_color, linear_stretch,
    rgb_to_hwc, true_color,
)
from app.algorithm.geotransform import AoiTooLargeError, BBox, validate_aoi_span
from app.algorithm.index_calculator import INDEX_DOMAIN, clip_index, ndvi, ndwi
from app.algorithm.otsu import otsu_threshold
from app.algorithm.radiometric import (
    BaselineUndeterminableError, FormulaMixedError, assert_same_caliber,
    check_baseline, resolve_formula, to_reflectance,
)
from app.algorithm.writers import ExportFormatError, validate_export_formats
from app.api.errors import ApiError, CODE_HTTP, as_api_error
from app.config.constants import (
    AOI_MAX_SPAN_DEG, ERROR_MESSAGES, EXPORT_FORMATS, KAPPA_TEACHING_MIN,
    MAX_CONCURRENCY, MIN_SAMPLES_PER_CLASS, OTSU_FALLBACK_DNDVI,
    S2_BASELINE_MIN, S2_DN_SCALE, S2_FORMULA, SENSOR_L8, SENSOR_S2,
    s2_reflectance,
)
from app.config.env import API_ROOT, DOTENV_PATH
from app.service.concurrency import serial


# --------------------------------------------------------------------------- #
# 硬约束 1 自证：.env 生效 + sys.path 正确
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-14")
def test_dotenv_and_syspath_ready():
    assert str(API_ROOT) in sys.path
    assert DOTENV_PATH.is_file()
    assert os.getenv("MINIO_ENDPOINT") == "localhost:19000"       # .env 覆盖生效
    assert os.getenv("PG_DSN", "").startswith("postgresql://")
    assert sys.version_info[:2] == (3, 12)                          # 指定解释器 3.12


# --------------------------------------------------------------------------- #
# REQ-06 NDVI / NDWI / clip_index
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-06")
def test_ndvi_zero_denominator_is_nan_and_domain_clipped():
    red = np.array([[0.0, 0.1, 0.001, 0.2]], dtype=np.float32)
    nir = np.array([[0.0, 0.3, -0.5, 0.2]], dtype=np.float32)
    out = ndvi(red, nir)
    assert out.dtype == np.float32
    assert np.isnan(out[0, 0])                                  # 0/0 → NaN
    assert float(out[0, 1]) == pytest.approx(0.5, abs=1e-6)    # (0.3-0.1)/(0.3+0.1)
    assert float(out[0, 2]) == pytest.approx(1.0, abs=1e-6)    # 近零分母放大 → 截断
    assert float(out[0, 3]) == pytest.approx(0.0, abs=1e-6)


@pytest.mark.req("REQ-06")
def test_clip_index_domain_and_nan_propagation():
    arr = np.array([-99.0, -1.0, 0.0, 1.0, 99.0, np.nan], dtype=np.float32)
    out = clip_index(arr)
    assert float(out[0]) == INDEX_DOMAIN[0]
    assert float(out[4]) == INDEX_DOMAIN[1]
    assert np.isnan(out[5])
    assert np.all(out[:5] >= INDEX_DOMAIN[0]) and np.all(out[:5] <= INDEX_DOMAIN[1])


@pytest.mark.req("REQ-06")
def test_ndvi_valid_mask_masks_out_to_nan():
    red = np.full((2, 2), 0.1, np.float32)
    nir = np.full((2, 2), 0.3, np.float32)
    vm = np.array([[True, False], [False, True]])
    out = ndvi(red, nir, valid_mask=vm)
    assert np.isnan(out[0, 1]) and np.isnan(out[1, 0])
    assert float(out[0, 0]) == pytest.approx(0.5, abs=1e-6)
    assert float(out[1, 1]) == pytest.approx(0.5, abs=1e-6)
    assert float(ndwi(np.array([[0.3]], np.float32),
                      np.array([[0.1]], np.float32))[0, 0]) == pytest.approx(0.5, abs=1e-6)


# --------------------------------------------------------------------------- #
# REQ-07 Otsu 退化回退
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-07")
@pytest.mark.parametrize("arr", [
    np.full((4, 4), np.nan, np.float32),     # 全 NaN
    np.full((8, 8), 0.2, np.float32),       # 单值单峰直方图
    np.full((2, 2), 0.05, np.float32),      # 有效样本 < MIN_SAMPLES_PER_CLASS
], ids=["all_nan", "single_peak", "too_few_samples"])
def test_otsu_degenerate_falls_back_to_constant(arr):
    t, used, reason = otsu_threshold(arr)
    assert used is True
    assert t == pytest.approx(OTSU_FALLBACK_DNDVI)
    assert isinstance(reason, str) and reason


@pytest.mark.req("REQ-07")
def test_otsu_bimodal_uses_adaptive_threshold():
    rng = np.random.default_rng(7)
    low = rng.normal(0.02, 0.01, 200).astype(np.float32)
    high = rng.normal(0.60, 0.05, 200).astype(np.float32)
    arr = np.concatenate([low, high]).reshape(20, 20)
    t, used, reason = otsu_threshold(arr)
    assert used is False and 0.0 < t < 2.0
    assert t != pytest.approx(OTSU_FALLBACK_DNDVI)
    assert "Otsu" in reason


# --------------------------------------------------------------------------- #
# REQ-09 混淆矩阵 / OA / Kappa / EX10 / 教学级标注
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-09")
def test_confusion_matrix_and_accuracy_report_exact_values():
    yt, yp = [], []
    n = MIN_SAMPLES_PER_CLASS + 5
    for code in (1, 2, 3):
        yt.extend([code] * n)
        yp.extend([code if i % 4 else (code % 3) + 1 for i in range(n)])
    yt, yp = np.asarray(yt), np.asarray(yp)
    cm = compute_confusion_matrix(yt, yp)
    assert cm.shape == (3, 3) and cm.dtype == np.int64
    assert int(cm.sum()) == yt.size
    rep = accuracy_report(yt, yp, run_id="unit-run")
    assert rep["confusion_matrix"] == cm.tolist()
    assert rep["overall_accuracy"] == pytest.approx(float(np.trace(cm)) / float(cm.sum()))
    assert -1.0 <= rep["kappa"] <= 1.0
    assert rep["total"] == yt.size and rep["correct"] == int(np.trace(cm))
    assert rep["labels"] == [1, 2, 3]
    assert rep["min_samples_per_class"] == MIN_SAMPLES_PER_CLASS
    assert rep["run_id"] == "unit-run"


@pytest.mark.req("REQ-09")
def test_accuracy_report_ex10_when_any_class_below_min():
    yt = np.array([1] * (MIN_SAMPLES_PER_CLASS - 1) + [2] * 40 + [3] * 40)
    with pytest.raises(SampleInsufficientError) as e:
        accuracy_report(yt, yt)
    assert e.value.code == "EX10"
    assert ERROR_MESSAGES["EX10"] in str(e.value)


@pytest.mark.req("REQ-09")
def test_accuracy_report_teaching_level_note():
    n = MIN_SAMPLES_PER_CLASS + 5
    yt = np.array([1] * n + [2] * n + [3] * n)
    yp = np.array([2] * n + [3] * n + [1] * n)     # 全循环错分 → Kappa < 0
    rep = accuracy_report(yt, yp)
    assert rep["overall_accuracy"] == pytest.approx(0.0)
    assert rep["kappa"] < KAPPA_TEACHING_MIN
    assert rep["note"] == "教学级，非工程级"
    assert rep["teaching_level"] is True


# --------------------------------------------------------------------------- #
# REQ-03 网格对齐 EX4 / REQ-01 跨度 EX1
# --------------------------------------------------------------------------- #
def _profile(**kw):
    base = {"transform": (10.0, 0.0, 100.0, 0.0, -10.0, 200.0),
            "width": 10, "height": 10, "crs": "EPSG:32650"}
    base.update(kw)
    return base


@pytest.mark.req("REQ-03")
def test_assert_grid_aligned_ok_and_ex4_mismatch():
    assert assert_grid_aligned(_profile(), _profile()) is True
    bad_profiles = (
        dict(_profile(), transform=(10.0, 0.0, 110.0, 0.0, -10.0, 200.0)),
        dict(_profile(), width=11),
        dict(_profile(), crs="EPSG:4326"),
    )
    for bad in bad_profiles:
        with pytest.raises(GridMismatchError) as e:
            assert_grid_aligned(_profile(), bad)
        assert e.value.code == "EX4"
        assert e.value.fields


@pytest.mark.req("REQ-01")
def test_validate_aoi_span_ex1():
    ok = BBox.from_sequence([116.25, 29.15,
                             116.25 + AOI_MAX_SPAN_DEG, 29.15 + AOI_MAX_SPAN_DEG])
    assert validate_aoi_span(ok) is ok
    big = BBox.from_sequence([116.25, 29.15,
                              116.25 + AOI_MAX_SPAN_DEG + 0.01, 29.25])
    with pytest.raises(AoiTooLargeError) as e:
        validate_aoi_span(big)
    assert e.value.code == "EX1"


# --------------------------------------------------------------------------- #
# REQ-04 辐射口径 EX8 / EX9 / 定标公式（禁止 (DN-1000)/10000）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-04")
def test_assert_same_caliber_ok_and_ex8_sensor_mixed():
    assert assert_same_caliber(SENSOR_S2, SENSOR_S2) == SENSOR_S2
    assert assert_same_caliber("s2", " S2 ") == SENSOR_S2
    # 关键风险：构造 S2 + L8 混用
    with pytest.raises(FormulaMixedError) as e:
        assert_same_caliber(SENSOR_S2, SENSOR_L8)
    assert e.value.code == "EX8"
    with pytest.raises(FormulaMixedError) as e2:
        assert_same_caliber("UNKNOWN", "UNKNOWN")
    assert e2.value.code == "EX8"


@pytest.mark.req("REQ-04")
def test_check_baseline_ex9_missing_or_below_min():
    assert check_baseline(SENSOR_S2, "05.09") == "05.09"
    assert check_baseline(SENSOR_S2, S2_BASELINE_MIN) == S2_BASELINE_MIN
    # 关键风险：基线缺失 / < 04.00
    for bad in (None, "", "03.99", "not-a-version"):
        with pytest.raises(BaselineUndeterminableError) as e:
            check_baseline(SENSOR_S2, bad)
        assert e.value.code == "EX9"
    assert check_baseline(SENSOR_L8, "C2L2") == "C2L2"
    with pytest.raises(BaselineUndeterminableError) as e:
        check_baseline(SENSOR_L8, "C1L1")
    assert e.value.code == "EX9"


@pytest.mark.req("REQ-04")
def test_s2_reflectance_is_dn_over_scale():
    """辐射口径铁律：ρ = DN / S2_DN_SCALE；禁止断言 (DN-1000)/10000。"""
    dn = np.array([0, 1000, 1836, 5000, 10000], dtype=np.uint16)
    assert S2_FORMULA == "rho=DN/10000"
    assert S2_FORMULA != "rho=(DN-1000)/10000"
    np.testing.assert_allclose(s2_reflectance(dn), dn / S2_DN_SCALE, rtol=1e-6)
    np.testing.assert_allclose(
        to_reflectance(dn, SENSOR_S2, proc_baseline="05.09"),
        dn.astype(np.float32) / np.float32(S2_DN_SCALE), rtol=1e-6,
    )
    assert resolve_formula(SENSOR_S2)["formula_id"] == "S2_L2A_BOA"
    with pytest.raises(FormulaMixedError) as e:
        resolve_formula(SENSOR_S2, formula_id="LS_C2L2_SR")   # 拿错公式 → EX8
    assert e.value.code == "EX8"


# --------------------------------------------------------------------------- #
# REQ-12 导出白名单 EX6
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-12")
def test_validate_export_formats_ok_and_ex6():
    assert validate_export_formats(list(EXPORT_FORMATS)) == tuple(EXPORT_FORMATS)
    assert validate_export_formats(["GeoTIFF", " PNG ", "PDF"]) == tuple(EXPORT_FORMATS)
    with pytest.raises(ExportFormatError) as e:
        validate_export_formats(["svg"])
    assert e.value.code == "EX6"
    assert ERROR_MESSAGES["EX6"] in str(e.value)


# --------------------------------------------------------------------------- #
# REQ-13 MVP 并发上限 1 / EX7 同步阻塞（无队列、无异步）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-13")
def test_mvp_max_concurrency_one_and_ex7_on_reentry():
    assert MAX_CONCURRENCY == 1
    with serial("outer"):
        with pytest.raises(ApiError) as e:
            with serial("inner"):
                pass
        assert e.value.code == "EX7" and e.value.http_status == 429
    with serial("again"):        # 释放后可再次占用（同步阻塞，不排队）
        assert True


# --------------------------------------------------------------------------- #
# HLD 第 5 章错误码 → HTTP 映射 / 统一错误体
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-14")
def test_error_mapping_matches_hld_5_2():
    assert CODE_HTTP["EX2"] == 404
    assert CODE_HTTP["EX1"] == CODE_HTTP["EX3"] == CODE_HTTP["EX10"] == 400
    assert CODE_HTTP["EX8"] == 400 and CODE_HTTP["EX9"] == 400
    assert CODE_HTTP["EX6"] == 400 and CODE_HTTP["EX11"] == 400
    assert CODE_HTTP["EX7"] == 429
    assert all(CODE_HTTP[c] == 500
               for c in ("SYS-01", "SYS-02", "SYS-03", "SYS-04", "SYS-05"))
    e = ApiError("EX10", run_id="r-1")
    assert (e.code, e.message, e.run_id, e.http_status) == (
        "EX10", ERROR_MESSAGES["EX10"], "r-1", 400,
    )
    mapped = as_api_error(SampleInsufficientError("short"), run_id="r-2")
    assert mapped.code == "EX10" and mapped.run_id == "r-2"
    assert as_api_error(ValueError("boom")).code == "SYS-01"


# --------------------------------------------------------------------------- #
# REQ-10 真彩/假彩合成（专题图渲染，纯函数确定性）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-10")
def test_composite_true_false_color_and_stretch_deterministic():
    h, w = 8, 8
    rng = np.random.default_rng(3)
    r = rng.uniform(0.02, 0.30, (h, w)).astype(np.float32)
    g = rng.uniform(0.02, 0.30, (h, w)).astype(np.float32)
    b = rng.uniform(0.02, 0.30, (h, w)).astype(np.float32)
    nir = rng.uniform(0.20, 0.60, (h, w)).astype(np.float32)
    tc, fc = true_color(r, g, b), false_color(nir, r, g)
    assert tc.shape == fc.shape == (3, h, w)
    assert tc.dtype == fc.dtype == np.uint8
    assert rgb_to_hwc(tc).shape == (h, w, 3)
    assert np.array_equal(true_color(r, g, b), tc)          # 同输入同输出
    # 常量数组（无对比度）不引入随机数 → 全 0
    assert int(linear_stretch(np.full((4, 4), 0.2, np.float32)).sum()) == 0


# --------------------------------------------------------------------------- #
# REQ-11 三类面积统计 / 变化像元占比
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-11")
def test_change_stats_area_ha_and_pixel_ratio():
    change = np.array([[1, 1, 0], [2, 3, 3], [0, 3, 1]], dtype=np.uint8)
    profile = {"transform": (10.0, 0.0, 0.0, 0.0, -10.0, 0.0)}
    out = change_stats(change, profile=profile, run_id="area-run")
    area_m2 = abs(10.0 * -10.0)      # 100 m² / 像元
    assert out["pixel_area_m2"] == pytest.approx(area_m2)
    counts = {int(i["change_class"]): int(i["pixel_count"]) for i in out["stats"]}
    assert counts == {1: 3, 2: 1, 3: 3}
    for item in out["stats"]:
        assert item["area_ha"] == pytest.approx(
            item["pixel_count"] * area_m2 / 10000.0,
        )
    assert out["invalid_count"] == 2 and out["valid_count"] == 7
    assert change_pixel_ratio(change) == pytest.approx(4.0 / 7.0)

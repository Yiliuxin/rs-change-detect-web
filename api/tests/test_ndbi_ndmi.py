# -*- coding: utf-8 -*-
"""批次 D11：NDBI / NDMI 指数接入单测（REQ-06 / REQ-07 / REQ-14）。

覆盖：公式手算与互为相反数、无效 NaN / 分母近零 / 物理定义域裁剪 / valid_mask、
``build_index_raster`` 4 指数分派与非法文案、``INDEX_AUXILIARY`` 四项映射、
B11 缺失 EX11 中文明确报错、NDVI / NDWI 辅助配对回归不变、VALID_INDEXES 与
EX12 / 模板枚举文案含 4 项、compare 变体接受 NDBI / NDMI 枚举、不可复算变体
返回明确 EX12 而非 500，以及真实鄱阳湖 B11 数值范围 sanity。

测试卫生：纯函数 + 合成上下文（monkeypatch），不触发真实 COG 下载、不跑整条
``/result/change`` 重链路；不改任何既有测试与 data/ 下文件。
"""
from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.algorithm import index_calculator as IC
from app.algorithm.geotransform import BBox
from app.algorithm.index_raster import build_index_raster
from app.api.errors import ApiError
from app.config import constants as C
from app.service import change_service as CS
from app.service import experiment_service as ES
from app.service import template_service as TS
from app.service.common import ARTIFACT_MEDIA_TYPES, DATA_ROOT

_PROFILE = {"transform": (10.0, 0.0, 0.0, 0.0, -10.0, 0.0)}


# --------------------------------------------------------------------------- #
# 1~4. 公式手算 / 无效值 / 定义域 / valid_mask（REQ-06 / REQ-07）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-06")
@pytest.mark.req("REQ-07")
def test_ndbi_formula_hand_calculated():
    """NDBI = (SWIR − NIR)/(SWIR + NIR)：手算 0.5 / 0 / 0÷0→NaN，float32。"""
    swir = np.array([[0.3, 0.2, 0.0]], dtype=np.float32)
    nir = np.array([[0.1, 0.2, 0.0]], dtype=np.float32)
    out = IC.ndbi(swir, nir)
    assert out.dtype == np.float32
    assert out.shape == (1, 3)
    assert float(out[0, 0]) == pytest.approx(0.5, abs=1e-6)   # (0.3-0.1)/(0.3+0.1)
    assert float(out[0, 1]) == pytest.approx(0.0, abs=1e-6)   # (0.2-0.2)/(0.2+0.2)
    assert np.isnan(out[0, 2])                                 # 0/0 分母近零 → NaN


@pytest.mark.req("REQ-06")
@pytest.mark.req("REQ-07")
def test_ndmi_formula_and_opposite_of_ndbi():
    """NDMI = (NIR − SWIR)/(NIR + SWIR)，与同波段对的 NDBI 逐点互为相反数。"""
    swir = np.array([[0.3, 0.5, 0.1, 0.4]], dtype=np.float32)
    nir = np.array([[0.1, 0.2, 0.6, 0.4]], dtype=np.float32)
    b = IC.ndbi(swir, nir)
    m = IC.ndmi(nir, swir)
    assert m.dtype == np.float32
    assert float(m[0, 0]) == pytest.approx(-0.5, abs=1e-6)    # (0.1-0.3)/(0.1+0.3)
    assert float(m[0, 3]) == pytest.approx(0.0, abs=1e-6)
    np.testing.assert_allclose(m, -b, rtol=0.0, atol=1e-6)    # NDBI = −NDMI


@pytest.mark.req("REQ-06")
def test_ndbi_ndmi_invalid_nan_denom_and_domain_clip():
    """NaN 输入传播、|分母|<1e-6 → NaN、近零分母放大后裁剪回 [-1, 1]。"""
    swir = np.array([[np.nan, 0.0, 1e-7, 0.3]], dtype=np.float32)
    nir = np.array([[0.2, 0.0, -1e-7, -0.2999]], dtype=np.float32)
    b = IC.ndbi(swir, nir)
    assert np.isnan(b[0, 0])                                   # NaN 输入
    assert np.isnan(b[0, 1])                                   # 0/0
    assert np.isnan(b[0, 2])                                   # sum≈0 → 分母近零
    assert -1.0 <= float(b[0, 3]) <= 1.0                       # 放大 → clip
    assert float(b[0, 3]) == pytest.approx(IC.INDEX_DOMAIN[1], abs=1e-6)
    m = IC.ndmi(nir, swir)
    assert np.isnan(m[0, 0]) and np.isnan(m[0, 1]) and np.isnan(m[0, 2])
    assert -1.0 <= float(m[0, 3]) <= 1.0
    assert float(m[0, 3]) == pytest.approx(IC.INDEX_DOMAIN[0], abs=1e-6)


@pytest.mark.req("REQ-06")
def test_ndbi_ndmi_valid_mask_masks_out_to_nan():
    """valid_mask 之外一律 NaN；掩膜内仍为手算值。"""
    swir = np.full((2, 2), 0.3, np.float32)
    nir = np.full((2, 2), 0.1, np.float32)
    vm = np.array([[True, False], [False, True]])
    b = IC.ndbi(swir, nir, valid_mask=vm)
    m = IC.ndmi(nir, swir, valid_mask=vm)
    for arr in (b, m):
        assert np.isnan(arr[0, 1]) and np.isnan(arr[1, 0])
    assert float(b[0, 0]) == pytest.approx(0.5, abs=1e-6)
    assert float(m[0, 0]) == pytest.approx(-0.5, abs=1e-6)
    assert float(b[1, 1]) == pytest.approx(0.5, abs=1e-6)
    assert float(m[1, 1]) == pytest.approx(-0.5, abs=1e-6)


# --------------------------------------------------------------------------- #
# 5~6. build_index_raster 4 指数分派 / 非法文案由 VALID_INDEXES 派生
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-06")
@pytest.mark.req("REQ-07")
@pytest.mark.parametrize("index", list(C.VALID_INDEXES))
def test_build_index_raster_four_indexes_dispatch(index):
    """4 指数都能构建 IndexRaster，且分派到对应纯函数（NDVI/NDWI 不变）。"""
    a = np.array([[0.3]], dtype=np.float32)
    b = np.array([[0.1]], dtype=np.float32)
    raster = build_index_raster(a, b, _PROFILE, index=index)
    assert raster.name == index
    assert raster.data.dtype == np.float32
    calc = {"NDVI": IC.ndvi, "NDWI": IC.ndwi,
            "NDBI": IC.ndbi, "NDMI": IC.ndmi}[index]
    assert float(raster.data[0, 0]) == pytest.approx(float(calc(a, b)[0, 0]), abs=1e-6)


@pytest.mark.req("REQ-07")
def test_build_index_raster_illegal_index_message_lists_four():
    """非法指数 ValueError 文案由 VALID_INDEXES 派生，自动含 4 项。"""
    with pytest.raises(ValueError) as e:
        build_index_raster(np.zeros((1, 1), np.float32), np.ones((1, 1), np.float32),
                           _PROFILE, index="FOO")
    for idx in C.VALID_INDEXES:
        assert idx in str(e.value)


# --------------------------------------------------------------------------- #
# 7~8. INDEX_AUXILIARY 唯一映射源与派生常量
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-07")
@pytest.mark.parametrize("primary,aux", [
    ("NDVI", "NDWI"), ("NDWI", "NDVI"), ("NDBI", "NDVI"), ("NDMI", "NDVI"),
])
def test_index_auxiliary_mapping_four_entries(primary, aux):
    """配对映射写死：NDVI↔NDWI；NDBI / NDMI 均 → NDVI。"""
    assert C.INDEX_AUXILIARY[primary] == aux
    assert primary in C.VALID_INDEXES and aux in C.VALID_INDEXES


@pytest.mark.req("REQ-07")
def test_swir_indexes_and_b11_message_derived():
    """SWIR_INDEXES / B11_MISSING_MESSAGE 由映射与枚举派生，文案逐字对齐。"""
    assert set(C.INDEX_AUXILIARY) == set(C.VALID_INDEXES)
    assert set(C.SWIR_INDEXES) == {"NDBI", "NDMI"}
    assert C.B11_MISSING_MESSAGE == "该影像对未包含 B11 短波红外，不支持 NDBI/NDMI"
    assert "NDBI" in C.B11_MISSING_MESSAGE and "NDMI" in C.B11_MISSING_MESSAGE


# --------------------------------------------------------------------------- #
# 9. B11 缺失 → EX11 中文明确报错（load_scene need_b11=True，不跑重链路）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-07")
def test_b11_missing_raises_ex11_chinese(monkeypatch):
    """need_b11=True 且 B11 缺失 → ApiError(EX11) + 指定中文文案，不抛 SYS-01。"""
    monkeypatch.setattr(CS, "resolve_optional_bands", lambda *a, **k: {})
    bbox = BBox.from_sequence([116.25, 29.15, 116.35, 29.25])
    with pytest.raises(ApiError) as e:
        CS.load_scene("minio://scenes/pair_nob11/t1/", "S2", "05.09",
                      bbox, "run-b11-miss", need_b11=True)
    assert e.value.code == "EX11"
    assert e.value.code != "SYS-01"
    assert e.value.run_id == "run-b11-miss"
    assert C.B11_MISSING_MESSAGE in e.value.message
    assert "该影像对未包含 B11 短波红外，不支持 NDBI/NDMI" in e.value.message


# --------------------------------------------------------------------------- #
# 10. NDVI / NDWI 辅助配对回归不变（回归红线）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-07")
def test_ndvi_ndwi_aux_pairing_regression_unchanged():
    """NDVI 主 aux=NDWI、NDWI 主 aux=NDVI；两者 ndvi/ndwi 数组逐字一致。"""
    assert C.INDEX_AUXILIARY["NDVI"] == "NDWI"
    assert C.INDEX_AUXILIARY["NDWI"] == "NDVI"
    rho = {
        "B04": np.full((2, 2), 0.1, np.float32),
        "B03": np.full((2, 2), 0.3, np.float32),
        "B08": np.full((2, 2), 0.4, np.float32),
    }
    valid = np.ones((2, 2), dtype=bool)
    p_ndvi = CS._build_pair_indexes("NDVI", rho, _PROFILE, valid, "r", {})
    p_ndwi = CS._build_pair_indexes("NDWI", rho, _PROFILE, valid, "r", {})
    assert p_ndvi["primary"].name == "NDVI" and p_ndwi["primary"].name == "NDWI"
    np.testing.assert_allclose(p_ndvi["aux"], p_ndvi["ndwi"], rtol=0.0, atol=1e-6)
    np.testing.assert_allclose(p_ndwi["aux"], p_ndwi["ndvi"], rtol=0.0, atol=1e-6)
    np.testing.assert_allclose(p_ndvi["ndvi"], p_ndwi["ndvi"], rtol=0.0, atol=1e-6)
    np.testing.assert_allclose(p_ndvi["ndwi"], p_ndwi["ndwi"], rtol=0.0, atol=1e-6)


# --------------------------------------------------------------------------- #
# 11. VALID_INDEXES 与 EX12 / 模板枚举文案含 4 项
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-07")
def test_valid_indexes_and_enum_error_text_contains_four():
    """VALID_INDEXES=4 项；变体 EX12 与模板 EX1 文案、白名单均自动含 NDBI/NDMI。"""
    assert set(C.VALID_INDEXES) == {"NDVI", "NDWI", "NDBI", "NDMI"}
    # EX12 变体非法 index 文案由 ' | '.join(VALID_INDEXES) 派生
    with pytest.raises(ApiError) as e:
        ES.validate_variants([{"index": "FOO"}], {"index": "NDVI", "method": "otsu"},
                             run_id="r")
    assert e.value.code == "EX12"
    for idx in C.VALID_INDEXES:
        assert idx in e.value.message
    # 模板 EX1 错误结构不变，枚举文案由 '/'.join(VALID_INDEXES) 派生
    with pytest.raises(ApiError) as e2:
        TS.normalize_params({"index": "FOO"})
    assert e2.value.code == "EX1"
    for idx in C.VALID_INDEXES:
        assert idx in e2.value.message
    # common.py 白名单由 VALID_INDEXES 循环生成（逻辑不变，NDBI/NDMI 已登记）
    assert "change_ndbi.tif" in ARTIFACT_MEDIA_TYPES
    assert "change_ndmi.tif" in ARTIFACT_MEDIA_TYPES


# --------------------------------------------------------------------------- #
# 12. compare 变体接受 NDBI / NDMI 枚举（validate_variants，避免重链路）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-07")
@pytest.mark.parametrize("idx", ["NDBI", "NDMI"])
def test_compare_variants_accept_ndbi_ndmi_enum(idx):
    """VALID_INDEXES 扩到 4 项后，compare 变体校验接受 NDBI / NDMI。"""
    out = ES.validate_variants(
        [{"index": idx, "method": "otsu"}],
        {"index": "NDVI", "method": "otsu"}, run_id="r",
    )
    assert out[0]["index"] == idx and out[0]["method"] == "otsu"


# --------------------------------------------------------------------------- #
# 13. 不可复算变体 → 明确中文 EX12 而非 500（合成 run，直接调 compare）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-07")
def test_unrecomputable_variant_returns_ex12_not_500(monkeypatch, tmp_path):
    """基准 NDVI run 只有 {NDVI, NDWI} 两键，请求 NDBI 变体 → EX12(400) 中文提示。"""
    run_id = "run-synth-d11"
    root = tmp_path / "runs" / run_id
    root.mkdir(parents=True, exist_ok=True)
    h = w = 8
    arr = np.full((h, w), 0.3, dtype=np.float32)
    prof = {"driver": "GTiff", "dtype": "float32", "count": 2,
            "width": w, "height": h, "crs": "EPSG:4326",
            "transform": from_origin(116.25, 29.25, 0.0001, 0.0001),
            "nodata": float("nan")}
    for name in (C.INDEX_RASTER_T1, C.INDEX_RASTER_T2):
        with rasterio.open(str(root / name), "w", **prof) as ds:
            ds.write(arr, 1)
            ds.write(arr + 0.01, 2)
    snap = {
        "index": "NDVI", "method": "otsu", "threshold": 0.2,
        "threshold_source": "manual", "pixel_area_m2": 100.0,
        "stats": [{"change_class": 3, "pixel_count": h * w,
                   "area_ha": h * w * 100.0 / 10000.0}],
        "transition_band": {"ratio": 0.0}, "test_data": True,
    }
    monkeypatch.setattr(
        ES.task_repo, "get_task",
        lambda rid: {"run_id": rid, "params_snapshot": snap, "user_id": None},
    )
    monkeypatch.setattr(ES, "run_dir", lambda rid: root)

    with pytest.raises(ApiError) as e:
        ES.compare(run_id, [{"index": "NDBI", "method": "otsu"}], force=True)
    assert e.value.code == "EX12" and e.value.http_status == 400
    assert e.value.code != "SYS-01"                      # 不是 KeyError → 500
    assert "仅包含 NDVI/NDWI" in e.value.message
    assert "不支持 NDBI" in e.value.message
    assert "请先以 NDBI 为指数重新检测" in e.value.message


# --------------------------------------------------------------------------- #
# 14. 真实鄱阳湖 B11 数值范围 sanity（只读两景文件，不跑整条检测链路）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-06")
def test_real_poyang_b11_index_value_range_sanity():
    """用真实 t1_B11/t1_B08 走 ρ=DN/10000 定标，NDBI 有限值恒落在 [-1, 1]。"""
    pair = DATA_ROOT / "pair_poyang_20230804_20231102"
    b11_path, b08_path = pair / "t1_B11.tif", pair / "t1_B08.tif"
    if not (b11_path.is_file() and b08_path.is_file()):
        pytest.skip("鄱阳湖 B11 / B08 数据缺失，跳过真实数值 sanity")
    with rasterio.open(str(b11_path)) as ds:
        b11 = ds.read(1).astype(np.float32)
    with rasterio.open(str(b08_path)) as ds:
        b08 = ds.read(1).astype(np.float32)
    if b11.shape != b08.shape:
        pytest.skip("B11 / B08 形状不一致，跳过真实数值 sanity")
    # B11 为 DN 量级，与 B08 同口径走 ρ=DN/10000（不额外缩放）
    rho_b11 = b11 / np.float32(C.S2_DN_SCALE)
    rho_b08 = b08 / np.float32(C.S2_DN_SCALE)
    out = IC.ndbi(rho_b11, rho_b08)
    finite = out[np.isfinite(out)]
    assert finite.size > 0
    assert float(finite.min()) >= IC.INDEX_DOMAIN[0]
    assert float(finite.max()) <= IC.INDEX_DOMAIN[1]

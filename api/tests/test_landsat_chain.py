# -*- coding: utf-8 -*-
"""Landsat C2 L2 链路接通单测（REQ-04 / REQ-05 / REQ-02）。

覆盖：
  * QA_PIXEL 位解析：bit0/1/3/4/5 任一命中即无效，bit2/6/7 不参与判定；
  * DN=0 归 NODATA、QA_PIXEL 16 位位域不被 uint8 截断；
  * 掩膜前置于定标、valid_ratio 四段判定与 SCL 分支共用不改；
  * cog_source 按 sensor 分派 SR_B2..B5 / QA_PIXEL 资产别名；
  * 双公式分派（S2 rho=DN/10000、L8/L9 rho=2.75e-5·DN−0.2）与 EX8 混源回归。

接口测自建的 aoi / task 在 finally 中按 FK 顺序删除，不依赖全局 cleanup 脚本。
"""
from __future__ import annotations

import shutil

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.algorithm.mask_builder import (
    QA_PIXEL_FILL_VALUE, build_qa_pixel_masks, landsat_dn_nodata_mask,
    qa_pixel_bit_mask, resample_qa_pixel,
)
from app.algorithm.mask_pipeline import judge_valid_ratio, run_mask_pipeline
from app.algorithm.radiometric import (
    FormulaMixedError, assert_same_caliber, resolve_formula, to_reflectance,
)
from app.config.constants import (
    LANDSAT_NODATA_DN, LS_FORMULA, LS_OFFSET, LS_SCALE,
    QA_PIXEL_BIT_CLOUD, QA_PIXEL_BIT_CLOUD_SHADOW, QA_PIXEL_BIT_DILATED_CLOUD,
    QA_PIXEL_BIT_FILL, QA_PIXEL_BIT_SNOW, QA_PIXEL_CLOUD_BITS,
    QA_PIXEL_INVALID_BITS, QA_PIXEL_SHADOW_BITS, QA_PIXEL_SNOW_BITS,
    S2_FORMULA, SENSOR_L8, SENSOR_S2, VALID_RATIO_MIN, VALID_RATIO_WARN,
)
from app.repository.db import db_cursor
from app.service import change_service, cog_source
from app.service.common import run_dir
from _helpers import (
    POYANG_T1, POYANG_T2, assert_error_body, poyang_geojson,
)

CHANGE_URL = "/api/v1/result/change"


# --------------------------------------------------------------------------- #
# 1. QA_PIXEL 位解析
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-05")
def test_qa_pixel_invalid_bits_constants():
    """USGS C2 L2 位定义：fill=0 / dilated=1 / cloud=3 / shadow=4 / snow=5。"""
    assert QA_PIXEL_INVALID_BITS == (0, 1, 3, 4, 5)
    assert QA_PIXEL_CLOUD_BITS == (1, 3)
    assert QA_PIXEL_SHADOW_BITS == (4,) and QA_PIXEL_SNOW_BITS == (5,)
    assert (QA_PIXEL_BIT_FILL, QA_PIXEL_BIT_DILATED_CLOUD,
            QA_PIXEL_BIT_CLOUD, QA_PIXEL_BIT_CLOUD_SHADOW,
            QA_PIXEL_BIT_SNOW) == (0, 1, 3, 4, 5)
    assert QA_PIXEL_FILL_VALUE == 1


@pytest.mark.req("REQ-05")
def test_qa_pixel_any_invalid_bit_marks_invalid():
    """bit0/1/3/4/5 任一命中 → 无效；bit2 cirrus / bit6 clear / bit7 water 不参与。"""
    qa = np.array([
        [0, 1 << 0, 1 << 1, 1 << 3],
        [1 << 4, 1 << 5, 1 << 2, 1 << 6],
        [1 << 7, (1 << 6) | (1 << 7), (1 << 3) | (1 << 7), (1 << 0) | (1 << 5)],
    ], dtype=np.uint16)
    masks = build_qa_pixel_masks(qa)
    expected_invalid = np.array([
        [False, True, True, True],
        [True, True, False, False],
        [False, False, True, True],
    ])
    assert np.array_equal(masks.invalid, expected_invalid)
    assert np.array_equal(masks.valid, ~expected_invalid)
    c = masks.counts()
    assert c["total"] == qa.size and c["valid"] == int((~expected_invalid).sum())
    assert c["invalid"] == int(expected_invalid.sum())


@pytest.mark.req("REQ-05")
def test_qa_pixel_group_masks_and_nodata_union():
    """分组统计（云/云影/雪）+ nodata_mask 并入 invalid，且 valid 取补集。"""
    qa = np.array([
        [(1 << 1), (1 << 3), (1 << 4), (1 << 5)],
        [(1 << 0), (1 << 6), 0, (1 << 7)],
    ], dtype=np.uint16)
    nodata = np.zeros(qa.shape, dtype=bool)
    nodata[1, 3] = True
    m = build_qa_pixel_masks(qa, nodata_mask=nodata)
    assert m.cloud[0, 0] and m.cloud[0, 1]
    assert m.cloud_shadow[0, 2] and m.snow[0, 3]
    assert m.invalid[1, 0] and m.invalid[1, 3]
    assert not m.valid[1, 3]
    assert np.array_equal(m.valid, ~m.invalid)
    assert qa_pixel_bit_mask(np.array([1 << 9], dtype=np.uint16), (9,)).all()


@pytest.mark.req("REQ-05")
def test_qa_pixel_16bit_values_not_truncated(tmp_path):
    """16 位位域（含高位）直读不截断；未覆盖区填 Fill 位而非 0。"""
    arr = np.array([[55052, 21824], [0, 1 << 15]], dtype=np.uint16)
    profile = {"driver": "GTiff", "dtype": "uint16", "count": 1,
               "width": 2, "height": 2, "crs": "EPSG:32650",
               "transform": from_origin(392835.0, 3219495.0, 30.0, 30.0),
               "nodata": 0}
    p = tmp_path / "qa.tif"
    with rasterio.open(str(p), "w", **profile) as ds:
        ds.write(arr, 1)
    aligned = resample_qa_pixel(str(p), profile)
    assert aligned.dtype == np.uint32
    assert np.array_equal(aligned.astype(np.uint16), arr)
    assert qa_pixel_bit_mask(aligned, (15,))[1, 1]


@pytest.mark.req("REQ-05")
def test_landsat_dn_zero_is_nodata():
    """SR 任一波段 DN=0 → NODATA 掩膜命中；全正值时无命中。"""
    dn = {
        "B02": np.array([[100, 200], [300, 400]], dtype=np.uint16),
        "B03": np.array([[100, 200], [300, 400]], dtype=np.uint16),
        "B04": np.array([[100, LANDSAT_NODATA_DN], [300, 400]], dtype=np.uint16),
        "B08": np.array([[100, 200], [300, 400]], dtype=np.uint16),
    }
    m = landsat_dn_nodata_mask(dn, ("B02", "B03", "B04", "B08"))
    assert m.dtype == bool and m.shape == (2, 2)
    assert m[0, 1] and not m.any(axis=1)[1]
    assert int(m.sum()) == 1


@pytest.mark.req("REQ-05")
def test_mask_pipeline_qa_pixel_branch_keeps_four_segments(tmp_path):
    """qa_pixel 分支与 SCL 分支共用 judge_valid_ratio；四段判定阈值口径不改。"""
    qa = np.full((10, 10), np.uint16(1 << 0), dtype=np.uint16)  # 全 Fill → 0 有效
    qa[:8, :8] = np.uint16(1 << 6)                              # 64 有效 / 100
    profile = {"driver": "GTiff", "dtype": "uint16", "count": 1,
               "width": 10, "height": 10, "crs": "EPSG:32650",
               "transform": from_origin(392835.0, 3219495.0, 30.0, 30.0),
               "nodata": 0}
    p = tmp_path / "qa2.tif"
    with rasterio.open(str(p), "w", **profile) as ds:
        ds.write(qa, 1)
    ref = dict(profile)
    masks, verdict = run_mask_pipeline(str(p), ref, qa_kind="qa_pixel")
    assert int(masks.valid.sum()) == 64
    assert verdict.valid_ratio == pytest.approx(0.64) and verdict.code == "OK"
    assert verdict == judge_valid_ratio(0.64, mask=masks.valid)
    assert VALID_RATIO_MIN == 0.10 and VALID_RATIO_WARN == 0.50
    # 四段边界逐段一致（终止 / 告警 / 通过）
    assert judge_valid_ratio(0.0).code == "EX3"
    assert judge_valid_ratio(VALID_RATIO_MIN - 0.01).code == "EX11"
    assert judge_valid_ratio(VALID_RATIO_MIN).code == "EX5"
    assert judge_valid_ratio(VALID_RATIO_WARN).code == "OK"


# --------------------------------------------------------------------------- #
# 2. cog_source 资产别名与双公式分派
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-02")
def test_cog_source_landsat_asset_aliases(fake_minio, monkeypatch):
    """L8/L9 前缀按 SR_B2..B5 / QA_PIXEL 别名解析，仍返回统一 BANDS 键。"""
    from app.service.cog_source import BANDS

    monkeypatch.setattr(
        cog_source, "list_objects",
        lambda c, bucket, prefix="": [
            f"{prefix}t1_{name}.tif"
            for name in ("SR_B2", "SR_B3", "SR_B4", "SR_B5", "QA_PIXEL")
        ],
    )
    paths = cog_source.resolve_bands("minio://scenes/landsat_poyang/t1/", "run-ls",
                                     sensor=SENSOR_L8)
    assert set(paths) == set(BANDS)
    assert paths["B02"].name.endswith("SR_B2.tif")
    assert paths["B08"].name.endswith("SR_B5.tif")
    assert paths["SCL"].name.endswith("QA_PIXEL.tif")
    assert len(fake_minio.downloads) == len(BANDS)


@pytest.mark.req("REQ-02")
def test_cog_source_stac_asset_names(fake_minio, monkeypatch):
    """同一 sensor 下 blue/green/red/nir08/qa_pixel 资产名也可解析。"""
    monkeypatch.setattr(
        cog_source, "list_objects",
        lambda c, bucket, prefix="": [
            f"{prefix}t1_{name}.tif"
            for name in ("blue", "green", "red", "nir08", "qa_pixel")
        ],
    )
    paths = cog_source.resolve_bands("minio://scenes/landsat_poyang/t1/", "run-ls2",
                                     sensor="L9")
    assert set(paths) == {"B02", "B03", "B04", "B08", "SCL"}


@pytest.mark.req("REQ-04")
def test_dual_radiometric_formula_dispatch():
    """S2 与 Landsat 公式按 sensor 锁定，禁止互套（EX8）。"""
    dn = np.array([0, 1000, 10000, 40000], dtype=np.uint16)
    assert S2_FORMULA == "rho=DN/10000" and LS_FORMULA == "rho=2.75e-5*DN-0.2"
    assert resolve_formula(SENSOR_S2)["text"] == S2_FORMULA
    assert resolve_formula(SENSOR_L8)["text"] == LS_FORMULA
    np.testing.assert_allclose(
        to_reflectance(dn, SENSOR_L8, proc_baseline="C2L2"),
        (LS_SCALE * dn.astype(np.float32) + LS_OFFSET).astype(np.float32), rtol=1e-6,
    )
    np.testing.assert_allclose(
        to_reflectance(dn, SENSOR_S2, proc_baseline="05.09"),
        dn.astype(np.float32) / 10000.0, rtol=1e-6,
    )
    assert not np.allclose(to_reflectance(dn, SENSOR_L8),
                           to_reflectance(dn, SENSOR_S2))
    assert assert_same_caliber(SENSOR_L8, SENSOR_L8) == SENSOR_L8
    with pytest.raises(FormulaMixedError) as e:
        assert_same_caliber(SENSOR_S2, SENSOR_L8)
    assert e.value.code == "EX8"


# --------------------------------------------------------------------------- #
# 3. EX8 混源回归（自建自清）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def fake_minio(monkeypatch, tmp_path):
    """复用 test_cog_source 的假 MinIO（本模块内独立定义，避免跨测试模块耦合）。"""
    class _FakeClient:
        def __init__(self):
            self.downloads = []

        def fget_object(self, bucket, obj, local):
            self.downloads.append(obj)
            from pathlib import Path
            Path(local).write_text(obj, encoding="utf-8")

    client = _FakeClient()
    monkeypatch.setattr(cog_source, "get_client", lambda settings: client)
    monkeypatch.setattr(
        cog_source, "StorageSettings",
        type("S", (), {"from_env": staticmethod(lambda: None)}),
    )
    monkeypatch.setattr(cog_source, "RUNS_ROOT", tmp_path)
    return client


@pytest.mark.req("REQ-02")
def test_scene_match_precise_ids_exposes_landsat_pair(client):
    """精确 scene id 查询可列出 Landsat 双时相，且锁定 LS_C2L2_SR 公式口径。"""
    with db_cursor() as cur:
        cur.execute(
            "SELECT scene_id::text FROM scene WHERE cog_path LIKE %s ORDER BY acq_date",
            ("minio://scenes/landsat_%",),
        )
        ids = [r["scene_id"] for r in cur.fetchall()]
    if len(ids) != 2:
        pytest.skip("未 seed Landsat 影像对（data/fetch_landsat.py 尚未执行）")
    r = client.get("/api/v1/scene/match",
                   params={"scene_t1_id": ids[0], "scene_t2_id": ids[1]})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["count"] == 2 and b["sensor"] == SENSOR_L8
    assert b["formula_id"] == resolve_formula(SENSOR_L8)["formula_id"]
    assert all(s["cog_path"].startswith("minio://scenes/landsat_") for s in b["scenes"])


@pytest.mark.req("REQ-04")
def test_ex8_mixed_sensor_pair_regression(client, monkeypatch):
    """S2 t1 + L8 t2 混源 → 409 EX8（HTTP 映射 / 文案沿用既有不变）。"""
    a = client.post("/api/v1/aoi/select", json={
        "aoi_geom": poyang_geojson(), "t1": POYANG_T1, "t2": POYANG_T2,
    })
    assert a.status_code == 200, a.text
    run_id, aoi_id = a.json()["run_id"], a.json()["aoi_id"]
    real_get = change_service.scene_repo.get_scene

    def _mixed(scene_id):
        sc = real_get(scene_id)
        if sc and str(sc.get("acq_date"))[:10] == POYANG_T2:
            sc = dict(sc, sensor=SENSOR_L8, proc_baseline="C2L2")
        return sc

    monkeypatch.setattr(change_service.scene_repo, "get_scene", _mixed)
    try:
        r = client.post(CHANGE_URL, json={"run_id": run_id})
        assert_error_body(r, "EX8", 409)
    finally:
        with db_cursor() as cur:
            cur.execute("DELETE FROM change_result WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM sample WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM task WHERE run_id = %s", (run_id,))
            cur.execute(
                "DELETE FROM aoi WHERE aoi_id = %s"
                " AND aoi_id NOT IN (SELECT aoi_id FROM scene)", (aoi_id,),
            )
        shutil.rmtree(run_dir(run_id), ignore_errors=True)

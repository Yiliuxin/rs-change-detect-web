# -*- coding: utf-8 -*-
"""批次 G · 变化向量分析（CVA）：算法单测 + 管线冒烟 + 批次 E 方法对比（REQ-07/11/14）。

覆盖任务书「新增 pytest ≥6 条」清单：
  * 幅值 sqrt(Σ_b (t2_b − t1_b)²) 的逐像元精确值 / float32 / NaN 与 valid_mask；
  * Otsu 幅值阈值、手动幅值阈值与三类判别（复用 classify_change / clean_change_map）；
  * 方法枚举与 EX12 校验（含批次 E 变体 EX12 文案同步）；
  * 真实鄱阳湖影像对跑 method=cva 全链路（自建自清）；
  * 批次 E 方法对比矩阵中 cva 与 otsu / regression 的双方法对比 + 缓存结构向后兼容；
  * 既有差值法鄱阳湖基准数字（T / 三类面积）逐项不变。

纯函数与复算用例全部走合成数据，不连数据库、不下载 COG；真实链路用例
复用 test_regression_change.py 同款自建自清 fixture，不依赖全局 cleanup 脚本。
"""
from __future__ import annotations

import json
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.algorithm.change_detector import (
    CHANGE_DEGRADED, CHANGE_ENHANCED, CHANGE_STABLE,
)
from app.algorithm.cva_detector import (
    cva_magnitude, cva_signed_magnitude, detect_change_cva,
)
from app.api.errors import ApiError
from app.config.constants import (
    EXPERIMENTS_CACHE_VERSION, METHOD_CVA, METHOD_OTSU, METHOD_REGRESSION,
    VALID_CHANGE_METHODS,
)
from app.repository.db import db_cursor
from app.service import change_async, change_service, experiment_service as ES
from app.service.common import ARTIFACT_MEDIA_TYPES, run_dir
from _helpers import POYANG_T1, POYANG_T2, assert_error_body, poyang_geojson

URL = "/api/v1/result/change"
AOI_URL = "/api/v1/aoi/select"
COMPARE_URL = "/api/v1/result/compare"
PROBE_URL = "/api/v1/result/change/probe"
BLOCK_ENH = (slice(10, 40), slice(10, 40))
BLOCK_DEG = (slice(60, 90), slice(65, 95))
H = W = 64


# --------------------------------------------------------------------------- #
# 合成数据：双波段指数栅格 [主指数, 辅助指数]
# --------------------------------------------------------------------------- #
def _synthetic_blocks():
    """稳定背景 + 增强块 / 退化块；辅助指数独立变化，保证幅值 ≠ |Δ主|。"""
    rng = np.random.default_rng(20261120)
    p1 = rng.uniform(0.10, 0.75, (120, 120)).astype(np.float32)
    a1 = rng.uniform(-0.20, 0.20, (120, 120)).astype(np.float32)
    p2, a2 = p1.copy(), a1.copy()
    p2[BLOCK_ENH] += np.float32(0.45)
    a2[BLOCK_ENH] += np.float32(0.30)
    p2[BLOCK_DEG] -= np.float32(0.45)
    a2[BLOCK_DEG] -= np.float32(0.30)
    return (p1, a1), (p2, a2), np.ones(p1.shape, dtype=bool)


def _synthetic_aux_dominant():
    """主指数 Δ 仅 0.05（T=0.20 下差值法判稳定），辅助指数 Δ=0.50 → CVA 幅值 ≈ 0.5025。"""
    grid = np.linspace(0.10, 0.70, H * W, dtype=np.float32).reshape(H, W)
    p1, a1 = grid.copy(), np.full((H, W), 0.10, dtype=np.float32)
    p2, a2 = p1 + np.float32(0.05), a1 + np.float32(0.50)
    return p1, a1, p2, a2


def _ctx_from_bands(p1, a1, p2, a2) -> ES.ExperimentContext:
    """只构造内存上下文，不写盘、不连 DB（批次 E 唯一复算入口的纯函数上下文）。"""
    return ES.ExperimentContext(
        run_id="cva-unit", base_index="NDVI", base_method=METHOD_OTSU,
        base_threshold=0.20, base_threshold_source="manual",
        pixel_area_m2=100.0,
        index_t1={"NDVI": p1, "NDWI": a1},
        index_t2={"NDVI": p2, "NDWI": a2},
        base_change=None,
        valid_mask=np.ones((H, W), dtype=bool),
        out_dir=Path("."),
        snapshot={},
    )


# --------------------------------------------------------------------------- #
# 1. 幅值计算单测（任务书规格 1/2）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-11")
def test_cva_magnitude_exact_two_bands_and_nan_semantics():
    """手算 sqrt(0.3²+0.1²)=sqrt(0.10)、sqrt((−0.4)²+0²)=0.4；float32 / NaN / valid_mask。"""
    p1 = np.array([[0.2, 0.5], [np.nan, 0.3]], dtype=np.float32)
    a1 = np.array([[0.1, 0.2], [0.1, 0.2]], dtype=np.float32)
    p2 = np.array([[0.5, 0.1], [0.9, 0.3]], dtype=np.float32)
    a2 = np.array([[0.2, 0.2], [0.1, 0.2]], dtype=np.float32)
    t1, t2 = (p1, a1), (p2, a2)

    mag = cva_magnitude(t1, t2)
    signed = cva_signed_magnitude(t1, t2)
    assert mag.dtype == np.float32 and signed.dtype == np.float32
    assert mag[0, 0] == pytest.approx(float(np.sqrt(0.10)), abs=1e-6)
    assert mag[0, 1] == pytest.approx(0.40, abs=1e-6)
    assert mag[1, 1] == pytest.approx(0.0)
    assert np.isnan(mag[1, 0]) and np.isnan(signed[1, 0])

    # 方向取主指数差符号：正 → +幅值，负 → −幅值；|signed| 恒等于幅值
    assert signed[0, 0] > 0 and signed[0, 1] < 0
    finite = np.isfinite(mag)
    assert np.allclose(np.abs(signed)[finite], mag[finite], atol=1e-6)

    # 过渡带口径 |CVA − T| ≤ 0.25T 由 |signed| ≡ CVA 直接对齐既有 |Δ| 语义
    vm = np.array([[True, False], [True, True]], dtype=bool)
    masked = cva_magnitude(t1, t2, valid_mask=vm)
    assert np.isnan(masked[0, 1])       # valid_mask 外为 NaN
    assert np.isnan(masked[1, 0])       # 任一时相/波段 NaN → NaN 传播


@pytest.mark.req("REQ-11")
def test_cva_single_band_degenerates_to_abs_delta():
    """单波段时 sqrt(Σ_b Δ²) = |Δ|，与差值法的 |Δ| 口径自然对齐。"""
    p1 = np.array([[0.1, -0.3], [0.5, 0.2]], dtype=np.float32)
    p2 = np.array([[0.4, -0.5], [0.1, 0.2]], dtype=np.float32)
    assert np.allclose(cva_magnitude(p1, p2), np.abs(p2 - p1), atol=1e-6)
    stack1 = np.stack([p1, p1])
    stack2 = np.stack([p2, p2])
    assert np.allclose(cva_magnitude(stack1, stack2), np.sqrt(2.0) * np.abs(p2 - p1),
                       atol=1e-6)


# --------------------------------------------------------------------------- #
# 2. Otsu 幅值阈值 / 手动幅值阈值 / 三类划分（任务书规格 3/4）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-11")
def test_cva_otsu_threshold_and_three_class_partition():
    """对幅值图做 Otsu：增强块 → 1、退化块 → 2、稳定背景 → 3，不回退。"""
    t1, t2, vm = _synthetic_blocks()
    out = detect_change_cva(t1, t2, valid_mask=vm)
    assert out["threshold_source"] == "otsu"
    assert out["otsu_fallback"] is False
    assert 0.0 < float(out["threshold"]) < 0.60
    assert np.array_equal(out["magnitude"], cva_magnitude(t1, t2, valid_mask=vm))

    change = out["change"]
    assert change.dtype == np.uint8
    assert set(np.unique(change)) <= {0, 1, 2, 3}
    assert np.mean(change[BLOCK_ENH] == CHANGE_ENHANCED) > 0.80
    assert np.mean(change[BLOCK_DEG] == CHANGE_DEGRADED) > 0.80
    background = change.copy()
    background[BLOCK_ENH] = 0
    background[BLOCK_DEG] = 0
    kept = background[background != 0]
    assert np.mean(kept == CHANGE_STABLE) > 0.90


@pytest.mark.req("REQ-11")
def test_cva_manual_threshold_is_magnitude_threshold_and_symmetric():
    """手动阈值跳过 Otsu，语义为幅值阈值；负号取 |T| 判别等价（与既有两法一致）。"""
    t1, t2, vm = _synthetic_blocks()
    pos = detect_change_cva(t1, t2, valid_mask=vm, threshold=0.20)
    neg = detect_change_cva(t1, t2, valid_mask=vm, threshold=-0.20)
    assert pos["threshold"] == pytest.approx(0.20)
    assert pos["threshold_source"] == "manual"
    assert pos["otsu_fallback"] is False
    assert "手动" in pos["otsu_reason"]
    assert np.array_equal(pos["change"], neg["change"])
    assert np.array_equal(pos["change_raw"], neg["change_raw"])


# --------------------------------------------------------------------------- #
# 3. 方法枚举 / EX12 校验（任务书「EX12 枚举」）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-11")
def test_method_constants_whitelist_and_validate_method():
    assert METHOD_CVA == "cva"
    assert tuple(VALID_CHANGE_METHODS) == (METHOD_OTSU, METHOD_REGRESSION, METHOD_CVA)
    assert change_service._validate_method(" CVA ", "r1") == METHOD_CVA
    with pytest.raises(ApiError) as exc:
        change_service._validate_method("linear", "r1")
    assert exc.value.code == "EX12"
    assert "cva" in str(exc.value.detail)


@pytest.mark.req("REQ-11")
def test_compare_variants_accept_cva_and_ex12_text_synced():
    """批次 E 变体枚举接受 cva；EX12 文案由白名单派生，须含 3 种方法。"""
    meta = {"index": "NDVI", "method": METHOD_OTSU}
    got = ES.validate_variants([{"method": " CVA "}], meta, run_id="r1")
    assert got == [{"index": "NDVI", "method": METHOD_CVA,
                    "threshold_mode": "otsu", "threshold": None}]

    with pytest.raises(ApiError) as exc:
        ES.validate_variants([{"method": "cva2"}], meta, run_id="r1")
    assert exc.value.code == "EX12"
    assert "仅支持 otsu | regression | cva" in exc.value.message

    # 既有语义不变：全同基准仍为 EX1，超限仍为 EX12
    with pytest.raises(ApiError) as same:
        ES.validate_variants([{}], {"index": "NDWI", "method": METHOD_CVA}, run_id="r1")
    assert same.value.code == "EX1"
    with pytest.raises(ApiError) as too_many:
        ES.validate_variants([{"method": METHOD_CVA}] * 5, meta, run_id="r1")
    assert too_many.value.code == "EX12"


@pytest.mark.req("REQ-11")
@pytest.mark.parametrize("bad_method", ["linear", "EVI", "CVA2", "", "otsu2"])
def test_ex12_illegal_method_http(client, change_ready, bad_method):
    """非法 method → 400 EX12；cva 加入白名单不改变既有错误体口径。"""
    r = client.post(URL, json={"run_id": change_ready["run_id"], "method": bad_method})
    body = assert_error_body(r, "EX12", 400)
    assert body["run_id"] == change_ready["run_id"]


# --------------------------------------------------------------------------- #
# 4. 批次 E 双方法对比（任务书「双方法对比」）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-11")
def test_recompute_cva_vs_otsu_and_regression():
    """同一上下文三方法复算：辅助指数变化使 CVA 与差值法/回归法判别不同。"""
    ctx = _ctx_from_bands(*_synthetic_aux_dominant())
    otsu = ES.recompute(ctx, "NDVI", METHOD_OTSU,
                         threshold_mode="manual", threshold=0.20)
    cva = ES.recompute(ctx, "NDVI", METHOD_CVA,
                        threshold_mode="manual", threshold=0.20)
    reg = ES.recompute(ctx, "NDVI", METHOD_REGRESSION,
                       threshold_mode="manual", threshold=0.20)

    for r in (otsu, cva, reg):
        assert [int(a["change_class"]) for a in r["areas"]] == [1, 2, 3]
        assert 0.0 <= r["transition_pct"] <= 100.0
        assert r["change"].shape == (H, W)
    assert cva["config"]["method"] == METHOD_CVA
    assert cva["threshold"] == pytest.approx(0.20)     # 手动「幅值阈值」
    assert cva["areas"][0]["pixel_count"] == H * W    # 幅值 > T → 全部增强
    assert otsu["areas"][2]["pixel_count"] == H * W   # |Δ| < T → 全部稳定
    assert reg["areas"][2]["pixel_count"] == H * W    # 残差 ≈ 0 → 全部稳定
    assert ES.agreement_pct(otsu["change"], cva["change"]) == pytest.approx(0.0)
    # 面积总量守恒，且三方法同源（形态学不改有效像元总数）
    total = H * W * 100.0 / 10000.0
    for r in (otsu, cva, reg):
        assert sum(float(a["area_ha"]) for a in r["areas"]) == pytest.approx(total, rel=1e-6)


@pytest.fixture()
def synth_run(tmp_path, monkeypatch):
    """合成 run：只读实验所需产物落 tmp_path，不碰真实 runs / DB。"""
    run_id = str(uuid.uuid4())
    root = tmp_path / "runs" / run_id
    root.mkdir(parents=True, exist_ok=True)
    p1, a1, p2, a2 = _synthetic_aux_dominant()
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 2,
        "width": W, "height": H, "crs": "EPSG:4326",
        "transform": from_origin(116.25, 29.25, 0.0001, 0.0001),
        "nodata": float("nan"),
    }
    for name, primary, auxiliary in (
        ("index_t1.tif", p1, a1), ("index_t2.tif", p2, a2),
    ):
        with rasterio.open(str(root / name), "w", **profile) as ds:
            ds.write(primary, 1)
            ds.write(auxiliary, 2)
    snap = {
        "index": "NDVI", "method": METHOD_OTSU,
        "threshold": 0.20, "threshold_source": "manual",
        "pixel_area_m2": 100.0,
        "stats": [
            {"change_class": 1, "pixel_count": 0, "area_ha": 0.0},
            {"change_class": 2, "pixel_count": 0, "area_ha": 0.0},
            {"change_class": 3, "pixel_count": H * W,
             "area_ha": H * W * 100.0 / 10000.0},
        ],
        "transition_band": {"ratio": 0.0},
        "test_data": True,
    }
    monkeypatch.setattr(
        ES.task_repo, "get_task",
        lambda rid: {"run_id": rid, "params_snapshot": snap, "user_id": None},
    )
    monkeypatch.setattr(ES, "run_dir", lambda rid: root)
    return {"run_id": run_id, "dir": root}


@pytest.mark.req("REQ-11")
def test_compare_endpoint_cva_variant_and_cache_backward_compatible(client, synth_run):
    """/result/compare 接受 cva 变体并实时算出；experiments.json 结构版次不变。"""
    resp = client.post(COMPARE_URL, json={
        "run_id": synth_run["run_id"],
        "variants": [
            {"index": "NDVI", "method": METHOD_CVA},
            {"index": "NDVI", "method": METHOD_REGRESSION},
        ],
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [v["config"]["method"] for v in body["variants"]] == [METHOD_CVA, METHOD_REGRESSION]
    assert [int(a["change_class"]) for a in body["variants"][0]["areas"]] == [1, 2, 3]
    assert body["variants"][0]["areas"][0]["pixel_count"] == H * W
    assert 0.0 <= body["variants"][0]["agreement_pct"] <= 100.0
    assert body["computed_at"]

    cache = json.loads((synth_run["dir"] / "experiments.json").read_text(encoding="utf-8"))
    assert cache["version"] == EXPERIMENTS_CACHE_VERSION == 1
    assert cache["compare"] and "sensitivity" in cache     # 既有键保持，向后兼容
    assert not (synth_run["dir"] / "change.png").exists()  # 只读复算不落盘产物


# --------------------------------------------------------------------------- #
# 5. 无效输入（任务书「无效输入」）
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-11")
@pytest.mark.parametrize("t1,t2,mask,expected", [
    ([np.zeros((2, 2), np.float32)], [np.zeros((2, 2), np.float32)] * 2, None, "波段数不一致"),
    ([np.zeros((2, 2), np.float32), np.zeros((3, 2), np.float32)],
     [np.zeros((2, 2), np.float32)] * 2, None, "波段形状不一致"),
    ([], [], None, "波段列表不能为空"),
    ([np.zeros((2, 2), np.float32)], [np.zeros((2, 2), np.float32)],
     np.ones((3, 3), dtype=bool), "valid_mask 形状"),
    (np.zeros(3, np.float32), np.zeros(3, np.float32), None, "需为 2D"),
    (3.14, 3.14, None, "需为 numpy 数组"),
])
def test_cva_invalid_inputs_raise_value_error(t1, t2, mask, expected):
    with pytest.raises(ValueError, match=expected):
        cva_magnitude(t1, t2, valid_mask=mask)
    with pytest.raises(ValueError, match=expected):
        detect_change_cva(t1, t2, valid_mask=mask)


# --------------------------------------------------------------------------- #
# 6. 真实链路冒烟 + 既有两法回归数字不变
# --------------------------------------------------------------------------- #
def _new_task(client):
    r = client.post(AOI_URL, json={
        "aoi_geom": poyang_geojson(), "t1": POYANG_T1, "t2": POYANG_T2,
    })
    assert r.status_code == 200, r.text
    return r.json()["run_id"], r.json()["aoi_id"]


def _cleanup(run_id: str, aoi_id: str) -> None:
    """按 FK 顺序删除本用例产生的数据与产物目录。"""
    with db_cursor() as cur:
        cur.execute("DELETE FROM change_result WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM sample WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM task WHERE run_id = %s", (run_id,))
        cur.execute(
            "DELETE FROM aoi WHERE aoi_id = %s"
            " AND aoi_id NOT IN (SELECT aoi_id FROM scene)",
            (aoi_id,),
        )
    shutil.rmtree(run_dir(run_id), ignore_errors=True)
    change_async._tasks.pop(run_id, None)


@contextmanager
def _temp_run(client):
    rid, aid = _new_task(client)
    try:
        yield rid
    finally:
        _cleanup(rid, aid)


@pytest.mark.req("REQ-11")
def test_cva_pipeline_smoke_on_poyang(client):
    """真实鄱阳湖影像对跑 method=cva 全链路：阈值/三类/快照/产物/探针。"""
    with _temp_run(client) as rid:
        r = client.post(URL, json={"run_id": rid, "method": " CVA "})
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["method"] == METHOD_CVA
        assert b["index"] == "NDVI"
        assert b["threshold_source"] == "otsu"
        assert 0.0 < float(b["threshold"]) < 1.0
        assert [int(s["change_class"]) for s in b["stats"]] == [1, 2, 3]
        assert all(float(s["area_ha"]) >= 0 for s in b["stats"])
        assert sum(float(s["area_ha"]) for s in b["stats"]) > 0

        snap = b["params_snapshot"]
        assert snap["method"] == METHOD_CVA
        assert snap["cva_threshold"] == pytest.approx(b["threshold"])
        assert snap["cva_bands"] == ["NDVI", "NDWI"]
        assert b["change_geotiff"].endswith("/change.tif")
        assert b["change_png"].endswith("/change.png")
        assert "变化向量分析" in (b.get("conclusion") or "")

        out = run_dir(rid)
        for name in ("change.tif", "change.png", "report.pdf",
                     "index_t1.tif", "index_t2.tif", "transition_band_mask.npy"):
            assert (out / name).is_file() and (out / name).stat().st_size > 0
        for name in ("change.tif", "change.png", "index_t1.tif", "index_t2.tif"):
            assert name in b["artifacts"]
            assert name in ARTIFACT_MEDIA_TYPES
            assert client.get(b["artifacts"][name]).status_code == 200

        probe = client.get(PROBE_URL, params={
            "run_id": rid, "lng": 116.30, "lat": 29.20,
        })
        assert probe.status_code == 200, probe.text
        assert probe.json()["method"] == METHOD_CVA


@pytest.mark.req("REQ-11")
def test_existing_otsu_baseline_numbers_unchanged(change_ready):
    """批次 G 硬约束：既有差值法鄱阳湖基准 T 与三类面积逐项不变（批次 E 实测）。"""
    b = change_ready["change_body"]
    assert float(b["threshold"]) == pytest.approx(0.38671875, abs=1e-6)
    areas = {int(s["change_class"]): float(s["area_ha"]) for s in b["stats"]}
    assert areas[1] == pytest.approx(2334.06, abs=0.01)
    assert areas[2] == pytest.approx(97.33, abs=0.01)
    assert areas[3] == pytest.approx(4364.75, abs=0.01)

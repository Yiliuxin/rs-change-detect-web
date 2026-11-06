# -*- coding: utf-8 -*-
"""REQ-11（提前实现）回归残差法变化检测：算法单测 + 接口正交组合 + 产物隔离。

* 纯函数单测用固定种子合成「已知线性关系 + 强变化块」，断言 a/b 恢复精度、
  迭代轮数与三类划分；不连数据库不写盘。
* 接口测真实跑预置鄱阳湖影像对；所有新建的 aoi / task / change_result /
  sample 与 runs 目录在 fixture teardown 自行删除，不依赖全局 cleanup 脚本。
"""
from __future__ import annotations

import shutil
from contextlib import contextmanager

import numpy as np
import pytest

from app.algorithm.change_detector import (
    CHANGE_DEGRADED, CHANGE_ENHANCED, CHANGE_STABLE,
)
from app.algorithm.regression_detector import (
    detect_change_regression, fit_robust_regression, residual_map,
)
from app.config.constants import (
    METHOD_OTSU, METHOD_REGRESSION, REGRESSION_MAX_ROUNDS,
    REGRESSION_SIGMA_FACTOR, VALID_CHANGE_METHODS,
)
from app.repository.db import db_cursor
from app.service import change_async, change_service
from app.service.common import ARTIFACT_MEDIA_TYPES, run_dir
from _helpers import (
    POYANG_T1, POYANG_T2, assert_error_body, poyang_geojson,
)

URL = "/api/v1/result/change"
AOI_URL = "/api/v1/aoi/select"
TRUE_A, TRUE_B = 0.85, 0.06
BLOCK_ENH = (slice(10, 40), slice(10, 40))
BLOCK_DEG = (slice(60, 90), slice(65, 95))


# --------------------------------------------------------------------------- #
# 合成数据：NDVI_t2 = 0.85·NDVI_t1 + 0.06 + 小噪声，叠加两个强变化方块
# --------------------------------------------------------------------------- #
def _synthetic():
    rng = np.random.default_rng(20261106)
    x = rng.uniform(0.10, 0.75, (120, 120)).astype(np.float32)
    noise = rng.normal(0.0, 0.008, x.shape).astype(np.float32)
    y = (TRUE_A * x + TRUE_B + noise).astype(np.float32)
    y[BLOCK_ENH] += np.float32(0.45)
    y[BLOCK_DEG] -= np.float32(0.45)
    return x, y, np.ones(x.shape, dtype=bool)


# --------------------------------------------------------------------------- #
# 1. 算法单测
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-11")
def test_robust_fit_recovers_linear_coefficients_and_rounds():
    """稳健 LS 剔除强变化块后恢复 a/b；迭代轮数 ≤ 常量上限，粗差被剔除。"""
    x, y, vm = _synthetic()
    fit = fit_robust_regression(x, y, valid_mask=vm)
    assert fit.a == pytest.approx(TRUE_A, abs=0.03)
    assert fit.b == pytest.approx(TRUE_B, abs=0.03)
    assert 1 <= fit.rounds <= REGRESSION_MAX_ROUNDS
    assert fit.n_valid == x.size
    assert fit.n_inliers < fit.n_valid          # 粗差确实被迭代剔除
    assert 0.0 <= fit.sigma < 0.05
    assert np.isnan(fit.residuals).sum() == 0
    assert fit.inlier_mask.dtype == bool and fit.inlier_mask.shape == x.shape


@pytest.mark.req("REQ-11")
def test_residual_map_is_exact_and_masks_invalid():
    """r = t2 − (a·t1 + b)；掩膜外为 NaN，口径与差值法 NaN 传播一致。"""
    x = np.array([[0.2, 0.4], [0.6, 0.8]], dtype=np.float32)
    y = np.array([[0.25, 0.45], [0.65, 0.85]], dtype=np.float32)
    vm = np.array([[True, True], [True, False]])
    r = residual_map(x, y, 1.0, 0.05, valid_mask=vm)
    assert r.dtype == np.float32
    assert np.allclose(r[:2][vm[:2]], 0.0, atol=1e-6)
    assert np.isnan(r[1, 1])


@pytest.mark.req("REQ-11")
def test_regression_otsu_three_class_partition():
    """残差 Otsu 自适应阈值：增强块 → 1、退化块 → 2、稳定背景 → 3。"""
    x, y, vm = _synthetic()
    out = detect_change_regression(x, y, valid_mask=vm)
    assert out["threshold_source"] == "otsu"
    assert out["otsu_fallback"] is False
    assert 0.0 < float(out["threshold"]) < 0.45
    assert out["max_rounds"] == REGRESSION_MAX_ROUNDS
    assert out["sigma_factor"] == REGRESSION_SIGMA_FACTOR

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
def test_regression_manual_threshold_skips_otsu():
    """手动阈值与 method 正交：跳过 Otsu，阈值来源 manual，不回退。"""
    x, y, vm = _synthetic()
    out = detect_change_regression(x, y, valid_mask=vm, threshold=0.20)
    assert out["threshold"] == pytest.approx(0.20)
    assert out["threshold_source"] == "manual"
    assert out["otsu_fallback"] is False
    assert "手动" in out["otsu_reason"]


# --------------------------------------------------------------------------- #
# 2. 测试数据自建自清（不依赖全局 cleanup）
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


@pytest.fixture(scope="module")
def reg_flow(client):
    """真实跑一次「AOI → method=regression」全链路（module 级只跑一次，teardown 自清）。"""
    rid, aid = _new_task(client)
    try:
        r = client.post(URL, json={"run_id": rid, "method": METHOD_REGRESSION})
        assert r.status_code == 200, r.text
        yield {"run_id": rid, "body": r.json()}
    finally:
        _cleanup(rid, aid)


# --------------------------------------------------------------------------- #
# 3. 接口契约
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-11")
def test_method_constants_and_whitelist():
    assert METHOD_OTSU == "otsu" and METHOD_REGRESSION == "regression"
    assert set(VALID_CHANGE_METHODS) == {"otsu", "regression"}
    assert REGRESSION_MAX_ROUNDS == 3 and REGRESSION_SIGMA_FACTOR == 2.0


@pytest.mark.req("REQ-11")
@pytest.mark.parametrize("bad_method", ["linear", "EVI", "REG", "", "otsu2"])
def test_ex12_illegal_method(client, change_ready, bad_method):
    """非法 method → 400 EX12（中文文案 / HTTP 映射沿用既有不变）。"""
    r = client.post(URL, json={
        "run_id": change_ready["run_id"], "method": bad_method,
    })
    body = assert_error_body(r, "EX12", 400)
    assert body["run_id"] == change_ready["run_id"]


@pytest.mark.req("REQ-11")
def test_method_case_insensitive_otsu_keeps_legacy_behavior(client):
    """大小写不敏感；显式 OTSU 与缺省行为完全一致（主名 URL、差值法阈值）。"""
    with _temp_run(client) as rid:
        r = client.post(URL, json={"run_id": rid, "method": " OTSU "})
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["method"] == METHOD_OTSU
        assert b["threshold_source"] == "otsu"
        assert b["change_geotiff"].endswith("/change.tif")
        assert b["change_png"].endswith("/change.png")
        assert b["params_snapshot"]["method"] == METHOD_OTSU
        assert "a" not in b["params_snapshot"]        # 差值法不写回归字段


@pytest.mark.req("REQ-11")
def test_regression_pipeline_snapshot_and_isolated_artifacts(client, reg_flow):
    """真链路：_reg 三件套隔离、快照含 method/a/b/sigma/rounds/reg_threshold。"""
    rid, b = reg_flow["run_id"], reg_flow["body"]
    assert b["method"] == METHOD_REGRESSION
    assert b["change_geotiff"].endswith("/change_reg.tif")
    assert b["change_png"].endswith("/change_reg.png")
    assert [s["change_class"] for s in b["stats"]] == [1, 2, 3]

    snap = b["params_snapshot"]
    for key in ("method", "a", "b", "sigma", "rounds", "reg_threshold",
                "regression_max_rounds", "regression_sigma_factor",
                "regression_valid_count", "regression_inlier_count", "comparison"):
        assert key in snap, key
    assert snap["method"] == METHOD_REGRESSION
    assert snap["rounds"] <= REGRESSION_MAX_ROUNDS
    assert snap["reg_threshold"] == pytest.approx(b["threshold"])
    assert snap["regression_valid_count"] > 0
    assert 0 < snap["regression_inlier_count"] <= snap["regression_valid_count"]
    assert set(snap["comparison"]) == {"difference", "regression"}
    assert len(snap["comparison"]["difference"]["stats"]) == 3
    assert len(snap["comparison"]["regression"]["stats"]) == 3

    out = run_dir(rid)
    for name in ("change_reg.tif", "change_reg.png", "report_reg.pdf",
                 "change_ndvi_reg.tif", "change_ndvi_reg.png",
                 "report_ndvi_reg.pdf", "t1_composite.png", "t2_composite.png"):
        assert (out / name).is_file() and (out / name).stat().st_size > 0
        assert name in b["artifacts"]
        assert name in ARTIFACT_MEDIA_TYPES
        got = client.get(b["artifacts"][name])
        assert got.status_code == 200 and len(got.content) > 0
    # 防覆盖：回归法不落差值法主名三件套，也不登记它们的 URL
    for name in ("change.tif", "change.png", "report.pdf"):
        assert not (out / name).exists()
        assert name not in b["artifacts"]


@pytest.mark.req("REQ-11")
def test_regression_method_index_threshold_orthogonal(client):
    """method × index × threshold 正交：NDWI + 手动阈值 + 回归法同时生效。"""
    with _temp_run(client) as rid:
        r = client.post(URL, json={
            "run_id": rid, "method": "REGRESSION",
            "index": "NDWI", "threshold": 0.18,
        })
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["method"] == METHOD_REGRESSION
        assert b["index"] == "NDWI" and b["threshold"] == pytest.approx(0.18)
        assert b["threshold_source"] == "manual"
        snap = b["params_snapshot"]
        assert snap["method"] == METHOD_REGRESSION and snap["index"] == "NDWI"
        assert snap["reg_threshold"] == pytest.approx(0.18)
        assert "change_ndwi_reg.tif" in b["artifacts"]
        assert "change_ndwi_reg.png" in b["artifacts"]
        assert "report_ndwi_reg.pdf" in b["artifacts"]
        assert (run_dir(rid) / "change_ndwi_reg.tif").is_file()


@pytest.mark.req("REQ-11")
def test_regression_report_export_uses_reg_names(client, reg_flow):
    """回归法 run 的 /report/export 读回 _reg 栅格并输出 _reg 三件套 URL。"""
    rid = reg_flow["run_id"]
    r = client.post("/api/v1/report/export", json={
        "run_id": rid, "formats": ["geotiff", "png", "pdf"],
    })
    assert r.status_code == 200, r.text
    names = [f["url"].rsplit("/", 1)[-1] for f in r.json()["files"]]
    assert set(names) == {"change_reg.tif", "change_reg.png", "report_reg.pdf"}
    assert all(f["size"] > 0 and client.get(f["url"]).status_code == 200
               for f in r.json()["files"])


@pytest.mark.req("REQ-11")
def test_async_regression_method_forwarded(client, monkeypatch):
    """method × async 正交：受理时校验并把 regression 透传给管线。"""
    seen = {}

    def _fake(run_id, index="NDVI", block_size=256, threshold=None,
              on_stage=None, method=METHOD_OTSU):
        seen["method"] = method
        return {
            "run_id": run_id, "change_geotiff": "", "change_png": "",
            "threshold": 0.1, "valid_ratio": 0.9, "stats": [],
            "params_snapshot": {}, "index": index, "method": method,
            "threshold_source": "otsu", "artifacts": {},
        }

    monkeypatch.setattr(change_service, "run_pipeline", _fake)
    with _temp_run(client) as rid:
        acc = client.post(URL, json={"run_id": rid, "method": "regression", "async": True})
        assert acc.status_code == 200, acc.text
        assert acc.json()["status"] == "PENDING"
        assert change_async.wait(rid, timeout=10)
        assert seen.get("method") == METHOD_REGRESSION
        assert change_async.get_status(rid)["status"] == "SUCCESS"


@pytest.mark.req("REQ-11")
def test_async_illegal_method_rejected_at_accept(client):
    """异步非法 method 在受理时即 EX12(400)，不会先返回 PENDING 再后台失败。"""
    with _temp_run(client) as rid:
        r = client.post(URL, json={"run_id": rid, "method": "linear", "async": True})
        assert_error_body(r, "EX12", 400)

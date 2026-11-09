# -*- coding: utf-8 -*-
"""接口测：POST /api/v1/accuracy/sample（HLD 2.2.3.4，REQ-09；EX10 / SYS-01）。

用户硬约束 2（务必先落盘 change.tif，否则命中 SYS-01）：
    sample_service.submit 先 open_change(run_id) 打开 change.tif，再累计每类样本数；
    若缺 /result/change，5 个样本会命中 SYS-01 而不是 EX10。
    完整顺序：① POST /api/v1/aoi/select → ② POST /api/v1/result/change
              → ③ POST /api/v1/accuracy/sample
    ①② 由 session fixture ``change_ready`` 只跑一次（见 tests/conftest.py 的
    make_change_flow，其中 /result/change 明确排在 /accuracy/sample 之前）；
    ③ 在本模块用例里发起，并在发起前断言 change.tif 已落盘。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config.constants import MIN_SAMPLES_PER_CLASS
from app.repository.db import db_cursor
from _helpers import (
    POYANG_T1, POYANG_T2, assert_error_body, build_samples_from_change,
    poyang_geojson,
)

URL = "/api/v1/accuracy/sample"


@pytest.fixture(autouse=True)
def _isolate_samples(change_ready):
    """每个样本用例前后清空该 run 的累计样本 → 与执行顺序无关的确定性隔离。"""
    sql = "DELETE FROM sample WHERE run_id::text = %s"
    with db_cursor() as cur:
        cur.execute(sql, (change_ready["run_id"],))
    yield
    with db_cursor() as cur:
        cur.execute(sql, (change_ready["run_id"],))


@pytest.mark.req("REQ-09")
def test_accuracy_sample_ex10_insufficient(client, change_ready):
    """EX10：先 /result/change（change.tif 已落盘），再只提交 5 个样本 → 400 EX10。"""
    # 前置 ② 产物：POST /api/v1/result/change 已生成 change.tif
    # （若缺失，submit 会命中 SYS-01，本断言先挡住，保证 EX10 语义成立）
    assert Path(change_ready["change_geotiff"]).is_file()

    five = [
        {"x": float(i + 1), "y": float(i + 1), "ref_class": "稳定"}
        for i in range(5)
    ]
    assert len(five) < MIN_SAMPLES_PER_CLASS

    # ③ POST /api/v1/accuracy/sample（在 /result/change 之后）
    r = client.post(URL, json={"run_id": change_ready["run_id"], "samples": five})
    body = assert_error_body(r, "EX10", 400)
    assert body["run_id"] == change_ready["run_id"]


@pytest.mark.req("REQ-09")
def test_accuracy_sample_ok_confusion_matrix_oa_kappa(client, change_ready):
    """正例：同一流程提交每类 ≥30 样本 → 200 + 3×3 混淆矩阵 / OA / Kappa。"""
    assert Path(change_ready["change_geotiff"]).is_file()
    samples = build_samples_from_change(
        change_ready["change_geotiff"], per_class=MIN_SAMPLES_PER_CLASS + 2,
    )
    r = client.post(URL, json={"run_id": change_ready["run_id"], "samples": samples})
    assert r.status_code == 200, r.text
    b = r.json()
    cm = b["confusion_matrix"]
    assert len(cm) == 3 and all(len(row) == 3 for row in cm)
    assert all(isinstance(v, int) for row in cm for v in row)
    assert sum(sum(row) for row in cm) >= 3 * MIN_SAMPLES_PER_CLASS
    # 参考类别与 change.tif 编码一致 → 纯对角阵，OA/Kappa 可精确复核
    assert b["overall_accuracy"] == pytest.approx(1.0, abs=1e-6)
    assert b["kappa"] == pytest.approx(1.0, abs=1e-6)
    assert sum(cm[i][j] for i in range(3) for j in range(3) if i != j) == 0
    assert b["note"] in (None, "", "教学级，非工程级")
    # 逐类生产者精度：纯对角阵 → 三类均为 1.0，键为中文类别名
    pca = b["per_class_accuracy"]
    assert set(pca) == {"增强", "退化", "稳定"}
    assert all(v == pytest.approx(1.0, abs=1e-6) for v in pca.values())


@pytest.mark.req("REQ-09")
def test_accuracy_sample_without_change_is_sys01(client):
    """反证硬约束 2：跳过 /result/change 直接提交样本 → 500 SYS-01（非 EX10）。"""
    a = client.post("/api/v1/aoi/select", json={
        "aoi_geom": poyang_geojson(), "t1": POYANG_T1, "t2": POYANG_T2,
    })
    assert a.status_code == 200
    run_id = a.json()["run_id"]
    r = client.post(URL, json={
        "run_id": run_id,
        "samples": [{"x": 1.0, "y": 1.0, "ref_class": "稳定"}],
    })
    assert_error_body(r, "SYS-01", 500)

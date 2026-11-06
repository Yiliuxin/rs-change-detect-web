# -*- coding: utf-8 -*-
"""接口测：POST /api/v1/accuracy/auto_sample（REQ-09 自动化，半自动分层抽样）。

用 rasterio 真实写一张小的 EPSG:4326 三类编码栅格（像元中心经纬度落在
鄱阳湖 AOI 内），断言：
  * 分层抽样数量 / 经纬度范围 / 不写 sample 表；
  * 同 seed 两次结果逐字相同（RandomState 可复现）；
  * 某类像元不足或为 0 时 per_class_actual 如实记实际数，不报错不凑数；
  * 按 params_snapshot.method 选 change.tif / change_reg.tif；
  * per_class 非法 → EX12，栅格缺失 → SYS-01，run 不存在 → EX2(404)。

用 monkeypatch 隔离 task 查询与产物目录：不写业务库、不污染 api/runs。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.config.constants import AUTO_SAMPLE_PER_CLASS_DEFAULT, AUTO_SAMPLE_PER_CLASS_MAX
from app.repository.sample_repo import count_by_class
from _helpers import assert_error_body

URL = "/api/v1/accuracy/auto_sample"
# 栅格左上角/分辨率：80×80 像元全部落在 AOI [116.25,29.15,116.35,29.25] 内
TRANSFORM = from_origin(116.26, 29.24, 0.001, 0.001)
AOI_BBOX = (116.25, 29.15, 116.35, 29.25)
SIZE = 80


def _write_change(path: Path, cls1: int = 10, cls2: int = 5, cls3: int = 40) -> Path:
    """写三类编码栅格：1 增强 / 2 退化 / 3 稳定 / 0 无效（constants 口径）。"""
    arr = np.zeros((SIZE, SIZE), dtype=np.uint8)
    arr[0:cls1, 0] = 1
    arr[0:cls2, 1] = 2
    arr[cls1:cls1 + cls3, 0] = 3
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        str(path), "w", driver="GTiff", dtype="uint8", count=1,
        width=SIZE, height=SIZE, crs="EPSG:4326", transform=TRANSFORM, nodata=0,
    ) as dst:
        dst.write(arr, 1)
    return path


@pytest.fixture
def fake_run(client, tmp_path, monkeypatch):
    """构造一个隔离的假 run：task 查询 + 产物目录均 monkeypatch，不落业务库。"""
    run_id = str(uuid.uuid4())
    state = {"snapshot": {"method": "otsu", "aoi_wgs84": list(AOI_BBOX)},
             "task_exists": True, "dir": tmp_path}

    def _get_task(_rid: str):
        if not state["task_exists"]:
            return None
        return {"run_id": run_id, "aoi_id": str(uuid.uuid4()),
                "status": "SUCCESS", "params_snapshot": state["snapshot"]}

    monkeypatch.setattr("app.service.sample_service.task_repo.get_task", _get_task)
    monkeypatch.setattr("app.service.sample_service.run_dir", lambda _rid: state["dir"])
    _write_change(tmp_path / "change.tif")
    return run_id, state


def _post(client, run_id: str, per_class=4, seed: Optional[int] = 42):
    body: Dict = {"run_id": run_id, "per_class": per_class}
    if seed is not None:
        body["seed"] = seed
    return client.post(URL, json=body)


def _by_class(samples: List[dict]) -> Dict[str, int]:
    out: Dict[str, int] = {"增强": 0, "退化": 0, "稳定": 0}
    for s in samples:
        out[s["ref_class"]] += 1
    return out


@pytest.mark.req("REQ-09")
def test_auto_sample_stratified_counts_and_lnglat_within_aoi(client, fake_run):
    """每类 4 个、坐标落在 AOI 内、响应字段固定、sample 表零写入。"""
    run_id, _state = fake_run
    r = _post(client, run_id, per_class=4, seed=42)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"run_id", "samples", "per_class_actual"}
    assert body["run_id"] == run_id
    assert body["per_class_actual"] == {"增强": 4, "退化": 4, "稳定": 4}
    assert len(body["samples"]) == 12
    assert _by_class(body["samples"]) == body["per_class_actual"]
    w, s, e, n = AOI_BBOX
    for p in body["samples"]:
        assert set(p) == {"lng", "lat", "ref_class"}
        assert w <= p["lng"] <= e and s <= p["lat"] <= n, p
    # 半自动流程：本接口绝不写 sample 表
    assert count_by_class(run_id) == {"增强": 0, "退化": 0, "稳定": 0}


@pytest.mark.req("REQ-09")
def test_auto_sample_seed_reproducible(client, fake_run):
    """同 seed 两次结果逐字相同；换 seed 结果不同。"""
    run_id, _state = fake_run
    a = _post(client, run_id, per_class=8, seed=2024)
    b = _post(client, run_id, per_class=8, seed=2024)
    c = _post(client, run_id, per_class=8, seed=2025)
    assert a.status_code == b.status_code == c.status_code == 200
    assert a.json()["samples"] == b.json()["samples"], "同 seed 必须可复现"
    assert a.json()["samples"] != c.json()["samples"], "不同 seed 应产生不同抽样"


@pytest.mark.req("REQ-09")
def test_auto_sample_insufficient_and_zero_class_report_actual(client, fake_run, tmp_path):
    """某类可用像元不足（退化仅 5 个）或为 0 时按实际数返回，不报错不凑数。"""
    run_id, state = fake_run
    r = _post(client, run_id, per_class=8, seed=7)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["per_class_actual"] == {"增强": 8, "退化": 5, "稳定": 8}
    assert len(body["samples"]) == 21
    assert _by_class(body["samples"]) == body["per_class_actual"]

    _write_change(tmp_path / "change.tif", cls1=10, cls2=0, cls3=40)
    r2 = _post(client, run_id, per_class=8, seed=7)
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert b2["per_class_actual"]["退化"] == 0
    assert not [p for p in b2["samples"] if p["ref_class"] == "退化"]
    assert len(b2["samples"]) == 16


@pytest.mark.req("REQ-09")
def test_auto_sample_chooses_raster_by_snapshot_method(client, fake_run, tmp_path):
    """method=regression 读 change_reg.tif；既有 /sample/from_lnglat 仍固定读 change.tif。"""
    run_id, state = fake_run
    # change.tif 只有增强类；change_reg.tif 只有退化/稳定类，用分布差异证明选文件
    _write_change(tmp_path / "change.tif", cls1=10, cls2=0, cls3=0)
    _write_change(tmp_path / "change_reg.tif", cls1=0, cls2=10, cls3=10)
    state["snapshot"]["method"] = "regression"
    r = _post(client, run_id, per_class=4, seed=1)
    assert r.status_code == 200, r.text
    assert r.json()["per_class_actual"] == {"增强": 0, "退化": 4, "稳定": 4}

    state["snapshot"]["method"] = "otsu"
    r2 = _post(client, run_id, per_class=4, seed=1)
    assert r2.status_code == 200, r2.text
    assert r2.json()["per_class_actual"] == {"增强": 4, "退化": 0, "稳定": 0}


@pytest.mark.req("REQ-09")
@pytest.mark.parametrize("bad", [0, -1, AUTO_SAMPLE_PER_CLASS_MAX + 1, "abc", 2.5, True])
def test_auto_sample_invalid_per_class_ex12(client, fake_run, bad):
    """per_class 越界 / 非整数 → EX12（400，既有映射），错误体带 run_id。"""
    run_id, _state = fake_run
    r = client.post(URL, json={"run_id": run_id, "per_class": bad})
    body = assert_error_body(r, "EX12", 400)
    assert body["run_id"] == run_id


@pytest.mark.req("REQ-09")
def test_auto_sample_default_per_class(client, fake_run):
    """缺省 per_class 取常量源 32（本例三类均不足 32 → 如实返回 10/5/40→32）。"""
    run_id, _state = fake_run
    r = client.post(URL, json={"run_id": run_id, "seed": 5})
    assert r.status_code == 200, r.text
    assert r.json()["per_class_actual"] == {
        "增强": 10, "退化": 5,
        "稳定": min(40, AUTO_SAMPLE_PER_CLASS_DEFAULT),
    }


@pytest.mark.req("REQ-09")
def test_auto_sample_missing_raster_sys01(client, fake_run):
    """结果栅格缺失 → SYS-01（沿用既有 CODE_HTTP 映射 500）。"""
    run_id, state = fake_run
    (state["dir"] / "change.tif").unlink()
    r = _post(client, run_id)
    body = assert_error_body(r, "SYS-01", 500)
    assert body["run_id"] == run_id


@pytest.mark.req("REQ-09")
def test_auto_sample_unknown_run_ex2(client, fake_run):
    """run 不存在 → EX2(404)。"""
    run_id, state = fake_run
    state["task_exists"] = False
    r = _post(client, run_id)
    body = assert_error_body(r, "EX2", 404)
    assert body["run_id"] == run_id

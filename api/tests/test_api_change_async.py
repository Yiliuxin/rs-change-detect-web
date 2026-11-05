# -*- coding: utf-8 -*-
"""异步变化检测单测（REQ-13 完整实现）。

只测异步编排层（change_async）+ 路由映射，管线本身用 monkeypatch 替换，
避免真实 COG 下载 / 计算（快、确定性）。每个用例自行清理其产生的 aoi / task / run 目录。

覆盖：
  * 状态迁移 PENDING → RUNNING → SUCCESS；
  * 5 阶段进度序列（读裁切→掩膜→指数→变化检测→渲染导出）；
  * 异步并发第二次提交 → EX7(409)；
  * 失败路径（构造 EX11）→ status FAILED + error.code 正确；
  * 未知 task_id → EX2(404)。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from _helpers import POYANG_T1, POYANG_T2, poyang_geojson
from app.api.errors import ApiError
from app.config.constants import ERROR_MESSAGES
from app.repository.db import db_cursor
from app.service import aoi_service, change_async, change_service
from app.service.change_async import STAGES
from app.service.common import RUNS_ROOT


def _make_task():
    """用预置影像对创建一个真实 aoi + task（快速，不下载 COG），返回 (run_id, aoi_id)。"""
    body = aoi_service.select(poyang_geojson(), POYANG_T1, POYANG_T2)
    return body["run_id"], body["aoi_id"]


def _cleanup(run_id: str, aoi_id: str) -> None:
    """删除本用例产生的 task / aoi（按 FK：task → aoi 级联），并清理产物目录与内存记录。"""
    with db_cursor() as cur:
        cur.execute("DELETE FROM task WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM aoi WHERE aoi_id = %s", (aoi_id,))
    d = RUNS_ROOT / run_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    change_async._tasks.pop(run_id, None)


def _fake_pipeline_factory(stages_seen, sleep_per_stage=0.02):
    """构造一个假的 run_pipeline：按 STAGES 顺序推进 on_stage，再返回假 ChangeResponse。"""
    def _fake(run_id, index="NDVI", block_size=256, threshold=None, on_stage=None):
        for s in STAGES:
            if on_stage is not None:
                on_stage(s)
            stages_seen.append(s)
            time.sleep(sleep_per_stage)
        return {
            "run_id": run_id,
            "change_geotiff": f"/api/v1/artifacts/{run_id}/change.tif",
            "change_png": f"/api/v1/artifacts/{run_id}/change.png",
            "threshold": 0.1,
            "valid_ratio": 0.9,
            "stats": [],
            "params_snapshot": {"index": index},
            "index": index,
            "threshold_source": "otsu",
            "artifacts": {},
        }
    return _fake


@pytest.mark.req("REQ-13")
def test_async_state_migration(client, monkeypatch):
    """状态迁移 PENDING → RUNNING → SUCCESS（轮询可见），终态 progress 步进到 5。"""
    run_id, aoi_id = _make_task()
    stages_seen: list = []
    monkeypatch.setattr(change_service, "run_pipeline",
                        _fake_pipeline_factory(stages_seen, sleep_per_stage=0.03))
    try:
        accept = change_async.start_change(run_id)
        assert accept["task_id"] == run_id
        assert accept["status"] == "PENDING"  # 受理即 PENDING

        seen_status = []
        deadline = time.time() + 5.0
        while time.time() < deadline:
            view = change_async.get_status(run_id)
            if view["status"] not in seen_status:
                seen_status.append(view["status"])
            if view["status"] == "SUCCESS":
                break
            time.sleep(0.005)

        # 迁移序列须包含 PENDING（受理）→ RUNNING → SUCCESS
        assert "RUNNING" in seen_status
        assert seen_status[-1] == "SUCCESS"
        final = change_async.get_status(run_id)
        assert final["progress"]["step"] == len(STAGES)
        assert final["progress"]["stage"] == STAGES[-1]
        assert final["result"] is not None
        assert final["result"]["run_id"] == run_id
    finally:
        change_async.wait(run_id)
        _cleanup(run_id, aoi_id)


@pytest.mark.req("REQ-13")
def test_async_stage_sequence(client, monkeypatch):
    """进度阶段序列严格等于 5 阶段顺序（读裁切→掩膜→指数→变化检测→渲染导出）。"""
    run_id, aoi_id = _make_task()
    stages_seen: list = []
    monkeypatch.setattr(change_service, "run_pipeline",
                        _fake_pipeline_factory(stages_seen, sleep_per_stage=0.01))
    try:
        change_async.start_change(run_id)
        change_async.wait(run_id, timeout=10)
        assert stages_seen == STAGES  # 顺序与内容完全一致
        final = change_async.get_status(run_id)
        assert final["status"] == "SUCCESS"
        assert final["progress"]["step"] == len(STAGES)
    finally:
        _cleanup(run_id, aoi_id)


@pytest.mark.req("REQ-13")
def test_async_concurrency_ex7(client, monkeypatch):
    """异步并发第二次提交 → EX7(409)。"""
    run_a, aoi_a = _make_task()
    run_b, aoi_b = _make_task()

    # 第一次提交：后台线程睡眠占位，持有并发闸门（gate）
    def _slow(run_id, index="NDVI", block_size=256, threshold=None, on_stage=None):
        time.sleep(1.0)
        return {"run_id": run_id, "change_geotiff": "", "change_png": "",
                "threshold": 0.1, "valid_ratio": 0.9, "stats": [],
                "params_snapshot": {}, "index": index, "threshold_source": "otsu",
                "artifacts": {}}

    monkeypatch.setattr(change_service, "run_pipeline", _slow)
    try:
        r1 = client.post("/api/v1/result/change",
                         json={"run_id": run_a, "async": True})
        assert r1.status_code == 200
        assert r1.json()["status"] == "PENDING"
        assert r1.json()["task_id"] == run_a

        # 第二次提交（闸门仍被首次任务占用）→ EX7(409)
        r2 = client.post("/api/v1/result/change",
                         json={"run_id": run_b, "async": True})
        from _helpers import assert_error_body
        assert_error_body(r2, "EX7", 409, run_id_required=False)
    finally:
        change_async.wait(run_a, timeout=5)
        _cleanup(run_a, aoi_a)
        _cleanup(run_b, aoi_b)


@pytest.mark.req("REQ-13")
def test_async_failure_ex11(client, monkeypatch):
    """失败路径（构造 EX11 场景）→ status FAILED + error.code 正确 + result 为 null。"""
    run_id, aoi_id = _make_task()

    def _raise_ex11(run_id, index="NDVI", block_size=256, threshold=None, on_stage=None):
        raise ApiError("EX11", run_id=run_id)

    monkeypatch.setattr(change_service, "run_pipeline", _raise_ex11)
    try:
        accept = change_async.start_change(run_id)
        assert accept["status"] == "PENDING"
        change_async.wait(run_id, timeout=10)
        view = change_async.get_status(run_id)
        assert view["status"] == "FAILED"
        assert view["error"] is not None
        assert view["error"]["code"] == "EX11"
        assert view["error"]["message"] == ERROR_MESSAGES["EX11"]
        assert view["result"] is None
    finally:
        _cleanup(run_id, aoi_id)


@pytest.mark.req("REQ-13")
def test_async_unknown_task_ex2(client):
    """未知 task_id → EX2(404)。"""
    from _helpers import assert_error_body
    r = client.get("/api/v1/result/change/task/00000000-0000-0000-0000-0000000000ee")
    assert_error_body(r, "EX2", 404, run_id_required=False)

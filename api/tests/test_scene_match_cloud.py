# -*- coding: utf-8 -*-
"""POST /scene/match：云端指数成品场景不得再按 5 波段 COG 解析（SYS-01 回归）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.service import scene_service

POYANG = "minio://scenes/pair_cloud/t1/"


def _scenes():
    return [
        {"scene_id": "s1", "sensor": "S2", "acq_date": "2023-09-01",
         "proc_baseline": "05.09", "cog_path": POYANG},
        {"scene_id": "s2", "sensor": "S2", "acq_date": "2023-10-01",
         "proc_baseline": "05.10", "cog_path": "minio://scenes/pair_cloud/t2/"},
    ]


def _stub_common(monkeypatch):
    monkeypatch.setattr(scene_service.scene_repo, "find_by_ids", lambda ids: _scenes())
    monkeypatch.setattr(scene_service.task_repo, "find_run_id_by_aoi",
                        lambda aid: "run-cloud")
    monkeypatch.setattr(scene_service.aoi_repo, "find_bbox",
                        lambda aid: (115.0, 28.0, 116.0, 29.0))


def test_match_post_cloud_scene_uses_index_meta_not_cog(monkeypatch):
    """两景均为云端指数成品：走 load_cloud_scene_meta，绝不调用 load_scene。"""
    _stub_common(monkeypatch)
    monkeypatch.setattr(scene_service, "has_cloud_indices", lambda p: True)

    seen = {}

    def fake_cloud(path, bbox, run_id):
        seen["path"] = path
        seen["run_id"] = run_id
        return {
            "window": SimpleNamespace(row_off=3, col_off=7, width=64, height=48),
            "profile": {"crs": "EPSG:32615", "res": (10.0, 10.0)},
            "valid_ratio": 0.87,
        }

    def forbidden(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("云端指数场景不应走 load_scene（会抛 SYS-01）")

    monkeypatch.setattr(scene_service, "load_cloud_scene_meta", fake_cloud)
    monkeypatch.setattr(scene_service, "load_scene", forbidden)

    result = scene_service.match_post("aoi-1", "s1", "s2")

    assert seen["path"] == POYANG
    assert seen["run_id"] == "run-cloud"
    assert result["run_id"] == "run-cloud"
    assert result["clip_window"]["width"] == 64
    assert result["clip_window"]["height"] == 48
    assert result["clip_window"]["res"] == 10.0
    assert result["valid_ratio"] == 0.87


def test_match_post_local_scene_still_uses_load_scene(monkeypatch):
    """非云端组保持原路径，避免回归既有 COG 口径。"""
    _stub_common(monkeypatch)
    monkeypatch.setattr(scene_service, "has_cloud_indices", lambda p: False)

    seen = {}

    def fake_local(path, sensor, baseline, bbox, run_id):
        seen["path"] = path
        return {
            "window": SimpleNamespace(row_off=0, col_off=0, width=32, height=32),
            "profile": {"crs": "EPSG:32615", "res": (10.0, 10.0)},
            "ratio_result": SimpleNamespace(valid_ratio=0.5),
        }

    def forbidden(*args, **kwargs):  # pragma: no cover
        raise AssertionError("本地 COG 场景不应走云端指数读取")

    monkeypatch.setattr(scene_service, "load_scene", fake_local)
    monkeypatch.setattr(scene_service, "load_cloud_scene_meta", forbidden)

    result = scene_service.match_post("aoi-1", "s1", "s2")

    assert seen["path"] == POYANG
    assert result["valid_ratio"] == 0.5
    assert result["clip_window"]["width"] == 32

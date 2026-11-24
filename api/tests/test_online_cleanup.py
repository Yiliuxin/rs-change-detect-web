# -*- coding: utf-8 -*-
"""在线取数数据清理的单元测试（不打真网 / 不碰真 DB）。

覆盖安全口径三要点：
  ① 预置组（含 landsat_*）与非法组名拒绝删除（EX2）；
  ② 列组只返回在线组、剔除预置与 landsat；
  ③ 删除统计正确（task/scene/aoi 级联计数 + 本地产物目录清理）。
"""
from __future__ import annotations

import pytest

from app.api.errors import ApiError
from app.config.constants import PRESET_GROUP_NAMES
from app.service import online_cleanup


def test_preset_groups_all_blocked():
    """白名单内每个预置组都不得删除。"""
    for g in PRESET_GROUP_NAMES:
        with pytest.raises(ApiError) as ei:
            online_cleanup._assert_deletable(g)
        assert ei.value.code == "EX2", g


def test_non_pair_group_blocked():
    """非 pair_* 组（landsat / 任意名）拒绝删除。"""
    for g in ("landsat_poyang", "random_group", "minio://scenes/foo"):
        with pytest.raises(ApiError) as ei:
            online_cleanup._assert_deletable(g)
        assert ei.value.code == "EX2", g


def test_online_group_deletable():
    """典型在线取数组允许删除。"""
    for g in ("pair_user_20261124_1426", "pair_mycustom"):
        online_cleanup._assert_deletable(g)  # 不抛即通过


def test_list_online_groups_filters_preset(monkeypatch):
    """列组剔除预置白名单与非 pair 组，只留在线组。"""
    fake = [
        {"group": "pair_nanchang", "scene_count": 2, "t1_date": "", "t2_date": ""},
        {"group": "landsat_poyang", "scene_count": 2, "t1_date": "", "t2_date": ""},
        {"group": "pair_user_20261124_1426", "scene_count": 2,
         "t1_date": "2023-08-17", "t2_date": "2023-11-20"},
        {"group": "pair_user_20261112_0925", "scene_count": 2,
         "t1_date": "2023-07-18", "t2_date": "2023-12-28"},
    ]
    monkeypatch.setattr(online_cleanup.online_repo, "list_fetch_groups", lambda: fake)
    r = online_cleanup.list_online_groups()
    assert r["total"] == 2, r
    assert [g["group"] for g in r["groups"]] == [
        "pair_user_20261124_1426", "pair_user_20261112_0925"]


def test_delete_group_cascade_stats(monkeypatch):
    """按组删除：级联统计求和正确，MinIO 按 group 前缀删除。"""
    monkeypatch.setattr(online_cleanup.online_repo, "group_scene_ids", lambda g: ["s1", "s2"])
    monkeypatch.setattr(online_cleanup.online_repo, "group_aoi_ids", lambda g: ["a1"])
    monkeypatch.setattr(online_cleanup.online_repo, "delete_tasks_for_scenes", lambda ids: 3)
    monkeypatch.setattr(online_cleanup.online_repo, "delete_scenes", lambda ids: 2)
    monkeypatch.setattr(online_cleanup.online_repo, "delete_aois", lambda ids: 1)
    # 采集 run_id 的 db_cursor 打桩
    class _Cur:
        def execute(self, *a, **k): pass
        def fetchall(self): return [{"rid": "r1"}, {"rid": "r2"}]
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr("app.repository.db.db_cursor", lambda: _Cur(), raising=False)
    prefixes = []
    monkeypatch.setattr(online_cleanup, "_delete_minio", lambda p: prefixes.append(p) or [])
    monkeypatch.setattr(online_cleanup, "_delete_result_dirs", lambda rids: len(rids))

    r = online_cleanup.delete_group("pair_user_20261124_1426")
    assert r["deleted_groups"] == ["pair_user_20261124_1426"]
    assert r["deleted_tasks"] == 3
    assert r["deleted_scenes"] == 2
    assert r["deleted_aois"] == 1
    assert r["deleted_result_dirs"] == 2
    assert r["deleted_runs"] == ["r1", "r2"]
    assert prefixes == ["pair_user_20261124_1426/"]


def test_delete_all_keeps_preset(monkeypatch):
    """一键清空只作用于在线组，统计为各组之和。"""
    monkeypatch.setattr(
        online_cleanup, "list_online_groups",
        lambda: {"groups": [
            {"group": "pair_user_a", "scene_count": 2, "t1_date": "", "t2_date": ""},
            {"group": "pair_user_b", "scene_count": 2, "t1_date": "", "t2_date": ""},
        ], "total": 2},
    )
    monkeypatch.setattr(online_cleanup.online_repo, "group_scene_ids", lambda g: [g + "_s"])
    monkeypatch.setattr(online_cleanup.online_repo, "group_aoi_ids", lambda g: [g + "_a"])
    monkeypatch.setattr(online_cleanup.online_repo, "delete_tasks_for_scenes", lambda ids: 2)
    monkeypatch.setattr(online_cleanup.online_repo, "delete_scenes", lambda ids: 1)
    monkeypatch.setattr(online_cleanup.online_repo, "delete_aois", lambda ids: 1)
    class _Cur:
        def execute(self, *a, **k): pass
        def fetchall(self): return [{"rid": "rx"}]
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr("app.repository.db.db_cursor", lambda: _Cur(), raising=False)
    monkeypatch.setattr(online_cleanup, "_delete_minio", lambda p: [])
    monkeypatch.setattr(online_cleanup, "_delete_result_dirs", lambda rids: 0)

    r = online_cleanup.delete_all()
    assert set(r["deleted_groups"]) == {"pair_user_a", "pair_user_b"}
    assert r["deleted_tasks"] == 4  # 2 组 × 2
    assert r["deleted_scenes"] == 2


def test_delete_all_empty_is_noop(monkeypatch):
    """无在线组时清空返回空统计，不报错。"""
    monkeypatch.setattr(
        online_cleanup, "list_online_groups", lambda: {"groups": [], "total": 0})
    r = online_cleanup.delete_all()
    assert r["deleted_groups"] == []
    assert r["deleted_tasks"] == 0
    assert r["minio_errors"] == []

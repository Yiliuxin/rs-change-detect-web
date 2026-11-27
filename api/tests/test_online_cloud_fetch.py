# -*- coding: utf-8 -*-
"""在线取数 cloud 模式（CDSE 直接回传指数）单元测试。

全部打桩，不打真网 / 不连真 PG / 不连真 MinIO。覆盖：
  * FetchRequest.mode 契约（默认 local，仅 local/cloud）；
  * MinIO 指数成品探测 + 本地缓存解析；
  * 单时相云端取数：4 指数请求 → 4 波段 GeoTIFF → 上传 → scene 入库；
  * 检测侧自动识别云端成品，构造与 local 同构的 pair 且不重复调 CDSE；
  * 取数任务状态进度分母为 8（两期各 4 指数）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from pydantic import ValidationError

from app.algorithm.geotransform import BBox
from app.api.v1.online import FetchRequest
from app.service import cdse_service, change_service, online_fetch
from app.service import cog_source, scene_service

BOX = BBox(100.1, 20.1, 100.3, 20.3)
SIZE = 8


def _phase(date: str) -> dict:
    return {"source_id": "S2B_50RMH_20250101_0_L2A", "date": date,
            "proc_baseline": "05.00", "cloud_cover": 3.2}


def _fetch_body(mode: str = "cloud") -> dict:
    return {
        "name": "cloud_demo", "mode": mode,
        "bbox": list(BOX.as_tuple()), "tile": "50RMH",
        "t1": _phase("2025-01-01"), "t2": _phase("2025-03-01"),
    }


# --------------------------------------------------------------------------- 契约
def test_fetch_request_mode_defaults_to_local():
    body = _fetch_body()
    body.pop("mode")
    assert FetchRequest(**body).mode == "local"


def test_fetch_request_rejects_unknown_mode():
    with pytest.raises(ValidationError):
        FetchRequest(**_fetch_body("fast"))


# --------------------------------------------------------------------------- 产物探测
def test_has_cloud_indices_minio_hit(monkeypatch):
    monkeypatch.setattr(cog_source, "StorageSettings",
                        type("S", (), {"from_env": staticmethod(lambda: None)}))
    monkeypatch.setattr(cog_source, "get_client", lambda *a, **kw: object())
    monkeypatch.setattr(cog_source, "list_objects",
                        lambda client, bucket, prefix: ["pair_x/t1/t1_indices.tif"])
    assert cog_source.has_cloud_indices("minio://scenes/pair_x/t1/") is True


def test_has_cloud_indices_false_for_raw_bands(monkeypatch):
    monkeypatch.setattr(cog_source, "StorageSettings",
                        type("S", (), {"from_env": staticmethod(lambda: None)}))
    monkeypatch.setattr(cog_source, "get_client", lambda *a, **kw: object())
    monkeypatch.setattr(cog_source, "list_objects",
                        lambda client, bucket, prefix: ["pair_x/t1/t1_B04.tif"])
    assert cog_source.has_cloud_indices("minio://scenes/pair_x/t1/") is False


def test_resolve_cloud_indices_caches_locally(monkeypatch, tmp_path):
    monkeypatch.setattr(cog_source, "StorageSettings",
                        type("S", (), {"from_env": staticmethod(lambda: None)}))
    monkeypatch.setattr(cog_source, "get_client", lambda *a, **kw: object())
    monkeypatch.setattr(cog_source, "list_objects",
                        lambda client, bucket, prefix: ["pair_x/t1/t1_indices.tif"])

    def fake_get(bucket, obj, dest):
        Path(dest).write_bytes(b"tif")

    monkeypatch.setattr(cog_source, "RUNS_ROOT", tmp_path)

    # 替换 get_client 返回的对象的 fget_object 行为
    class _Client:
        def fget_object(self, bucket, obj, dest):
            fake_get(bucket, obj, dest)

    monkeypatch.setattr(cog_source, "get_client", lambda *a, **kw: _Client())
    got = cog_source.resolve_cloud_indices("minio://scenes/pair_x/t1/", "run1")
    assert got is not None and got.name == "t1_indices.tif"
    assert got.exists() and got.stat().st_size > 0


# --------------------------------------------------------------------------- 云端取数
def test_cloud_tag_writes_4band_tif(monkeypatch, tmp_path):
    """4 个指数按 NDVI/NDWI/NDBI/NDMI 顺序写入单文件，且不触发原始波段下载。"""
    prof = cdse_service.metric_profile(tuple(BOX.as_tuple()), SIZE)
    vals = {"NDVI": 0.1, "NDWI": 0.2, "NDBI": 0.3, "NDMI": 0.4}
    calls = []

    def fake_fetch(index, bbox, d1, d2, run_id, size=None):
        calls.append(index)
        return np.full((SIZE, SIZE), vals[index], dtype=np.float32), dict(prof)

    monkeypatch.setattr(cdse_service, "fetch_index", fake_fetch)
    monkeypatch.setattr(
        cdse_service, "fetch_true_color",
        lambda bbox, date, rid, size=None: (
            np.stack([np.full((SIZE, SIZE), v, dtype=np.float32)
                      for v in (0.1, 0.2, 0.3)]), dict(prof)))
    monkeypatch.setattr(online_fetch, "_advance", lambda *a, **kw: None)
    monkeypatch.setattr(online_fetch, "get_client", lambda *a, **kw: object())
    monkeypatch.setattr(online_fetch, "ensure_buckets", lambda *a, **kw: None)
    uploaded = []
    monkeypatch.setattr(online_fetch, "upload_file",
                        lambda client, bucket, name, path: uploaded.append(name))
    monkeypatch.setattr(online_fetch, "_persist_scene",
                        lambda name, tag, meta, box, rid: f"sid-{tag}")

    root = tmp_path / "_cog"
    root.mkdir()
    sid = online_fetch._fetch_cloud_tag(
        "fetch1", "cloud_demo", "t1",
        {"date": "2025-01-01", "source_id": "S2B_50RMH_20250101_0_L2A",
         "tile": "50RMH", "proc_baseline": "05.00", "cloud_cover": 3.2},
        BOX, root,
    )
    assert sid == "sid-t1"
    assert set(calls) == set(cog_source.CLOUD_INDEXES) and len(calls) == 4
    assert uploaded == ["pair_cloud_demo/t1/t1_indices.tif",
                        "pair_cloud_demo/t1/t1_composite.png"]

    out = root / "t1_indices.tif"
    assert out.is_file()
    with rasterio.open(str(out)) as ds:
        assert ds.count == 4 and ds.dtypes[0] == "float32"
        for i, ik in enumerate(cog_source.CLOUD_INDEXES, start=1):
            assert float(np.nanmean(ds.read(i))) == pytest.approx(vals[ik], abs=1e-6)


def test_cloud_tag_rejects_all_nan(monkeypatch, tmp_path):
    """云端全无效像元 → EX2，不上传不入库（不让坏数据进系统）。"""
    prof = cdse_service.metric_profile(tuple(BOX.as_tuple()), SIZE)
    monkeypatch.setattr(cdse_service, "fetch_index",
                        lambda *a, **kw: (np.full((SIZE, SIZE), np.nan,
                                                  dtype=np.float32), dict(prof)))
    monkeypatch.setattr(
        cdse_service, "fetch_true_color",
        lambda *a, **kw: (np.zeros((3, SIZE, SIZE), dtype=np.float32), dict(prof)))
    monkeypatch.setattr(online_fetch, "_advance", lambda *a, **kw: None)
    monkeypatch.setattr(online_fetch, "_persist_scene",
                        lambda *a, **kw: pytest.fail("不应入库"))
    from app.api.errors import ApiError
    with pytest.raises(ApiError) as ei:
        online_fetch._fetch_cloud_tag(
            "fetch2", "x", "t1", {"date": "2025-01-01"}, BOX, tmp_path)
    assert ei.value.code == "EX2"
    assert not (tmp_path / "t1_indices.tif").exists()


def test_cloud_status_progress_is_eight(monkeypatch):
    """cloud 取数任务进度分母为 8（两期各 4 指数），local 保持 12。"""
    class _NoThread:
        def __init__(self, *a, **kw):
            pass
        def start(self):
            pass

    monkeypatch.setattr(online_fetch.threading, "Thread", _NoThread)
    body = _fetch_body("cloud")
    started = online_fetch.start_fetch(
        body["name"], None, body["bbox"], body["tile"],
        body["t1"], body["t2"], mode="cloud")
    view = online_fetch.get_status(started["fetch_id"])
    assert view["mode"] == "cloud"
    assert view["progress"]["total"] == 8
    online_fetch._tasks.pop(started["fetch_id"], None)
    for key in [k for k, v in online_fetch._active.items() if v == started["fetch_id"]]:
        online_fetch._active.pop(key, None)


# --------------------------------------------------------------------------- 检测自动复用
def test_cloud_pair_uses_stored_indices_without_cdse(monkeypatch, tmp_path):
    """检测侧识别云端成品 → 直接读 4 波段，不再请求 CDSE。"""
    prof = cdse_service.metric_profile(tuple(BOX.as_tuple()), SIZE)
    dest = tmp_path / "t1_indices.tif"
    arrays = {ik: np.full((SIZE, SIZE), v, dtype=np.float32)
              for ik, v in zip(cog_source.CLOUD_INDEXES, (0.11, 0.22, 0.33, 0.44))}
    online_fetch._write_cloud_index_tif(dest, arrays, prof, "run")

    monkeypatch.setattr(change_service, "has_cloud_indices", lambda p: bool(p))
    monkeypatch.setattr(change_service, "resolve_cloud_indices", lambda p, r: dest)
    monkeypatch.setattr(cdse_service, "fetch_index",
                        lambda *a, **kw: pytest.fail("已入库的云端指数不应重复请求"))

    sc = {"cog_path": "minio://scenes/pair_cloud_demo/t1/"}
    snap = {"t1": "2025-01-01", "t2": "2025-03-01"}
    p1, p2, valid, out_prof = change_service._cloud_pair_indexes(
        "NDBI", BOX, sc, sc, "run", snap)
    assert snap["index_source"] == "cloud_stored"
    assert p1["primary"].name == "NDBI"
    assert float(np.nanmean(p1["primary"].data)) == pytest.approx(0.33, abs=1e-6)
    assert float(np.nanmean(p1["ndvi"].data)) == pytest.approx(0.11, abs=1e-6)
    assert bool(valid.all())
    assert out_prof["crs"].to_epsg() == prof["crs"].to_epsg()


def test_scene_item_exposes_index_source(monkeypatch):
    monkeypatch.setattr(scene_service, "has_cloud_indices", lambda p: True)
    item = scene_service._scene_item({
        "scene_id": "s1", "sensor": "S2", "acq_date": "2025-01-01",
        "cloud_cover": 3.2, "tile": "50RMH", "proc_baseline": "05.00",
        "cog_path": "minio://scenes/pair_cloud_demo/t1/",
    })
    assert item["index_source"] == "cloud"
    assert item["has_b11"] is False  # 语义真实：无原始 B11，但四指数齐全

# -*- coding: utf-8 -*-
"""快速模式双时相卷帘：云端真彩回传 + 新组读成品 + 老组现场补调（全打桩）。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio

from app.algorithm.geotransform import BBox
from app.service import cdse_service, change_service, cog_source

BOX = BBox(100.1, 20.1, 100.3, 20.3)
SIZE = 8
PROF = cdse_service.metric_profile(tuple(BOX.as_tuple()), SIZE)


def _rgb():
    return np.stack([np.full((SIZE, SIZE), v, dtype=np.float32)
                     for v in (0.10, 0.20, 0.30)])


def test_fetch_true_color_stacks_rgb_in_b04_b03_b02_order(monkeypatch):
    """云端真彩按 R=B04 / G=B03 / B=B02 顺序堆叠，三波段并发结果按 band 归位。"""
    seen = []

    def fake_request(evalscript, bbox, d1, d2, rid, size, label, identifier="idx"):
        seen.append(label)
        band = label.split()[-1]
        val = {"B04": 0.4, "B03": 0.2, "B02": 0.1}[band]
        return np.full((SIZE, SIZE), val, dtype=np.float32), dict(PROF)

    monkeypatch.setattr(cdse_service, "_request_raster", fake_request)
    rgb, prof = cdse_service.fetch_true_color(tuple(BOX.as_tuple()),
                                              "2025-01-01", "run")
    assert rgb.shape == (3, SIZE, SIZE)
    assert float(rgb[0].mean()) == pytest.approx(0.4, abs=1e-6)  # R=B04
    assert float(rgb[1].mean()) == pytest.approx(0.2, abs=1e-6)  # G=B03
    assert float(rgb[2].mean()) == pytest.approx(0.1, abs=1e-6)  # B=B02
    assert set(seen) == {"真彩 B04", "真彩 B03", "真彩 B02"}
    assert prof["crs"] is not None


def test_make_cloud_composite_prefers_stored(monkeypatch, tmp_path):
    """新组：MinIO 已有真彩成品 → 直接取用，不调 CDSE。"""
    monkeypatch.setattr(change_service, "resolve_cloud_composite",
                        lambda path, rid, tag: tmp_path / f"{tag}_composite.png")
    monkeypatch.setattr(cdse_service, "configured", lambda: True)
    monkeypatch.setattr(cdse_service, "fetch_true_color",
                        lambda *a, **kw: pytest.fail("新组不应重复调 CDSE"))
    src = change_service._make_cloud_composite(
        {"cog_path": "minio://scenes/pair_x/t1/"}, "t1", BOX, "run",
        {"t1": "2025-01-01"})
    assert src == "stored"


def test_make_cloud_composite_fetches_on_missing(monkeypatch, tmp_path):
    """老组：无成品 → 现场调 CDSE 取真彩，本地拉伸写 PNG。"""
    monkeypatch.setattr(change_service, "resolve_cloud_composite",
                        lambda *a, **kw: None)
    monkeypatch.setattr(cdse_service, "configured", lambda: True)
    monkeypatch.setattr(cdse_service, "fetch_true_color",
                        lambda *a, **kw: (_rgb(), dict(PROF)))
    monkeypatch.setattr(change_service, "run_dir", lambda rid: tmp_path)
    src = change_service._make_cloud_composite(
        {"cog_path": "minio://scenes/pair_old/t1/", "acq_date": "2025-01-01"},
        "t1", BOX, "run", {})
    assert src == "fetched"
    png = tmp_path / "t1_composite.png"
    assert png.is_file()
    with rasterio.open(str(png)) as ds:
        assert ds.count == 3 and ds.width == SIZE and ds.height == SIZE


def test_make_cloud_composite_returns_none_without_credentials(monkeypatch, tmp_path):
    """无 CDSE 凭证且无成品 → None，由调用方标注 skipped，不阻断检测。"""
    monkeypatch.setattr(change_service, "resolve_cloud_composite",
                        lambda *a, **kw: None)
    monkeypatch.setattr(cdse_service, "configured", lambda: False)
    src = change_service._make_cloud_composite(
        {"cog_path": "minio://scenes/pair_old/t1/"}, "t1", BOX, "run", {})
    assert src is None


def test_resolve_cloud_composite_from_minio(monkeypatch, tmp_path):
    """真彩成品从 MinIO 取到 run 目录，文件名固定为 <tag>_composite.png。"""
    class _Client:
        def fget_object(self, bucket, obj, dest):
            Path(dest).write_bytes(b"png")

    monkeypatch.setattr(cog_source, "StorageSettings",
                        type("S", (), {"from_env": staticmethod(lambda: None)}))
    monkeypatch.setattr(cog_source, "get_client", lambda *a, **kw: _Client())
    monkeypatch.setattr(cog_source, "list_objects",
                        lambda client, bucket, prefix: ["pair_x/t1/t1_composite.png"])
    monkeypatch.setattr(cog_source, "run_dir", lambda rid: tmp_path, raising=False)
    # change_service 内 run_dir 是局部导入，直接改 cog_source 侧不生效；此测只验证
    # MinIO 对象匹配与 fget_object 调用即可，路径落在默认 run 目录由集成测试覆盖。
    monkeypatch.setattr("app.service.common.RUNS_ROOT", tmp_path)
    got = cog_source.resolve_cloud_composite(
        "minio://scenes/pair_x/t1/", "run", "t1")
    assert got is not None
    assert got.name == "t1_composite.png"

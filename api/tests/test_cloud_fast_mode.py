# -*- coding: utf-8 -*-
"""快速模式（source=cloud）单元测试：全部打桩，不打真网、不连 PG/MinIO。

覆盖：
  * cdse_service 凭证缺失 → SYS-06；configured() 读 .env；
  * utm_crs_for 按经度选带；metric_profile 为米制 transform（面积口径与本地一致）；
  * _evalscript 四指数波段对与本地 index_calculator 一致；
  * fetch_index 单波段请求打桩：回传合成 GeoTIFF → float32 数组 + 米制 profile；
    失败重试；云端口径（clip [-1,1]）；
  * _cloud_pair_indexes 打桩：构造与 local 同构的 pair（primary/aux/ndvi/ndwi）
    + valid 交集 + snapshot.index_source=cloud；
  * ChangeRequest.source 默认 local，透传 cloud（契约不改历史调用）。
"""
from __future__ import annotations

import io
import struct

import numpy as np
import pytest
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from app.algorithm.geotransform import BBox
from app.api.errors import ApiError
from app.service import cdse_service, change_service


# --------------------------------------------------------------------------- cdse_service
def test_configured_reads_env(monkeypatch):
    monkeypatch.setenv("CDSE_CLIENT_ID", "x")
    monkeypatch.setenv("CDSE_CLIENT_SECRET", "y")
    assert cdse_service.configured() is True


def test_credentials_missing_raises_sys06(monkeypatch):
    # load_env 会把 .env 凭证重新加载，故打桩为 no-op 后再清空
    monkeypatch.setattr(cdse_service, "load_env", lambda *a, **kw: True)
    monkeypatch.delenv("CDSE_CLIENT_ID", raising=False)
    monkeypatch.delenv("CDSE_CLIENT_SECRET", raising=False)
    with pytest.raises(ApiError) as ei:
        cdse_service._credentials()
    assert ei.value.code == "SYS-06"
    assert "CDSE_CLIENT_ID" in ei.value.detail


def test_utm_crs_for_china():
    # 南昌 ~115.9E → UTM 50N → EPSG:32650
    assert cdse_service.utm_crs_for((115.88, 28.98, 116.02, 29.10)).to_epsg() == 32650
    # 成都 ~104E → UTM 48N
    assert cdse_service.utm_crs_for((104.0, 30.0, 104.2, 30.2)).to_epsg() == 32648


def test_metric_profile_is_metric():
    prof = cdse_service.metric_profile((115.88, 28.98, 116.02, 29.10), 256)
    assert prof["crs"].to_epsg() == 32650
    # 米制像元边长应在 10~1000 m 之间（非度制）
    a = abs(prof["transform"].a)
    e = abs(prof["transform"].e)
    assert 10.0 < a < 1000.0 and 10.0 < e < 1000.0
    assert prof["width"] == 256 and prof["height"] == 256


def test_evalscript_band_pairs_match_local():
    # 云端波段对与本地 index_calculator 口径同源
    assert cdse_service.INDEX_BAND_EXPR == {
        "NDVI": ("B08", "B04"),
        "NDWI": ("B03", "B08"),
        "NDBI": ("B11", "B08"),
        "NDMI": ("B08", "B11"),
    }
    for key in cdse_service.INDEX_BAND_EXPR:
        script = cdse_service._evalscript(key)
        a, b = cdse_service.INDEX_BAND_EXPR[key]
        assert f's.{a} - s.{b}' in script
        assert 'SCL' in script
        assert 'id: "idx"' in script


def _fake_tif_bytes(arr: np.ndarray, size: int) -> bytes:
    """合成 256² float32 GeoTIFF（CRS84），模拟云端回传。"""
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", width=size, height=size, count=1,
                     dtype="float32", crs="EPSG:4326",
                     transform=from_origin(100.0, 30.0, 0.01, 0.01)) as ds:
            ds.write(arr.astype(np.float32), 1)
        return mf.read()


def test_fetch_index_clips_and_returns_metric_profile(monkeypatch):
    size = 256
    raw = np.linspace(-1.5, 1.5, size * size).reshape(size, size).astype(np.float32)
    tif_bytes = _fake_tif_bytes(raw, size)
    monkeypatch.setattr(cdse_service, "get_token", lambda force=False: "tok")

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return tif_bytes

    monkeypatch.setattr(cdse_service.urllib.request, "urlopen",
                        lambda req, timeout: _FakeResp())

    data, prof = cdse_service.fetch_index(
        "NDVI", (100.0, 29.9, 100.9, 30.0), "2025-01-01", "2025-01-01", "r", size=size)
    assert data.shape == (size, size)
    assert data.dtype == np.float32
    assert float(np.nanmax(data)) <= 1.0
    assert float(np.nanmin(data)) >= -1.0
    assert prof["crs"].to_epsg() == 32647  # 100.45E → UTM 47N


def test_fetch_index_retries_then_raises(monkeypatch):
    import urllib.error
    monkeypatch.setattr(cdse_service, "get_token", lambda force=False: "tok")
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(cdse_service.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cdse_service.time, "sleep", lambda s: None)
    with pytest.raises(ApiError) as ei:
        cdse_service.fetch_index("NDVI", (100.0, 29.9, 100.9, 30.0),
                                 "2025-01-01", "2025-01-01", "r", size=64)
    assert ei.value.code == "SYS-06"
    assert calls["n"] == cdse_service.MAX_RETRIES


# --------------------------------------------------------------------------- change_service 云端 pair
def test_cloud_pair_indexes_structure(monkeypatch):
    size = 8
    g = np.ones((size, size), dtype=np.float32) * 0.4
    g[0, 0] = np.nan
    prof = cdse_service.metric_profile((115.88, 28.98, 116.02, 29.10), size)
    monkeypatch.setattr(cdse_service, "fetch_index", lambda *a, **kw: (g.copy(), dict(prof)))

    snap = {"t1": "2025-03-26", "t2": "2025-10-09"}
    sc1 = {"acq_date": "2025-03-26"}
    sc2 = {"acq_date": "2025-10-09"}
    p1, p2, valid, out_prof = change_service._cloud_pair_indexes(
        "NDBI", BBox(115.88, 28.98, 116.02, 29.10), sc1, sc2, "run", snap, size=size)

    for p in (p1, p2):
        assert {"primary", "aux", "ndvi", "ndwi"} <= set(p)
        assert p["primary"].name == "NDBI"
        assert p["primary"].data.shape == (size, size)
        assert p["primary"].profile["crs"].to_epsg() == 32650
    assert valid.shape == (size, size)
    assert valid.sum() == size * size - 1  # 一个 NaN 被排除
    assert snap["index_source"] == "cloud"
    assert snap["cloud_provider"].startswith("CDSE")


def test_cloud_pair_indexes_missing_dates_raises_ex2(monkeypatch):
    monkeypatch.setattr(cdse_service, "fetch_index", lambda *a, **kw: None)
    with pytest.raises(ApiError) as ei:
        change_service._cloud_pair_indexes(
            "NDVI", BBox(100, 20, 100.2, 20.2), {}, {}, "run", {}, size=8)
    assert ei.value.code == "EX2"


# --------------------------------------------------------------------------- 契约兼容
def test_change_request_source_default_local():
    from app.api.v1.change import ChangeRequest
    req = ChangeRequest(run_id="r")
    assert req.source == "local"
    req2 = ChangeRequest(run_id="r", source="cloud")
    assert req2.source == "cloud"

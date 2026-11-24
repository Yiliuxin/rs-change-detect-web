# -*- coding: utf-8 -*-
"""在线取数 B11 支持 NDBI/NDMI 的单元测试。

覆盖三层：
  * 波段常量：BANDS 含 B11、数量/进度分母为 12、B11 在 B04 之后；
  * 重采样：resample_b11_to_ref 把 20m B11 双线性重采样到 10m B04 网格，
    输出 shape/transform/crs 与参考逐像元一致（assert_grid_aligned 口径）；
  * has_optional_band：轻量探测 B11 存在/缺失（monkeypatch MinIO/本地，不打真网）；
  * 前端门控字段：_scene_item 透出 has_b11。

全部合成栅格 / 打桩，不打真网、不连真 MinIO/PG。
"""
from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.algorithm.geotransform import BBox
from app.service import online_fetch
from app.service.cog_source import has_optional_band
from app.service.scene_service import _scene_item


# 合成栅格：EPSG:4326，覆盖 lon[100,101] × lat[20,21]
# 10m 参考（B04）像元 0.01°→100×100；20m B11 像元 0.02°→50×50
GRID_WEST, GRID_NORTH = 100.0, 21.0
AOI = BBox(100.1, 20.1, 100.9, 20.9)


def _make_grid(path, res, value):
    size = int(round(1.0 / res))
    transform = from_origin(GRID_WEST, GRID_NORTH, res, res)
    with rasterio.open(
        str(path), "w", driver="GTiff", width=size, height=size, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform, nodata=-9999,
    ) as ds:
        ds.write(np.full((size, size), value, dtype="float32"), 1)
    return path


# --------------------------------------------------------------------------- #
# 波段常量
# --------------------------------------------------------------------------- #
def test_bands_include_b11_after_b04():
    assert "B11" in online_fetch.BANDS
    assert online_fetch.BANDS.index("B11") > online_fetch.BANDS.index("B04")
    assert online_fetch.BANDS_PER_TAG == 6
    assert online_fetch.TOTAL_BANDS == 12


# --------------------------------------------------------------------------- #
# 重采样对齐
# --------------------------------------------------------------------------- #
def test_resample_b11_matches_ref_grid(tmp_path):
    """B11(20m) 重采样后 shape/transform/crs 与 B04(10m) 逐项一致。"""
    ref_p = _make_grid(tmp_path / "t1_B04.tif", 0.01, 1000.0)
    b11_p = _make_grid(tmp_path / "t1_B11.tif", 0.02, 2000.0)
    out = tmp_path / "t1_B11_out.tif"

    with rasterio.open(str(b11_p)) as src, rasterio.open(str(ref_p)) as ref:
        info = online_fetch.resample_b11_to_ref(src, ref, AOI, out)

    with rasterio.open(str(ref_p)) as ref, rasterio.open(str(out)) as got:
        assert (got.height, got.width) == (ref.height, ref.width) == info["shape"]
        assert got.transform == ref.transform
        assert got.crs == ref.crs
        assert got.dtypes[0] == "float32"
        arr = got.read(1)
        assert arr.shape == (ref.height, ref.width)
        assert np.isfinite(arr).all()          # AOI 完整覆盖 → 无 NaN 空洞


def test_resample_b11_preserves_gradient(tmp_path):
    """重采样保持数值梯度（双线性，非全 0/常量），证明真的取了源像元。"""
    ref_p = _make_grid(tmp_path / "t2_B04.tif", 0.01, 0.0)
    b11_p = tmp_path / "t2_B11.tif"
    transform = from_origin(GRID_WEST, GRID_NORTH, 0.02, 0.02)
    grad = np.arange(50 * 50, dtype="float32").reshape(50, 50)
    with rasterio.open(str(b11_p), "w", driver="GTiff", width=50, height=50,
                       count=1, dtype="float32", crs="EPSG:4326",
                       transform=transform) as ds:
        ds.write(grad, 1)
    out = tmp_path / "t2_B11_out.tif"
    with rasterio.open(str(b11_p)) as src, rasterio.open(str(ref_p)) as ref:
        online_fetch.resample_b11_to_ref(src, ref, AOI, out)
    with rasterio.open(str(out)) as ds:
        arr = ds.read(1)
    assert float(np.nanmax(arr)) > 1000        # 梯度被带过来
    assert float(np.nanstd(arr)) > 100         # 不是常量


def test_resample_b11_outside_aoi_rejected(tmp_path):
    """AOI 落在影像外 → EX2，不产出坏文件。"""
    ref_p = _make_grid(tmp_path / "B04.tif", 0.01, 0.0)
    b11_p = _make_grid(tmp_path / "B11.tif", 0.02, 0.0)
    out = tmp_path / "B11_out.tif"
    from app.api.errors import ApiError
    with rasterio.open(str(b11_p)) as src, rasterio.open(str(ref_p)) as ref:
        with pytest.raises(ApiError) as ei:
            online_fetch.resample_b11_to_ref(src, ref, BBox(200.0, 50.0, 201.0, 51.0), out)
    assert ei.value.code == "EX2"
    assert not out.exists()


# --------------------------------------------------------------------------- #
# has_optional_band 轻量探测（打桩，不打真网）
# --------------------------------------------------------------------------- #
def test_has_optional_band_minio_hit(monkeypatch):
    """MinIO 列举到 t1_B11.tif → True，且不调用 fget_object（不下载）。"""
    import app.service.cog_source as cs

    monkeypatch.setattr(cs, "StorageSettings", type("S", (), {"from_env": staticmethod(lambda: None)}))
    monkeypatch.setattr(cs, "get_client", lambda: object())
    monkeypatch.setattr(cs, "list_objects",
                        lambda client, bucket, prefix: ["pair_x/t1/t1_B11.tif", "pair_x/t1/t1_B04.tif"])
    assert has_optional_band("minio://scenes/pair_x/t1/", "B11") is True


def test_has_optional_band_missing(monkeypatch):
    """MinIO 无 B11、本地也无 → False（老在线组/Landsat 口径）。"""
    import app.service.cog_source as cs

    monkeypatch.setattr(cs, "StorageSettings", type("S", (), {"from_env": staticmethod(lambda: None)}))
    monkeypatch.setattr(cs, "get_client", lambda: object())
    monkeypatch.setattr(cs, "list_objects", lambda client, bucket, prefix: ["pair_x/t1/t1_B04.tif"])
    monkeypatch.setattr(cs, "_from_local", lambda *a, **k: None)
    assert has_optional_band("minio://scenes/pair_x/t1/", "B11") is False


def test_has_optional_band_fallback_local(monkeypatch):
    """MinIO 抛错时回退本地命中 → True。"""
    import app.service.cog_source as cs

    def boom(*a, **k):
        raise RuntimeError("minio down")

    monkeypatch.setattr(cs, "StorageSettings", type("S", (), {"from_env": staticmethod(lambda: None)}))
    monkeypatch.setattr(cs, "get_client", lambda: object())
    monkeypatch.setattr(cs, "list_objects", boom)
    monkeypatch.setattr(cs, "_from_local", lambda *a, **k: {"B11": object()})
    assert has_optional_band("minio://scenes/pair_x/t1/", "B11") is True


# --------------------------------------------------------------------------- #
# scene 清单字段
# --------------------------------------------------------------------------- #
def test_scene_item_exposes_has_b11(monkeypatch):
    """_scene_item 透出 has_b11（新在线组 True / 老组 False）。"""
    import app.service.scene_service as ss

    monkeypatch.setattr(ss, "has_optional_band",
                        lambda cp, band, sensor=None: "pair_new" in cp)
    scene = {"scene_id": "s1", "sensor": "S2", "cog_path": "minio://scenes/pair_new/t1/"}
    assert _scene_item(scene)["has_b11"] is True
    scene["cog_path"] = "minio://scenes/pair_old/t1/"
    assert _scene_item(scene)["has_b11"] is False

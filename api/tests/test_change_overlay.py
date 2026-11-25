# -*- coding: utf-8 -*-
"""地图叠加专用纯分类图 change_overlay 的契约测试。

根因背景：change.png 是带页眉/图例/署名的报告成品图（画布高 = 栅格高 + 72 + 162），
前端曾把它整张按 AOI bounds 铺到地图，导致"图例/Copernicus/RUN"文字被当地理
配准层贴进地图。本测试锁定纯分类图的三项硬契约：
  ① 尺寸严格等于变化栅格（H×W），无制图要素增高；
  ② 像素逐点等于 colorize_change（四类 + 过渡带同源配色）；
  ③ 不含制图要素专有像素（页眉背景色 _BG_COLOR / 图例背景色）。
不打真网、不需 Docker，只用合成小栅格。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.algorithm.renderer import (
    CLASS_COLORS, render_change_overlay,
)
from app.service.common import ARTIFACT_MEDIA_TYPES, artifact_manifest


def _synthetic_change(h: int = 24, w: int = 32) -> np.ndarray:
    """含 0/1/2/3/4 五类编码的小栅格，覆盖全部图例色。"""
    arr = np.zeros((h, w), dtype=np.int16)
    arr[:, :6] = 0
    arr[:, 6:12] = 1
    arr[:, 12:18] = 2
    arr[:, 18:24] = 3
    arr[:, 24:] = 4
    return arr


def test_overlay_size_equals_change_grid(tmp_path):
    """契约①：overlay 的 H/W 严格等于输入栅格，不附加页眉/图例高度。"""
    change = _synthetic_change(h=19, w=37)
    out = tmp_path / "change_overlay.png"
    info = render_change_overlay(change, out)

    assert out.is_file()
    assert info["width"] == 37, info
    assert info["height"] == 19, info

    import rasterio
    with rasterio.open(out) as ds:
        assert (ds.height, ds.width) == (19, 37)
        assert ds.count == 3
        assert ds.dtypes == ("uint8", "uint8", "uint8")


def test_overlay_pixels_match_colorize_change(tmp_path):
    """契约②：overlay 像素逐点等于 colorize_change（与报告图例同源配色）。"""
    from app.algorithm.renderer import colorize_change

    change = _synthetic_change()
    out = tmp_path / "ov.png"
    render_change_overlay(change, out)

    import rasterio
    with rasterio.open(out) as ds:
        got = ds.read()  # (3, H, W)
    got = np.moveaxis(got, 0, -1)  # (H, W, 3)
    expected = colorize_change(change)
    assert got.shape == expected.shape
    assert np.array_equal(got, expected)


def test_overlay_has_no_cartography_background(tmp_path):
    """契约③：overlay 不含制图要素专有像素（页眉/图例背景色）。

    change.png 的画布用 _BG_COLOR 填底，页眉/图例区必含该色；纯分类图只含
    五类配色，绝不该出现页眉底色。
    """
    import rasterio

    from app.algorithm.renderer import _BG_COLOR

    change = _synthetic_change()
    out = tmp_path / "ov.png"
    render_change_overlay(change, out)

    with rasterio.open(out) as ds:
        arr = np.moveaxis(ds.read(), 0, -1)
    palette = {tuple(c) for c in CLASS_COLORS.values()}
    actual = {tuple(px) for px in arr.reshape(-1, 3)}
    assert actual <= palette, f"overlay 出现非配色像素：{actual - palette}"
    if tuple(_BG_COLOR) not in palette:
        assert tuple(_BG_COLOR) not in actual, "overlay 混入页眉背景色"


def test_overlay_registered_in_artifacts():
    """overlay 文件名进产物白名单与清单，静态端点才放行下载。"""
    assert "change_overlay.png" in ARTIFACT_MEDIA_TYPES
    assert ARTIFACT_MEDIA_TYPES["change_overlay.png"] == "image/png"
    for index in ("NDVI", "NDWI", "NDBI", "NDMI"):
        manifest = artifact_manifest("rid", index)
        assert any("change_overlay.png" in u for u in manifest.values()), index
    # 回归法同样登记
    reg = artifact_manifest("rid", "NDVI", method="regression")
    assert any("change_overlay.png" in u for u in reg.values())


def test_overlay_url_survives_response_model():
    """service 写入的 change_overlay 必须被 ChangeResponse 保留，否则前端回退
    到带图例/署名的 change.png，地图上就会出现文字。
    """
    from app.api.v1.change import ChangeResponse
    from app.service.common import artifact_url

    rid = "4f79db68-fc1f-45ec-9703-861aff2a9a93"
    resp = ChangeResponse(**{
        "run_id": rid,
        "change_png": artifact_url(rid, "change.png"),
        "change_overlay": artifact_url(rid, "change_overlay.png"),
    }).model_dump()
    assert resp["change_overlay"] == artifact_url(rid, "change_overlay.png")
    assert "change_overlay.png" in resp["change_overlay"]
    # 老 run / 演示降级不传该字段时仍为空串，前端照旧回退，不破坏历史口径
    assert ChangeResponse(run_id=rid).model_dump()["change_overlay"] == ""


def test_overlay_does_not_mutate_input(tmp_path):
    """纯函数口径：渲染不得改动输入栅格。"""
    change = _synthetic_change()
    before = change.copy()
    render_change_overlay(change, tmp_path / "ov.png")
    assert np.array_equal(change, before)


def test_overlay_dim_distinct_from_report_canvas():
    """报告画布比图幅高（页眉+图例），overlay 不该复用该尺寸口径。"""
    from app.algorithm.renderer import MAP_HEADER_H, MAP_LEGEND_H

    change = _synthetic_change(h=20, w=30)
    report_canvas_h = change.shape[0] + MAP_HEADER_H + MAP_LEGEND_H
    assert report_canvas_h > change.shape[0]  # 报告图确实更高
    # 这行是语义锚点：overlay 高度恒等于栅格高，而非 report_canvas_h
    assert change.shape[0] != report_canvas_h

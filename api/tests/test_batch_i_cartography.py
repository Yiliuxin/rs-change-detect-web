# -*- coding: utf-8 -*-
"""批次 I · 制图级 PNG + 双时相 GIF 动画（REQ-12 / REQ-14）。

覆盖：
  PNG —— 要素合成不报错、尺寸合法（独立底栏不遮挡）、关开关回到历史裸图、
         微软雅黑缺失时降级 Pillow 默认字体且元数据注明；
  GIF —— 3 帧 / 1.2s / 循环 / 最长边 ≤1024、zip 打包 + 同源鉴权端点可达、
         无变化专题帧时如实降级为双帧真彩。

测试卫生：纯函数单测只写 tmp_path；接口测复用 change_ready 并在 finally 里
再用 cartographic=False 导出一次，恢复历史裸图，不影响其它用例。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np
import pytest
from affine import Affine
from PIL import Image

from app.algorithm.animation import write_change_animation
from app.algorithm.renderer import (
    CARTOGRAPHY_STRIP_H, MAP_HEADER_H, MAP_LEGEND_H,
    build_change_canvas, render_change_png,
)
from app.algorithm.writers import validate_export_formats
from app.config.constants import (
    ANIMATION_FILENAME, ANIMATION_FORMAT, ANIMATION_FRAME_MS,
    ANIMATION_MAX_EDGE, ALL_EXPORT_FORMATS, EXPORT_FORMATS,
)
from app.service.common import export_zip_name, run_dir
from _helpers import export_products, export_zip_entries

URL = "/api/v1/report/export"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _change() -> np.ndarray:
    arr = np.full((64, 200), 3, dtype=np.uint8)
    arr[:20, :40] = 1
    arr[20:40, 40:80] = 2
    return arr


def _transform() -> Affine:
    return Affine(10.0, 0.0, 428030.0, 0.0, -10.0, 3234760.0)


def _meta() -> dict:
    return {
        "index": "NDVI", "method": "otsu", "threshold": 0.38671875,
        "t1_date": "2023-08-04", "t2_date": "2023-11-02",
        "generated_at": "2026-11-23 10:00:00 UTC", "run_id": "0cdc51cc-test",
    }


# --------------------------------------------------------------------------- #
# PNG 制图要素
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-12")
def test_cartographic_elements_compose_without_error_and_font_fallback(tmp_path):
    """正常字体 + 缺失字体两路都成功落盘，降级原因写入渲染元数据。"""
    path = tmp_path / "change_cartographic.png"
    info = render_change_png(
        _change(), 0.9, path, run_id="0cdc51cc-test",
        cartographic=True, transform=_transform(), crs="EPSG:32650",
        cartography_meta=_meta(),
    )
    assert path.read_bytes()[:8] == PNG_MAGIC
    assert info["cartographic"] is True
    assert info["writer"] == "rasterio.PNG"
    assert info["scale_m"] in (500, 1000, 2000) and info["scale_px"] > 0
    assert info["meters_per_pixel"] == pytest.approx(10.0)
    assert info["cartography_font_mode"] == "ttf-msyh"
    assert info["cartography_font_note"] == ""

    fallback = tmp_path / "change_fallback.png"
    info_fb = render_change_png(
        _change(), 0.9, fallback, run_id="0cdc51cc-test",
        cartographic=True, transform=_transform(), crs="EPSG:32650",
        cartography_meta=_meta(), font_path="C:/__no_such_font__.ttf",
    )
    assert fallback.read_bytes()[:8] == PNG_MAGIC
    assert info_fb["cartography_font_mode"] in ("pil-default", "ascii5x7")
    assert "降级" in str(info_fb["cartography_font_note"])


@pytest.mark.req("REQ-10")
def test_cartographic_png_dimensions_valid_and_no_overlay_on_map(tmp_path):
    """尺寸 = 图幅 + 页眉 + 图例 + 独立底栏；图例色块与指北针/比例尺均真实绘出。"""
    arr = _change()
    path = tmp_path / "change.png"
    info = render_change_png(
        arr, 0.9, path, cartographic=True,
        transform=_transform(), crs="EPSG:32650", cartography_meta=_meta(),
    )
    with Image.open(path) as im:
        assert im.size == (200, 64 + MAP_HEADER_H + MAP_LEGEND_H + CARTOGRAPHY_STRIP_H)
        pixels = np.asarray(im.convert("RGB"))
    # 专题图本体与图例区域逐点不变（底栏独立追加，不遮挡任何既有内容）
    bare, _ = build_change_canvas(arr, 0.9)
    np.testing.assert_array_equal(pixels[:bare.shape[0]], bare)
    strip = pixels[bare.shape[0]:]
    non_bg = int((np.any(strip != (250, 250, 250), axis=2)).sum())
    assert non_bg > 100
    blue = int(((strip[:, :, 2] > strip[:, :, 0] + 40)).sum())
    assert blue > 10                                    # 指北针 N 箭头
    flat = pixels.reshape(-1, 3)
    assert np.any(np.all(flat == (46, 160, 67), axis=1))   # 增强=绿
    assert np.any(np.all(flat == (214, 48, 49), axis=1))   # 退化=红
    assert info["scale_px"] <= 180


@pytest.mark.req("REQ-12")
def test_scale_tick_derived_from_geotransform_and_toggled_off_is_legacy(tmp_path):
    """30m 像元按 2000m 刻度；关闭开关后画布与文件名均保持历史行为。"""
    big = Affine(30.0, 0.0, 428030.0, 0.0, -30.0, 3234760.0)
    info = render_change_png(
        _change(), 0.9, tmp_path / "scale.png", cartographic=True,
        transform=big, crs="EPSG:32650", cartography_meta=_meta(),
    )
    assert info["meters_per_pixel"] == pytest.approx(30.0)
    assert info["scale_m"] == 2000
    assert info["scale_px"] == pytest.approx(2000 / 30, abs=1)

    bare_path = tmp_path / "bare.png"
    bare_meta = render_change_png(_change(), 0.9, bare_path)
    assert bare_meta["cartographic"] is False
    with Image.open(bare_path) as im:
        assert im.size == (200, 64 + MAP_HEADER_H + MAP_LEGEND_H)


# --------------------------------------------------------------------------- #
# GIF 动画
# --------------------------------------------------------------------------- #
def _solid_png(path: Path, rgb, size) -> None:
    Image.new("RGB", size, rgb).save(path)


@pytest.mark.req("REQ-12")
def test_gif_frames_duration_loop_size_and_seq(tmp_path):
    """三帧 t1→t2→专题、每帧 1.2s、循环、最长边 ≤1024。"""
    size = (1200, 400)
    t1, t2, theme = (tmp_path / n for n in ("t1.png", "t2.png", "theme.png"))
    _solid_png(t1, (220, 30, 30), size)
    _solid_png(t2, (30, 180, 60), size)
    _solid_png(theme, (40, 60, 200), (1200, 400 + MAP_HEADER_H + MAP_LEGEND_H))
    out = tmp_path / ANIMATION_FILENAME
    meta = write_change_animation(
        t1_png=t1, t2_png=t2, theme_png=theme, output_path=out,
        t1_label="2023-08-04", t2_label="2023-11-02",
    )
    assert out.read_bytes()[:6] in (b"GIF89a", b"GIF87a")
    assert meta["n_frames"] == 3 and meta["duration_ms"] == ANIMATION_FRAME_MS
    assert meta["loop"] == 0 and meta["theme_used"] is True
    assert max(meta["width"], meta["height"]) <= ANIMATION_MAX_EDGE
    assert meta["stretch_note"].find("2%–98%") >= 0

    with Image.open(out) as im:
        assert im.n_frames == 3
        assert im.info["duration"] == ANIMATION_FRAME_MS and im.info["loop"] == 0
        assert im.size == (meta["width"], meta["height"])
        colors = []
        for i in range(im.n_frames):
            im.seek(i)
            colors.append(np.asarray(im.convert("RGB"), dtype=np.int32))
        # GIF 调色板有量化误差，但三帧主色必须有明显区分且顺序为 t1→t2→专题
        means = [c.reshape(-1, 3).mean(axis=0) for c in colors]
        assert means[0][0] > means[0][1] and means[1][1] > means[1][0]
        assert means[2][2] > means[2][0] and means[2][2] > means[2][1]


@pytest.mark.req("REQ-12")
def test_gif_without_theme_frame_degrades_to_two_true_color_frames(tmp_path):
    """无变化专题图时如实降级为 t1/t2 双帧，不伪造专题帧。"""
    t1, t2 = tmp_path / "t1.png", tmp_path / "t2.png"
    _solid_png(t1, (200, 20, 20), (120, 120))
    _solid_png(t2, (20, 160, 20), (120, 120))
    out = tmp_path / ANIMATION_FILENAME
    meta = write_change_animation(
        t1_png=t1, t2_png=t2, theme_png=None, output_path=out,
    )
    assert meta["n_frames"] == 2 and meta["theme_used"] is False
    assert meta["degraded"] is True and "双帧真彩" in meta["reason"]
    with Image.open(out) as im:
        assert im.n_frames == 2 and im.info["duration"] == ANIMATION_FRAME_MS


@pytest.mark.req("REQ-12")
def test_gif_format_whitelist_additive_and_ex6_unchanged():
    """只增不改：原四类白名单与默认 formats 行为不变，gif 显式放行。"""
    assert EXPORT_FORMATS == ("geotiff", "png", "pdf", "geojson")
    assert ALL_EXPORT_FORMATS == EXPORT_FORMATS + (ANIMATION_FORMAT,)
    assert validate_export_formats(["PNG", " GIF "]) == ("png", "gif")
    with pytest.raises(Exception) as exc:
        validate_export_formats(["svg"])
    assert getattr(exc.value, "code", None) == "EX6"


@pytest.mark.req("REQ-12")
def test_export_gif_joins_zip_and_same_origin_artifact(client, change_ready):
    """勾选 GIF → files[] 含产物、zip 打包、同源鉴权端点返回真实 GIF。"""
    rid = change_ready["run_id"]
    try:
        r = client.post(URL, json={
            "run_id": rid, "formats": ["png", "gif"], "cartographic": True,
        })
        assert r.status_code == 200, r.text
        files = r.json()["files"]
        products = export_products(files)
        assert {f["format"] for f in products} == {"png", "gif"}
        gif = next(f for f in products if f["format"] == "gif")
        assert gif["filename"] == ANIMATION_FILENAME
        assert gif["url"] == f"/api/v1/artifacts/{rid}/{ANIMATION_FILENAME}"
        assert gif["size"] > 0

        got = client.get(gif["url"])
        assert got.status_code == 200, got.text
        assert got.headers["content-type"].startswith("image/gif")
        assert got.content[:6] in (b"GIF89a", b"GIF87a")
        with Image.open(run_dir(rid) / ANIMATION_FILENAME) as im:
            assert im.n_frames == 3

        zips = export_zip_entries(files)
        assert len(zips) == 1
        with zipfile.ZipFile(run_dir(rid) / zips[0]["filename"]) as zf:
            assert zf.testzip() is None
            assert set(zf.namelist()) == {"change.png", ANIMATION_FILENAME}
            assert zf.read(ANIMATION_FILENAME) == (run_dir(rid) / ANIMATION_FILENAME).read_bytes()
    finally:
        # 恢复历史裸图与既有产物，避免影响共享 change_ready 的其它用例
        client.post(URL, json={"run_id": rid, "formats": ["png"]})

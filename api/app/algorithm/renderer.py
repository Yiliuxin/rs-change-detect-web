# -*- coding: utf-8 -*-
"""变化专题图 PNG 渲染（HLD 模块4 功能一，REQ-10 / NF6 / NF7）。

依赖约束：专题图本体不依赖 matplotlib（PDF 报告的面积柱状图由 writers 单独使用
matplotlib Agg）。本模块用 numpy 生成 RGB 画布、用内置 **5×7 ASCII 点阵字模**
逐像素绘制 ``VALID_RATIO=0.xxx``；
中文（图例 / EX5 告警）在 PIL + ``C:\\Windows\\Fonts\\msyh.ttc`` 可用时使用，
否则退化为 5×7 ASCII 点阵文字并在返回元数据中如实说明。PNG 优先用 rasterio
``driver="PNG"``（3 波段 uint8）落盘，失败再退 PIL。

配色：1 增强=绿、2 退化=红、3 稳定=灰蓝、0 无效=深灰；批次 B1 追加
4 过渡带=独立中性灰（|Δ| 贴近阈值、证据不足的旁路标记，不是第四类变化）。
图例含 1/2/3/4 色块与中文名，并叠加 Copernicus 合规署名。
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.errors import NotGeoreferencedWarning

from app.algorithm.change_detector import (
    CHANGE_CODES, CHANGE_NODATA, CHANGE_TRANSITION,
)
from app.algorithm.mask_pipeline import judge_valid_ratio
from app.config.constants import CARTOGRAPHY_SCALE_TICKS_M, TRANSITION_BAND_COLOR

# 配色（R, G, B）：三类 + 批次 B1 过渡带独立中性灰
CLASS_COLORS: Dict[int, Tuple[int, int, int]] = {
    CHANGE_NODATA: (64, 64, 64),   # 0 无效：深灰
    1: (46, 160, 67),              # 1 增强：绿
    2: (214, 48, 49),              # 2 退化：红
    3: (120, 140, 160),            # 3 稳定：灰蓝
    CHANGE_TRANSITION: TRANSITION_BAND_COLOR,  # 4 过渡带：独立灰（证据不足）
}
CLASS_NAMES_ZH: Dict[int, str] = {
    1: "增强", 2: "退化", 3: "稳定", CHANGE_TRANSITION: "过渡带", 0: "无效",
}
CLASS_NAMES_EN: Dict[int, str] = {
    1: "ENHANCED", 2: "DEGRADED", 3: "STABLE",
    CHANGE_TRANSITION: "TRANSITION", 0: "NODATA",
}

DEFAULT_FONT_PATH: str = r"C:\Windows\Fonts\msyh.ttc"
_BG_COLOR: Tuple[int, int, int] = (245, 245, 245)
_HEADER_H: int = 72
# 图例多一行「过渡带」，纵向留出 5 行色块 + 署名行的空间
_LEGEND_H: int = 162
# 批次 I：制图要素独立占一条底栏，绝不覆盖专题图本体与既有图例
_CARTOGRAPHY_H: int = 96
_CARTOGRAPHY_BG: Tuple[int, int, int] = (250, 250, 250)
_NORTH_COLOR: Tuple[int, int, int] = (18, 62, 130)
_GLYPH_SCALE: int = 2
_GLYPH_ADVANCE: int = 6 * _GLYPH_SCALE

# ---------------------------------------------------------------------------
# 内置 5x7 ASCII 点阵字模：每行 5 像素，共 7 行，'#' 表示点亮
# ---------------------------------------------------------------------------
_FONT_5X7: Dict[str, str] = {
    " ": "...../...../...../...../...../...../.....",
    "A": "..#../#...#/#...#/#####/#...#/#...#/#...#",
    "B": "####./#...#/#...#/####./#...#/#...#/####.",
    "C": ".####/#..../#..../#..../#..../#..../.####",
    "D": "####./#...#/#...#/#...#/#...#/#...#/####.",
    "E": "#####/#..../#..../####./#..../#..../#####",
    "F": "#####/#..../#..../####./#..../#..../#....",
    "G": ".####/#..../#..../#..##/#...#/#...#/.###.",
    "H": "#...#/#...#/#...#/#####/#...#/#...#/#...#",
    "I": "#####/..#../..#../..#../..#../..#../#####",
    "J": "..###/...#./...#./...#./...#./#..#./.##..",
    "K": "#...#/#..#./#.#../##.../#.#../#..#./#...#",
    "L": "#..../#..../#..../#..../#..../#..../#####",
    "M": "#...#/##.##/#.#.#/#...#/#...#/#...#/#...#",
    "N": "#...#/##..#/#.#.#/#..##/#...#/#...#/#...#",
    "O": ".###./#...#/#...#/#...#/#...#/#...#/.###.",
    "P": "####./#...#/#...#/####./#..../#..../#....",
    "Q": ".###./#...#/#...#/#...#/#.#.#/#..#./.##.#",
    "R": "####./#...#/#...#/####./#.#../#..#./#...#",
    "S": ".####/#..../#..../.###./....#/....#/####.",
    "T": "#####/..#../..#../..#../..#../..#../..#..",
    "U": "#...#/#...#/#...#/#...#/#...#/#...#/.###.",
    "V": "#...#/#...#/#...#/#...#/#...#/.#.#./..#..",
    "W": "#...#/#...#/#...#/#...#/#.#.#/##.##/#...#",
    "X": "#...#/#...#/.#.#./..#../.#.#./#...#/#...#",
    "Y": "#...#/#...#/.#.#./..#../..#../..#../..#..",
    "Z": "#####/....#/...#./..#../.#.../#..../#####",
    "0": ".###./#...#/#..##/#.#.#/##..#/#...#/.###.",
    "1": "..#../.##../..#../..#../..#../..#../.###.",
    "2": ".###./#...#/....#/...#./..#../.#.../#####",
    "3": "#####/...#./..#../...#./....#/#...#/.###.",
    "4": "...#./..##./.#.#./#..#./#####/...#./...#.",
    "5": "#####/#..../####./....#/....#/#...#/.###.",
    "6": "..##./.#.../#..../####./#...#/#...#/.###.",
    "7": "#####/....#/...#./..#../.#.../.#.../.#...",
    "8": ".###./#...#/#...#/.###./#...#/#...#/.###.",
    "9": ".###./#...#/#...#/.####/....#/...#./.##..",
    ".": "...../...../...../...../...../.##../.##..",
    ",": "...../...../...../...../.##../.##../.#...",
    ":": "...../.##../.##../...../.##../.##../.....",
    "=": "...../...../#####/...../#####/...../.....",
    "-": "...../...../...../#####/...../...../.....",
    "_": "...../...../...../...../...../...../#####",
    "/": "....#/....#/...#./..#../.#.../#..../#....",
    "(": "...#./..#../.#.../.#.../.#.../..#../...#.",
    ")": ".#.../..#../...#./...#./...#./..#../.#...",
    "%": "##..#/##..#/...#./..#../.#.../#..##/#..##",
    "+": "...../..#../..#../#####/..#../..#../.....",
    "#": ".#.#./.#.#./#####/.#.#./#####/.#.#./.#.#.",
    "<": "...#./..#../.#.../#..../.#.../..#../...#.",
    ">": ".#.../..#../...#./....#/...#./..#../.#...",
    "!": "..#../..#../..#../..#../..#../...../..#..",
    "[": ".###./.#.../.#.../.#.../.#.../.#.../.###.",
    "]": ".###./...#./...#./...#./...#./...#./.###.",
}

__all__ = [
    "CLASS_COLORS", "CLASS_NAMES_ZH", "CLASS_NAMES_EN",
    "MAP_HEADER_H", "MAP_LEGEND_H", "CARTOGRAPHY_STRIP_H",
    "draw_text_5x7", "colorize_change", "build_change_canvas", "render_change_png",
    "render_change_overlay",
]

# 供 GIF 动画复用专题图版面：真彩帧与专题帧必须同尺寸、同图幅位置
MAP_HEADER_H = _HEADER_H
MAP_LEGEND_H = _LEGEND_H
CARTOGRAPHY_STRIP_H = _CARTOGRAPHY_H


def draw_text_5x7(canvas: np.ndarray, x: int, y: int, text: str,
                  color: Tuple[int, int, int] = (20, 20, 20),
                  scale: int = _GLYPH_SCALE) -> int:
    """用内置 5×7 点阵字模把 ASCII 文字逐像素画到 RGB 画布，返回光标终点 x。"""
    h, w = canvas.shape[:2]
    scale = max(1, int(scale))
    cursor = int(x)
    for ch in str(text).upper():
        rows = _FONT_5X7.get(ch, _FONT_5X7[" "]).split("/")
        for r, row in enumerate(rows[:7]):
            for c, pixel in enumerate(row[:5]):
                if pixel != "#":
                    continue
                y0, x0 = int(y) + r * scale, cursor + c * scale
                if 0 <= y0 and 0 <= x0 and y0 + scale <= h and x0 + scale <= w:
                    canvas[y0:y0 + scale, x0:x0 + scale] = color
        cursor += 6 * scale
    return cursor


def colorize_change(change, colors: Optional[Dict[int, Tuple[int, int, int]]] = None) -> np.ndarray:
    """三类着色 → (H, W, 3) uint8；未知编码置白。纯函数。"""
    arr = np.asarray(change)
    palette = dict(CLASS_COLORS if colors is None else colors)
    rgb = np.full((*arr.shape, 3), (255, 255, 255), dtype=np.uint8)
    for code, color in palette.items():
        rgb[arr == code] = color
    return rgb


def _load_ttf(font_path: str, size: int):
    """尝试加载中文 TTF/TTC；PIL 或字体不可用返回 None。"""
    try:
        from PIL import ImageFont

        return ImageFont.truetype(font_path, size, index=0)
    except Exception:
        return None


def _pil_image(canvas: np.ndarray):
    try:
        from PIL import Image

        return Image.fromarray(canvas)
    except Exception:
        return None


def _meters_per_pixel(transform, crs, map_height: int, map_width: int) -> Optional[float]:
    """由 GeoTIFF 地理变换算 x 方向米/像元（旋转网格只取主对角线，展示口径）。"""
    if transform is None:
        return None
    try:
        t = transform if hasattr(transform, "a") else Affine(*tuple(transform))
        a, e = abs(float(t.a)), abs(float(t.e))
    except Exception:  # noqa: BLE001 - 变换损坏时如实跳过比例尺
        return None
    if not (np.isfinite(a) and a > 0) or not np.isfinite(e):
        return None
    try:
        geographic = bool(CRS.from_user_input(crs).is_geographic) if crs is not None else False
    except Exception:  # noqa: BLE001
        geographic = False
    if geographic:
        # 4326 等地理坐标系：按图幅中心纬度做米制近似（AOI ≤0.25°，误差可忽略）
        lat = float(t.f + t.d * map_width / 2.0 + t.e * map_height / 2.0)
        return float(a * 111320.0 * max(0.05, abs(np.cos(np.radians(lat)))))
    return float(a)


def _choose_scale_tick(meters_per_px: Optional[float], width: int) -> Tuple[int, int, bool]:
    """按 500/1000/2000m 候选取图中放得下的最大刻度，返回 (米, 像素, 是否截断)。"""
    max_px = max(12, min(180, int(width) - 24))
    if not meters_per_px or not np.isfinite(meters_per_px) or meters_per_px <= 0:
        return 500, min(50, max_px), True
    fits = [m for m in CARTOGRAPHY_SCALE_TICKS_M if m / meters_per_px <= max_px]
    tick = int(max(fits) if fits else min(CARTOGRAPHY_SCALE_TICKS_M))
    px = int(round(tick / meters_per_px))
    clamped = px > max_px
    return tick, max(1, min(max_px, px)), clamped


def _draw_cartographic_strip(
    canvas: np.ndarray,
    info: Dict[str, object],
    *,
    map_height: int,
    transform,
    crs,
    meta: dict,
    font_path: str,
) -> np.ndarray:
    """在专题图下方独立底栏合成比例尺 / 指北针 / 角注（字体降级如实写 info）。"""
    height, width = canvas.shape[:2]
    strip_top = _HEADER_H + map_height + _LEGEND_H
    mpp = _meters_per_pixel(transform, crs, map_height, width)
    tick_m, tick_px, clamped = _choose_scale_tick(mpp, width)
    info.update({
        "cartographic": True,
        "meters_per_pixel": mpp,
        "scale_m": tick_m,
        "scale_px": tick_px,
        "scale_clamped": clamped,
    })

    m = dict(meta or {})
    index = str(m.get("index") or "N/A")
    method = str(m.get("method") or "N/A")
    threshold = m.get("threshold")
    t_text = f"{float(threshold):.4f}" if threshold is not None else "N/A"
    t1 = str(m.get("t1_date") or "N/A")
    t2 = str(m.get("t2_date") or "N/A")
    generated = str(m.get("generated_at") or "N/A")
    run8 = str(m.get("run_id") or info.get("run_id") or "")[:8] or "N/A"

    img = _pil_image(canvas)
    font = _load_ttf(font_path, 12)
    if img is None:
        # 极端降级：Pillow 不可用时仍用内置 5×7 点阵画出要素，不抛异常
        canvas[strip_top:height, :, :] = _CARTOGRAPHY_BG
        x0 = 12
        for i in range(4):
            xa, xb = x0 + i * tick_px // 4, x0 + (i + 1) * tick_px // 4
            canvas[strip_top + 8:strip_top + 18, xa:xb] = (
                (0, 0, 0) if i % 2 == 0 else (255, 255, 255)
            )
        draw_text_5x7(canvas, 12, strip_top + 22,
                      f"0 ... {tick_m}M", color=(30, 30, 30), scale=1)
        draw_text_5x7(canvas, 12, strip_top + 40,
                      f"INDEX {index} METHOD {method} T={t_text}", color=(40, 40, 40), scale=1)
        draw_text_5x7(canvas, 12, strip_top + 56,
                      f"T1={t1} T2={t2} RUN={run8} AT {generated}",
                      color=(40, 40, 40), scale=1)
        info.update({
            "cartography_font_mode": "ascii5x7",
            "cartography_font_note": "Pillow 不可用：制图要素退化为内置 5x7 点阵字模",
        })
        return canvas

    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(img)
    if font is None:
        font = ImageFont.load_default()
        info.update({
            "cartography_font_mode": "pil-default",
            "cartography_font_note": f"系统字体 {font_path} 不可用，制图要素降级为 Pillow 默认字体",
        })
        # 默认字体无中文字形：角注用 ASCII，避免豆腐块
        line1 = f"INDEX {index}  METHOD {method}  T={t_text}"
        line2 = f"T1={t1}  T2={t2}  RUN={run8}  GENERATED {generated}"
    else:
        info.update({
            "cartography_font_mode": "ttf-msyh",
            "cartography_font_note": "",
        })
        line1 = f"指数 {index}  |  方法 {method}  |  阈值 T={t_text}"
        line2 = f"t1 {t1}  |  t2 {t2}  |  生成 {generated}  |  RUN {run8}"

    draw.rectangle([0, strip_top, width - 1, height - 1], fill=_CARTOGRAPHY_BG)
    bar_y = strip_top + 8
    for i in range(4):
        xa = 12 + i * tick_px / 4.0
        xb = 12 + (i + 1) * tick_px / 4.0
        draw.rectangle([xa, bar_y, xb, bar_y + 8],
                       fill=(0, 0, 0) if i % 2 == 0 else (255, 255, 255),
                       outline=(0, 0, 0))
    draw.text((12, bar_y + 11), "0", font=font, fill=(30, 30, 30))
    draw.text((max(12, 12 + tick_px - 32), bar_y + 11),
              f"{tick_m} m", font=font, fill=(30, 30, 30))

    cx = max(24, width - 30)
    arrow_top = strip_top + 6
    draw.polygon([(cx, arrow_top), (cx - 7, arrow_top + 22),
                  (cx, arrow_top + 17), (cx + 7, arrow_top + 22)],
                 fill=_NORTH_COLOR)
    draw.line([(cx, arrow_top + 16), (cx, arrow_top + 30)],
              fill=_NORTH_COLOR, width=2)
    draw.text((cx - 4, arrow_top + 30), "N", font=font, fill=_NORTH_COLOR)

    draw.text((12, strip_top + 42), line1, font=font, fill=(45, 45, 45))
    draw.text((12, strip_top + 62), line2, font=font, fill=(45, 45, 45))
    return np.asarray(img).copy()


def build_change_canvas(
    change,
    valid_ratio: float,
    *,
    run_id: str = "",
    params_snapshot: Optional[dict] = None,
    otsu_threshold: Optional[float] = None,
    font_path: str = DEFAULT_FONT_PATH,
    colors: Optional[Dict[int, Tuple[int, int, int]]] = None,
    attribution_lines: Optional[Sequence[str]] = None,
    cartographic: bool = False,
    transform=None,
    crs=None,
    cartography_meta: Optional[dict] = None,
) -> Tuple[np.ndarray, Dict[str, object]]:
    """构建专题图 RGB 画布（纯函数，不落盘），返回 (canvas, info)。

    ``cartographic=False`` 时像素与历史版本逐点一致（裸图开关关闭 = 向后兼容）；
    ``True`` 时在下方追加独立底栏合成比例尺 / 指北针 / 角注，不遮挡专题图与图例。
    """
    if not 0.0 <= float(valid_ratio) <= 1.0:
        raise ValueError(f"valid_ratio 需落在 [0, 1]，实际 {valid_ratio}")
    palette = dict(CLASS_COLORS if colors is None else colors)
    arr = np.asarray(change)
    if arr.ndim != 2:
        raise ValueError(f"变化图需为二维数组，实际 {arr.shape}")

    rgb = colorize_change(arr, palette)
    height, width = arr.shape
    extra_h = _CARTOGRAPHY_H if cartographic else 0
    canvas = np.full((height + _HEADER_H + _LEGEND_H + extra_h, width, 3),
                     _BG_COLOR, dtype=np.uint8)
    canvas[_HEADER_H:_HEADER_H + height] = rgb

    verdict = judge_valid_ratio(float(valid_ratio))
    info: Dict[str, object] = {
        "cartographic": bool(cartographic),
        "valid_ratio": float(valid_ratio),
        "code": verdict.code,
        "warn": verdict.warn,
        "terminate": verdict.terminate,
        "text_mode": "ascii5x7",
        "degraded": False,
        "reason": "",
        "font_path": font_path,
        "run_id": run_id,
        "params_snapshot": dict(params_snapshot or {}),
    }

    # ① 像素文字回显 valid_ratio：始终用内置 5x7 点阵，不依赖任何字体/第三方库
    draw_text_5x7(canvas, 8, 6, f"VALID_RATIO={float(valid_ratio):.4f}", color=(15, 15, 15))
    second = f"RUN={run_id.upper()}" if run_id else "CHANGE CLASS MAP"
    if otsu_threshold is not None:
        second += f"  OTSU_T={float(otsu_threshold):.4f}"
    draw_text_5x7(canvas, 8, 26, second, color=(60, 60, 60))

    # 字体能力探测（PIL + msyh.ttc）
    font = _load_ttf(font_path, 18)
    pil_img = _pil_image(canvas) if font is not None else None
    ttf_ok = font is not None and pil_img is not None

    # ② EX5 告警文字（0.10 ≤ ratio < 0.50）
    if verdict.warn and not ttf_ok:
        draw_text_5x7(canvas, 8, 48, "LOW VALID DATA - FOR REFERENCE ONLY",
                      color=(160, 30, 30))
        info["degraded"] = True
        info["reason"] = "中文字体/PIL 不可用：EX5 告警退化为内置 5x7 ASCII 点阵字模"

    legend_y0 = _HEADER_H + height
    # 窄图（合成小栅格自测）时图例色块/告警底框需收敛到画布内
    swatch_x1 = min(42, width)
    banner_x1 = max(7, min(width - 6, 520))
    # ③ ASCII 图例（无中文字体时）
    if not ttf_ok:
        draw_text_5x7(canvas, 8, legend_y0 + 4, "LEGEND", color=(20, 20, 20))
        for i, code in enumerate((1, 2, 3, CHANGE_TRANSITION, 0)):
            y = legend_y0 + 24 + i * 20
            if swatch_x1 > 8:
                canvas[y:y + 14, 8:swatch_x1] = palette[code]
            draw_text_5x7(canvas, 50, y, f"{code} {CLASS_NAMES_EN[code]}", color=(20, 20, 20))
        for j, line in enumerate(attribution_lines or ()):
            draw_text_5x7(canvas, 8, legend_y0 + 126 + j * 16, line, color=(70, 70, 70))
        info["text_mode"] = "ascii5x7"
    else:
        # ④ 中文图例 / 中文 EX5 告警（PIL + 微软雅黑）
        from PIL import ImageDraw

        draw = ImageDraw.Draw(pil_img)
        if verdict.warn:
            draw.rectangle([6, 44, banner_x1, 44 + 24], fill=(255, 238, 185))
            draw.text((10, 46), "有效数据偏低，结论仅供参考", font=font, fill=(150, 30, 30))
        draw.text((8, legend_y0 + 2), "图例 LEGEND / CHANGE CLASS", font=font, fill=(20, 20, 20))
        for i, code in enumerate((1, 2, 3, CHANGE_TRANSITION, 0)):
            y = legend_y0 + 24 + i * 20
            if swatch_x1 > 8:
                draw.rectangle([8, y, swatch_x1, y + 14], fill=palette[code], outline=(0, 0, 0))
            draw.text((50, y - 2), f"{code}  {CLASS_NAMES_ZH[code]}  {CLASS_NAMES_EN[code]}",
                      font=font, fill=(20, 20, 20))
        for j, line in enumerate(attribution_lines or ()):
            draw.text((8, legend_y0 + 124 + j * 16), line, font=font, fill=(70, 70, 70))
        canvas = np.asarray(pil_img).copy()
        info["text_mode"] = "ttf"
        info["font"] = "msyh.ttc"

    if cartographic:
        meta = dict(cartography_meta or {})
        meta.setdefault("run_id", run_id)
        canvas = _draw_cartographic_strip(
            canvas, info, map_height=height, transform=transform, crs=crs,
            meta=meta, font_path=font_path,
        )

    return canvas, info


def _write_png_rasterio(canvas: np.ndarray, path: Path) -> None:
    bands = np.moveaxis(np.ascontiguousarray(canvas), -1, 0)  # (3, H, W)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.open(
            str(path), "w", driver="PNG",
            width=int(canvas.shape[1]), height=int(canvas.shape[0]),
            count=3, dtype="uint8",
        ) as dst:
            dst.write(bands)


def render_change_png(
    change,
    valid_ratio: float,
    output_path,
    *,
    run_id: str = "",
    params_snapshot: Optional[dict] = None,
    otsu_threshold: Optional[float] = None,
    font_path: str = DEFAULT_FONT_PATH,
    colors: Optional[Dict[int, Tuple[int, int, int]]] = None,
    attribution_lines: Optional[Sequence[str]] = None,
    cartographic: bool = False,
    transform=None,
    crs=None,
    cartography_meta: Optional[dict] = None,
) -> Dict[str, object]:
    """渲染三类变化专题图 PNG 并落盘，返回含退化说明的元数据 dict。"""
    canvas, info = build_change_canvas(
        change, valid_ratio,
        run_id=run_id, params_snapshot=params_snapshot,
        otsu_threshold=otsu_threshold, font_path=font_path,
        colors=colors, attribution_lines=attribution_lines,
        cartographic=cartographic, transform=transform, crs=crs,
        cartography_meta=cartography_meta,
    )
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        _write_png_rasterio(canvas, path)
        info["writer"] = "rasterio.PNG"
    except Exception as exc:  # 极端情况下退 PIL
        from PIL import Image

        Image.fromarray(canvas).save(str(path))
        info["writer"] = "PIL"
        info["degraded"] = True
        info["reason"] = (str(info["reason"]) + f"；rasterio PNG 写入失败改用 PIL：{exc}").strip("；")

    info.update({
        "path": str(path.resolve()),
        "width": int(canvas.shape[1]),
        "height": int(canvas.shape[0]),
    })
    return info


def render_change_overlay(
    change,
    output_path,
    *,
    colors: Optional[Dict[int, Tuple[int, int, int]]] = None,
) -> Dict[str, object]:
    """纯分类着色图（地图叠加专用）：尺寸严格等于变化栅格，无页眉/图例/署名。

    与 :func:`render_change_png` 的「报告专题图」用途分离：

    - ``render_change_png`` 产出带制图要素的成品图（页眉 + 图幅 + 图例 + 署名），
      用于报告 / 下载 / PDF；画布比图幅高，不能当地理配准层。
    - 本函数只输出 ``colorize_change(change)`` 的 (H, W, 3) 像素，H/W 与输入栅格
      逐像元一致，前端按 AOI bounds 铺到地图上正好对齐，且不含任何文字。

    配色与 ``render_change_png`` 同源（``CLASS_COLORS`` / 过渡带色），因此地图
    叠加层与报告图例的四类联动高亮语义一致。纯函数口径：不改 ``change``。
    """
    rgb = colorize_change(change, colors)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_png_rasterio(rgb, path)
        writer = "rasterio.PNG"
    except Exception:  # noqa: BLE001 - 与 render_change_png 同款 PIL 兜底
        from PIL import Image
        Image.fromarray(rgb).save(str(path))
        writer = "PIL"
    return {
        "path": str(path.resolve()),
        "width": int(rgb.shape[1]),
        "height": int(rgb.shape[0]),
        "writer": writer,
    }

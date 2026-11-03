# -*- coding: utf-8 -*-
"""变化专题图 PNG 渲染（HLD 模块4 功能一，REQ-10 / NF6 / NF7）。

依赖约束：requirements 无 matplotlib，禁止新增大包。本模块用 numpy 生成
RGB 画布、用内置 **5×7 ASCII 点阵字模**逐像素绘制 ``VALID_RATIO=0.xxx``；
中文（图例 / EX5 告警）在 PIL + ``C:\\Windows\\Fonts\\msyh.ttc`` 可用时使用，
否则退化为 5×7 ASCII 点阵文字并在返回元数据中如实说明。PNG 优先用 rasterio
``driver="PNG"``（3 波段 uint8）落盘，失败再退 PIL。

三类配色：1 增强=绿、2 退化=红、3 稳定=灰蓝、0 无效=深灰；图例含 1/2/3
色块与中文名，并叠加 Copernicus 合规署名。
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning

from app.algorithm.change_detector import CHANGE_CODES, CHANGE_NODATA
from app.algorithm.mask_pipeline import judge_valid_ratio

# 三类配色（R, G, B）
CLASS_COLORS: Dict[int, Tuple[int, int, int]] = {
    CHANGE_NODATA: (64, 64, 64),   # 0 无效：深灰
    1: (46, 160, 67),              # 1 增强：绿
    2: (214, 48, 49),              # 2 退化：红
    3: (120, 140, 160),            # 3 稳定：灰蓝
}
CLASS_NAMES_ZH: Dict[int, str] = {1: "增强", 2: "退化", 3: "稳定", 0: "无效"}
CLASS_NAMES_EN: Dict[int, str] = {1: "ENHANCED", 2: "DEGRADED", 3: "STABLE", 0: "NODATA"}

DEFAULT_FONT_PATH: str = r"C:\Windows\Fonts\msyh.ttc"
_BG_COLOR: Tuple[int, int, int] = (245, 245, 245)
_HEADER_H: int = 72
_LEGEND_H: int = 140
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
    "draw_text_5x7", "colorize_change", "build_change_canvas", "render_change_png",
]


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
) -> Tuple[np.ndarray, Dict[str, object]]:
    """构建专题图 RGB 画布（纯函数，不落盘），返回 (canvas, info)。"""
    if not 0.0 <= float(valid_ratio) <= 1.0:
        raise ValueError(f"valid_ratio 需落在 [0, 1]，实际 {valid_ratio}")
    palette = dict(CLASS_COLORS if colors is None else colors)
    arr = np.asarray(change)
    if arr.ndim != 2:
        raise ValueError(f"变化图需为二维数组，实际 {arr.shape}")

    rgb = colorize_change(arr, palette)
    height, width = arr.shape
    canvas = np.full((height + _HEADER_H + _LEGEND_H, width, 3), _BG_COLOR, dtype=np.uint8)
    canvas[_HEADER_H:_HEADER_H + height] = rgb

    verdict = judge_valid_ratio(float(valid_ratio))
    info: Dict[str, object] = {
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
        for i, code in enumerate((1, 2, 3, 0)):
            y = legend_y0 + 24 + i * 20
            if swatch_x1 > 8:
                canvas[y:y + 14, 8:swatch_x1] = palette[code]
            draw_text_5x7(canvas, 50, y, f"{code} {CLASS_NAMES_EN[code]}", color=(20, 20, 20))
        for j, line in enumerate(attribution_lines or ()):
            draw_text_5x7(canvas, 8, legend_y0 + 106 + j * 16, line, color=(70, 70, 70))
        info["text_mode"] = "ascii5x7"
    else:
        # ④ 中文图例 / 中文 EX5 告警（PIL + 微软雅黑）
        from PIL import ImageDraw

        draw = ImageDraw.Draw(pil_img)
        if verdict.warn:
            draw.rectangle([6, 44, banner_x1, 44 + 24], fill=(255, 238, 185))
            draw.text((10, 46), "有效数据偏低，结论仅供参考", font=font, fill=(150, 30, 30))
        draw.text((8, legend_y0 + 2), "图例 LEGEND / CHANGE CLASS", font=font, fill=(20, 20, 20))
        for i, code in enumerate((1, 2, 3, 0)):
            y = legend_y0 + 24 + i * 20
            if swatch_x1 > 8:
                draw.rectangle([8, y, swatch_x1, y + 14], fill=palette[code], outline=(0, 0, 0))
            draw.text((50, y - 2), f"{code}  {CLASS_NAMES_ZH[code]}  {CLASS_NAMES_EN[code]}",
                      font=font, fill=(20, 20, 20))
        for j, line in enumerate(attribution_lines or ()):
            draw.text((8, legend_y0 + 104 + j * 16), line, font=font, fill=(70, 70, 70))
        canvas = np.asarray(pil_img).copy()
        info["text_mode"] = "ttf"
        info["font"] = "msyh.ttc"

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
) -> Dict[str, object]:
    """渲染三类变化专题图 PNG 并落盘，返回含退化说明的元数据 dict。"""
    canvas, info = build_change_canvas(
        change, valid_ratio,
        run_id=run_id, params_snapshot=params_snapshot,
        otsu_threshold=otsu_threshold, font_path=font_path,
        colors=colors, attribution_lines=attribution_lines,
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

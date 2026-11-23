# -*- coding: utf-8 -*-
"""批次 I · 双时相变化动画 GIF（t1 真彩 → t2 真彩 → 变化专题）。

真彩帧直接复用检测阶段落盘的 ``t1_composite.png`` / ``t2_composite.png``：
两图由 ``composite.true_color`` 的 2%–98% 截断拉伸生成，与页面
「双时相对比」卷帘视图完全同源，不在本模块重算、不引入第二套拉伸口径。

帧版式沿用 ``renderer`` 的专题图画布（页眉 + 图幅 + 图例 + 可选制图底栏），
三帧尺寸一致；整幅最长边超过 ``ANIMATION_MAX_EDGE`` 时等比缩放，保证 GIF 体积可控。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from app.algorithm.renderer import (
    CARTOGRAPHY_STRIP_H, DEFAULT_FONT_PATH, MAP_HEADER_H, MAP_LEGEND_H,
)
from app.config.constants import ANIMATION_FRAME_MS, ANIMATION_MAX_EDGE

__all__ = ["write_change_animation"]

_BG_COLOR = (245, 245, 245)
_CAPTION_COLOR = (25, 25, 25)


def _load_font(font_path: str, size: int) -> Tuple[object, str, str]:
    """微软雅黑优先；缺字体退 Pillow 默认字体并如实返回退化说明。"""
    try:
        return ImageFont.truetype(font_path, size, index=0), "ttf-msyh", ""
    except Exception:
        return ImageFont.load_default(), "pil-default", (
            f"系统字体 {font_path} 不可用，GIF 帧角注降级为 Pillow 默认字体"
        )


def _open_rgb(path) -> Image.Image:
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"动画帧源图不存在：{p}")
    with Image.open(p) as im:
        return im.convert("RGB")


def write_change_animation(
    *,
    t1_png,
    t2_png,
    output_path,
    theme_png=None,
    t1_label: str = "t1",
    t2_label: str = "t2",
    theme_label: str = "变化专题",
    cartographic: bool = False,
    font_path: str = DEFAULT_FONT_PATH,
) -> Dict[str, object]:
    """写 ``change_animation.gif``，返回帧数 / 尺寸 / 降级说明。

    ``theme_png`` 缺失或不可读时降级为 t1/t2 双帧真彩，不伪造专题帧；
    两个真彩帧缺任一则明确抛错（由 service 统一映射为 SYS-01）。
    """
    t1 = _open_rgb(t1_png)
    t2 = _open_rgb(t2_png)
    theme: Optional[Image.Image] = None
    if theme_png is not None and Path(theme_png).is_file():
        try:
            theme = _open_rgb(theme_png)
        except Exception:  # noqa: BLE001 - 专题帧损坏时按无专题降级
            theme = None

    base_w, base_h = theme.size if theme is not None else t1.size
    strip_h = CARTOGRAPHY_STRIP_H if (theme is not None and cartographic) else 0
    map_top = MAP_HEADER_H
    map_h = base_h - MAP_HEADER_H - MAP_LEGEND_H - strip_h
    if map_h < 32:  # 合成小图自测兜底：整幅当图幅用
        map_top, map_h = 0, base_h

    font, font_mode, font_note = _load_font(font_path, 15)
    ascii_mode = font_mode != "ttf-msyh"

    def caption_frame(src: Image.Image, label: str) -> Image.Image:
        canvas = Image.new("RGB", (base_w, base_h), _BG_COLOR)
        fitted = src.copy()
        fitted.thumbnail((base_w, map_h), Image.Resampling.LANCZOS)
        x = (base_w - fitted.width) // 2
        y = map_top + (map_h - fitted.height) // 2
        canvas.paste(fitted, (x, y))
        draw = ImageDraw.Draw(canvas)
        text = label if ascii_mode else f"{label} · 双时相真彩"
        draw.text((10, max(2, map_top // 3)), text, font=font, fill=_CAPTION_COLOR)
        return canvas

    t1_caption = "T1 TRUE COLOR" if ascii_mode else f"t1 真彩 {t1_label}"
    t2_caption = "T2 TRUE COLOR" if ascii_mode else f"t2 真彩 {t2_label}"
    frames = [caption_frame(t1, t1_caption), caption_frame(t2, t2_caption)]
    if theme is not None:
        # 第三帧逐像素复用刚导出的专题图 PNG（含或不含制图要素由同一开关决定）
        frames.append(theme.copy())

    scale = min(1.0, ANIMATION_MAX_EDGE / max(base_w, base_h))
    out_w, out_h = max(1, round(base_w * scale)), max(1, round(base_h * scale))
    if (out_w, out_h) != (base_w, base_h):
        frames = [f.resize((out_w, out_h), Image.Resampling.LANCZOS) for f in frames]

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        str(path), format="GIF", save_all=True,
        append_images=frames[1:], duration=ANIMATION_FRAME_MS,
        loop=0, disposal=2, optimize=False,
    )

    return {
        "path": str(path.resolve()),
        "width": out_w,
        "height": out_h,
        "n_frames": len(frames),
        "duration_ms": ANIMATION_FRAME_MS,
        "loop": 0,
        "theme_used": theme is not None,
        "font_mode": font_mode,
        "degraded": bool(font_note) or theme is None,
        "reason": "；".join(x for x in (
            font_note,
            "变化专题图缺失，降级为 t1/t2 双帧真彩" if theme is None else "",
        ) if x),
        "stretch_note": "真彩帧复用 t1/t2_composite.png 的 2%–98% 截断拉伸，与页面卷帘同源",
    }

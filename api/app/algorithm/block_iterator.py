# -*- coding: utf-8 -*-
"""分块迭代（HLD 模块1 功能二，REQ-03 / REQ-06 / NF3）。

按 BLOCK_SIZE（默认 256，可选 512）切分读写窗口，控制峰值内存。
"""
from __future__ import annotations

from typing import Iterator, Tuple

from rasterio.windows import Window

from app.config.constants import BLOCK_SIZE, BLOCK_SIZE_OPTIONS


def validate_block_size(block_size: int) -> int:
    """校验分块尺寸属于 BLOCK_SIZE_OPTIONS（256 / 512）。"""
    if block_size not in BLOCK_SIZE_OPTIONS:
        raise ValueError(f"block_size 需属于 {BLOCK_SIZE_OPTIONS}，实际 {block_size}")
    return int(block_size)


def iter_blocks(
    height: int,
    width: int,
    block_size: int = BLOCK_SIZE,
) -> Iterator[Window]:
    """按 block_size 迭代 rasterio 窗口（行主序）。"""
    bs = validate_block_size(block_size)
    for row_off in range(0, height, bs):
        rows = min(bs, height - row_off)
        for col_off in range(0, width, bs):
            cols = min(bs, width - col_off)
            yield Window(col_off=col_off, row_off=row_off, width=cols, height=rows)


def iter_slices(
    height: int,
    width: int,
    block_size: int = BLOCK_SIZE,
) -> Iterator[Tuple[slice, slice]]:
    """按 block_size 迭代 numpy 切片，等价于 :func:`iter_blocks` 的数组版。"""
    bs = validate_block_size(block_size)
    for row_off in range(0, height, bs):
        row_end = min(row_off + bs, height)
        for col_off in range(0, width, bs):
            col_end = min(col_off + bs, width)
            yield slice(row_off, row_end), slice(col_off, col_end)


def count_blocks(height: int, width: int, block_size: int = BLOCK_SIZE) -> int:
    """计算分块总数。"""
    bs = validate_block_size(block_size)
    n_rows = (height + bs - 1) // bs
    n_cols = (width + bs - 1) // bs
    return n_rows * n_cols

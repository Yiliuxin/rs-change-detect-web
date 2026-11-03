# -*- coding: utf-8 -*-
"""掩膜流水线与有效像元占比四段判定（HLD 模块2 功能二，REQ-05）。

四段判定（阈值取自 ``app.config.constants``，禁止硬编码）：
  ┌ valid_ratio == 0                     → EX3  全云，终止
  ├ 0 < ratio < VALID_RATIO_MIN(0.10)    → EX11 有效像元不足，终止
  ├ 0.10 ≤ ratio < VALID_RATIO_WARN(0.50)→ EX5  告警（可继续档），不终止
  └ ratio ≥ VALID_RATIO_WARN(0.50)       → OK   通过
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.algorithm.mask_builder import MaskSet, build_masks, resample_scl
from app.config.constants import ERROR_MESSAGES, VALID_RATIO_MIN, VALID_RATIO_WARN

CODE_OK = "OK"


@dataclass(frozen=True)
class ValidRatioResult:
    """有效像元占比判定结果。"""

    valid_ratio: float
    total: int
    valid_count: int
    code: str
    message: str
    terminate: bool
    warn: bool

    def as_dict(self) -> dict:
        return {
            "valid_ratio": self.valid_ratio,
            "total": self.total,
            "valid_count": self.valid_count,
            "code": self.code,
            "message": self.message,
            "terminate": self.terminate,
            "warn": self.warn,
        }


def compute_valid_ratio(valid_mask: np.ndarray) -> float:
    """有效像元占比 = 有效像元数 / 总像元数；空数组返回 0.0。"""
    mask = np.asarray(valid_mask)
    if mask.size == 0:
        return 0.0
    return float(mask.sum()) / float(mask.size)


def judge_valid_ratio(valid_ratio: float, mask: Optional[np.ndarray] = None) -> ValidRatioResult:
    """四段判定：严格互斥且穷尽，无死分支。

    参数：
        valid_ratio：0~1 的有效像元占比
        mask       ：可选的有效掩膜，用于统计像元数与总数
    """
    if not 0.0 <= valid_ratio <= 1.0:
        raise ValueError(f"valid_ratio 需落在 [0, 1]，实际 {valid_ratio}")

    if mask is not None:
        total = int(np.asarray(mask).size)
        valid_count = int(np.asarray(mask).sum())
    else:
        total = -1
        valid_count = -1

    if valid_ratio == 0.0:
        # 第 1 段：全云 / 全无效 —— 终止
        code, terminate, warn = "EX3", True, False
    elif valid_ratio < VALID_RATIO_MIN:
        # 第 2 段：0 < ratio < 0.10 —— 终止档
        code, terminate, warn = "EX11", True, False
    elif valid_ratio < VALID_RATIO_WARN:
        # 第 3 段：0.10 ≤ ratio < 0.50 —— 告警但可继续
        code, terminate, warn = "EX5", False, True
    else:
        # 第 4 段：ratio ≥ 0.50 —— 通过
        code, terminate, warn = CODE_OK, False, False

    return ValidRatioResult(
        valid_ratio=valid_ratio,
        total=total,
        valid_count=valid_count,
        code=code,
        message=ERROR_MESSAGES.get(code, "有效像元占比合格"),
        terminate=terminate,
        warn=warn,
    )


def run_mask_pipeline(
    scl_path,
    ref_profile: dict,
    dn_nodata_mask: Optional[np.ndarray] = None,
) -> tuple[MaskSet, ValidRatioResult]:
    """掩膜流水线：SCL 重采样对齐 → 构建掩膜 → 计算占比 → 四段判定。

    返回 (MaskSet, ValidRatioResult)。调用方依据 ``terminate`` 决定是否中断流程。
    """
    scl_aligned = resample_scl(scl_path, ref_profile)
    masks = build_masks(scl_aligned, nodata_mask=dn_nodata_mask)
    ratio = compute_valid_ratio(masks.valid)
    result = judge_valid_ratio(ratio, mask=masks.valid)
    return masks, result

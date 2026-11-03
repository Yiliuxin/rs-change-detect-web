# -*- coding: utf-8 -*-
"""辐射口径确认与定标（HLD 模块2 功能一，REQ-04 / NF5）。

铁律：S2 与 Landsat 公式**禁止互套**；混用抛 :class:`FormulaMixedError`（EX8）。
处理基线缺失或不达标（S2 < 04.00、Landsat 非 C2 L2）→ :class:`BaselineUndeterminableError`（EX9）。
公式系数一律取自 ``app.config.constants``，本模块不出现任何数值字面量。
"""
from __future__ import annotations

import re
from typing import Optional

import numpy as np

from app.config.constants import (
    LS_COLLECTION,
    RADIOMETRIC_FORMULAS,
    S2_BASELINE_MIN,
    SENSOR_L8,
    SENSOR_L9,
    SENSOR_S2,
    VALID_SENSORS,
)

_BASELINE_RE = re.compile(r"^(\d+)\.(\d+)$")


class FormulaMixedError(Exception):
    """辐射口径混用（双时相数据源不一致或拿错 sensor 的公式）→ EX8（NF5）。"""

    code = "EX8"


class BaselineUndeterminableError(Exception):
    """处理基线版次缺失或不可判定 / 未达标 → EX9。"""

    code = "EX9"


def _baseline_to_float(baseline: str) -> float:
    m = _BASELINE_RE.match(baseline.strip())
    if not m:
        raise BaselineUndeterminableError(f"处理基线版次格式不可判定：{baseline!r}（EX9）")
    return float(f"{int(m.group(1))}.{m.group(2)}")


def check_baseline(sensor: str, proc_baseline: Optional[str]) -> str:
    """判定处理基线是否可用，返回规范化后的版次串；否则抛 EX9。

    - S2：缺失或 < S2_BASELINE_MIN（04.00）→ EX9；
    - L8/L9：缺失或不含 C2 L2 标识 → EX9。
    """
    key = (sensor or "").strip().upper()
    if key not in VALID_SENSORS:
        raise BaselineUndeterminableError(f"未知 sensor：{sensor!r}，基线不可判定（EX9）")

    if not proc_baseline or not str(proc_baseline).strip():
        raise BaselineUndeterminableError(f"{key} 处理基线缺失，基线不可判定（EX9）")

    value = str(proc_baseline).strip()

    if key == SENSOR_S2:
        version = _baseline_to_float(value)
        if version < _baseline_to_float(S2_BASELINE_MIN):
            raise BaselineUndeterminableError(
                f"S2 处理基线 {value} < {S2_BASELINE_MIN}，基线不达标（EX9）"
            )
        return value

    # Landsat：要求 Collection 2 Level-2
    norm = value.upper().replace("-", "").replace("_", "")
    if "C2" not in norm or "L2" not in norm:
        raise BaselineUndeterminableError(
            f"{key} 处理基线 {value} 非 {LS_COLLECTION}，基线不可判定（EX9）"
        )
    return value


def resolve_formula(sensor: str, formula_id: Optional[str] = None) -> dict:
    """按 sensor 锁定公式；若显式传入 formula_id 且与 sensor 不匹配 → EX8。"""
    key = (sensor or "").strip().upper()
    if key not in RADIOMETRIC_FORMULAS:
        raise FormulaMixedError(f"未知 sensor：{sensor!r}，无法锁定辐射定标公式（EX8）")

    entry = RADIOMETRIC_FORMULAS[key]
    if formula_id and str(formula_id).strip() != entry["formula_id"]:
        raise FormulaMixedError(
            f"辐射口径混用：sensor={key} 期望公式 {entry['formula_id']}，实际传入 {formula_id}（EX8）"
        )
    return entry


def assert_same_caliber(sensor_t1: str, sensor_t2: str) -> str:
    """双时相口径一致性校验；sensor 不一致（数据源混用）→ EX8。"""
    s1 = (sensor_t1 or "").strip().upper()
    s2 = (sensor_t2 or "").strip().upper()
    if s1 != s2:
        raise FormulaMixedError(
            f"双时相数据源不一致：t1={sensor_t1!r} / t2={sensor_t2!r}（EX8）"
        )
    if s1 not in VALID_SENSORS:
        raise FormulaMixedError(f"未知 sensor：{s1!r}（EX8）")
    return s1


def to_reflectance(
    dn,
    sensor: str,
    proc_baseline: Optional[str] = None,
    formula_id: Optional[str] = None,
):
    """把 DN 转为地表反射率 ρ（float32）。

    参数：
        dn            ：DN 数组（uint16 为主）
        sensor        ：S2 / L8 / L9
        proc_baseline ：处理基线；传 None 时跳过 EX9 判定（供纯算法自测使用）
        formula_id    ：显式指定公式标识，与 sensor 不符即 EX8
    """
    key = (sensor or "").strip().upper()
    entry = resolve_formula(key, formula_id)

    if proc_baseline is not None:
        check_baseline(key, proc_baseline)

    arr = np.asarray(dn)
    func = entry["func"]
    rho = func(arr.astype(np.float32))
    return rho.astype(np.float32)


__all__ = [
    "BaselineUndeterminableError",
    "FormulaMixedError",
    "assert_same_caliber",
    "check_baseline",
    "resolve_formula",
    "to_reflectance",
    "SENSOR_S2",
    "SENSOR_L8",
    "SENSOR_L9",
]

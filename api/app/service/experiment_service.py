# -*- coding: utf-8 -*-
"""批次 E · 只读实验服务：方法对比 / 阈值敏感性（NF9 可复现 · REQ-07/11/14）。

语义（务必与检测主管线区分）：
  * 全部基于**已有 run** 的落盘双时相指数栅格与参数快照在内存里复算；
  * **不产生新 run、不进历史列表、不写 task 表、不写数据库、不落盘任何
    GeoTIFF / GeoJSON / PNG / PDF 产物**；
  * 仅把计算结果缓存到 ``runs/<run_id>/experiments.json``（受 .gitignore 保护），
    命中缓存原样带回 ``computed_at``，保证口径可复现、演示秒开。

口径锁定：
  * 三类编码 1/2/3、形态学后处理、过渡带、面积单位（ha）与
    ``app.service.change_service`` 完全一致；检测调用链只读复用
    ``app.algorithm.*`` 纯函数，**绝不复制粘贴两份检测逻辑**：
    :func:`recompute` 是 compare 与 sensitivity 的唯一复算入口。
  * 面积一律取 ``params_snapshot.pixel_area_m2``，绝不从 rasterio profile 换算
    （profile 可能是经纬度，见 NF9 / REQ-11）。
  * 敏感性只扫「人工可调的阈值维度」：以基准 T=|threshold| 为中心做 5 档
    ``scale*T``；Otsu 自适应阈值场景以 Otsu 值作为基准 T 照样可扫——
    此时各档等价于「若改用该固定阈值」的假设分析，不改变基准 run 本身。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import rasterio

from app.algorithm.change_detector import (
    CHANGE_CODES, CHANGE_NODATA, M2_PER_HA, change_stats, classify_change,
    delta_ndvi, transition_band_mask, transition_band_stats,
)
from app.algorithm.cva_detector import detect_change_cva
from app.algorithm.morphology import clean_change_map
from app.algorithm.otsu import otsu_threshold
from app.algorithm.regression_detector import detect_change_regression
from app.api.errors import ApiError
from app.config.constants import (
    COMPARE_MAX_VARIANTS, ERROR_MESSAGES, EXPERIMENTS_CACHE_COMPARE_KEY,
    EXPERIMENTS_CACHE_FILE, EXPERIMENTS_CACHE_SENSITIVITY_KEY,
    EXPERIMENTS_CACHE_SIGNATURE_LEN, EXPERIMENTS_CACHE_VERSION,
    INDEX_AUXILIARY, INDEX_RASTER_T1, INDEX_RASTER_T2, MANUAL_THRESHOLD_RANGE,
    METHOD_CVA, METHOD_OTSU, METHOD_REGRESSION, SENSITIVITY_SCALES,
    THRESHOLD_MODE_MANUAL, THRESHOLD_MODE_OTSU, VALID_CHANGE_METHODS,
    VALID_INDEXES, VALID_THRESHOLD_MODES,
)
from app.repository import task_repo
from app.service.common import index_raster_names, regression_artifact_names, run_dir

# 差值法基准三类主图名，与 probe_service.py / common.py 主三件套同源（文件名，非业务数值）。
_BASE_CHANGE_DIFF = "change.tif"

__all__ = [
    "ExperimentContext", "load_context", "agreement_pct", "recompute",
    "validate_variants", "compare", "sensitivity",
    "cache_path", "read_cache", "write_cache",
    # 批次 F：只读实验数据出报告 / CSV（REQ-12/14）
    "sensitivity_best_interval", "sensitivity_conclusion",
    "compare_rows", "sensitivity_rows", "compare_csv", "sensitivity_csv",
    "read_compare_cached", "read_sensitivity_cached",
    "COMPARE_CSV_HEADER", "SENSITIVITY_CSV_HEADER",
    "COMPARE_COLUMNS_ZH", "COMPARE_COLUMNS_EN",
    "SENSITIVITY_COLUMNS_ZH", "SENSITIVITY_COLUMNS_EN",
]


@dataclass
class ExperimentContext:
    """一次只读实验的上下文（全部来自既有 run 的落盘产物与参数快照，NF9）。

    ``index_t1`` / ``index_t2`` 的键为 {基准 index, INDEX_AUXILIARY[基准 index]}
    （历史 NDVI 基准即 "NDVI" / "NDWI"），值为 2D float32 数组：由双波段 index
    raster 映射（band1=主指数 / band2=辅助指数），因此「换指数」变体只读已落盘的
    这两个键之一；基准 run 未落盘的指数不可复算（recompute 抛中文 EX12）。
    ``base_change`` 为落盘三类图或 None。
    """

    run_id: str
    base_index: str
    base_method: str
    base_threshold: float
    base_threshold_source: str
    pixel_area_m2: float
    index_t1: Dict[str, np.ndarray]
    index_t2: Dict[str, np.ndarray]
    base_change: Optional[np.ndarray]
    valid_mask: np.ndarray
    out_dir: Path
    snapshot: dict


# --------------------------------------------------------------------------- #
# 上下文加载（只读：不产生新 run / 不写 task 表 / 不落盘产物）
# --------------------------------------------------------------------------- #
def _load_meta(run_id: str) -> dict:
    """只读 ``task.params_snapshot``（一次 DB 查询）；读不到 task → EX2(404)。"""
    task = task_repo.get_task(run_id)
    if not task:
        raise ApiError("EX2", run_id=run_id, message=ERROR_MESSAGES["EX2"])
    return dict(task.get("params_snapshot") or {})


def _normalize_base(meta: dict) -> Tuple[str, str]:
    """基准 index / method 统一规范化（upper / lower）并兜底到合法枚举。"""
    idx = str(meta.get("index") or VALID_INDEXES[0]).strip().upper()
    if idx not in VALID_INDEXES:
        idx = VALID_INDEXES[0]
    mth = str(meta.get("method") or METHOD_OTSU).strip().lower()
    if mth not in VALID_CHANGE_METHODS:
        mth = METHOD_OTSU
    return idx, mth


def _resolve_pixel_area_m2(snapshot: dict, run_id: str) -> float:
    """面积口径：优先 ``snapshot.pixel_area_m2``；否则由基准 stats 反推；再缺 → SYS-01。

    绝不使用 ``pixel_area_from_profile``（profile 可能是经纬度，NF9 / REQ-11）。
    反推式：``area_ha * 10000 / pixel_count``，其中 10000 取 ``M2_PER_HA`` 常量。
    """
    try:
        area = float(snapshot.get("pixel_area_m2"))
        if math.isfinite(area) and area > 0:
            return area
    except (TypeError, ValueError):
        pass
    for item in snapshot.get("stats") or []:
        try:
            pc = float(item.get("pixel_count") or 0)
            ah = float(item.get("area_ha") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        if pc > 0 and ah > 0 and math.isfinite(ah):
            return ah * M2_PER_HA / pc
    raise ApiError(
        "SYS-01", run_id=run_id,
        message="参数快照缺少 pixel_area_m2 且无法由 stats 反推单像元面积，无法保证面积口径一致",
    )


def _read_two_bands(path: Path, run_id: str) -> Tuple[np.ndarray, np.ndarray]:
    """读 2 波段 float32 / NaN 的 index raster；缺文件 / 缺波段 / 损坏 → EX2(404)。"""
    if not path.is_file():
        raise ApiError(
            "EX2", run_id=run_id,
            message="双时相指数栅格产物缺失（index_t1/index_t2.tif），无法开展只读实验",
        )
    try:
        with rasterio.open(str(path)) as ds:
            b1 = np.asarray(ds.read(1), dtype=np.float32)
            b2 = np.asarray(ds.read(2), dtype=np.float32)
    except Exception as exc:  # noqa: BLE001 - 产物损坏同按缺失处理
        raise ApiError(
            "EX2", run_id=run_id,
            message="双时相指数栅格产物缺失或损坏，无法开展只读实验",
            detail=str(exc),
        ) from exc
    return b1, b2


def load_context(run_id: str) -> ExperimentContext:
    """载入既有 run 的复算上下文（批次 E · 只读实验，REQ-14 / NF9）。

    读 task / 参数快照 → 定位双时相双波段 index raster → 按 INDEX_AUXILIARY 映射
    还原 {基准 index, 辅助 index} 两键 → 计算双时相有限像元交集 ``valid_mask`` →
    读基准三类图（缺失置 None）。
    全程只读：不产生新 run、不写 task 表、不落盘 GeoTIFF/GeoJSON/PNG/PDF 产物。
    缺 index raster → EX2(404，说明产物缺失)；双时相形状不一致 → SYS-01。
    """
    snapshot = _load_meta(run_id)
    base_index, base_method = _normalize_base(snapshot)
    # 辅助指数一律查 INDEX_AUXILIARY 映射（扩到 4 指数后「另一个」不再唯一）。
    other_index = INDEX_AUXILIARY[base_index]

    try:
        base_threshold = float(snapshot.get("threshold") or 0.0)
    except (TypeError, ValueError):
        base_threshold = 0.0
    if not math.isfinite(base_threshold):
        base_threshold = 0.0
    base_threshold_source = str(
        snapshot.get("threshold_source") or THRESHOLD_MODE_OTSU
    ).strip().lower()
    if base_threshold_source not in VALID_THRESHOLD_MODES:
        base_threshold_source = THRESHOLD_MODE_OTSU

    pixel_area_m2 = _resolve_pixel_area_m2(snapshot, run_id)

    out_dir = run_dir(run_id)
    names = index_raster_names(base_method)
    b1_t1, b2_t1 = _read_two_bands(out_dir / names[INDEX_RASTER_T1], run_id)
    b1_t2, b2_t2 = _read_two_bands(out_dir / names[INDEX_RASTER_T2], run_id)
    if not (b1_t1.shape == b2_t1.shape == b1_t2.shape == b2_t2.shape):
        raise ApiError("SYS-01", run_id=run_id,
                       message="双时相指数栅格波段形状不一致，无法复算")

    # band1 = 基准主指数，band2 = 另一指数（换指数变体直接读另一波段）
    index_t1 = {base_index: b1_t1, other_index: b2_t1}
    index_t2 = {base_index: b1_t2, other_index: b2_t2}
    # 双时相 × 双波段均有限才有效（NaN nodata 自然排除）
    valid_mask = (np.isfinite(b1_t1) & np.isfinite(b2_t1)
                  & np.isfinite(b1_t2) & np.isfinite(b2_t2))

    change_name = (regression_artifact_names()["geotiff"]
                   if base_method == METHOD_REGRESSION else _BASE_CHANGE_DIFF)
    change_path = out_dir / change_name
    base_change: Optional[np.ndarray] = None
    if change_path.is_file():
        try:
            with rasterio.open(str(change_path)) as cds:
                base_change = np.asarray(cds.read(1))
        except Exception:  # noqa: BLE001 - 三类图缺失/损坏仅影响 agreement_pct，降级 None
            base_change = None

    return ExperimentContext(
        run_id=run_id,
        base_index=base_index,
        base_method=base_method,
        base_threshold=base_threshold,
        base_threshold_source=base_threshold_source,
        pixel_area_m2=pixel_area_m2,
        index_t1=index_t1,
        index_t2=index_t2,
        base_change=base_change,
        valid_mask=valid_mask,
        out_dir=out_dir,
        snapshot=snapshot,
    )


# --------------------------------------------------------------------------- #
# 纯函数 / 唯一复算入口（检测调用链只读复用 app.algorithm.*）
# --------------------------------------------------------------------------- #
def agreement_pct(base_change, variant_change, valid_mask=None) -> float:
    """纯函数：两轮三类图在「共同有效像元」上的一致率（0~100 百分数，批次 E）。

    共同有效 = ``(base != 0) & (variant != 0)``，再与可选 ``valid_mask`` 相交；
    分母恒为共同有效像元数。共同有效像元为 0（或 ``base_change`` 为 None /
    形状不符）时返回 0.0。0 取 ``CHANGE_NODATA`` 常量，不写死数值。
    """
    if base_change is None:
        return 0.0
    b = np.asarray(base_change)
    v = np.asarray(variant_change)
    if b.shape != v.shape:
        return 0.0
    both = (b != CHANGE_NODATA) & (v != CHANGE_NODATA)
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape == b.shape:
            both &= vm
    denom = int(np.count_nonzero(both))
    if denom == 0:
        return 0.0
    agree = int(np.count_nonzero(both & (b == v)))
    return float(agree) / float(denom) * 100.0


def _require_index(ctx: ExperimentContext, idx: str) -> None:
    """变体 / 辅助 index 必须存在于基准 run 已落盘的两个键里，否则中文 EX12。

    4 指数扩展后 compare 变体可接受 NDBI / NDMI 等合法枚举，但基准 run 的双波段
    index raster 只有 {base, aux} 两个波段；请求未落盘的 index 时明确以中文 EX12
    拒绝并提示需以该指数重新检测，绝不让它退化成 KeyError → SYS-01(500)。
    """
    keys = sorted(ctx.index_t1.keys())
    if idx not in ctx.index_t1 or idx not in ctx.index_t2:
        raise ApiError(
            "EX12", run_id=ctx.run_id,
            message=(f"该基准 run 的双波段指数栅格仅包含 {'/'.join(keys)}，"
                     f"不支持 {idx}，请先以 {idx} 为指数重新检测"),
        )


def recompute(ctx: ExperimentContext, index: str, method: str,
              threshold_mode: str = THRESHOLD_MODE_OTSU,
              threshold: Optional[float] = None,
              t_override: Optional[float] = None) -> dict:
    """唯一复算入口（批次 E · 只读实验）：compare 与 sensitivity 都必须经此函数。

    不复制粘贴检测调用链：差值法用 ``delta_ndvi`` + ``otsu_threshold``，
    回归法用 ``detect_change_regression`` 取 ``residual``，CVA 用
    ``detect_change_cva`` 取双波段欧氏幅值（``signed_magnitude``）；随后统一走
    ``classify_change(|T|) → clean_change_map → change_stats → transition_band_*``，
    与 ``change_service.py`` 基准管线同口径。全部在内存完成，不落盘任何产物。
    CVA 变体读同一 index raster 的两个波段（主指数 / 按 INDEX_AUXILIARY 配对的
    辅助指数），与 ``index`` 变体只换主指数波段正交；可复算的指数恒为基准 run
    已落盘的 {基准, 辅助} 两键（NDVI 基准即 NDVI / NDWI），请求其它合法枚举
    （如 NDBI / NDMI）由 ``_require_index`` 抛中文 EX12，不退化成 500。

    ``t_override`` 非空时直接把它当作阈值 T（敏感性五档），跳过 Otsu/manual
    求值，其余口径不变；``threshold_mode=otsu`` 时对 hist_src 自适应求阈值，
    ``manual`` 时用给定阈值。阈值一律取 ``abs(float(T))`` 传给 ``classify_change``。
    返回 dict 含 config / threshold / areas(恒 3 项) / transition_pct /
    change / valid / valid_count（后三者仅供内存计算 agreement，不进响应缓存）。
    """
    idx = str(index).strip().upper()
    mth = str(method).strip().lower()
    mode = str(threshold_mode or THRESHOLD_MODE_OTSU).strip().lower()
    # 变体 index 必须是基准 run 已落盘的两个键之一，否则中文 EX12（不退化成 500）。
    _require_index(ctx, idx)
    t1 = ctx.index_t1[idx]
    t2 = ctx.index_t2[idx]
    valid = ctx.valid_mask

    # 固定阈值（manual / t_override）；otsu 模式为 None，交由检测源自适应求阈值。
    if t_override is not None:
        fixed_T = abs(float(t_override))
    elif mode == THRESHOLD_MODE_MANUAL:
        fixed_T = abs(float(threshold))
    else:
        fixed_T = None

    if mth == METHOD_REGRESSION:
        # 回归残差法：detect_change_regression 产出残差 hist_src；
        # threshold=None 时内部对残差做 Otsu（与差值法 otsu_threshold 同源口径），
        # 传入固定阈值时跳过 Otsu。reg["threshold"] 即最终阈值 T。
        reg = detect_change_regression(t1, t2, valid_mask=valid, threshold=fixed_T)
        hist_src = reg["residual"]
        T = abs(float(reg["threshold"]))
    elif mth == METHOD_CVA:
        # 批次 G：与 change_service 主管线同源的双波段 [主指数, 辅助指数] 输入；
        # 欧氏幅值做 Otsu（fixed_T 非空时为手动/敏感性档位幅值阈值）。
        # signed_magnitude 的 |·| 恒等于 CVA 幅值，过渡带口径 |CVA−T| ≤ 0.25T。
        # 辅助指数按 INDEX_AUXILIARY 映射取（4 指数后「另一个」不再唯一），
        # 且必须已落盘；缺波段一律中文 EX12，绝不 KeyError → 500。
        other = INDEX_AUXILIARY.get(idx, "")
        _require_index(ctx, other)
        cva = detect_change_cva(
            (ctx.index_t1[idx], ctx.index_t1[other]),
            (ctx.index_t2[idx], ctx.index_t2[other]),
            valid_mask=valid, threshold=fixed_T,
        )
        hist_src = cva["signed_magnitude"]
        T = abs(float(cva["threshold"]))
    else:
        # 差值法：Δ = index(t2) − index(t1)
        hist_src = delta_ndvi(t2, t1)
        if fixed_T is not None:
            T = fixed_T
        else:
            T, _fallback, _reason = otsu_threshold(hist_src, valid_mask=valid)
            T = abs(float(T))

    # 对称阈值一律取 |T|；形态学与基准管线同一函数、同一参数
    change_raw = classify_change(hist_src, abs(float(T)), valid_mask=valid)
    change = clean_change_map(change_raw)

    # 面积口径：pixel_area_m2 来自 params_snapshot，绝不从 rasterio profile 换算
    summary = change_stats(
        change, pixel_area_m2_value=ctx.pixel_area_m2,
        valid_mask=valid, run_id=ctx.run_id, params_snapshot=None,
    )
    areas = summary["stats"]                    # 恒 3 项，change_class 1/2/3
    valid_count = int(summary["valid_count"])

    # 过渡带：与 change_service.py 573-584 逐字一致，占比转 0~100 百分数
    band_mask = transition_band_mask(hist_src, T, valid_mask=valid)
    band_mask &= (change != CHANGE_NODATA)
    transition_pct = float(
        transition_band_stats(band_mask, valid_count,
                              ctx.pixel_area_m2, T)["ratio"]
    ) * 100.0

    try:
        cfg_threshold = None if threshold is None else float(threshold)
    except (TypeError, ValueError):
        cfg_threshold = None

    return {
        "config": {"index": idx, "method": mth,
                   "threshold_mode": mode, "threshold": cfg_threshold},
        "threshold": float(T),
        "areas": areas,
        "transition_pct": float(transition_pct),
        "change": change,
        "valid": valid,
        "valid_count": valid_count,
    }


# --------------------------------------------------------------------------- #
# 变体校验 / 签名（错误一律 ApiError，避免 Pydantic 422）
# --------------------------------------------------------------------------- #
def validate_variants(variants, meta: dict, run_id: str = "") -> List[dict]:
    """规范化 + 校验 compare 变体（批次 E，错误一律 ``ApiError``）。

    - 空 / 超过 ``COMPARE_MAX_VARIANTS``(4) → EX12(400)；
    - index upper / method lower / threshold_mode lower 规范化，非法枚举 → EX12
      （非法 index 文案由 ``' | '.join(VALID_INDEXES)`` 自动含 NDVI/NDWI/NDBI/NDMI）；
    - 缺省：index / method 取基准，threshold_mode 取 otsu；
    - manual 时 threshold 必填、有限、落在 ``MANUAL_THRESHOLD_RANGE`` 开区间 → 否则 EX12；
    - 所有变体 (index, method) 都与基准相同 → EX1(400，带具体中文提示)。

    ``run_id`` 随错误体透出，保证 ``{code,message,run_id}`` 三字段齐全。
    """
    base_index, base_method = _normalize_base(meta)
    if not isinstance(variants, list) or len(variants) == 0:
        raise ApiError("EX12", run_id=run_id,
                       message="方法对比变体不能为空，至少需提供 1 个变体")
    if len(variants) > COMPARE_MAX_VARIANTS:
        raise ApiError(
            "EX12", run_id=run_id,
            message=f"方法对比变体数量超过上限 {COMPARE_MAX_VARIANTS} 个，请精简后重试",
        )
    lo, hi = MANUAL_THRESHOLD_RANGE
    out: List[dict] = []
    for raw in variants:
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump()
        v = dict(raw) if isinstance(raw, dict) else {}
        idx = str(v.get("index") or base_index).strip().upper()
        if idx not in VALID_INDEXES:
            raise ApiError(
                "EX12", run_id=run_id,
                message=f"变体 index 非法：{idx!r}，仅支持 {' | '.join(VALID_INDEXES)}",
            )
        mth = str(v.get("method") or base_method).strip().lower()
        if mth not in VALID_CHANGE_METHODS:
            raise ApiError(
                "EX12", run_id=run_id,
                message=f"变体 method 非法：{mth!r}，仅支持 {' | '.join(VALID_CHANGE_METHODS)}",
            )
        mode = str(v.get("threshold_mode") or THRESHOLD_MODE_OTSU).strip().lower()
        if mode not in VALID_THRESHOLD_MODES:
            raise ApiError(
                "EX12", run_id=run_id,
                message=f"变体 threshold_mode 非法：{mode!r}，仅支持 "
                        f"{' | '.join(VALID_THRESHOLD_MODES)}",
            )
        thr_value: Optional[float] = None
        if mode == THRESHOLD_MODE_MANUAL:
            try:
                thr_value = float(v.get("threshold"))
            except (TypeError, ValueError):
                raise ApiError(
                    "EX12", run_id=run_id,
                    message="阈值方式为 manual 时 threshold 必填且需为有限数",
                ) from None
            if not math.isfinite(thr_value) or not (lo < thr_value < hi):
                raise ApiError(
                    "EX12", run_id=run_id,
                    message=f"手动阈值需落在 {MANUAL_THRESHOLD_RANGE} 开区间且为有限数",
                )
        out.append({"index": idx, "method": mth,
                    "threshold_mode": mode, "threshold": thr_value})

    if all(v["index"] == base_index and v["method"] == base_method for v in out):
        raise ApiError(
            "EX1", run_id=run_id,
            message="方法对比变体需至少一项与基准的指数或检测方法不同，请调整后重试",
        )
    return out


def _signature(normalized: List[dict]) -> str:
    """规范化 variants → sha256 前 16 位（``EXPERIMENTS_CACHE_SIGNATURE_LEN``）。"""
    payload = [
        {
            "index": str(v["index"]).upper(),
            "method": str(v["method"]).lower(),
            "threshold_mode": str(v["threshold_mode"]).lower(),
            "threshold": v.get("threshold"),
        }
        for v in normalized
    ]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:EXPERIMENTS_CACHE_SIGNATURE_LEN]


# --------------------------------------------------------------------------- #
# 缓存（只写 runs/<run_id>/experiments.json，原子替换；不写 DB / 不落盘产物）
# --------------------------------------------------------------------------- #
def _default_cache() -> dict:
    """缓存初始结构：version / compare / sensitivity。"""
    return {
        "version": EXPERIMENTS_CACHE_VERSION,
        EXPERIMENTS_CACHE_COMPARE_KEY: {},
        EXPERIMENTS_CACHE_SENSITIVITY_KEY: None,
    }


def cache_path(run_id: str) -> Path:
    """``run_dir(run_id) / experiments.json``（受 .gitignore 保护）。"""
    return run_dir(run_id) / EXPERIMENTS_CACHE_FILE


def read_cache(run_id: str) -> dict:
    """不存在 / 损坏 / version 不匹配 → 返回 ``_default_cache()``。"""
    default = _default_cache()
    path = cache_path(run_id)
    if not path.is_file():
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return default
    if not isinstance(data, dict) or data.get("version") != EXPERIMENTS_CACHE_VERSION:
        return default
    if not isinstance(data.get(EXPERIMENTS_CACHE_COMPARE_KEY), dict):
        data[EXPERIMENTS_CACHE_COMPARE_KEY] = {}
    if EXPERIMENTS_CACHE_SENSITIVITY_KEY not in data:
        data[EXPERIMENTS_CACHE_SENSITIVITY_KEY] = None
    return data


def write_cache(run_id: str, data: dict) -> None:
    """原子写：同目录临时文件 + ``os.replace``，``ensure_ascii=False, default=str``。"""
    path = cache_path(run_id)
    payload = json.dumps(data, ensure_ascii=False, default=str)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{EXPERIMENTS_CACHE_FILE}.", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# 基准摘要（NF9 快照优先；老 run 缺字段时按同一套纯函数复算，不硬编码）
# --------------------------------------------------------------------------- #
def _base_areas(ctx: ExperimentContext) -> List[dict]:
    """基准三类面积：直接取 NF9 快照 ``stats``；恒 3 项（``CHANGE_CODES`` 1/2/3）。"""
    by_class: Dict[int, dict] = {}
    for item in ctx.snapshot.get("stats") or []:
        try:
            code = int(item.get("change_class"))
            by_class[code] = {
                "change_class": code,
                "pixel_count": int(item.get("pixel_count") or 0),
                "area_ha": float(item.get("area_ha") or 0.0),
            }
        except (TypeError, ValueError, AttributeError):
            continue
    return [
        by_class.get(int(code),
                     {"change_class": int(code), "pixel_count": 0, "area_ha": 0.0})
        for code in CHANGE_CODES
    ]


def _base_transition_pct(ctx: ExperimentContext, valid_count: int) -> float:
    """基准 ``transition_pct``：优先 ``params_snapshot.transition_band.ratio * 100``；

    老 run 无该字段时按基准 T 用同一套 ``transition_band_mask`` /
    ``transition_band_stats`` 复算，绝不硬编码数字。
    """
    tb = ctx.snapshot.get("transition_band")
    if isinstance(tb, dict) and tb.get("ratio") is not None:
        try:
            return float(tb["ratio"]) * 100.0
        except (TypeError, ValueError):
            pass
    T = abs(float(ctx.base_threshold))
    t1 = ctx.index_t1[ctx.base_index]
    t2 = ctx.index_t2[ctx.base_index]
    if ctx.base_method == METHOD_REGRESSION:
        reg = detect_change_regression(t1, t2, valid_mask=ctx.valid_mask, threshold=T)
        hist_src = reg["residual"]
    elif ctx.base_method == METHOD_CVA:
        # 辅助指数按 INDEX_AUXILIARY 映射取（与 load_context 建键同源）。
        other = INDEX_AUXILIARY[ctx.base_index]
        cva = detect_change_cva(
            (ctx.index_t1[ctx.base_index], ctx.index_t1[other]),
            (ctx.index_t2[ctx.base_index], ctx.index_t2[other]),
            valid_mask=ctx.valid_mask, threshold=T,
        )
        hist_src = cva["signed_magnitude"]
    else:
        hist_src = delta_ndvi(t2, t1)
    band_mask = transition_band_mask(hist_src, T, valid_mask=ctx.valid_mask)
    if ctx.base_change is not None and ctx.base_change.shape == band_mask.shape:
        band_mask &= (ctx.base_change != CHANGE_NODATA)
    ratio = float(
        transition_band_stats(band_mask, valid_count, ctx.pixel_area_m2, T)["ratio"]
    )
    return ratio * 100.0


def _base_summary(ctx: ExperimentContext) -> dict:
    """组装 compare 响应的 ``base``（NF9 参数快照字段 + 复算过渡带）。"""
    areas = _base_areas(ctx)
    valid_count = int(sum(int(item["pixel_count"]) for item in areas))
    # threshold_source 已规范为 otsu / manual，与 threshold_mode 值域一致
    src = ctx.base_threshold_source
    return {
        "run_id": ctx.run_id,
        "index": ctx.base_index,
        "method": ctx.base_method,
        "threshold": float(ctx.base_threshold),
        "threshold_source": src,
        "threshold_mode": src,
        "areas": areas,
        "transition_pct": float(_base_transition_pct(ctx, valid_count)),
        "pixel_area_m2": float(ctx.pixel_area_m2),
        "valid_count": valid_count,
    }


# --------------------------------------------------------------------------- #
# 业务编排（全部经由 recompute；缓存命中不重算，computed_at 仅计算时生成）
# --------------------------------------------------------------------------- #
def compare(run_id: str, variants, force: bool = False) -> dict:
    """POST /result/compare 编排（批次 E · 只读实验，NF9 可复现）。

    先校验变体 → 算签名 → 查缓存；命中（force=false 且 version 匹配且条目存在）
    **原样返回缓存对象（含原 computed_at），不重算**；未命中或 force=true 时
    ``load_context`` + 逐变体 ``recompute`` 并覆盖该签名条目。
    不产生新 run / 不写 task 表 / 不落盘产物，仅写 experiments.json 缓存。
    """
    meta = _load_meta(run_id)
    normalized = validate_variants(variants, meta, run_id=run_id)
    signature = _signature(normalized)

    cache = read_cache(run_id)
    compare_map = cache.get(EXPERIMENTS_CACHE_COMPARE_KEY)
    if (not force
            and cache.get("version") == EXPERIMENTS_CACHE_VERSION
            and isinstance(compare_map, dict)
            and signature in compare_map):
        return compare_map[signature]

    ctx = load_context(run_id)
    base = _base_summary(ctx)
    items: List[dict] = []
    for v in normalized:
        r = recompute(ctx, v["index"], v["method"],
                      threshold_mode=v["threshold_mode"], threshold=v["threshold"])
        items.append({
            "config": r["config"],
            "threshold": r["threshold"],
            "areas": r["areas"],
            "transition_pct": r["transition_pct"],
            "agreement_pct": agreement_pct(ctx.base_change, r["change"], r["valid"]),
        })
    response = {
        "base": base,
        "variants": items,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    cache["version"] = EXPERIMENTS_CACHE_VERSION
    cache.setdefault(EXPERIMENTS_CACHE_COMPARE_KEY, {})[signature] = response
    write_cache(run_id, cache)
    return response


def sensitivity(run_id: str, force: bool = False) -> dict:
    """POST /result/sensitivity 编排（批次 E · 只读实验，NF9 可复现）。

    对基准 T = ``abs(float(params_snapshot["threshold"]))`` 取
    ``SENSITIVITY_SCALES`` 五档 ``t = scale * T``，用**基准 index/method**、
    固定阈值 t 逐档走 ``recompute``（t_override=t，内存计算、不落盘）。
    缓存用固定键 ``sensitivity``，命中直接返回（不重算）；force=true 重算覆盖。
    口径见模块 docstring：敏感性只扫人工可调的阈值维度，Otsu 场景以 Otsu 值
    作为基准 T 照样可扫。

    即便命中缓存也先校验 run 存在（一次只读 DB 查询）：实验一律基于**已有 run**，
    AUTH_DISABLED=1 时归属校验直通，此处兜底保证不存在的 run 仍返回 EX2(404)。
    """
    _load_meta(run_id)
    cache = read_cache(run_id)
    cached = cache.get(EXPERIMENTS_CACHE_SENSITIVITY_KEY)
    if (not force
            and cache.get("version") == EXPERIMENTS_CACHE_VERSION
            and isinstance(cached, dict)):
        return cached

    ctx = load_context(run_id)
    T = abs(float(ctx.base_threshold))
    sweep: List[dict] = []
    for scale in SENSITIVITY_SCALES:
        t = float(scale) * T
        r = recompute(ctx, ctx.base_index, ctx.base_method, t_override=t)
        sweep.append({
            "t": float(t),
            "scale": float(scale),
            "areas": r["areas"],
            "transition_pct": r["transition_pct"],
        })
    response = {
        "base_threshold": float(T),
        "sweep": sweep,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    cache["version"] = EXPERIMENTS_CACHE_VERSION
    cache[EXPERIMENTS_CACHE_SENSITIVITY_KEY] = response
    write_cache(run_id, cache)
    return response


# --------------------------------------------------------------------------- #
# 批次 F · 只读实验数据出报告 / CSV（REQ-12/14 / EX3）
#
# 严格只读：以下函数一律基于 read_cache 的命中结果做**格式化 / 结论文案**，
# 绝不调用 compare() / sensitivity()、绝不重算、绝不写缓存、绝不落盘产物。
# 缓存缺字段一律标「—」，不补算、不补零、不臆造。
# CSV 文本一律不含 BOM（BOM 由路由层加），行尾 ``\r\n``，与 run_service 同款。
# --------------------------------------------------------------------------- #
_DASH: str = "—"
# 面积 ha / 占比 % / 阈值 T / 倍数的小数位：CSV 与 PDF 共用同一口径（统一小数位）
_EXP_AREA_DIGITS: int = 2
_EXP_PCT_DIGITS: int = 2
_EXP_T_DIGITS: int = 4
_EXP_SCALE_DIGITS: int = 1

COMPARE_CSV_HEADER: str = (
    "类型,指数,方法,阈值方式,阈值T,增强_ha,退化_ha,稳定_ha,过渡带占比_%,一致率_%"
)
SENSITIVITY_CSV_HEADER: str = "倍数,阈值T,增强_ha,退化_ha,稳定_ha,过渡带占比_%"
# PDF 表头（中文 / 英文退化）；与 CSV 列名同源，逐字对齐批次 F 契约
COMPARE_COLUMNS_ZH: Tuple[str, ...] = (
    "指数", "方法", "阈值方式", "阈值T", "增强_ha", "退化_ha", "稳定_ha",
    "过渡带占比_%", "一致率_%",
)
COMPARE_COLUMNS_EN: Tuple[str, ...] = (
    "INDEX", "METHOD", "MODE", "THRESHOLD T", "ENHANCED ha", "DEGRADED ha",
    "STABLE ha", "TRANSITION %", "AGREEMENT %",
)
SENSITIVITY_COLUMNS_ZH: Tuple[str, ...] = (
    "倍数", "阈值T", "增强_ha", "退化_ha", "稳定_ha", "过渡带占比_%",
)
SENSITIVITY_COLUMNS_EN: Tuple[str, ...] = (
    "SCALE", "THRESHOLD T", "ENHANCED ha", "DEGRADED ha", "STABLE ha",
    "TRANSITION %",
)


def _text_or_dash(value) -> str:
    """缓存缺字段（None / 空串）一律标「—」，绝不补算。"""
    if value is None:
        return _DASH
    text = str(value).strip()
    return text if text else _DASH


def _num_or_dash(value, digits: int) -> str:
    """数值按统一小数位格式化；缺失 / 非数 / 非有限 → 「—」（不补零、不补算）。"""
    if value is None or value == "":
        return _DASH
    try:
        num = float(value)
    except (TypeError, ValueError):
        return _DASH
    if not math.isfinite(num):
        return _DASH
    return f"{num:.{digits}f}"


def _areas_ha(areas) -> Tuple[str, str, str]:
    """按 change_class 1/2/3 取 area_ha（增强 / 退化 / 稳定）；缺类或缺值 → 「—」。"""
    by_class: Dict[int, object] = {}
    if isinstance(areas, (list, tuple)):
        for item in areas:
            if not isinstance(item, dict):
                continue
            try:
                code = int(item.get("change_class"))
            except (TypeError, ValueError):
                continue
            by_class[code] = item.get("area_ha")
    return tuple(  # type: ignore[return-value]
        _num_or_dash(by_class.get(int(code)), _EXP_AREA_DIGITS)
        for code in CHANGE_CODES
    )


def _areas_map(areas) -> Dict[int, float]:
    """逐字移植前端 ``areasMap``：按 change_class 1/2/3 累加 area_ha 并各自保留 2 位。"""
    acc = {1: 0.0, 2: 0.0, 3: 0.0}
    if isinstance(areas, (list, tuple)):
        for item in areas:
            if not isinstance(item, dict):
                continue
            try:
                code = int(item.get("change_class"))
            except (TypeError, ValueError):
                continue
            if code in (1, 2, 3):
                try:
                    value = float(item.get("area_ha") or 0)
                except (TypeError, ValueError):
                    value = 0.0
                acc[code] += value
    return {code: round(acc[code], _EXP_AREA_DIGITS) for code in (1, 2, 3)}


def _scale_value(value) -> float:
    """scale → float（缺字段兜底 0.0，仅用于排序 / 相邻档偏差，不进任何响应）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return num if math.isfinite(num) else 0.0


def _sorted_sweep(sweep) -> List[dict]:
    """按 scale 升序返回**新的** list（不改动缓存对象本身）。"""
    items = [r for r in (sweep if isinstance(sweep, (list, tuple)) else [])
             if isinstance(r, dict)]
    return sorted(items, key=lambda r: _scale_value(r.get("scale")))


def sensitivity_best_interval(sweep) -> Optional[dict]:
    """相邻档「增强+退化面积合计」变化绝对值最大的一档（逐字移植前端 sensConclusion）。

    返回 ``{"delta", "from", "to", "dev"}`` 或 None（无可比较相邻档）。
    只做只读结构化，绝不重算、绝不写缓存、绝不改 sensitivity() 响应结构。
    """
    items = _sorted_sweep(sweep)
    best: Optional[dict] = None
    for i in range(1, len(items)):
        frm, to = items[i - 1], items[i]
        a_frm, a_to = _areas_map(frm.get("areas")), _areas_map(to.get("areas"))
        sum_frm = round(a_frm[1] + a_frm[2], _EXP_AREA_DIGITS)
        sum_to = round(a_to[1] + a_to[2], _EXP_AREA_DIGITS)
        delta = abs(sum_to - sum_frm)
        f_scale = _scale_value(frm.get("scale"))
        t_scale = _scale_value(to.get("scale"))
        dev = max(abs(f_scale - 1), abs(t_scale - 1))
        if best is None or delta > best["delta"]:
            best = {"delta": delta, "from": f_scale, "to": t_scale, "dev": dev}
    return best


def sensitivity_conclusion(sweep) -> str:
    """敏感性自动结论文案（文案与算法逐字移植前端 sensConclusion，不重新措辞）。

    供 PDF「实验分析」章节使用；**不改 sensitivity() 响应结构、不往缓存塞 conclusion**。
    """
    best = sensitivity_best_interval(sweep)
    if best is None:
        return "本次扫描未返回可比较的相邻档，无法给出敏感区间（如实说明，不作定性结论）。"
    x = int(math.floor(best["dev"] * 100 + 0.5))          # JS Math.round（dev ≥ 0）
    d = f"{round(best['delta'], _EXP_AREA_DIGITS):.{_EXP_AREA_DIGITS}f}"
    frm = f"{best['from']:.{_EXP_SCALE_DIGITS}f}×"         # JS fmtScale：一位小数 + ×
    to = f"{best['to']:.{_EXP_SCALE_DIGITS}f}×"
    return (
        f"结果对阈值敏感区间在 ±{x}%：最陡档 {frm}→{to}"
        f"，增强＋退化面积合计变化 {d} ha（数字全部取自本次五档实测扫描）。"
    )


def compare_rows(response) -> List[List[str]]:
    """方法对比 9 列纯文本行（基准行 + 每个变体行），CSV 与 PDF 共用同一口径。

    列：指数 / 方法 / 阈值方式 / 阈值T / 增强_ha / 退化_ha / 稳定_ha /
    过渡带占比_% / 一致率_%。基准行一致率恒为「—」，缺字段一律「—」，绝不补算。
    """
    data = response if isinstance(response, dict) else {}
    rows: List[List[str]] = []
    base = data.get("base")
    if isinstance(base, dict):
        mode = base.get("threshold_source")
        if mode is None:
            mode = base.get("threshold_mode")
        a1, a2, a3 = _areas_ha(base.get("areas"))
        rows.append([
            _text_or_dash(base.get("index")),
            _text_or_dash(base.get("method")),
            _text_or_dash(mode),
            _num_or_dash(base.get("threshold"), _EXP_T_DIGITS),
            a1, a2, a3,
            _num_or_dash(base.get("transition_pct"), _EXP_PCT_DIGITS),
            _DASH,  # 基准行一致率
        ])
    variants = data.get("variants")
    for item in (variants if isinstance(variants, (list, tuple)) else []):
        if not isinstance(item, dict):
            continue
        cfg = item.get("config") if isinstance(item.get("config"), dict) else {}
        a1, a2, a3 = _areas_ha(item.get("areas"))
        rows.append([
            _text_or_dash(cfg.get("index")),
            _text_or_dash(cfg.get("method")),
            _text_or_dash(cfg.get("threshold_mode")),
            _num_or_dash(item.get("threshold"), _EXP_T_DIGITS),
            a1, a2, a3,
            _num_or_dash(item.get("transition_pct"), _EXP_PCT_DIGITS),
            _num_or_dash(item.get("agreement_pct"), _EXP_PCT_DIGITS),
        ])
    return rows


def sensitivity_rows(response) -> List[List[str]]:
    """敏感性 6 列纯文本行（按 scale 升序，一档一行），CSV 与 PDF 共用同一口径。"""
    data = response if isinstance(response, dict) else {}
    rows: List[List[str]] = []
    for item in _sorted_sweep(data.get("sweep")):
        a1, a2, a3 = _areas_ha(item.get("areas"))
        scale_text = _num_or_dash(item.get("scale"), _EXP_SCALE_DIGITS)
        scale_cell = _DASH if scale_text == _DASH else scale_text + "×"
        rows.append([
            scale_cell,
            _num_or_dash(item.get("t"), _EXP_T_DIGITS),
            a1, a2, a3,
            _num_or_dash(item.get("transition_pct"), _EXP_PCT_DIGITS),
        ])
    return rows


def compare_csv(response) -> str:
    """方法对比 CSV 文本（**不含 BOM**，BOM 由路由加）；表头 + 基准行 + 每个变体行。"""
    data = response if isinstance(response, dict) else {}
    has_base = isinstance(data.get("base"), dict)
    lines = [COMPARE_CSV_HEADER]
    for idx, row in enumerate(compare_rows(response)):
        kind = "基准" if (has_base and idx == 0) else "变体"
        lines.append(",".join([kind, *row]))
    return "\r\n".join(lines) + "\r\n"


def sensitivity_csv(response) -> str:
    """敏感性 CSV 文本（**不含 BOM**）；表头 + 按 scale 升序每档一行。"""
    lines = [SENSITIVITY_CSV_HEADER]
    for row in sensitivity_rows(response):
        lines.append(",".join(row))
    return "\r\n".join(lines) + "\r\n"


def read_compare_cached(run_id: str, variants) -> Optional[dict]:
    """批次 F · 只读：以与 :func:`compare` **完全相同**的签名定位缓存条目。

    链：``_load_meta → validate_variants → _signature → read_cache()[compare][signature]``。
    未命中返回 None；绝不调用 compare()、绝不重算、绝不写缓存、绝不落盘产物。
    task 不存在 → EX2(404)；变体非法 → EX12 / EX1（与主接口同口径）。
    """
    meta = _load_meta(run_id)
    normalized = validate_variants(variants, meta, run_id=run_id)
    signature = _signature(normalized)
    cache = read_cache(run_id)
    compare_map = cache.get(EXPERIMENTS_CACHE_COMPARE_KEY)
    if isinstance(compare_map, dict):
        hit = compare_map.get(signature)
        if isinstance(hit, dict):
            return hit
    return None


def read_sensitivity_cached(run_id: str) -> Optional[dict]:
    """批次 F · 只读：取 sensitivity 缓存条目；未命中返回 None，不调用 sensitivity()。"""
    _load_meta(run_id)  # 与 sensitivity() 一样先兜底判 task 存在 → EX2(404)
    cache = read_cache(run_id)
    cached = cache.get(EXPERIMENTS_CACHE_SENSITIVITY_KEY)
    return cached if isinstance(cached, dict) else None

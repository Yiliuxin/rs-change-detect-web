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
    INDEX_RASTER_T1, INDEX_RASTER_T2, MANUAL_THRESHOLD_RANGE,
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
]


@dataclass
class ExperimentContext:
    """一次只读实验的上下文（全部来自既有 run 的落盘产物与参数快照，NF9）。

    ``index_t1`` / ``index_t2`` 的键为 ``"NDVI"`` / ``"NDWI"``，值为 2D float32
    数组：由双波段 index raster 按基准 index 映射（band1=主指数 / band2=辅助指数），
    因此「换指数」变体直接读另一个波段即可。``base_change`` 为落盘三类图或 None。
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

    读 task / 参数快照 → 定位双时相双波段 index raster → 按基准 index 映射
    NDVI/NDWI → 计算双时相有限像元交集 ``valid_mask`` → 读基准三类图（缺失置 None）。
    全程只读：不产生新 run、不写 task 表、不落盘 GeoTIFF/GeoJSON/PNG/PDF 产物。
    缺 index raster → EX2(404，说明产物缺失)；双时相形状不一致 → SYS-01。
    """
    snapshot = _load_meta(run_id)
    base_index, base_method = _normalize_base(snapshot)
    other_index = next(i for i in VALID_INDEXES if i != base_index)

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
    CVA 变体读同一 index raster 的两个波段（主指数 / 辅助指数），与
    ``index`` 变体只换主指数波段正交，故批次 E 对比矩阵 3 方法 × 2 指数可复算。

    ``t_override`` 非空时直接把它当作阈值 T（敏感性五档），跳过 Otsu/manual
    求值，其余口径不变；``threshold_mode=otsu`` 时对 hist_src 自适应求阈值，
    ``manual`` 时用给定阈值。阈值一律取 ``abs(float(T))`` 传给 ``classify_change``。
    返回 dict 含 config / threshold / areas(恒 3 项) / transition_pct /
    change / valid / valid_count（后三者仅供内存计算 agreement，不进响应缓存）。
    """
    idx = str(index).strip().upper()
    mth = str(method).strip().lower()
    mode = str(threshold_mode or THRESHOLD_MODE_OTSU).strip().lower()
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
        other = next(i for i in VALID_INDEXES if i != idx)
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
    - index upper / method lower / threshold_mode lower 规范化，非法枚举 → EX12；
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
        other = next(i for i in VALID_INDEXES if i != ctx.base_index)
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

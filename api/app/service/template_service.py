# -*- coding: utf-8 -*-
"""检测参数模板编排（批次 D，NF9 参数快照模板化）。

路由层保持薄：本模块负责口径参数白名单过滤（多余键丢弃）、指数/方法/分块尺寸
枚举校验（非法值按 EX1 口径报错：{code,message,run_id}，中文提示）、模板名校验、
同用户重名 → TMPL-01(409)、模板不存在或属他人 → TMPL-02(404)（不泄漏存在性）。

白名单键集合（``TEMPLATE_PARAM_KEYS``，前端回填用，见 constants.py）：
  index / method / block_size / threshold；取值枚举与 POST /result/change
  口径面板（ChangeRequest）同源，阈值方式 otsu/manual 由 threshold 是否为空派生。
"""
from __future__ import annotations

import math
import uuid
from typing import Any, Dict, List, Optional

from app.api.errors import ApiError, as_api_error
from app.config.constants import (
    BLOCK_SIZE_OPTIONS, MANUAL_THRESHOLD_RANGE, TEMPLATE_NAME_MAX,
    TEMPLATE_PARAM_KEYS, VALID_CHANGE_METHODS, VALID_INDEXES,
)
from app.repository import template_repo

__all__ = ["create_template", "list_templates", "delete_template", "normalize_params"]

# EX1 口径参数报错的统一中文文案前缀（code=EX1，HTTP 400，结构 {code,message,run_id}）
_PARAM_ERR_PREFIX = "检测参数非法"


def _ex1_param(detail: str) -> ApiError:
    """模板口径参数错误：按 EX1 口径（code=EX1 / 400 / {code,message,run_id}），文案中文。"""
    return ApiError("EX1", message=f"{_PARAM_ERR_PREFIX}：{detail}", detail=detail)


def normalize_params(raw: Any) -> Dict[str, Any]:
    """口径参数白名单过滤 + 枚举校验（模板保存前的唯一入口）。

    - 只保留 ``TEMPLATE_PARAM_KEYS`` 内的键，多余键一律丢弃（不入库）；
    - index：归一化大写，须 ∈ VALID_INDEXES（NDVI / NDWI / NDBI / NDMI，
      错误文案由 '/'.join(VALID_INDEXES) 自动派生），非法 → EX1 口径 400；
    - method：归一化小写，须 ∈ VALID_CHANGE_METHODS（otsu/regression/cva），非法 → EX1；
    - block_size：整数，须 ∈ BLOCK_SIZE_OPTIONS（256/512），非法 → EX1；
    - threshold：可空；给定时须为有限数且落在 MANUAL_THRESHOLD_RANGE 开区间，
      非法 → EX1（null = Otsu 自适应，阈值方式由调用方派生）；
    - 键值合法但类型随意的（如 block_size="256"）先尝试规范化再校验。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _ex1_param("params 须为对象（键值对）")

    out: Dict[str, Any] = {}
    # —— index（REQ-07）——
    if "index" in raw and raw["index"] is not None:
        idx = str(raw["index"]).strip().upper()
        if idx not in VALID_INDEXES:
            raise _ex1_param(f"index 须为 {'/'.join(VALID_INDEXES)}，收到 {raw['index']!r}")
        out["index"] = idx
    # —— method（REQ-11）——
    if "method" in raw and raw["method"] is not None:
        method = str(raw["method"]).strip().lower()
        if method not in VALID_CHANGE_METHODS:
            raise _ex1_param(f"method 须为 {'/'.join(VALID_CHANGE_METHODS)}，收到 {raw['method']!r}")
        out["method"] = method
    # —— block_size（REQ-06）——
    if "block_size" in raw and raw["block_size"] is not None:
        try:
            block = int(raw["block_size"])
        except (TypeError, ValueError) as exc:
            raise _ex1_param(
                f"block_size 须为 {'/'.join(str(b) for b in BLOCK_SIZE_OPTIONS)}，"
                f"收到 {raw['block_size']!r}"
            ) from exc
        if block not in BLOCK_SIZE_OPTIONS:
            raise _ex1_param(
                f"block_size 须为 {'/'.join(str(b) for b in BLOCK_SIZE_OPTIONS)}，收到 {block!r}"
            )
        out["block_size"] = block
    # —— threshold（REQ-07；null = Otsu 自适应）——
    if "threshold" in raw and raw["threshold"] is not None:
        try:
            thr = float(raw["threshold"])
        except (TypeError, ValueError) as exc:
            raise _ex1_param(f"threshold 须为数字，收到 {raw['threshold']!r}") from exc
        lo, hi = MANUAL_THRESHOLD_RANGE
        if not (math.isfinite(thr) and lo < thr < hi):
            raise _ex1_param(f"threshold 越界：需满足 {lo} < T < {hi}，收到 {thr!r}")
        out["threshold"] = thr
    elif "threshold" in raw:
        out["threshold"] = None  # 显式 null：Otsu 自适应口径

    # 兜底防线：只保留白名单键（前述分支已逐一过滤，此处再收敛一次语义）
    return {k: v for k, v in out.items() if k in TEMPLATE_PARAM_KEYS}


def _require_user(user_id: Optional[str]) -> str:
    """模板接口全部 JWT 保护：取不到登录用户 → AUTH-01(401)。"""
    if not user_id:
        raise ApiError("AUTH-01")
    return user_id


def create_template(user_id: Optional[str], name: Any, params: Any) -> dict:
    """POST /api/v1/templates：白名单过滤 → 落库；同用户重名 → TMPL-01(409)。

    ``name`` 必填（去除首尾空白后非空、长度 ≤ TEMPLATE_NAME_MAX），缺失/非法 → EX12。
    """
    try:
        uid = _require_user(user_id)
        clean_name = str(name or "").strip()
        if not clean_name or len(clean_name) > TEMPLATE_NAME_MAX:
            raise ApiError("EX12",
                           detail=f"name 必填且长度 1..{TEMPLATE_NAME_MAX}：{name!r}")
        clean_params = normalize_params(params)
        row = template_repo.insert_template(
            str(uuid.uuid4()), uid, clean_name, clean_params,
        )
        if row is None:  # uq_detect_template_user_name 命中
            raise ApiError("TMPL-01", detail=f"模板名已存在：{clean_name!r}")
        return _vo(row)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一归一为 EX*/SYS-*
        raise as_api_error(exc) from exc


def list_templates(user_id: Optional[str]) -> dict:
    """GET /api/v1/templates：本人模板列表，按 created_at 倒序。"""
    try:
        uid = _require_user(user_id)
        rows = template_repo.list_templates(uid)
        return {"total": len(rows), "items": [_vo(r) for r in rows]}
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise as_api_error(exc) from exc


def delete_template(user_id: Optional[str], template_id: str) -> dict:
    """DELETE /api/v1/templates/{id}：仅本人可删。

    模板不存在或属他人一律 TMPL-02(404)——不区分两种情形，不泄漏存在性。
    """
    try:
        uid = _require_user(user_id)
        ok = template_repo.delete_template(str(template_id), uid)
        if not ok:
            raise ApiError("TMPL-02", detail=f"模板不存在或无权访问：{template_id!r}")
        return {"id": str(template_id), "deleted": True}
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise as_api_error(exc) from exc


def _vo(row: dict) -> dict:
    """DB 行 → 模板 VO（params 为白名单过滤后的口径参数）。"""
    created = row.get("created_at")
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "name": row["name"],
        "params": dict(row.get("params") or {}),
        "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created),
    }

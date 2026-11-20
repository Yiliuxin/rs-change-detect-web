# -*- coding: utf-8 -*-
"""平台总览仪表盘编排（批次 H / REQ-13 扩展）。

路由层保持薄：本模块把 ``stats_repo`` 的明细行汇总成响应 VO，负责
  - ``by_index`` 至少含 NDVI / NDWI 两键（可为 0），其余实际出现的指数一并列出；
  - ``by_method`` 取 ``params_snapshot.method``，缺省按 ``"otsu"`` 口径与历史面板一致；
  - 面积只汇总「三类齐全」的 run（change_result 三类 1/2/3 全有），
    无行或三类不全的 run 计入 ``skipped`` 且不补算、不进入面积合计；
  - ``recent`` 取最近 10 条（列表已按 aoi.created_at 倒序），缺统计者面积如实记 0；
  - 面积统一 ha、保留两位，口径与 ``change_result.area_ha`` / ``/runs.total_area_ha`` 一致。

不读 experiments.json、不触发任何重算；异常统一经 ``as_api_error`` 归一为
``{code,message,run_id}``。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.api.errors import as_api_error
from app.repository import stats_repo
from app.service.cog_source import parse_cog_path

__all__ = ["overview", "RECENT_LIMIT", "INDEX_KEYS"]

# 契约：recent 固定最近 10 条；by_index 至少出现的键
RECENT_LIMIT: int = 10
INDEX_KEYS: tuple = ("NDVI", "NDWI")


def _round2(value: Any) -> float:
    """NUMERIC / None / 字符串统一成两位小数的 float。"""
    try:
        return round(float(value or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _pair_name(cog_path: Optional[str]) -> str:
    """与 ``run_service._pair_name`` 同口径：由 t1 COG 路径派生可读地名。"""
    if not cog_path:
        return ""
    try:
        name = parse_cog_path(str(cog_path))[2]
    except Exception:  # noqa: BLE001 - 命名派生失败不阻断总览
        return ""
    return name[5:] if name.startswith("pair_") else name


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _date(value: Any) -> str:
    """recent 的 date：优先 t2 成像日期，回退 run 创建日期，统一 YYYY-MM-DD。"""
    if value is None:
        return ""
    return str(value)[:10]


def _threshold(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _recent_item(row: dict, index: str, complete: bool,
                 gain: float, loss: float, stable: float) -> Dict[str, Any]:
    rid = str(row.get("run_id") or "")
    return {
        "run_id": rid[:8],
        "aoi_name": _pair_name(row.get("t1_source")) or f"AOI-{rid[:8]}",
        "index": index,
        "date": _date(row.get("t2_date") or row.get("created_at")),
        "threshold": _threshold(row.get("threshold")),
        "gain_ha": _round2(gain) if complete else 0.0,
        "loss_ha": _round2(loss) if complete else 0.0,
        "stable_ha": _round2(stable) if complete else 0.0,
    }


def overview(user_id: Optional[str] = None) -> Dict[str, Any]:
    """GET /api/v1/stats/overview：按人汇总 run 数、面积与最近记录（REQ-13 扩展）。

    空数据（新注册用户/无 run）返回零值结构：by_index 含 NDVI/NDWI=0、
    by_method={}、面积全 0、last_run_at=None、recent=[]，不报错。
    """
    try:
        rows: List[dict] = stats_repo.overview_rows(user_id)

        by_index: Dict[str, int] = {key: 0 for key in INDEX_KEYS}
        by_method: Dict[str, int] = {}
        gain_total = loss_total = stable_total = 0.0
        skipped = 0
        recent: List[Dict[str, Any]] = []

        for row in rows:
            index = str(row.get("index") or "NDVI").upper()
            by_index[index] = by_index.get(index, 0) + 1
            method = str(row.get("method") or "otsu")
            by_method[method] = by_method.get(method, 0) + 1

            # 三类齐全才进入面积合计；缺行 / 三类不全 → skipped，不补算
            complete = int(row.get("class_count") or 0) == 3
            gain = float(row.get("gain_ha") or 0.0)
            loss = float(row.get("loss_ha") or 0.0)
            stable = float(row.get("stable_ha") or 0.0)
            if complete:
                gain_total += gain
                loss_total += loss
                stable_total += stable
            else:
                skipped += 1

            if len(recent) < RECENT_LIMIT:
                recent.append(_recent_item(row, index, complete, gain, loss, stable))

        # 先圆整分量再相加，保证 total == 展示的 gain+loss+stable、changed == gain+loss
        gain_ha = _round2(gain_total)
        loss_ha = _round2(loss_total)
        stable_ha = _round2(stable_total)
        return {
            "total_runs": len(rows),
            "by_index": by_index,
            "by_method": by_method,
            "area": {"gain_ha": gain_ha, "loss_ha": loss_ha, "stable_ha": stable_ha},
            "total_area_ha": _round2(gain_ha + loss_ha + stable_ha),
            "changed_area_ha": _round2(gain_ha + loss_ha),
            "skipped": skipped,
            "last_run_at": _iso(rows[0].get("created_at")) if rows else None,
            "recent": recent,
        }
    except Exception as exc:  # noqa: BLE001 - 统一归一为 EX*/SYS-*（此处多为 SYS-04）
        raise as_api_error(exc) from exc

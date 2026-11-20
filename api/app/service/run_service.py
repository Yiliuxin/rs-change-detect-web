# -*- coding: utf-8 -*-
"""历史运行记录编排（REQ-13 / REQ-14 / REQ-08）。

路由层保持薄：本模块负责分页参数校验（非法 → EX12）、联表行转 VO、run 不存在
→ EX2(404)、枚举落盘产物生成同源下载 URL，以及面积统计 CSV 文本。

面积口径优先取 ``change_result`` 表的真实入库值，未跑检测的 run 回退参数快照；
CSV 始终给出增强 / 退化 / 稳定三行（缺数据记 0，不伪造）。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

import rasterio

from app.algorithm.change_detector import CHANGE_LABELS_ZH
from app.algorithm.geotransform import raster_meta
from app.api.errors import ApiError, as_api_error
from app.config.constants import VALID_INDEXES
from app.repository import run_repo, task_repo
from app.service.cog_source import parse_cog_path
from app.service.common import RUNS_ROOT, artifact_media_type, artifact_url

logger = logging.getLogger(__name__)

# 裁切栅格候选：主名 + 按指数归档名（与 ARTIFACT_MEDIA_TYPES / artifact_manifest 同源）。
# 旧 run 若没有这些产物，raster 如实返回 null；_cog/ 下的是原始未裁切 COG，不作为来源。
_RASTER_ARTIFACTS: tuple = (
    "change.tif", "change_ndvi.tif", "change_ndwi.tif",
    "change_ndbi.tif", "change_ndmi.tif",
    "change_reg.tif", "change_ndvi_reg.tif", "change_ndwi_reg.tif",
    "change_ndbi_reg.tif", "change_ndmi_reg.tif",
)

__all__ = [
    "DEFAULT_PAGE_LIMIT", "MAX_PAGE_LIMIT", "CSV_BOM", "CSV_HEADER",
    "list_runs", "detail", "area_stats", "area_stats_csv",
]

# 分页缺省与上限（REQ-14 历史列表）
DEFAULT_PAGE_LIMIT: int = 20
MAX_PAGE_LIMIT: int = 100
# CSV 契约：UTF-8 BOM 供 Excel 正确识别中文表头
CSV_BOM: str = "\ufeff"
CSV_HEADER: str = "类别,像元数,面积_公顷"


def _pair_name(cog_path: Optional[str]) -> str:
    """由 ``minio://scenes/pair_<name>/t1/`` 派生可读 AOI 名称（aoi 表无 name 列）。"""
    if not cog_path:
        return ""
    try:
        name = parse_cog_path(str(cog_path))[2]
    except Exception:  # noqa: BLE001 - 命名派生失败不阻断列表
        return ""
    return name[5:] if name.startswith("pair_") else name


def _date(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:10] if hasattr(value, "isoformat") or isinstance(value, str) else str(value)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _summary(row: dict) -> Dict[str, Any]:
    """联表行 → 列表/详情共用的 run 摘要 VO（REQ-14）。"""
    snap: Dict[str, Any] = dict(row.get("params_snapshot") or {})
    paths: Dict[str, Any] = dict(snap.get("scene_cog_paths") or {})
    t1_source = row.get("t1_source") or paths.get("t1") or ""
    t2_source = row.get("t2_source") or paths.get("t2") or ""

    bbox = None
    if all(row.get(k) is not None for k in ("minx", "miny", "maxx", "maxy")):
        bbox = [float(row[k]) for k in ("minx", "miny", "maxx", "maxy")]
    elif snap.get("aoi_wgs84"):
        bbox = [float(v) for v in snap["aoi_wgs84"]]

    threshold = snap.get("threshold", snap.get("otsu_threshold"))
    return {
        "run_id": row["run_id"],
        "aoi_id": row["aoi_id"],
        "aoi_name": _pair_name(t1_source) or f"AOI-{str(row['aoi_id'])[:8]}",
        "bbox": bbox,
        "t1": _date(row.get("t1_date") or snap.get("t1")),
        "t2": _date(row.get("t2_date") or snap.get("t2")),
        "t1_source": t1_source,
        "t2_source": t2_source,
        "sensor": row.get("t1_sensor") or snap.get("sensor") or "",
        "created_at": _iso(row.get("created_at")),
        "threshold": float(threshold) if threshold is not None else None,
        "threshold_source": snap.get("threshold_source"),
        "index": snap.get("index") or "NDVI",
        "status": row.get("status"),
        "total_area_ha": float(row.get("total_area_ha") or 0.0),
        "total_pixel_count": int(row.get("total_pixel_count") or 0),
    }


def _artifacts(run_id: str) -> List[dict]:
    """枚举 ``runs/<run_id>/`` 下落盘产物，复用 ``artifact_url`` 生成同源下载 URL。

    只暴露 ``ARTIFACT_MEDIA_TYPES`` 白名单内的文件（含带 index 后缀归档与
    t1/t2_composite.png）；``_cog`` 缓存子目录与 ``run_report.json`` 不算产物。
    """
    try:
        canonical = str(uuid.UUID(run_id))
    except (ValueError, AttributeError, TypeError):
        return []
    out = RUNS_ROOT / canonical
    if not out.is_dir():
        return []
    return [
        {
            "name": p.name,
            "url": artifact_url(canonical, p.name),
            "size": int(p.stat().st_size),
        }
        for p in sorted(out.iterdir())
        if p.is_file() and artifact_media_type(p.name, canonical) is not None
    ]


def _raster_meta(run_id: str) -> Optional[dict]:
    """读取该 run 裁切栅格的 profile/geotransform（REQ-14 图上点选用）。

    只认 ``runs/<规范化 run_id>/`` 下已落盘的结果 GeoTIFF；文件缺失、损坏或
    profile 无效一律返回 ``None``（详情不报错，前端按无底图元信息处理）。
    """
    try:
        canonical = str(uuid.UUID(run_id))
    except (ValueError, AttributeError, TypeError):
        return None
    out = RUNS_ROOT / canonical
    for name in _RASTER_ARTIFACTS:
        path = out / name
        if not path.is_file():
            continue
        try:
            with rasterio.open(str(path)) as ds:
                meta = raster_meta(ds)
        except Exception as exc:  # noqa: BLE001 - 损坏栅格不阻断详情
            logger.warning("run=%s 栅格 %s 读取失败：%s", run_id, name, exc)
            continue
        if meta:
            return meta
    return None


# 批次 D /runs 可选过滤参数边界：q 最长 100 字符；days 为 1..3650 的整数
Q_MAX_LEN: int = 100
DAYS_MAX: int = 3650


def list_runs(limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0,
              user_id: Optional[str] = None,
              q: Optional[str] = None,
              index: Optional[str] = None,
              days: Optional[int] = None) -> dict:
    """GET /api/v1/runs：分页列表，按 created_at 倒序（REQ-14）；user_id 非空时按人隔离。

    批次 D 可选过滤（全部缺省时行为与历史版本完全一致，老前端不受影响）：
      * q：模糊匹配 run_id 前缀与 params_snapshot 里的地名/组名文本；
      * index：指数枚举过滤（NDVI / NDWI / NDBI / NDMI，非法 → EX12）；
      * days：最近 N 天（按 run 创建时间 aoi.created_at，1..3650，非法 → EX12）。
    total 按过滤后计数；分页语义不变（q/index/days 变化时前端归零 offset）。
    """
    try:
        try:
            lim, off = int(limit), int(offset)
        except (TypeError, ValueError) as exc:
            raise ApiError("EX12", detail=f"分页参数需为整数：limit={limit!r}, offset={offset!r}") from exc
        if lim < 1 or lim > MAX_PAGE_LIMIT or off < 0:
            raise ApiError(
                "EX12",
                detail=f"分页参数越界：limit 需在 1..{MAX_PAGE_LIMIT}，offset 需 ≥ 0",
            )
        # q：可选字符串，去首尾空白；空串视同未传（与缺省行为一致）
        clean_q = str(q).strip() if q is not None else None
        if clean_q == "":
            clean_q = None
        if clean_q and len(clean_q) > Q_MAX_LEN:
            raise ApiError("EX12", detail=f"q 过长：最长 {Q_MAX_LEN} 字符，收到 {len(clean_q)}")
        # index：归一化大写后校验枚举（与 /result/change 的 index 校验同源）
        clean_index: Optional[str] = None
        if index is not None:
            clean_index = str(index).strip().upper()
            if clean_index not in VALID_INDEXES:
                raise ApiError(
                    "EX12",
                    detail=f"index 非法：{index!r}，仅支持 {list(VALID_INDEXES)}",
                )
        # days：可选整数，1..DAYS_MAX，非法 → EX12
        clean_days: Optional[int] = None
        if days is not None:
            try:
                clean_days = int(days)
            except (TypeError, ValueError) as exc:
                raise ApiError("EX12", detail=f"days 需为整数：{days!r}") from exc
            if clean_days < 1 or clean_days > DAYS_MAX:
                raise ApiError("EX12", detail=f"days 越界：需在 1..{DAYS_MAX}，收到 {clean_days}")
        total, rows = run_repo.list_runs(lim, off, user_id=user_id,
                                         q=clean_q, index=clean_index, days=clean_days)
        return {
            "total": int(total),
            "limit": lim,
            "offset": off,
            "items": [_summary(r) for r in rows],
        }
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一归一为 EX*/SYS-*
        raise as_api_error(exc) from exc


def detail(run_id: str, user_id: Optional[str] = None) -> dict:
    """GET /api/v1/runs/{run_id}：摘要 + params_snapshot 全量 + 全部产物 URL + raster。

    raster 由裁切栅格 profile 读取；产物缺失/损坏时为 null（不报错、不伪造）。
    """
    try:
        row = run_repo.get_run(run_id, user_id=user_id)
        if not row:
            raise ApiError("EX2", run_id=run_id,
                           detail=f"run_id 无对应 task：{run_id}")
        item = _summary(row)
        item["params_snapshot"] = dict(row.get("params_snapshot") or {})
        item["artifacts"] = _artifacts(run_id)
        item["raster"] = _raster_meta(run_id)
        return item
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise as_api_error(exc, run_id=run_id) from exc


def area_stats(run_id: str) -> List[dict]:
    """三类面积统计（增强 / 退化 / 稳定），run 不存在 → EX2 404。"""
    try:
        task = task_repo.get_task(run_id)
        if not task:
            raise ApiError("EX2", run_id=run_id,
                           detail=f"run_id 无对应 task：{run_id}")
        rows = run_repo.stats_for_run(run_id)
        if not rows:
            snap = task.get("params_snapshot") or {}
            rows = snap.get("stats") or []
        by_class = {int(r["change_class"]): r for r in rows}
        return [
            {
                "label": CHANGE_LABELS_ZH[code],
                "pixel_count": int(by_class.get(code, {}).get("pixel_count", 0)),
                "area_ha": float(by_class.get(code, {}).get("area_ha", 0.0)),
            }
            for code in (1, 2, 3)
        ]
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise as_api_error(exc, run_id=run_id) from exc


def area_stats_csv(run_id: str) -> str:
    """CSV 文本（不含 BOM，由路由层加 ``\\ufeff``）：表头 + 三行数据。"""
    lines = [CSV_HEADER]
    for item in area_stats(run_id):
        lines.append(f'{item["label"]},{item["pixel_count"]},{item["area_ha"]:.4f}')
    return "\r\n".join(lines) + "\r\n"

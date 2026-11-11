# -*- coding: utf-8 -*-
"""B2 鼠标拾取探针：单像元返回双时相指数 / Δ / 三类 / 强度 / 地物类别。

只读、只连 task 表取参数快照；产物用 ``rasterio.warp.transform`` 把 WGS84
经纬度换算到栅格 CRS，再反算行列并用 ``Window(col, row, 1, 1)`` 只读 1 个像元。
越界 / 产物缺失一律 200 + 字段 None，绝不 500、绝不弹窗打断前端打点模式。
归属校验由路由先调用 ``ensure_run_owner`` 完成（他人 run → AUTH-03）。
"""
from __future__ import annotations

import math
from typing import Dict

import rasterio
from rasterio.warp import transform as warp_transform
from rasterio.windows import Window

from app.algorithm.change_detector import CHANGE_LABELS_ZH
from app.algorithm.intensity import intensity_grade
from app.algorithm.landcover import classify_landcover_scalar
from app.api.errors import ApiError
from app.config.constants import (
    INDEX_RASTER_T1, INDEX_RASTER_T2, INTENSITY_LABELS_ZH,
    LANDCOVER_LABELS_ZH, METHOD_OTSU, METHOD_REGRESSION,
)
from app.repository import task_repo
from app.service.common import (
    index_raster_names, regression_artifact_names, run_dir,
)

__all__ = ["probe"]


def _empty(reason: str) -> Dict[str, object]:
    """越界 / 缺产物：所有测量字段为 None，in_bounds=False，仍返回 200。"""
    return {
        "in_bounds": False,
        "t1_index": None,
        "t2_index": None,
        "delta": None,
        "change_class": None,
        "intensity": None,
        "landcover_t1": None,
        "landcover_t2": None,
        "reason": str(reason),
    }


def _finite_or_none(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _band(raw, i: int) -> float:
    """取 1×1 Window 的第 i+1 波段；缺波段返回 NaN。"""
    if getattr(raw, "ndim", 0) == 3 and raw.shape[0] > i:
        return float(raw[i, 0, 0])
    return float("nan")


def probe(run_id: str, lng: float, lat: float) -> Dict[str, object]:
    """按 run 的参数快照定位产物，读取单个像元并给出全部探针字段。"""
    task = task_repo.get_task(run_id)
    if not task:
        # 认证关闭（AUTH_DISABLED）时 ensure_run_owner 直通，此处仍须 EX2
        raise ApiError("EX2", run_id=run_id,
                       detail=f"run_id 无对应 task：{run_id}")
    snap = dict(task.get("params_snapshot") or {})
    method = str(snap.get("method") or METHOD_OTSU).strip().lower()
    index = str(snap.get("index") or "NDVI").strip().upper()

    names = index_raster_names(method)
    out = run_dir(run_id)
    p1 = out / names[INDEX_RASTER_T1]
    p2 = out / names[INDEX_RASTER_T2]
    if not (p1.is_file() and p2.is_file()):
        return _empty("双时相指数栅格产物缺失（index_t1/index_t2.tif）")

    try:
        with rasterio.open(str(p1)) as ds:
            # 先经纬度 → 栅格 CRS，再反算行列；只读 1 个像元
            xs, ys = warp_transform("EPSG:4326", ds.crs,
                                    [float(lng)], [float(lat)])
            x, y = float(xs[0]), float(ys[0])
            if not (math.isfinite(x) and math.isfinite(y)):
                return _empty("坐标无法投影到栅格 CRS")
            row, col = ds.index(x, y)
            if not (0 <= row < ds.height and 0 <= col < ds.width):
                return _empty("坐标位于栅格范围之外")
            window = Window(col, row, 1, 1)
            raw1 = ds.read(window=window)  # (2, 1, 1)
        with rasterio.open(str(p2)) as ds2:
            raw2 = ds2.read(window=window)
    except Exception:  # noqa: BLE001 - 产物损坏 / CRS 异常同样按缺数据处理
        return _empty("指数栅格读取失败")

    t1, aux1 = _band(raw1, 0), _band(raw1, 1)
    t2, aux2 = _band(raw2, 0), _band(raw2, 1)
    # band1 = 主检测指数；band2 = 辅助指数（NDWI 场景辅助指数即 NDVI）
    if index == "NDWI":
        ndvi1, ndwi1 = aux1, t1
        ndvi2, ndwi2 = aux2, t2
    else:
        ndvi1, ndwi1 = t1, aux1
        ndvi2, ndwi2 = t2, aux2

    thr = _finite_or_none(snap.get("threshold"))
    thr = 0.0 if thr is None else abs(float(thr))
    if method == METHOD_REGRESSION:
        a = _finite_or_none(snap.get("a")) or 0.0
        b = _finite_or_none(snap.get("b")) or 0.0
        delta = t2 - (float(a) * t1 + float(b))  # 回归残差，与 A1 同口径
    else:
        delta = t2 - t1

    # 三类图同窗口 1 像元；缺产物时 change_class=None，其余字段照常给出
    change_name = (regression_artifact_names()["geotiff"]
                   if method == METHOD_REGRESSION else "change.tif")
    cp = out / change_name
    change_class = None
    if cp.is_file():
        try:
            with rasterio.open(str(cp)) as cds:
                value = int(cds.read(1, window=window)[0, 0])
                change_class = value if value in (0, 1, 2, 3) else 0
        except Exception:  # noqa: BLE001
            change_class = None

    finite = all(math.isfinite(v) for v in (t1, t2, delta))
    grade = (intensity_grade(delta, thr, change_class=change_class)
             if finite else 0)
    lc1 = classify_landcover_scalar(ndvi1, ndwi1)
    lc2 = classify_landcover_scalar(ndvi2, ndwi2)

    return {
        "run_id": str(run_id),
        "lng": float(lng),
        "lat": float(lat),
        "index": index,
        "method": method,
        "threshold": float(thr),
        "in_bounds": True,
        "t1_index": _finite_or_none(t1),
        "t2_index": _finite_or_none(t2),
        "delta": _finite_or_none(delta),
        "change_class": change_class,
        "change_label": CHANGE_LABELS_ZH.get(change_class, "无效"),
        "intensity": int(grade),
        "intensity_label": INTENSITY_LABELS_ZH.get(int(grade), "无效"),
        "landcover_t1": int(lc1),
        "landcover_t2": int(lc2),
        "landcover_t1_label": LANDCOVER_LABELS_ZH.get(int(lc1), "无效"),
        "landcover_t2_label": LANDCOVER_LABELS_ZH.get(int(lc2), "无效"),
        "reason": "",
    }

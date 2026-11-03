# -*- coding: utf-8 -*-
"""样本入库与精度评价（REQ-09 / EX10）。

要点：
  - ``SampleItem.x/y`` 语义为像元列/行；用已生成 change.tif 的 transform +
    ``rasterio.warp.transform`` 把像元转 WGS84 存 geom；若传入明显是经纬度则直接存并反算像元；
  - 统计该 run_id **累计**每类样本数，任一类 < MIN_SAMPLES_PER_CLASS → EX10；
  - y_pred = change.tif 该像元三类编码，y_true = ref_class → 1/2/3；
  - 调 ``accuracy.accuracy_report`` 得 3×3 混淆矩阵 / OA / Kappa / note。
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform

from app.algorithm.accuracy import SampleInsufficientError, accuracy_report
from app.api.errors import ApiError, as_api_error
from app.config.constants import MIN_SAMPLES_PER_CLASS
from app.repository import sample_repo, task_repo
from app.service.common import run_dir

# 中文参考类别 → 三类编码（与 change_detector.CHANGE_CODES 一致）
REF_CLASS_CODE = {"增强": 1, "退化": 2, "稳定": 3}
VALID_CLASSES = ("增强", "退化", "稳定")

__all__ = ["REF_CLASS_CODE", "VALID_CLASSES", "open_change", "build_xy",
           "accuracy_for_run", "submit"]


def open_change(run_id: str) -> rasterio.DatasetReader:
    """打开该 run 的 change.tif（结果三类编码栅格）；不存在 → SYS-01。"""
    path: Path = run_dir(run_id) / "change.tif"
    if not path.exists():
        raise ApiError("SYS-01", run_id=run_id,
                       detail="change.tif 不存在，请先执行 /result/change")
    return rasterio.open(str(path))


def _is_lonlat(x: float, y: float, width: int, height: int) -> bool:
    """判定输入是否"明显是经纬度"（否则按像元列/行解释）。"""
    if not (-180.0 <= x <= 180.0 and -90.0 <= y <= 90.0):
        return False
    looks_pixel = (float(x).is_integer() and float(y).is_integer()
                   and 0 <= x < width and 0 <= y < height)
    return not looks_pixel


def _locate(ds: rasterio.DatasetReader, x: float, y: float) -> Tuple[int, int, float, float]:
    """把 (x,y) 归一为 (row, col, lon, lat)。"""
    height, width = ds.height, ds.width
    if _is_lonlat(x, y, width, height):
        lon, lat = float(x), float(y)
        xs, ys = warp_transform("EPSG:4326", ds.crs, [lon], [lat])
        row, col = ds.index(xs[0], ys[0])
    else:
        col, row = int(round(x)), int(round(y))
        px, py = ds.xy(row, col)
        lons, lats = warp_transform(ds.crs, "EPSG:4326", [px], [py])
        lon, lat = float(lons[0]), float(lats[0])
    return int(row), int(col), lon, lat


def build_xy(run_id: str, ds: rasterio.DatasetReader) -> Tuple[np.ndarray, np.ndarray]:
    """由累计样本 + change.tif 组装 (y_true, y_pred)（过滤无效像元）。"""
    change = ds.read(1)
    h, w = change.shape
    y_true: List[int] = []
    y_pred: List[int] = []
    for s in sample_repo.list_samples(run_id):
        row, col = s["px_y"], s["px_x"]
        if not (0 <= row < h and 0 <= col < w):
            continue
        pred = int(change[row, col])
        code = REF_CLASS_CODE.get(s["ref_class"])
        if pred in (1, 2, 3) and code is not None:
            y_true.append(code)
            y_pred.append(pred)
    return np.asarray(y_true, dtype=np.int64), np.asarray(y_pred, dtype=np.int64)


def accuracy_for_run(run_id: str, ds: rasterio.DatasetReader,
                     params_snapshot: Optional[dict] = None) -> Optional[dict]:
    """供 /report/export 复用：样本充足时返回精度报告 dict，否则 None（不伪造）。"""
    y_true, y_pred = build_xy(run_id, ds)
    if y_true.size == 0:
        return None
    try:
        return accuracy_report(y_true, y_pred, run_id=run_id,
                               params_snapshot=params_snapshot)
    except SampleInsufficientError:
        return None


def submit(run_id: str, samples: List[dict]) -> dict:
    """样本提交与精度评价（REQ-09）。"""
    try:
        if not task_repo.get_task(run_id):
            raise ApiError("EX2", run_id=run_id,
                           detail=f"run_id 无对应 task：{run_id}")

        # 参考类别合法性（DB CHECK 亦是三类，此处提前给出明确 400）
        for s in samples:
            if s.get("ref_class") not in VALID_CLASSES:
                raise ApiError("EX10", run_id=run_id, http_status=400,
                               detail=f"非法 ref_class：{s.get('ref_class')!r}")

        with open_change(run_id) as ds:
            rows = []
            for s in samples:
                row, col, lon, lat = _locate(ds, float(s["x"]), float(s["y"]))
                rows.append({
                    "lon": lon, "lat": lat,
                    "px_x": max(0, col), "px_y": max(0, row),
                    "ref_class": s["ref_class"], "label": s["ref_class"],
                })
            sample_repo.insert_samples(run_id, rows)

            # 累计每类样本数，任一类 < MIN_SAMPLES_PER_CLASS → EX10（禁用计算精度）
            counts = sample_repo.count_by_class(run_id)
            insufficient = [c for c in VALID_CLASSES
                            if counts.get(c, 0) < MIN_SAMPLES_PER_CLASS]
            if insufficient:
                raise ApiError("EX10", run_id=run_id,
                               detail=f"{insufficient} 各类计数={counts}")

            y_true, y_pred = build_xy(run_id, ds)
            snapshot = (task_repo.get_task(run_id) or {}).get("params_snapshot") or {}
            acc = accuracy_report(y_true, y_pred, run_id=run_id,
                                  params_snapshot=snapshot)
            return {
                "confusion_matrix": acc["confusion_matrix"],
                "overall_accuracy": acc["overall_accuracy"],
                "kappa": acc["kappa"],
                "note": acc["note"],
            }
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise as_api_error(exc, run_id=run_id) from exc

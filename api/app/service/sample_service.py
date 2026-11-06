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

import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform

from app.algorithm.accuracy import SampleInsufficientError, accuracy_report
from app.algorithm.change_detector import CHANGE_LABELS_ZH
from app.algorithm.geotransform import BBox, lonlat_to_pixel, pixel_to_lonlat
from app.algorithm.stratified_sampler import stratified_pixels
from app.api.errors import ApiError, as_api_error
from app.config.constants import (
    AUTO_SAMPLE_PER_CLASS_MAX, METHOD_OTSU, METHOD_REGRESSION,
    MIN_SAMPLES_PER_CLASS,
)
from app.repository import aoi_repo, sample_repo, task_repo
from app.service.common import regression_artifact_names, run_dir

# 中文参考类别 → 三类编码（与 change_detector.CHANGE_CODES 一致）
REF_CLASS_CODE = {"增强": 1, "退化": 2, "稳定": 3}
VALID_CLASSES = ("增强", "退化", "稳定")

__all__ = ["REF_CLASS_CODE", "VALID_CLASSES", "open_change", "build_xy",
           "accuracy_for_run", "submit", "from_lnglat", "auto_sample"]


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


# --------------------------------------------------------------------------- #
# 两条样本提交路径（像元点选 / 经纬度点选）共用的校验与入库构件（REQ-09）
# --------------------------------------------------------------------------- #
def _require_task(run_id: str) -> dict:
    """run 存在性校验：task 不存在 → EX2 404。"""
    task = task_repo.get_task(run_id)
    if not task:
        raise ApiError("EX2", run_id=run_id,
                       detail=f"run_id 无对应 task：{run_id}")
    return task


def _validate_ref_classes(run_id: str, samples: List[dict]) -> None:
    """参考类别合法性（DB CHECK 亦是三类，此处提前给出明确 400）。"""
    for s in samples:
        if s.get("ref_class") not in VALID_CLASSES:
            raise ApiError("EX10", run_id=run_id, http_status=400,
                           detail=f"非法 ref_class：{s.get('ref_class')!r}")


def _aoi_bbox(task: dict) -> Optional[Tuple[float, float, float, float]]:
    """AOI WGS84 外接矩形：优先参数快照，回退 aoi 表几何（取不到返回 None）。"""
    seq = (task.get("params_snapshot") or {}).get("aoi_wgs84")
    if seq:
        try:
            return BBox.from_sequence(list(seq)).as_tuple()
        except (TypeError, ValueError):
            pass
    return aoi_repo.find_bbox(task["aoi_id"])


def _locate_lonlat(ds: rasterio.DatasetReader, run_id: str,
                   lng: float, lat: float,
                   bbox: Optional[Tuple[float, float, float, float]]) -> Tuple[int, int, float, float]:
    """经纬度 → (row, col) 并复算越界（AOI 外 → 400 EX12）。

    用 ``geotransform.lonlat_to_pixel`` 反算像元；像元必须落在裁切栅格范围内。
    再把像元中心用 ``pixel_to_lonlat`` 回投 WGS84，与 AOI 外接矩形复算一次
    （容差 1 个像元），避免仅凭前端坐标或仅凭栅格窗口（窗口按 AOI 外扩）漏判。
    """
    if not (math.isfinite(lng) and math.isfinite(lat)
            and -180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
        raise ApiError("EX12", run_id=run_id,
                       detail=f"经纬度非法：lng={lng!r}, lat={lat!r}")
    row, col = lonlat_to_pixel(ds, lng, lat)
    if not (0 <= row < ds.height and 0 <= col < ds.width):
        raise ApiError("EX12", run_id=run_id,
                       detail=f"点选位置在 AOI 外：像元 (col={col}, row={row}) "
                              f"超出裁切栅格 {ds.width}×{ds.height}")
    if bbox:
        minx, miny, maxx, maxy = bbox
        tol_x = abs(maxx - minx) / max(ds.width, 1)
        tol_y = abs(maxy - miny) / max(ds.height, 1)
        px_lng, px_lat = pixel_to_lonlat(ds, row, col)
        for x, y in ((lng, lat), (px_lng, px_lat)):
            if not (minx - tol_x <= x <= maxx + tol_x
                    and miny - tol_y <= y <= maxy + tol_y):
                raise ApiError("EX12", run_id=run_id,
                               detail=f"点选位置在 AOI 外：({x:.6f}, {y:.6f}) "
                                      f"不在 {bbox} 内")
    return int(row), int(col), lng, lat


def _sample_rows(ds: rasterio.DatasetReader, run_id: str, samples: List[dict],
                 *, lonlat: bool = False,
                 bbox: Optional[Tuple[float, float, float, float]] = None) -> List[Dict]:
    """样本请求 → 待入库行；像元/经纬度两条路径共用同一字段口径与 label 缺省。"""
    rows: List[Dict] = []
    for s in samples:
        ref = s["ref_class"]
        if lonlat:
            row, col, lon, lat = _locate_lonlat(
                ds, run_id, float(s["lng"]), float(s["lat"]), bbox)
        else:
            row, col, lon, lat = _locate(ds, float(s["x"]), float(s["y"]))
            # 既有 /accuracy/sample 口径保持不变（负值按左上边界截断）
            col, row = max(0, col), max(0, row)
        rows.append({
            "lon": lon, "lat": lat,
            "px_x": col, "px_y": row,
            "ref_class": ref, "label": s.get("label") or ref,
        })
    return rows


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
    """样本提交与精度评价（REQ-09）；校验/定位/入库与 ``from_lnglat`` 共用构件。"""
    try:
        _require_task(run_id)
        _validate_ref_classes(run_id, samples)

        with open_change(run_id) as ds:
            rows = _sample_rows(ds, run_id, samples)
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


def from_lnglat(run_id: str, lng: float, lat: float,
                ref_class: str, label: Optional[str] = None) -> dict:
    """POST /api/v1/sample/from_lnglat（REQ-09）：经纬度点选 → 像元坐标 → 入库。

    只落库并返回坐标，不触发 /accuracy/sample 的累计精度评价（EX10 计数门槛
    仍归既有接口）；AOI 外 → 400 EX12，非法 ref_class → 400 EX10。
    """
    try:
        task = _require_task(run_id)
        item = {"lng": float(lng), "lat": float(lat),
                "ref_class": ref_class, "label": label}
        _validate_ref_classes(run_id, [item])
        bbox = _aoi_bbox(task)
        with open_change(run_id) as ds:
            rows = _sample_rows(ds, run_id, [item], lonlat=True, bbox=bbox)
            sample_id = sample_repo.insert_one(run_id, rows[0])
        row = rows[0]
        return {
            "sample_id": sample_id,
            "px_x": int(row["px_x"]), "px_y": int(row["px_y"]),
            "ref_class": row["ref_class"],
        }
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise as_api_error(exc, run_id=run_id) from exc


# --------------------------------------------------------------------------- #
# 半自动分层抽样（REQ-09 自动化）：只返回样本点，不写 sample 表
# --------------------------------------------------------------------------- #
def _validate_per_class(per_class, run_id: str) -> int:
    """per_class ∈ (0, AUTO_SAMPLE_PER_CLASS_MAX]；非法 → EX12（既有 400 映射）。"""
    if (isinstance(per_class, bool) or not isinstance(per_class, int)
            or not (0 < per_class <= AUTO_SAMPLE_PER_CLASS_MAX)):
        raise ApiError(
            "EX12", run_id=run_id,
            detail=f"per_class 需为 (0, {AUTO_SAMPLE_PER_CLASS_MAX}] 内整数：{per_class!r}",
        )
    return int(per_class)


def _auto_change_path(run_id: str, task: dict) -> Path:
    """按 params_snapshot.method 选结果栅格（与 report_service._files_for 同口径）。

    regression → ``change_reg.tif``（回归法独立主名）；其余（含老 run 快照无
    method 字段）→ ``change.tif``。``/sample/from_lnglat`` 与 ``/accuracy/sample``
    经 ``open_change`` 固定读 change.tif 的既有行为完全不动。
    """
    snapshot = task.get("params_snapshot") or {}
    method = str(snapshot.get("method") or METHOD_OTSU).strip().lower()
    filename = (regression_artifact_names()["geotiff"]
                if method == METHOD_REGRESSION else "change.tif")
    return run_dir(run_id) / filename


def auto_sample(run_id: str, per_class: int, seed: Optional[int] = None) -> dict:
    """POST /accuracy/auto_sample（REQ-09 自动化）：分层随机抽样，半自动流程。

    读该 run 的变化检测结果栅格，按 1/2/3 三类分层用 ``RandomState(seed)`` 各抽
    ``per_class`` 个像元，像元中心经 ``geotransform.pixel_to_lonlat`` 反算 WGS84
    经纬度。**不写 sample 表**：样本点由前端填入本地列表，用户修正 ref_class 后
    再走既有 POST /accuracy/sample 提交并计算精度。

    run 不存在 → EX2(404)；per_class 非法 → EX12(400)；栅格缺失/不可读 → SYS-01
    （沿用既有 CODE_HTTP 映射，不改错误码表）。某类可用像元不足时
    ``per_class_actual`` 如实记实际数，不报错、不凑数。
    """
    try:
        n_per_class = _validate_per_class(per_class, run_id)
        task = _require_task(run_id)
        path = _auto_change_path(run_id, task)
        if not path.is_file():
            raise ApiError("SYS-01", run_id=run_id,
                           detail=f"{path.name} 不存在，请先执行 /result/change")
        with rasterio.open(str(path)) as ds:
            change = ds.read(1)
            picks, actual = stratified_pixels(change, n_per_class, seed)
            samples = []
            for row, col, code in picks:
                lng, lat = pixel_to_lonlat(ds, row, col)
                samples.append({
                    "lng": float(lng), "lat": float(lat),
                    "ref_class": CHANGE_LABELS_ZH[code],
                })
        return {"run_id": run_id, "samples": samples, "per_class_actual": actual}
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - 栅格读取/换算失败统一 SYS-01
        raise as_api_error(exc, run_id=run_id) from exc

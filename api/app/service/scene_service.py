# -*- coding: utf-8 -*-
"""场景匹配与辐射口径确认（REQ-02/03/04，GET 清单 + POST 契约）。

- GET：按 aoi_id 空间相交（或 scene id / 全部）+ t1/t2 日期返回匹配影像对清单；
  sensor 不一致 → EX8（assert_same_caliber）；基线缺失或 <04.00 → EX9（check_baseline）；
  无匹配 → EX2。
- POST：保持原 SceneMatchResponse 契约，补真实 formula_id / proc_baseline /
  clip_window / valid_ratio（复用 change_service.load_scene 读 COG 窗口与掩膜占比）。
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import List, Optional

from app.algorithm.geotransform import BBox
from app.algorithm.radiometric import assert_same_caliber, check_baseline
from app.api.errors import ApiError, as_api_error
from app.config.constants import BLOCK_SIZE, RADIOMETRIC_FORMULAS, SENSOR_S2
from app.repository import aoi_repo, scene_repo, task_repo
from app.service.change_service import load_scene

__all__ = ["match_get", "match_post"]


def _clip_window(window, profile: dict) -> dict:
    """把 rasterio Window + profile 转成 ClipWindow 字段口径。"""
    crs = profile.get("crs")
    transform = profile.get("transform")
    res = None
    if transform is not None and hasattr(transform, "a"):
        res = abs(float(transform.a))
    elif profile.get("res"):
        res = abs(float(profile["res"][0]))
    return {
        "row_off": int(window.row_off) if window is not None else 0,
        "col_off": int(window.col_off) if window is not None else 0,
        "width": int(window.width) if window is not None else 0,
        "height": int(window.height) if window is not None else 0,
        "crs": str(crs) if crs else None,
        "res": res,
    }


def _scene_item(scene: dict) -> dict:
    """匹配清单单项（日期/云量/tile/基线/sensor/cog_path）。"""
    return {
        "scene_id": scene["scene_id"],
        "sensor": scene["sensor"],
        "acq_date": scene.get("acq_date"),
        "cloud_cover": scene.get("cloud_cover"),
        "tile": scene.get("tile"),
        "proc_baseline": scene.get("proc_baseline"),
        "cog_path": scene["cog_path"],
    }


def _resolve_candidates(aoi_id: Optional[str], scene_t1_id: Optional[str],
                        scene_t2_id: Optional[str]) -> List[dict]:
    """按优先级确定候选场景集：显式 id → aoi 空间相交 → 全部。"""
    if scene_t1_id and scene_t2_id:
        return scene_repo.find_by_ids([scene_t1_id, scene_t2_id])
    if aoi_id:
        geom_json = aoi_repo.geom_geojson(aoi_id)
        if not geom_json:
            return []
        return scene_repo.find_intersecting(geom_json)
    return scene_repo.find_all()


def _filter_by_dates(scenes: List[dict], t1: Optional[date],
                     t2: Optional[date]) -> List[dict]:
    """按 t1/t2 精确日期过滤；都缺省时返回全部候选。"""
    s1, s2 = (str(t1) if t1 else None), (str(t2) if t2 else None)
    if not (s1 or s2):
        return scenes
    return [s for s in scenes
            if (s1 and s["acq_date"] == s1) or (s2 and s["acq_date"] == s2)]


def _validate_caliber(scenes: List[dict], run_id: Optional[str]) -> str:
    """EX8（sensor 不一致）/ EX9（基线缺失或 <04.00）；返回锁定 sensor。"""
    if not scenes:
        raise ApiError("EX2", run_id=run_id)
    sensors = [s["sensor"] for s in scenes]
    # sensor 不一致 → EX8（复用算法 assert_same_caliber 抛 FormulaMixedError）
    sensor = assert_same_caliber(sensors[0], sensors[-1])
    if len(set(sensors)) > 1:
        raise ApiError("EX8", run_id=run_id,
                       detail=f"候选影像 sensor 不一致：{sorted(set(sensors))}")
    # 基线逐景校验 → EX9（BaselineUndeterminableError）
    for s in scenes:
        check_baseline(s["sensor"], s.get("proc_baseline"))
    return sensor


def match_get(aoi_id: Optional[str], t1: Optional[date], t2: Optional[date],
              scene_t1_id: Optional[str], scene_t2_id: Optional[str],
              block_size: int = BLOCK_SIZE) -> dict:
    """GET /scene/match：返回匹配影像对清单（REQ-02/04）。"""
    run_id = task_repo.find_run_id_by_aoi(aoi_id) if aoi_id else None
    try:
        candidates = _resolve_candidates(aoi_id, scene_t1_id, scene_t2_id)
        # 兼容既有契约：GET 的"全量清单"分支（无 aoi_id / 无显式 scene id）响应里
        # 只有一个 sensor + formula_id 字段，无法表达混合辐射口径；历史语义是
        # 返回同口径的 S2 预置景清单并做 EX8/EX9 校验。新增 Landsat C2 L2 景后
        # 该分支仍只列 S2，避免把"混源"误报成 EX8；Landsat 景由 /aoi/select
        # （ST_Intersects + 日期配对）以及带 aoi_id / scene id 的精确查询消费。
        if not aoi_id and not (scene_t1_id and scene_t2_id):
            candidates = [s for s in candidates
                          if str(s["sensor"]).strip().upper() == SENSOR_S2]
        selected = _filter_by_dates(candidates, t1, t2)
        if not selected:
            raise ApiError("EX2", run_id=run_id)

        sensor = _validate_caliber(selected, run_id)
        formula = RADIOMETRIC_FORMULAS.get(sensor, {})
        proc_baseline = next((s.get("proc_baseline") for s in selected
                              if s.get("proc_baseline")), None)
        return {
            "aoi_id": aoi_id,
            "run_id": run_id,
            "t1": str(t1) if t1 else None,
            "t2": str(t2) if t2 else None,
            "block_size": int(block_size),
            "sensor": sensor,
            "formula_id": formula.get("formula_id", ""),
            "proc_baseline": proc_baseline,
            "count": len(selected),
            "scenes": [_scene_item(s) for s in selected],
        }
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise as_api_error(exc, run_id=run_id) from exc


def match_post(aoi_id: str, scene_t1_id: str, scene_t2_id: str,
               block_size: int = BLOCK_SIZE) -> dict:
    """POST /scene/match：保持原契约，补真实窗口/占比/口径（REQ-02/03/04）。"""
    run_id = task_repo.find_run_id_by_aoi(aoi_id)
    try:
        scenes = scene_repo.find_by_ids([scene_t1_id, scene_t2_id])
        if len(scenes) < 2:
            raise ApiError("EX2", run_id=run_id)
        sensor = _validate_caliber(scenes, run_id)
        formula = RADIOMETRIC_FORMULAS.get(sensor, {})

        # 按日期区分 t1/t2 两景
        scenes_sorted = sorted(scenes, key=lambda s: s["acq_date"])
        sc_t1, sc_t2 = scenes_sorted[0], scenes_sorted[-1]
        proc_baseline = sc_t2.get("proc_baseline") or sc_t1.get("proc_baseline")

        clip_window = {"row_off": 0, "col_off": 0, "width": 0, "height": 0,
                       "crs": None, "res": None}
        valid_ratio = 0.0
        # 真实读取 COG 窗口与掩膜占比（尽力而为，失败保留结构性默认，不伪造结果）
        box = aoi_repo.find_bbox(aoi_id)
        if box:
            bbox = BBox.from_sequence(list(box))
            probe_run = run_id or f"scenematch_{uuid.uuid4().hex[:8]}"
            loaded = load_scene(sc_t1["cog_path"], sensor,
                                sc_t1.get("proc_baseline"), bbox, probe_run)
            clip_window = _clip_window(loaded["window"], loaded["profile"])
            valid_ratio = float(loaded["ratio_result"].valid_ratio)

        return {
            "run_id": run_id or "",
            "formula_id": formula.get("formula_id", ""),
            "proc_baseline": proc_baseline,
            "clip_window": clip_window,
            "valid_ratio": valid_ratio,
        }
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise as_api_error(exc, run_id=run_id) from exc

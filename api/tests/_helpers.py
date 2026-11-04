# -*- coding: utf-8 -*-
"""测试共用辅助（非测试模块：文件名不以 test_ 开头，不会被 pytest 收集）。

只构造测试数据 / 校验响应，不实现任何业务逻辑，也不修改业务实现代码。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Sequence

import numpy as np
import rasterio
from rasterio.transform import from_origin

from app.algorithm.mask_builder import SCL_CLOUD_HIGH_PROB, SCL_CLOUD_MEDIUM_PROB
from app.config.constants import (
    AOI_MAX_SPAN_DEG, ERROR_MESSAGES, MIN_SAMPLES_PER_CLASS,
)
from app.service.common import run_dir

# —— 预置影像对（与 data/*/manifest.json、scripts/seed_report.md 一致）——
POYANG_BBOX = (116.25, 29.15, 116.35, 29.25)
POYANG_T1 = "2023-08-04"
POYANG_T2 = "2023-11-02"

SCL_VALID_VEGETATION = 4          # SCL 类别 4 = 植被（有效像元）
REF_CLASS_BY_CODE = {1: "增强", 2: "退化", 3: "稳定"}


def geojson_polygon(bbox: Sequence[float]) -> str:
    """闭合的 GeoJSON Polygon 字符串（WGS84）。"""
    minx, miny, maxx, maxy = [float(v) for v in bbox]
    return json.dumps(
        {
            "type": "Polygon",
            "coordinates": [[
                [minx, miny], [maxx, miny], [maxx, maxy],
                [minx, maxy], [minx, miny],
            ]],
        },
        ensure_ascii=False,
    )


def poyang_geojson(inset: float = 0.01) -> str:
    """鄱阳湖 footprint 内部的合法 AOI：跨度 0.08° ≤ AOI_MAX_SPAN_DEG。"""
    minx, miny, maxx, maxy = POYANG_BBOX
    return geojson_polygon((minx + inset, miny + inset,
                            maxx - inset, maxy - inset))


def too_large_geojson() -> str:
    """跨度 > AOI_MAX_SPAN_DEG 的 AOI（触发 EX1）。"""
    span = AOI_MAX_SPAN_DEG + 0.05
    return geojson_polygon((116.25, 29.15, 116.25 + span, 29.15 + span))


def make_scl_raster(path, valid_ratio: float, size: int = 100,
                    seed: int = 20240607) -> Dict:
    """构造**真实** SCL 风格 uint8 栅格并写盘（numpy + rasterio），返回元数据。

    有效像元 = SCL 4（植被）；无效像元 = SCL 8 / 9（中/高概率云）交替。
    固定随机种子选有效像元位置 → 同输入同输出，确定性可复现。
    """
    rng = np.random.default_rng(seed)
    total = size * size
    n_valid = int(round(total * float(valid_ratio)))
    flat = np.where(
        np.arange(total) % 2 == 0,
        np.uint8(SCL_CLOUD_MEDIUM_PROB),
        np.uint8(SCL_CLOUD_HIGH_PROB),
    ).astype(np.uint8)
    if n_valid:
        idx = rng.permutation(total)[:n_valid]
        flat[idx] = np.uint8(SCL_VALID_VEGETATION)
    arr = flat.reshape(size, size)

    transform = from_origin(116.25, 29.25, 0.0001, 0.0001)
    profile = {
        "driver": "GTiff", "dtype": "uint8", "count": 1,
        "width": size, "height": size,
        "crs": "EPSG:4326", "transform": transform, "nodata": None,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(str(path), "w", **profile) as dst:
        dst.write(arr, 1)
    # 回读，确保后续 build_masks 用的是磁盘上的真实栅格数据
    with rasterio.open(str(path)) as src:
        read_back = src.read(1)
    return {"path": path, "array": read_back, "profile": dict(profile)}


def assert_error_body(resp, code: str, http_status: int,
                      run_id_required: bool = True) -> dict:
    """统一错误体校验：{code, message, run_id} + HTTP 映射 + 中文文案取自常量。"""
    body = resp.json()
    assert resp.status_code == http_status, (resp.status_code, body)
    assert {"code", "message", "run_id"} <= set(body.keys()), body
    assert body["code"] == code, body
    assert body["message"] == ERROR_MESSAGES[code], body
    if run_id_required:
        assert body["run_id"], body
    return body


def make_change_flow(client, geom: str, t1: str, t2: str) -> Dict:
    """完整前置流程：① /aoi/select → ② /result/change（change.tif 落盘）。

    该顺序是用户硬约束 2：sample_service.submit 先 open_change(run_id) 打开
    change.tif，再判断每类样本数；缺 ② 会命中 SYS-01 而非 EX10。
    """
    r_aoi = client.post(
        "/api/v1/aoi/select",
        json={"aoi_geom": geom, "t1": t1, "t2": t2},
    )
    aoi_body = r_aoi.json()
    run_id = aoi_body.get("run_id") or ""
    r_change = client.post("/api/v1/result/change", json={"run_id": run_id})
    change_body = r_change.json()
    # 响应里的 change_png / change_geotiff 已是同源相对 URL（见 test_api_artifacts.py）；
    # 既有用例按落盘绝对路径断言 rasterio/is_file，故此处仍提供真实磁盘路径。
    out = run_dir(run_id)
    return {
        "aoi_status": r_aoi.status_code,
        "aoi_body": aoi_body,
        "change_status": r_change.status_code,
        "change_body": change_body,
        "run_id": run_id,
        "aoi_id": aoi_body.get("aoi_id") or "",
        "matched_scenes": aoi_body.get("matched_scenes") or [],
        "change_geotiff": str(out / "change.tif"),
        "change_png": str(out / "change.png"),
        "valid_ratio": float(change_body.get("valid_ratio", 0.0) or 0.0),
        "threshold": float(change_body.get("threshold", 0.0) or 0.0),
        "stats": change_body.get("stats", []),
        "params_snapshot": change_body.get("params_snapshot", {}),
    }


def build_samples_from_change(change_path, per_class: int = None) -> List[dict]:
    """从 change.tif 真实三类编码像元里确定性抽取每类 ≥ MIN_SAMPLES_PER_CLASS 个样本。

    x=col / y=row 为整数像素坐标（sample_service._locate 按像素列/行解释），
    ref_class 与该像元预测编码一致 → y_true == y_pred，OA/Kappa 可精确复核。
    """
    if per_class is None:
        per_class = MIN_SAMPLES_PER_CLASS + 2
    with rasterio.open(str(change_path)) as ds:
        arr = ds.read(1)
    rng = np.random.default_rng(20240607)
    samples: List[dict] = []
    for code, ref in REF_CLASS_BY_CODE.items():
        coords = np.argwhere(arr == code)
        assert coords.shape[0] >= per_class, (
            f"change.tif 类别 {code} 仅 {coords.shape[0]} 个像元，不足 {per_class}"
        )
        pick = rng.choice(coords.shape[0], size=per_class, replace=False)
        for row, col in coords[pick]:
            samples.append({
                "x": float(int(col)), "y": float(int(row)), "ref_class": ref,
            })
    return samples

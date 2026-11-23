# -*- coding: utf-8 -*-
"""报告导出编排（REQ-12 / REQ-14 / REQ-15 / EX6 / EX7）。

流程：formats 白名单校验（EX6）→ 串行限流（EX7）→ 读回 change.tif + 报告元数据 →
调 ``export_triplet`` 产出 GeoTIFF/PNG/PDF 三件套 → 上传 results 桶 →
返回 files（同源相对 URL + 实际字节数）/ run_id / commit / attribution。
"""
from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import rasterio

from app.algorithm.change_detector import (
    CHANGE_LABELS_ZH, CHANGE_NODATA, CHANGE_TRANSITION,
)
from app.algorithm.writers import (
    attribution_lines, export_triplet, validate_export_formats,
)
from app.api.errors import ApiError, as_api_error
from app.config.constants import (
    ANIMATION_FILENAME, EXPERIMENTS_CACHE_COMPARE_KEY,
    EXPERIMENTS_CACHE_SENSITIVITY_KEY, EXPORT_FORMATS, METHOD_OTSU,
    METHOD_REGRESSION, RESULT_BUCKET,
)
from app.config.storage import (
    StorageSettings, ensure_buckets, get_client, upload_file,
)
from app.repository import task_repo
from app.service import experiment_service
from app.service.common import (
    artifact_url, export_zip_name, git_short_commit,
    regression_artifact_names, run_dir, utc_now,
)
from app.service.concurrency import serial
from app.service.sample_service import accuracy_for_run

# 格式 → (产物文件名, 上传 content_type)；缺省 otsu 差值法主名不变
_FORMAT_CONTENT_TYPE = {
    "geotiff": "image/tiff",
    "png": "image/png",
    "pdf": "application/pdf",
    "geojson": "application/geo+json",
    "gif": "image/gif",
}
_FORMAT_FILE = {
    "geotiff": ("change.tif", "image/tiff"),
    "png": ("change.png", "image/png"),
    "pdf": ("report.pdf", "application/pdf"),
    "geojson": ("change.geojson", "application/geo+json"),
    "gif": (ANIMATION_FILENAME, "image/gif"),
}
# REQ-11：回归法产物名（与 change_service 的 _reg 主名同源）
_FORMAT_FILE_REG = {
    **regression_artifact_names(),
    "gif": ANIMATION_FILENAME,
}

__all__ = ["export", "experiment_sections_from_cache"]

logger = logging.getLogger(__name__)


def experiment_sections_from_cache(run_id: str) -> Dict[str, Optional[dict]]:
    """批次 F · 只读：取 PDF「实验分析」章节数据（REQ-12/14）。

    导出时**只读**一次 ``experiment_service.read_cache(run_id)``：绝不写缓存、
    绝不触发 compare() / sensitivity() 复算、绝不产生新 run。``compare`` 是
    「签名 → 响应」字典，可能多条，**如实取 computed_at 最大的一条**（不编造、
    不改缓存）；``sensitivity`` 为响应 dict 或 None。两者皆无时返回两个 None，
    writers 会整段不输出该章节。
    """
    cache = experiment_service.read_cache(run_id)
    compare_map = cache.get(EXPERIMENTS_CACHE_COMPARE_KEY)
    candidates = [v for v in (compare_map.values() if isinstance(compare_map, dict) else [])
                  if isinstance(v, dict)]
    latest = (max(candidates, key=lambda item: str(item.get("computed_at") or ""))
              if candidates else None)
    sens = cache.get(EXPERIMENTS_CACHE_SENSITIVITY_KEY)
    return {
        "compare": latest,
        "sensitivity": sens if isinstance(sens, dict) else None,
    }


def _files_for(method: str) -> Dict[str, str]:
    """按本轮检测方法选择三件套文件名；老 run 快照无 method 时按 otsu 处理。"""
    if str(method or METHOD_OTSU).strip().lower() == METHOD_REGRESSION:
        return dict(_FORMAT_FILE_REG)
    return {fmt: name for fmt, (name, _) in _FORMAT_FILE.items()}


def _upload(client, run_id: str, fmt: str, local: Path, filename: str) -> str:
    """保留 MinIO 上传动作，但返回给前端的 ``files[].url`` 为同源相对 URL。

    ``storage.object_url()`` 的 ``minio://`` 口径只用于 ``scene.cog_path`` 登记
    （``cog_source.resolve_bands()`` 依赖它），此处不调用、也不改动该函数本身；
    上传失败时仅告警，产物已落盘，仍可由 /api/v1/artifacts 端点读取。
    """
    content_type = _FORMAT_CONTENT_TYPE[fmt]
    if client is not None:
        try:
            object_name = f"{run_id}/{filename}"
            upload_file(client, RESULT_BUCKET, object_name, str(local),
                        content_type=content_type)
        except Exception:  # noqa: BLE001 - 存储故障不阻断导出
            logger.warning("MinIO 产物上传失败，改由本地 HTTP 端点提供：%s", filename)
    return artifact_url(run_id, filename)


def export(run_id: str, formats: List[str], cartographic: bool = False) -> dict:
    """报告导出（REQ-12/14/15；批次 I 的 cartographic 只增不改默认行为）。"""
    try:
        # EX6：格式白名单（writers.validate_export_formats 抛 ExportFormatError.code=EX6）
        allowed = list(validate_export_formats(formats))
        if not allowed:
            allowed = list(EXPORT_FORMATS)

        if not task_repo.get_task(run_id):
            raise ApiError("EX2", run_id=run_id,
                           detail=f"run_id 无对应 task：{run_id}")

        with serial(run_id):  # REQ-13：同步阻塞、并发上限 1
            out_dir = run_dir(run_id)
            meta_path: Path = out_dir / "run_report.json"
            if not meta_path.exists():
                raise ApiError("SYS-01", run_id=run_id,
                               detail="结果产物不存在，请先执行 /result/change")
            report: Dict = json.loads(meta_path.read_text(encoding="utf-8"))
            # 批次 F：导出时只读一次实验复算缓存 → 注入 PDF「实验分析」章节数据。
            # 只注入内存 report（export_triplet 的 enriched=dict(report) 原样带给 write_pdf）；
            # 绝不写缓存、绝不触发复算、绝不产生新 run。
            report["experiments"] = experiment_sections_from_cache(run_id)
            snapshot: Optional[dict] = report.get("params_snapshot")
            # 回归法 run 只有 _reg 三件套：按快照的 method 选文件名，绝不误读/覆盖差值法主名
            method = str((snapshot or {}).get("method") or report.get("method") or METHOD_OTSU)
            file_map = _files_for(method)
            names = {fmt: file_map[fmt] for fmt in _FORMAT_CONTENT_TYPE}
            change_path: Path = out_dir / file_map["geotiff"]
            if not change_path.exists():
                raise ApiError("SYS-01", run_id=run_id,
                               detail="结果产物不存在，请先执行 /result/change")

            with rasterio.open(str(change_path)) as ds:
                change = ds.read(1)
                profile = dict(ds.profile)
                # 若已完成样本评价，把真实精度并入报告（不伪造）
                acc = accuracy_for_run(run_id, ds,
                                       params_snapshot=report.get("params_snapshot"))
                if acc is not None:
                    report["accuracy"] = {
                        "confusion_matrix": acc["confusion_matrix"],
                        "overall_accuracy": acc["overall_accuracy"],
                        "kappa": acc["kappa"],
                        "note": acc["note"],
                        "teaching_level": acc["teaching_level"],
                        # 逐类生产者精度（键=中文类别名），PDF / 前端同源展示
                        "per_class_accuracy": {
                            CHANGE_LABELS_ZH[int(k)]: round(float(v), 4)
                            for k, v in (acc.get("per_class_accuracy") or {}).items()
                        },
                    }

            valid_ratio = float(report.get("valid_ratio", 0.0))
            threshold = report.get("threshold")
            if threshold is None:
                threshold = report.get("otsu_threshold")
            cartography_meta = {
                "index": str(report.get("index")
                             or (snapshot or {}).get("index") or "N/A"),
                "method": method,
                "threshold": threshold,
                "t1_date": str(report.get("t1_date")
                               or (snapshot or {}).get("t1") or "N/A"),
                "t2_date": str(report.get("t2_date")
                               or (snapshot or {}).get("t2") or "N/A"),
                "generated_at": utc_now().strftime("%Y-%m-%d %H:%M:%S UTC"),
                "run_id": run_id,
            }
            # 批次 B1：检测时落盘的过渡带掩膜 → 重建专题图渲染副本，保证
            # 重新导出的 PNG / GeoJSON 与首次检测同口径。老 run 无掩膜文件
            # 时 render_change=None，write_png / write_geojson 如实回退三类。
            render_change = None
            mask_path = out_dir / "transition_band_mask.npy"
            if mask_path.is_file():
                try:
                    band_mask = np.load(str(mask_path))
                    if band_mask.shape == change.shape:
                        render_change = change.copy()
                        render_change[(band_mask.astype(bool))
                                      & (change != CHANGE_NODATA)] = CHANGE_TRANSITION
                except Exception:  # noqa: BLE001 - 掩膜损坏/旧版格式一律跳过
                    render_change = None
            export_triplet(
                change, profile, valid_ratio, report, out_dir,
                formats=allowed, run_id=run_id, params_snapshot=snapshot,
                names=names, render_change=render_change,
                cartographic=cartographic,
                transform=profile.get("transform"), crs=profile.get("crs"),
                cartography_meta=cartography_meta,
            )

            # 防御性确保 results 桶存在；不可用时仅回退本地路径
            client = None
            try:
                client = get_client(StorageSettings.from_env())
                ensure_buckets(client, [RESULT_BUCKET])
            except Exception:  # noqa: BLE001
                client = None

            files: List[dict] = []
            for fmt in allowed:
                filename = file_map[fmt]
                local = out_dir / filename
                size = int(local.stat().st_size) if local.exists() else 0
                files.append({
                    "format": fmt,
                    "filename": filename,
                    "url": _upload(client, run_id, fmt, local, filename),
                    "size": size,
                })

            # 批次 A：所选格式 ≥ 2 时，把本轮已落盘的白名单产物复用打成一个 zip
            # （标准库 zipfile，不重新生成文件）；files[] 追加一条 format='zip'。
            # 归属校验仍由路由 ensure_run_owner 完成；URL 走同一产物白名单口径。
            if len(allowed) >= 2:
                zip_name = export_zip_name(run_id)
                zip_path = out_dir / zip_name
                with zipfile.ZipFile(zip_path, "w",
                                    compression=zipfile.ZIP_DEFLATED) as zf:
                    for item in files:
                        local = out_dir / item["filename"]
                        if local.is_file():
                            zf.write(local, arcname=item["filename"])
                files.append({
                    "format": "zip",
                    "filename": zip_name,
                    "url": artifact_url(run_id, zip_name),
                    "size": int(zip_path.stat().st_size),
                })

        # REQ-15 / NF6 / NF7：与 PDF 署名页共用同一常量源，逐字复用不重写
        attribution = "; ".join(attribution_lines(report))
        return {
            "files": files,
            "run_id": run_id,
            "commit": git_short_commit(),
            "attribution": attribution,
        }
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise as_api_error(exc, run_id=run_id) from exc

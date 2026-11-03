# -*- coding: utf-8 -*-
"""报告导出编排（REQ-12 / REQ-14 / REQ-15 / EX6 / EX7）。

流程：formats 白名单校验（EX6）→ 串行限流（EX7）→ 读回 change.tif + 报告元数据 →
调 ``export_triplet`` 产出 GeoTIFF/PNG/PDF 三件套 → 上传 results 桶 →
返回 files（url + 实际字节数）/ run_id / commit / attribution。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

import rasterio

from app.algorithm.writers import export_triplet, validate_export_formats
from app.api.errors import ApiError, as_api_error
from app.config.constants import (
    ATTRIBUTION_S2, ATTRIBUTION_S2_MODIFIED, EXPORT_FORMATS, RESULT_BUCKET,
)
from app.config.storage import (
    StorageSettings, ensure_buckets, get_client, object_url, upload_file,
)
from app.repository import task_repo
from app.service.common import git_short_commit, run_dir
from app.service.concurrency import serial
from app.service.sample_service import accuracy_for_run

# 格式 → (产物文件名, 上传 content_type)
_FORMAT_FILE = {
    "geotiff": ("change.tif", "image/tiff"),
    "png": ("change.png", "image/png"),
    "pdf": ("report.pdf", "application/pdf"),
}

__all__ = ["export"]


def _upload(client, run_id: str, fmt: str, local: Path) -> str:
    """上传单个产物到 results 桶；MinIO 不可用时回退本地绝对路径。"""
    filename, content_type = _FORMAT_FILE[fmt]
    if client is not None:
        try:
            object_name = f"{run_id}/{filename}"
            upload_file(client, RESULT_BUCKET, object_name, str(local),
                        content_type=content_type)
            return object_url(RESULT_BUCKET, object_name)
        except Exception:  # noqa: BLE001 - 存储故障回退本地路径，不阻断导出
            pass
    return str(local.resolve())


def export(run_id: str, formats: List[str]) -> dict:
    """报告导出（REQ-12/14/15）。"""
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
            change_path: Path = out_dir / "change.tif"
            meta_path: Path = out_dir / "run_report.json"
            if not change_path.exists() or not meta_path.exists():
                raise ApiError("SYS-01", run_id=run_id,
                               detail="结果产物不存在，请先执行 /result/change")

            report: Dict = json.loads(meta_path.read_text(encoding="utf-8"))
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
                    }

            valid_ratio = float(report.get("valid_ratio", 0.0))
            snapshot: Optional[dict] = report.get("params_snapshot")
            export_triplet(
                change, profile, valid_ratio, report, out_dir,
                formats=allowed, run_id=run_id, params_snapshot=snapshot,
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
                filename, _ = _FORMAT_FILE[fmt]
                local = out_dir / filename
                size = int(local.stat().st_size) if local.exists() else 0
                files.append({
                    "format": fmt,
                    "url": _upload(client, run_id, fmt, local),
                    "size": size,
                })

        year = str(report.get("year") or "2023")
        attribution = "; ".join([
            ATTRIBUTION_S2.format(year=year),
            ATTRIBUTION_S2_MODIFIED.format(year=year),
        ])
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

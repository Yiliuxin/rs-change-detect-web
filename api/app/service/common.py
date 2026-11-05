# -*- coding: utf-8 -*-
"""服务层共用小工具（可复现三要素 / 时间，REQ-14）。"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from app.config.constants import VALID_INDEXES

# api/app/service/common.py → parents[3] = 工程根 C:\Test\Web
REPO_ROOT = Path(__file__).resolve().parents[3]
# 产物落盘根：api/runs/<run_id>/（与算法 _smoke 一致，且被 .gitignore 忽略）
RUNS_ROOT = Path(__file__).resolve().parents[2] / "runs"
# 本地影像回退根：C:\Test\Web\data\pair_*
DATA_ROOT = REPO_ROOT / "data"
# 产物同源 HTTP URL 前缀（REQ-10 / REQ-12）：静态端点在 main.py 注册，根即 RUNS_ROOT。
# 响应只暴露相对 URL；本地绝对路径仍用于落盘与 change_result 表登记（schema 不变）。
ARTIFACT_URL_PREFIX = "/api/v1/artifacts"

# 产物文件名白名单 → Content-Type（REQ-10 / REQ-12 / REQ-14）。
# 由 main.py 的静态端点（兼防穿越）与 run_service 的产物清单共用，避免两处漂移。
# 主三件套保持历史文件名不变：NDVI 缺省行为、sample/report/e2e 链路均依赖它们。
# 每个指数额外归档一份 ``<stem>_<index小写>.<ext>``，NDVI / NDWI 互不覆盖。
# 双时相真彩合成固定名，供批次 2 卷帘对比消费。
ARTIFACT_MEDIA_TYPES: Dict[str, str] = {
    "change.tif": "image/tiff",
    "change.png": "image/png",
    "report.pdf": "application/pdf",
    "t1_composite.png": "image/png",
    "t2_composite.png": "image/png",
}
for _index in VALID_INDEXES:
    _suffix = "_" + _index.lower()
    ARTIFACT_MEDIA_TYPES[f"change{_suffix}.tif"] = "image/tiff"
    ARTIFACT_MEDIA_TYPES[f"change{_suffix}.png"] = "image/png"
    ARTIFACT_MEDIA_TYPES[f"report{_suffix}.pdf"] = "application/pdf"

__all__ = ["REPO_ROOT", "RUNS_ROOT", "DATA_ROOT", "ARTIFACT_URL_PREFIX",
           "ARTIFACT_MEDIA_TYPES", "git_short_commit", "utc_now", "run_dir",
           "artifact_url", "index_artifact_names", "artifact_manifest"]


def git_short_commit() -> Optional[str]:
    """只读取 ``git rev-parse --short HEAD``（REQ-12 可复现 commit）。失败返回 None。"""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT), stderr=subprocess.DEVNULL, timeout=5,
        )
        return out.decode("utf-8", "ignore").strip() or None
    except Exception:
        return None


def utc_now() -> datetime:
    """带时区当前时间（task.started_at / finished_at）。"""
    return datetime.now(timezone.utc)


def run_dir(run_id: str) -> Path:
    """返回并创建 ``api/runs/<run_id>/`` 产物目录。"""
    d = RUNS_ROOT / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def artifact_url(run_id: str, filename: str) -> str:
    """产物同源相对 URL（REQ-10 / REQ-12），供 change_service / report_service 复用。

    仅用于响应；落盘与 ``change_result`` 表仍使用本地绝对路径。
    """
    return f"{ARTIFACT_URL_PREFIX}/{run_id}/{filename}"


def index_artifact_names(index: str) -> Dict[str, str]:
    """主三件套名 → 带 index 小写后缀的归档名（NDVI / NDWI 互不覆盖）。"""
    key = str(index).strip().upper()
    if key not in VALID_INDEXES:
        raise ValueError(f"不支持的指数：{index!r}，仅支持 {list(VALID_INDEXES)}")
    suffix = "_" + key.lower()
    return {
        "change.tif": f"change{suffix}.tif",
        "change.png": f"change{suffix}.png",
        "report.pdf": f"report{suffix}.pdf",
    }


def artifact_manifest(run_id: str, index: str) -> Dict[str, str]:
    """本轮全部产物清单 ``{文件名: 同源下载 URL}``（REQ-10/12/14）。

    含主三件套、按指数归档的带后缀三件套、双时相真彩合成图；文件名一律
    落在 ``ARTIFACT_MEDIA_TYPES`` 白名单内，静态端点可直接读取。
    """
    names = [
        "change.tif", "change.png", "report.pdf",
        "t1_composite.png", "t2_composite.png",
        *index_artifact_names(index).values(),
    ]
    return {n: artifact_url(run_id, n) for n in names
            if n in ARTIFACT_MEDIA_TYPES}

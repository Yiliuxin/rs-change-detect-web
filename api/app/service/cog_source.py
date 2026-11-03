# -*- coding: utf-8 -*-
"""COG 源解析（REQ-02 / REQ-03 / SYS-01）。

把 scene 表登记的 ``cog_path``（``minio://scenes/pair_<name>/<t1|t2>/``，是**目录前缀**
而非单文件）解析为 5 个波段本地临时文件：

1) 走 MinIO：``list_objects(client, COG_BUCKET, prefix)`` 匹配 B02/B03/B04/B08/SCL
   （兼容 nanchang 带日期的文件名 ``t1_20231102_B04.tif``），``fget_object`` 下载到
   ``api/runs/<run_id>/_cog/<tag>/``；
2) MinIO 不可用 / 对象不全时回退本地 ``C:\\Test\\Web\\data\\pair_<name>_*\\``。

返回 ``{B02,B03,B04,B08,SCL: Path}``；两路都失败 → SYS-01。不修改 data/ 下任何文件。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from app.api.errors import ApiError
from app.config.constants import COG_BUCKET
from app.config.storage import StorageSettings, get_client, list_objects
from app.service.common import DATA_ROOT, RUNS_ROOT

logger = logging.getLogger(__name__)

BANDS: Tuple[str, ...] = ("B02", "B03", "B04", "B08", "SCL")
_TAG_RE = re.compile(r"^t[12]$", re.IGNORECASE)

__all__ = ["BANDS", "parse_cog_path", "resolve_bands"]


def parse_cog_path(cog_path: str) -> Tuple[str, str, str, str]:
    """解析 ``minio://scenes/pair_<name>/<tag>/`` → (bucket, prefix, pair_name, tag)。"""
    s = str(cog_path).strip().replace("\\", "/")
    if s.startswith("minio://"):
        s = s[len("minio://"):]
    parts = s.split("/", 1)
    bucket = parts[0] or COG_BUCKET
    prefix = parts[1] if len(parts) > 1 else ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    segs = [seg for seg in prefix.split("/") if seg]
    pair_name = segs[0] if segs else ""
    tag = next((seg for seg in segs if _TAG_RE.match(seg)), "scene")
    return bucket, prefix, pair_name, tag


def _match_band(object_name: str, band: str) -> bool:
    """对象 basename 是否以 ``_<band>.tif`` 结尾（大小写不敏感）。"""
    base = object_name.rsplit("/", 1)[-1].upper()
    return base.endswith(f"_{band.upper()}.TIF")


def _from_minio(prefix: str, tag: str, run_id: str) -> Optional[Dict[str, Path]]:
    """从 MinIO 下载 5 波段到 api/runs/<run_id>/_cog/<tag>/；失败返回 None。"""
    try:
        client = get_client(StorageSettings.from_env())
        objects = list_objects(client, COG_BUCKET, prefix=prefix)
        picked: Dict[str, str] = {}
        for band in BANDS:
            hits = [o for o in objects if _match_band(o, band)]
            if not hits:
                logger.warning("MinIO prefix=%s 缺少波段 %s", prefix, band)
                return None
            picked[band] = sorted(hits)[0]

        dest = RUNS_ROOT / run_id / "_cog" / tag
        dest.mkdir(parents=True, exist_ok=True)
        paths: Dict[str, Path] = {}
        for band, obj in picked.items():
            local = dest / obj.rsplit("/", 1)[-1]
            if not local.exists() or local.stat().st_size == 0:
                client.fget_object(COG_BUCKET, obj, str(local))
            paths[band] = local
        return paths
    except Exception as exc:  # noqa: BLE001 - MinIO 不可用即回退本地
        logger.warning("MinIO COG 解析失败（回退本地）：%s", exc)
        return None


def _from_local(pair_name: str, tag: str) -> Optional[Dict[str, Path]]:
    """回退本地 data/pair_<name>_*/ 目录（只读，不修改）。"""
    if not pair_name or not DATA_ROOT.exists():
        return None
    for pair_dir in sorted(p for p in DATA_ROOT.glob(f"{pair_name}*") if p.is_dir()):
        paths: Dict[str, Path] = {}
        for band in BANDS:
            # 兼容 t1_B04.tif 与 t1_20231102_B04.tif 两种命名
            hits = sorted(pair_dir.glob(f"{tag}*_{band}.tif"))
            if not hits:
                hits = sorted(pair_dir.glob(f"{tag}*_{band}.TIF"))
            if hits:
                paths[band] = hits[0]
        if len(paths) == len(BANDS):
            return paths
    return None


def resolve_bands(cog_path: str, run_id: str) -> Dict[str, Path]:
    """把目录前缀解析为 5 波段本地文件路径（MinIO 优先，本地回退）。"""
    _bucket, prefix, pair_name, tag = parse_cog_path(cog_path)

    paths = _from_minio(prefix, tag, run_id)
    if paths is None:
        paths = _from_local(pair_name, tag)

    if paths is None or len(paths) != len(BANDS):
        raise ApiError("SYS-01", run_id=run_id,
                       detail=f"COG 5 波段解析失败：cog_path={cog_path}, prefix={prefix}")
    return paths

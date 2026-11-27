# -*- coding: utf-8 -*-
"""COG 源解析（REQ-02 / REQ-03 / SYS-01 / REQ-07）。

把 scene 表登记的 ``cog_path``（``minio://scenes/pair_<name>/<t1|t2>/``，是**目录前缀**
而非单文件）解析为 5 个波段本地临时文件：

1) 走 MinIO：``list_objects(client, COG_BUCKET, prefix)`` 匹配 B02/B03/B04/B08/SCL
   （兼容 nanchang 带日期的文件名 ``t1_20231102_B04.tif``），``fget_object`` 下载到
   ``api/runs/<run_id>/_cog/<pair_name>/<tag>/``（按影像对隔离，避免切换时串文件）；
2) MinIO 不可用 / 对象不全时回退本地 ``C:\\Test\\Web\\data\\pair_<name>_*\\``。

返回 ``{B02,B03,B04,B08,SCL: Path}``；两路都失败 → SYS-01。不修改 data/ 下任何文件。

可选波段（NDBI / NDMI 的 B11 短波红外）由 :func:`resolve_optional_bands` **按需**
解析：``BANDS`` 仍固定 5 个不变、``resolve_bands`` 的 5 波段强制语义与 SYS-01 文案
均不变；可选波段缺失只返回空 dict，由调用方按 EX11 中文明确报错，绝不抛 SYS-01，
保证无 B11 的 Landsat 组跑 NDVI / NDWI 链路不受牵连。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from app.api.errors import ApiError
from app.config.constants import COG_BUCKET, LANDSAT_BAND_ALIASES, LANDSAT_SENSORS
from app.config.storage import StorageSettings, get_client, list_objects
from app.service.common import DATA_ROOT, RUNS_ROOT

logger = logging.getLogger(__name__)

BANDS: Tuple[str, ...] = ("B02", "B03", "B04", "B08", "SCL")
# NDBI/NDMI 按需读取的可选短波红外波段；**不**并入 BANDS / change_service._BANDS_10M。
OPTIONAL_BANDS: Tuple[str, ...] = ("B11",)
_TAG_RE = re.compile(r"^t[12]$", re.IGNORECASE)

# 云端在线取数：CDSE 直接回传的 4 指数合并为单文件 ``<tag>_indices.tif``
# （band1=NDVI / band2=NDWI / band3=NDBI / band4=NDMI）。它是「指数成品」，
# 不是原始反射率波段，故刻意不并入 BANDS / OPTIONAL_BANDS；检测侧识别到该文件
# 会跳过读 COG 与本地算指数，直接复用云端结果。
CLOUD_INDEXES: Tuple[str, ...] = ("NDVI", "NDWI", "NDBI", "NDMI")
CLOUD_INDEX_SUFFIX = "_indices.tif"
# 云端真彩成品（在线取数 cloud 模式顺带落盘），供双时相卷帘对比消费。
CLOUD_COMPOSITE_SUFFIX = "_composite.png"

__all__ = ["BANDS", "OPTIONAL_BANDS", "CLOUD_INDEXES", "CLOUD_INDEX_SUFFIX",
           "CLOUD_COMPOSITE_SUFFIX", "parse_cog_path", "resolve_bands",
           "resolve_optional_bands", "has_optional_band", "has_cloud_indices",
           "resolve_cloud_indices", "resolve_cloud_composite"]


def _band_names(band: str, sensor: Optional[str] = None) -> Tuple[str, ...]:
    """按 sensor 分派波段文件名别名（REQ-04 / Landsat C2 L2 链路接通）。

    S2：``B02/B03/B04/B08/SCL``；L8/L9：额外接受
    ``blue/green/red/nir08/qa_pixel`` 与 ``SR_B2/SR_B3/SR_B4/SR_B5/QA_PIXEL``。
    内部统一键仍是 BANDS，服务层 / 算法层无需感知资产命名差异。
    可选波段 B11 在 LANDSAT_BAND_ALIASES 中无 SWIR 别名，退化为仅匹配 ``B11``。
    """
    names = [band]
    if str(sensor or "").strip().upper() in LANDSAT_SENSORS:
        names.extend(LANDSAT_BAND_ALIASES.get(band, ()))
    return tuple(dict.fromkeys(names))


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


def _match_band(object_name: str, band: str,
                sensor: Optional[str] = None) -> bool:
    """对象 basename 是否以 ``_<波段名>.tif`` 结尾（大小写不敏感，按 sensor 取别名）。"""
    base = object_name.rsplit("/", 1)[-1].upper()
    return any(base.endswith(f"_{name.upper()}.TIF")
               for name in _band_names(band, sensor))


def _from_minio(prefix: str, tag: str, run_id: str,
                sensor: Optional[str] = None,
                bands: Tuple[str, ...] = BANDS) -> Optional[Dict[str, Path]]:
    """从 MinIO 下载 ``bands`` 到 ``api/runs/<run_id>/_cog/<pair_name>/<tag>/``；失败返回 None。

    缓存目录**必须按 pair_name 隔离**：不同影像对的波段文件名完全相同
    （都是 ``t1_B02.tif``），若只按 ``run_id/tag`` 缓存，同一 run 内切换影像对时
    会把上一组的文件当成本组的，静默产出错误结果（实测表现为 clip_window 离谱、
    valid_ratio=0 → EX3）。

    ``bands`` 缺省为既有 5 波段（``resolve_bands`` 行为逐字不变）；按需解析可选
    波段（如 B11）时由 ``resolve_optional_bands`` 传入子集，缺任一即返回 None，
    不抛 SYS-01，交由调用方决定是回退本地还是明确报错。
    """
    try:
        client = get_client(StorageSettings.from_env())
        objects = list_objects(client, COG_BUCKET, prefix=prefix)
        picked: Dict[str, str] = {}
        for band in bands:
            hits = [o for o in objects if _match_band(o, band, sensor)]
            if not hits:
                logger.warning("MinIO prefix=%s 缺少波段 %s", prefix, band)
                return None
            picked[band] = sorted(hits)[0]

        pair_name = prefix.strip("/").split("/")[0] or "scene"
        dest = RUNS_ROOT / run_id / "_cog" / pair_name / tag
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


def _from_local(pair_name: str, tag: str,
                sensor: Optional[str] = None,
                bands: Tuple[str, ...] = BANDS) -> Optional[Dict[str, Path]]:
    """回退本地 data/pair_<name>_*/ 目录（只读，不修改）。

    ``bands`` 缺省为既有 5 波段；按需解析可选波段时传入子集（如 B11），
    文件名匹配支持 ``t1_B11.tif`` / ``t1_20231102_B11.tif`` 两种命名，
    缺任一即返回 None，不抛 SYS-01。
    """
    if not pair_name or not DATA_ROOT.exists():
        return None
    for pair_dir in sorted(p for p in DATA_ROOT.glob(f"{pair_name}*") if p.is_dir()):
        paths: Dict[str, Path] = {}
        for band in bands:
            # 兼容 t1_B04.tif 与 t1_20231102_B04.tif 两种命名，以及 Landsat 资产别名
            for name in _band_names(band, sensor):
                hits = sorted(pair_dir.glob(f"{tag}*_{name}.tif"))
                if not hits:
                    hits = sorted(pair_dir.glob(f"{tag}*_{name}.TIF"))
                if hits:
                    paths[band] = hits[0]
                    break
        if len(paths) == len(bands):
            return paths
    return None


def resolve_bands(cog_path: str, run_id: str,
                  sensor: Optional[str] = None) -> Dict[str, Path]:
    """把目录前缀解析为 5 波段本地文件路径（MinIO 优先，本地回退）。

    ``sensor`` 为 L8/L9 时按 Landsat C2 L2 资产别名匹配；缺省 / S2 时行为不变。
    """
    _bucket, prefix, pair_name, tag = parse_cog_path(cog_path)

    paths = _from_minio(prefix, tag, run_id, sensor)
    if paths is None:
        paths = _from_local(pair_name, tag, sensor)

    if paths is None or len(paths) != len(BANDS):
        raise ApiError("SYS-01", run_id=run_id,
                       detail=f"COG 5 波段解析失败：cog_path={cog_path}, prefix={prefix}")
    return paths


def has_optional_band(cog_path: str, band: str = "B11",
                      sensor: Optional[str] = None) -> bool:
    """轻量探测可选波段是否存在（只列对象名 / 查本地文件，**不下载**）。

    供 scene 清单 ``has_b11`` 与在线取数复用判定使用：只判断"这组影像有没有 B11"，
    不触发 ``resolve_optional_bands`` 的 MinIO 下载与缓存写入。判据与解析同口径：
    MinIO prefix 下匹配到 ``_<band>.tif`` 即 True；MinIO 不可用/无对象时回退本地
    ``data/pair_<name>_*``，按同样文件名规则匹配。两路都没有 → False。

    成本：一次 MinIO ``list_objects``（前缀列举，不取对象体）或一次本地目录扫描。
    scene 清单逐景调用，故刻意不做 ``fget_object``。
    """
    _bucket, prefix, pair_name, tag = parse_cog_path(cog_path)
    try:
        client = get_client(StorageSettings.from_env())
        objects = list_objects(client, COG_BUCKET, prefix=prefix)
        if any(_match_band(o, band, sensor) for o in objects):
            return True
    except Exception as exc:  # noqa: BLE001 - MinIO 不可用 → 回退本地
        logger.warning("可选波段探测 MinIO 失败（回退本地）：%s", exc)
    try:
        return _from_local(pair_name, tag, sensor, bands=(band,)) is not None
    except Exception:  # noqa: BLE001 - 本地探测异常一律视为缺失
        return False


def resolve_optional_bands(cog_path: str, run_id: str,
                           sensor: Optional[str] = None,
                           bands: Tuple[str, ...] = OPTIONAL_BANDS
                           ) -> Dict[str, Path]:
    """按需解析可选波段（缺省 B11 短波红外）：MinIO 优先、本地 data/pair_* 回退。

    与 :func:`resolve_bands` 的 5 波段强制语义正交：``BANDS`` 仍固定 5 个不变，
    本函数只解析 ``bands`` 子集，复用同一套 ``_match_band`` / ``_from_local``
    文件名匹配（支持 ``t?_B11.tif``）与 pair_name/tag 隔离缓存目录。

    缺任一波段 / 两路都失败 → 返回空 dict（**不抛 SYS-01**），由调用方按 EX11
    中文明确报错；这样无 B11 的 Landsat 组跑 NDVI / NDWI 完全不受牵连。
    """
    _bucket, prefix, pair_name, tag = parse_cog_path(cog_path)
    wanted = tuple(bands)
    paths = _from_minio(prefix, tag, run_id, sensor, bands=wanted)
    if paths is None:
        paths = _from_local(pair_name, tag, sensor, bands=wanted)
    if paths is None or len(paths) != len(wanted):
        return {}
    return dict(paths)


# --------------------------------------------------------------------------- #
# 云端指数成品（在线取数 cloud 模式）
# --------------------------------------------------------------------------- #
def _is_cloud_index_object(object_name: str) -> bool:
    """对象名是否以 ``_indices.tif`` 结尾（大小写不敏感）。"""
    return object_name.rsplit("/", 1)[-1].lower().endswith(CLOUD_INDEX_SUFFIX)


def has_cloud_indices(cog_path: Optional[str]) -> bool:
    """轻量探测该时相是否存有云端 4 指数成品（只列对象名，不下载）。

    与 :func:`has_optional_band` 同口径：MinIO 优先，失败回退本地目录；
    只判断"有没有指数成品"，不读取像素。检测侧用它自动识别在线云端取数组，
    无需用户再勾快速模式，也不会重复调 CDSE。
    """
    if not cog_path:
        return False
    _bucket, prefix, pair_name, tag = parse_cog_path(str(cog_path))
    try:
        client = get_client(StorageSettings.from_env())
        if any(_is_cloud_index_object(o)
               for o in list_objects(client, COG_BUCKET, prefix=prefix)):
            return True
    except Exception as exc:  # noqa: BLE001 - MinIO 不可用 → 回退本地
        logger.warning("云端指数探测 MinIO 失败（回退本地）：%s", exc)
    try:
        if pair_name and DATA_ROOT.exists():
            for pair_dir in sorted(p for p in DATA_ROOT.glob(f"{pair_name}*") if p.is_dir()):
                if list(pair_dir.glob(f"{tag}*{CLOUD_INDEX_SUFFIX}")):
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


def resolve_cloud_indices(cog_path: str, run_id: str) -> Optional[Path]:
    """把 ``<tag>_indices.tif`` 下载到 run 缓存，返回本地路径；缺失 → None。

    缓存目录与波段解析共用 ``runs/<run_id>/_cog/<pair_name>/<tag>/``，按影像对
    与 tag 隔离，避免切换组时串文件。MinIO 不可用时回退本地 ``data/pair_*``。
    """
    _bucket, prefix, pair_name, tag = parse_cog_path(str(cog_path))
    try:
        client = get_client(StorageSettings.from_env())
        objects = [o for o in list_objects(client, COG_BUCKET, prefix=prefix)
                   if _is_cloud_index_object(o)]
        if objects:
            obj = sorted(objects)[0]
            dest_dir = RUNS_ROOT / run_id / "_cog" / pair_name / tag
            dest_dir.mkdir(parents=True, exist_ok=True)
            local = dest_dir / obj.rsplit("/", 1)[-1]
            if not local.exists() or local.stat().st_size == 0:
                client.fget_object(COG_BUCKET, obj, str(local))
            return local
    except Exception as exc:  # noqa: BLE001 - MinIO 不可用 → 回退本地
        logger.warning("云端指数解析失败（回退本地）：%s", exc)
    if pair_name and DATA_ROOT.exists():
        for pair_dir in sorted(p for p in DATA_ROOT.glob(f"{pair_name}*") if p.is_dir()):
            hits = sorted(pair_dir.glob(f"{tag}*{CLOUD_INDEX_SUFFIX}"))
            if hits:
                return hits[0]
    return None


def resolve_cloud_composite(cog_path: Optional[str], run_id: str,
                            tag: str) -> Optional[Path]:
    """把在线取数 cloud 模式已落盘的 ``<tag>_composite.png`` 取到 run 目录。

    与 :func:`resolve_cloud_indices` 同口径（MinIO 优先、本地回退），但真彩 PNG
    不参与栅格计算，只需复制到 ``runs/<run_id>/<tag>_composite.png`` 供产物清单
    与卷帘前端消费；缺失返回 None，由检测端现场补调 CDSE 兜底。
    """
    from app.service.common import run_dir

    if not cog_path:
        return None
    _bucket, prefix, pair_name, scene_tag = parse_cog_path(str(cog_path))
    dest = run_dir(run_id) / f"{tag}_composite.png"
    wanted = f"{scene_tag}_composite.png"
    try:
        client = get_client(StorageSettings.from_env())
        objects = [o for o in list_objects(client, COG_BUCKET, prefix=prefix)
                   if o.rsplit("/", 1)[-1].lower() == wanted.lower()]
        if objects:
            if not dest.exists() or dest.stat().st_size == 0:
                client.fget_object(COG_BUCKET, sorted(objects)[0], str(dest))
            return dest
    except Exception as exc:  # noqa: BLE001 - MinIO 不可用 → 回退本地
        logger.warning("云端真彩成品解析失败（回退本地）：%s", exc)
    if pair_name and DATA_ROOT.exists():
        for pair_dir in sorted(p for p in DATA_ROOT.glob(f"{pair_name}*") if p.is_dir()):
            hits = sorted(pair_dir.glob(wanted))
            if hits:
                import shutil
                shutil.copyfile(hits[0], dest)
                return dest
    return None

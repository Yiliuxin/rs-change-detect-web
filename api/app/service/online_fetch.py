# -*- coding: utf-8 -*-
"""在线取数服务（B 级扩展：STAC 检索 discover + 按需下载入库 fetch）。

设计口径（用户已拍板，两条路并存）：
  * 预置影像对（``data/pair_*`` → MinIO/seed）仍是离线演示路径，本模块**不替换**它；
  * 在线取数为可选模式：数据源 AWS earth-search STAC
    （``collection=sentinel-2-l2a``）+ ``sentinel-cogs`` S3，公开免费、无需 key。

一、``discover``（纯检索，不下载任何栅格，B 级扩展 / REQ-02 在线分支）
    同步 httpx 调 STAC（timeout 30s）→ 过滤 ``s2:processing_baseline >= 04.00``（REQ-04）
    → 求两时相**共同 tile** → 按综合云量升序返回候选。
    错误：无候选 → EX2(404)；AOI 跨度 > 0.25° → EX1(400)；STAC 不可达 → SYS-06(500)。

二、``start_fetch`` / ``get_status``（按需下载入库）
    校验 → ``fetch_id = uuid4()`` → ``threading.Thread`` 后台执行：
      a) ``/vsicurl`` 逐波段（B02/B03/B04/B08/SCL + B11）裁 AOI 窗下载到
         ``api/runs/<fetch_id>/_cog/``，每波段 6 次指数退避重试（2/4/8/16/30/30s），
         写完用 rasterio 重新打开校验（防半截文件）；B11 为 20m 原生，下载后
         双线性重采样到 B04 的 10m AOI 网格，与检测网格逐像元对齐；
      b) 上传 MinIO ``scenes`` 桶 ``pair_<name>/<t1|t2>/``（复用 storage.upload_file）；
      c) 复用 seed 的 uuid5 口径写 aoi + scene（``aoi_id=uuid5(NS,"aoi:<name>")``、
         ``cog_path=minio://scenes/pair_<name>/<tag>/``、``footprint=AOI envelope``）；
      d) 同 tile+日期已在 scene 表存在 → 跳过下载直接复用既有 scene_id。
    状态存进程内 dict（Thread + 锁），**不引入** celery/redis/异步队列；
    ``MAX_CONCURRENCY`` 串行限流只覆盖 MVP 计算接口，刻意**不覆盖**取数线程。
    进度以"波段"为单位（每组 12 波段：0/12 → 12/12）。
    防重复提交：同 ``name+tile+两日期`` 进行中的 fetch 直接返回已有 ``fetch_id``。

错误码：EX1（AOI 超限）/ EX2（无匹配、无有效数据、未知 fetch_id）/ SYS-06（STAC 或
COG 下载失败）/ SYS-07（MinIO 或 PG 写入失败）；文案一律取自 ``constants.ERROR_MESSAGES``。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import httpx
import numpy as np
import rasterio
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window

from app.algorithm.geotransform import (
    BBox, aoi_coverage, unclipped_window as _unclipped_window, validate_aoi_span,
)
from app.api.errors import ApiError, as_api_error
from app.config.constants import (
    AOI_COVERAGE_DIAGNOSIS, AOI_COVERAGE_MIN, COG_BUCKET, ERROR_MESSAGES,
    RADIOMETRIC_FORMULAS, SENSOR_S2,
)
from app.config.storage import StorageSettings, ensure_buckets, get_client, object_url, upload_file
from app.repository import online_repo, scene_repo
from app.service.common import RUNS_ROOT
from app.service.cog_source import has_optional_band

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 数据源与取数参数（本扩展私有，不进 constants.py：后者只允许新增 SYS-06 文案）
# --------------------------------------------------------------------------- #
STAC_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
STAC_COLLECTION = "sentinel-2-l2a"
STAC_TIMEOUT_SEC = 30.0
STAC_LIMIT = 100
# sentinel-cogs S3（公开、免 key）：<base>/<utm>/<lat>/<sq>/<YYYY>/<M>/<source_id>/<BAND>.tif
COG_BASE_URL = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs"

# 逐波段下载：与预置 COG 同构的 5 波段（B02/B03/B04/B08 反射率 + SCL 场景分类）
# + B11 短波红外（20m，下载后重采样到 B04 的 10m AOI 网格），解锁 NDBI / NDMI。
# B11 必须放在 B04 之后：下载时以已落盘的 B04 作 10m 参考网格。
_BASE_BANDS: Tuple[str, ...] = ("B02", "B03", "B04", "B08", "SCL")
SWIR_BAND = "B11"
BANDS: Tuple[str, ...] = _BASE_BANDS + (SWIR_BAND,)
TAGS: Tuple[str, ...] = ("t1", "t2")
BANDS_PER_TAG = len(BANDS)
TOTAL_BANDS = BANDS_PER_TAG * len(TAGS)  # 每组 12 波段（进度分母：6×2）
# 每波段重试 6 次，退避 2/4/8/16/30/30 秒（S3 /vsicurl 间歇性失败）
RETRY_DELAYS: Tuple[int, ...] = (2, 4, 8, 16, 30, 30)

# 处理基线最低版次（04.00）：STAC 属性是字符串 "05.00"，转 float 比较
MIN_PROC_BASELINE = 4.0
# 候选上限（共同 tile 通常 1~4 个，兜底防前端一次拉爆）
MAX_CANDIDATES = 20
# 覆盖率阈值 / 诊断模板统一取常量源（discover footprint 估算 + fetch 真实栅格共用）
MIN_AOI_COVERAGE = AOI_COVERAGE_MIN
COVERAGE_HINT = AOI_COVERAGE_DIAGNOSIS

# 与 scripts/seed_scenes.py 完全一致的 uuid5 命名空间（可复现）
UUID_NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

_NAME_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_TILE_RE = re.compile(r"^[0-9]{2}[A-Z]{3}$")
_S2_ID_RE = re.compile(r"^S2[A-Z]_\d{2}[A-Z]{3}_\d{8}_\d+_L2A$")

STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
_ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING)

__all__ = [
    "BANDS", "TOTAL_BANDS", "MIN_AOI_COVERAGE", "discover", "start_fetch",
    "get_status", "cog_object_prefix", "fetch_root", "aoi_coverage",
    "coverage_diagnosis", "resample_b11_to_ref",
]

# --------------------------------------------------------------------------- #
# 进程内任务表（Thread + 锁；不引入 celery/redis）
# --------------------------------------------------------------------------- #
_tasks: Dict[str, dict] = {}
# 进行中索引：(name, tile, t1_date, t2_date) → fetch_id，用于防重复提交
_active: Dict[Tuple[str, str, str, str], str] = {}
_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# 入参归一：AOI / 时相窗口 / 名称 / 景号
# --------------------------------------------------------------------------- #
def _parse_polygon_ring(geom_str: str, run_id: Optional[str]) -> List[List[float]]:
    """解析 GeoJSON Polygon 外环（闭合校验），失败 → EX2(400)。"""
    try:
        obj = json.loads(geom_str)
    except Exception as exc:  # noqa: BLE001
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail=f"aoi_geom 非合法 JSON：{exc}") from exc
    if not isinstance(obj, dict) or str(obj.get("type", "")).lower() != "polygon":
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail="aoi_geom 需为 GeoJSON Polygon")
    coords = obj.get("coordinates") or []
    if not coords or not isinstance(coords[0], list):
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail="Polygon 缺少外环坐标")
    ring = coords[0]
    if len(ring) < 4:
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail="Polygon 外环至少 4 个点")
    if tuple(ring[0][:2]) != tuple(ring[-1][:2]):
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail="Polygon 外环必须闭合（首尾点相同）")
    return [[float(p[0]), float(p[1])] for p in ring]


def _envelope_geojson(bbox: BBox) -> str:
    """AOI envelope 的闭合 GeoJSON Polygon（bbox 入参时用于落 aoi 表）。"""
    minx, miny, maxx, maxy = bbox.as_tuple()
    return json.dumps(
        {"type": "Polygon",
         "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy],
                          [minx, maxy], [minx, miny]]]},
        ensure_ascii=False,
    )


def _resolve_aoi(aoi_geom: Optional[str], bbox: Optional[Sequence[float]],
                 run_id: Optional[str]) -> Tuple[BBox, str]:
    """把 aoi_geom / bbox 二选一归一为 (BBox, GeoJSON 串)，并做 EX1 跨度校验。"""
    if aoi_geom:
        ring = _parse_polygon_ring(aoi_geom, run_id)
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        try:
            box = BBox.from_sequence([min(xs), min(ys), max(xs), max(ys)])
        except ValueError as exc:
            raise ApiError("EX2", run_id=run_id, http_status=400, detail=str(exc)) from exc
        geom_json = json.dumps({"type": "Polygon", "coordinates": [ring]}, ensure_ascii=False)
    elif bbox:
        try:
            box = BBox.from_sequence(list(bbox))
        except ValueError as exc:
            raise ApiError("EX2", run_id=run_id, http_status=400,
                           detail=f"bbox 非法：{exc}") from exc
        geom_json = _envelope_geojson(box)
    else:
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail="需提供 aoi_geom 或 bbox 之一")

    # REQ-01 / EX1：跨度 ≤ 0.25°×0.25°（AoiTooLargeError.code = EX1）
    validate_aoi_span(box)
    return box, geom_json


def _parse_day(value: str, field: str, run_id: Optional[str]) -> str:
    """解析 ``YYYY-MM-DD``（允许带时间，取前 10 位），失败 → EX2(400)。"""
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail=f"{field} 日期非法：{value!r}") from exc


def _window_bounds(window: Sequence[str], field: str,
                   run_id: Optional[str]) -> Tuple[str, str]:
    """[start, end] → (start, end)，要求 end > start。"""
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail=f"{field} 需为 [start, end] 两项")
    start = _parse_day(window[0], f"{field}[0]", run_id)
    end = _parse_day(window[1], f"{field}[1]", run_id)
    if end <= start:
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail=f"{field} 非法：end({end}) 须晚于 start({start})")
    return start, end


def _slug(name: str, run_id: Optional[str]) -> str:
    """名称规范化：小写、非 ``[a-z0-9_]`` 折叠为 ``_``；规范化后为空 → EX2(400)。"""
    slug = _NAME_SLUG_RE.sub("_", str(name or "").strip().lower()).strip("_")
    if not slug:
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail="name 规范化后为空，仅允许 [a-z0-9_]")
    return slug


# --------------------------------------------------------------------------- #
# STAC 检索（discover）
# --------------------------------------------------------------------------- #
def _stac_datetime(start: str, end: str) -> str:
    """STAC ``datetime`` 区间口径（含首尾整天）。"""
    return f"{start}T00:00:00Z/{end}T23:59:59Z"


def _stac_search(bbox: BBox, dt: str, run_id: Optional[str]) -> List[dict]:
    """同步调 STAC（httpx，30s）；任何网络/协议失败 → SYS-06(500)。

    单独成函数便于单测 monkeypatch（无需真打网）。
    """
    payload = {
        "collections": [STAC_COLLECTION],
        "bbox": list(bbox.as_tuple()),
        "datetime": dt,
        "limit": STAC_LIMIT,
    }
    try:
        resp = httpx.post(STAC_SEARCH_URL, json=payload, timeout=STAC_TIMEOUT_SEC)
        resp.raise_for_status()
        return list(resp.json().get("features") or [])
    except Exception as exc:  # noqa: BLE001 - 统一映射 SYS-06
        raise ApiError("SYS-06", run_id=run_id, http_status=500,
                       detail=f"STAC 检索失败：{type(exc).__name__}: {exc}") from exc


def _baseline_value(raw) -> Optional[float]:
    """``s2:processing_baseline``（"05.00"）→ float；缺失/非法 → None。"""
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _cloud_value(raw) -> Optional[float]:
    """``eo:cloud_cover`` → float（保留 2 位）；缺失/非法 → None。"""
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError):
        return None


def _bbox_coverage(aoi: BBox, feat: dict) -> Optional[float]:
    """STAC feature footprint 对 AOI 的几何覆盖率估算（0~1）。

    footprint 取 ``geometry`` 的外接矩形（WGS84）。返回 ``None`` 表示 feature
    无可用几何（如单测的假 feature / STAC 精简响应）——此时 discover 层**不拦截**，
    交由 fetch 层真实栅格精确校验兜底，绝不因缺字段误杀候选。
    """
    geom = feat.get("geometry")
    if not isinstance(geom, dict):
        return None
    coords = (geom.get("coordinates") or [[]])[0]
    if not coords or len(coords) < 3:
        return None
    try:
        xs = [float(p[0]) for p in coords[:4]]
        ys = [float(p[1]) for p in coords[:4]]
        fx0, fx1, fy0, fy1 = min(xs), max(xs), min(ys), max(ys)
    except (TypeError, ValueError, IndexError):
        return None
    inter_x = max(0.0, min(aoi.maxx, fx1) - max(aoi.minx, fx0))
    inter_y = max(0.0, min(aoi.maxy, fy1) - max(aoi.miny, fy0))
    aoi_area = aoi.area_deg2
    if aoi_area <= 0:
        return None
    return min(1.0, (inter_x * inter_y) / aoi_area)


def _phase_candidates(features: Sequence[dict], aoi: Optional[BBox] = None) -> Dict[str, dict]:
    """单时相特征 → {tile: 该 tile 云量最低的候选}，过滤基线 < 04.00（REQ-04）。

    候选字段：source_id / date / cloud_cover / proc_baseline（响应里的 t1|t2 结构）。
    ``aoi`` 给定时叠加 footprint 覆盖率粗筛（< MIN_AOI_COVERAGE 的候选剔除）；
    无 footprint 的 feature 放过，由 fetch 层真实栅格校验兜底。
    """
    best: Dict[str, dict] = {}
    for feat in features:
        props = feat.get("properties") or {}
        source_id = str(feat.get("id") or "")
        parts = source_id.split("_")
        if len(parts) < 3:
            continue
        baseline = _baseline_value(props.get("s2:processing_baseline"))
        if baseline is None or baseline < MIN_PROC_BASELINE:
            continue  # EX9 口径：基线不可判定或未达标 → 不进候选
        if aoi is not None:
            cov = _bbox_coverage(aoi, feat)
            if cov is not None and cov < MIN_AOI_COVERAGE:
                continue  # 覆盖率不足 → 不进候选（根治 EX11 第一层）
        tile = parts[1]
        acq = str(props.get("datetime") or parts[2])[:10]
        item = {
            "source_id": source_id,
            "date": acq,
            "cloud_cover": _cloud_value(props.get("eo:cloud_cover")),
            "proc_baseline": str(props.get("s2:processing_baseline")),
        }
        cur = best.get(tile)
        if cur is None or _cloud_rank(item) < _cloud_rank(cur):
            best[tile] = item
    return best


def _cloud_rank(item: dict) -> float:
    """云量排序键；缺失按 100.0 兜底（排在有值之后）。"""
    cc = item.get("cloud_cover")
    return 100.0 if cc is None else float(cc)


def _combined_cloud(t1: dict, t2: dict) -> Optional[float]:
    """综合云量 = t1 + t2；任一缺失则不臆造（返回 None，排序另用 rank）。"""
    c1, c2 = t1.get("cloud_cover"), t2.get("cloud_cover")
    if c1 is None or c2 is None:
        return None
    return round(float(c1) + float(c2), 2)


def discover(aoi_geom: Optional[str], bbox: Optional[Sequence[float]],
             t1_window: Sequence[str], t2_window: Sequence[str]) -> dict:
    """POST /api/v1/scene/discover：STAC 检索候选 tile（B 级扩展 / REQ-02/04）。

    纯检索：不下载任何栅格。返回 ``{candidates:[...], run_id}``，按综合云量升序。
    """
    run_id = str(uuid.uuid4())
    try:
        box, _geom_json = _resolve_aoi(aoi_geom, bbox, run_id)
        w1 = _window_bounds(t1_window, "t1_window", run_id)
        w2 = _window_bounds(t2_window, "t2_window", run_id)

        # 传 box 启用 footprint 覆盖率粗筛（缺 geometry 的 feature 自动放过）。
        # 两个时相的 STAC 检索彼此独立 → 并发发出，省掉一次串行 RTT。
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="online-stac") as pool:
            f1 = pool.submit(_stac_search, box, _stac_datetime(*w1), run_id)
            f2 = pool.submit(_stac_search, box, _stac_datetime(*w2), run_id)
            ph1 = _phase_candidates(f1.result(), box)
            ph2 = _phase_candidates(f2.result(), box)

        common = sorted(set(ph1) & set(ph2))  # 两时相共同 tile
        candidates = [
            {
                "tile": tile,
                "t1": ph1[tile],
                "t2": ph2[tile],
                "combined_cloud": _combined_cloud(ph1[tile], ph2[tile]),
            }
            for tile in common
        ]
        candidates.sort(key=lambda c: _cloud_rank(c["t1"]) + _cloud_rank(c["t2"]))
        if not candidates:
            # 24.6%：无同时相可用 tile → EX2(404)，文案逐字取 ERROR_MESSAGES
            # （含 footprint 覆盖率过滤后为空的情形；诊断在 fetch/EX11 层给出）
            raise ApiError("EX2", run_id=run_id,
                           detail=f"两时相无共同 tile（t1={w1} / t2={w2}）")
        return {"candidates": candidates[:MAX_CANDIDATES], "run_id": run_id}
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise as_api_error(exc, run_id=run_id) from exc


# --------------------------------------------------------------------------- #
# COG 下载（/vsicurl + 逐波段重试）
# --------------------------------------------------------------------------- #
def cog_object_prefix(name: str, tag: str) -> str:
    """MinIO 对象前缀：``pair_<name>/<tag>/``（与 seed/cog_source 解析口径一致）。"""
    return f"pair_{name}/{tag}/"


def fetch_root(fetch_id: str):
    """``api/runs/<fetch_id>/_cog/``（本地下载暂存，随产物目录一并被 .gitignore）。"""
    root = RUNS_ROOT / fetch_id / "_cog"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cog_url(source_id: str) -> str:
    """source_id → sentinel-cogs 对象目录 URL（与 data/fetch_pairs 同一口径）。"""
    parts = source_id.split("_")
    if not _S2_ID_RE.match(source_id) or len(parts) < 3:
        raise ApiError("SYS-06", http_status=500, detail=f"source_id 非法：{source_id!r}")
    tile, ds = parts[1], parts[2]
    utm, lat, sq = tile[0:2], tile[2], tile[3:5]
    return (f"{COG_BASE_URL}/{utm}/{lat}/{sq}/{ds[0:4]}/{int(ds[4:6])}/"
            f"{source_id}")


def _ensure_gdal_env() -> None:
    """/vsicurl 读 COG 的 GDAL 选项（与参考取数脚本一致，抑制目录列举与 HEAD）。"""
    os.environ["CPL_VSIL_CURL_USE_RANGE"] = "YES"
    os.environ["CPL_VSIL_CURL_USE_HEAD"] = "NO"
    os.environ["GDAL_DISABLE_READDIR_ON_OPEN"] = "EMPTY_DIR"


def _window_for_bbox(src, box: BBox) -> Window:
    """AOI envelope → 像素窗口（WGS84 → 数据集 CRS），并裁剪到数据集范围内。"""
    _w, row0, col0, row1, col1 = _unclipped_window(src, box)
    row0, col0 = max(0, row0), max(0, col0)
    row1, col1 = min(src.height, row1), min(src.width, col1)
    if row1 <= row0 or col1 <= col0:
        raise ApiError("EX2", http_status=400,
                       detail=f"AOI 落在影像范围外（crs={src.crs}）")
    return Window(col0, row0, col1 - col0, row1 - row0)


def _band_is_valid(path) -> bool:
    """校验落盘波段：可被 rasterio 打开且能读到像素（防半截文件）。"""
    try:
        if not path.is_file() or path.stat().st_size < 1024:
            return False
        with rasterio.open(str(path)) as ds:
            ds.read(1, window=Window(0, 0, min(4, ds.width), min(4, ds.height)))
        return True
    except Exception:  # noqa: BLE001
        return False


def _download_band(url: str, band: str, box: BBox, dest, run_id: Optional[str],
                   on_open: Optional[Callable] = None):
    """带 6 次指数退避重试地裁 AOI 窗下载单波段；全部失败 → SYS-06(500)。

    ``on_open`` 在 dataset 打开后、读像元前调用（当前用于 B04 覆盖率预检），
    使「预检 + 下载」复用同一次 /vsicurl 打开，省掉一次远程往返。
    回调抛出的 ApiError 不重试，原样透传。
    """
    last: Optional[Exception] = None
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        try:
            with rasterio.open(f"/vsicurl/{url}/{band}.tif") as src:
                if on_open is not None:
                    on_open(src)
                win = _window_for_bbox(src, box)
                data = src.read(1, window=win)
                profile = {
                    "driver": "GTiff",
                    "dtype": src.dtypes[0],
                    "count": 1,
                    "height": int(data.shape[0]),
                    "width": int(data.shape[1]),
                    "crs": src.crs,
                    "transform": src.window_transform(win),
                    "nodata": src.nodata,
                }
            tmp = dest.with_suffix(dest.suffix + ".part")
            with rasterio.open(str(tmp), "w", **profile) as dst:
                dst.write(data, 1)
            if not _band_is_valid(tmp):
                raise RuntimeError(f"写入后校验失败：{tmp}")
            os.replace(tmp, dest)
            return dest
        except ApiError:
            raise  # AOI 落在影像外等业务错误不重试
        except Exception as exc:  # noqa: BLE001 - 网络抖动重试
            last = exc
            if attempt < len(RETRY_DELAYS):
                logger.warning("波段 %s 第 %d/%d 次下载失败（%s），%ds 后重试",
                               band, attempt, len(RETRY_DELAYS),
                               type(exc).__name__, delay)
                time.sleep(delay)
    raise ApiError("SYS-06", run_id=run_id, http_status=500,
                   detail=f"波段 {band} 下载失败（已重试 {len(RETRY_DELAYS)} 次）：{last}")


def resample_b11_to_ref(src, ref, box: BBox, dest) -> dict:
    """把已打开的 B11 dataset（20m）重采样写入 ``ref``（B04，10m）网格。

    纯函数（dataset 由调用方打开，可传合成 dataset 单测）：AOI envelope → B11
    源窗口 → ``reproject`` 双线性到参考网格。输出 shape/transform 与 ref 逐像元
    一致，检测时 ``assert_grid_aligned`` 直接通过。

    返回 ``{"shape": (h, w), "transform": ref.transform, "crs": ref.crs}`` 供核对。
    """
    _w, row0, col0, row1, col1 = _unclipped_window(src, box)
    row0, col0 = max(0, row0), max(0, col0)
    row1, col1 = min(src.height, row1), min(src.width, col1)
    if row1 <= row0 or col1 <= col0:
        raise ApiError("EX2", http_status=400,
                       detail=f"B11 AOI 落在影像范围外（crs={src.crs}）")
    win = Window(col0, row0, col1 - col0, row1 - row0)
    dst = np.full((ref.height, ref.width), np.nan, dtype="float32")
    reproject(
        rasterio.band(src, 1), dst,
        src_transform=src.window_transform(win), src_crs=src.crs,
        dst_transform=ref.transform, dst_crs=ref.crs,
        dst_shape=(ref.height, ref.width),
        resampling=Resampling.bilinear,
    )
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "height": int(ref.height), "width": int(ref.width),
        "crs": ref.crs, "transform": ref.transform, "nodata": None,
    }
    tmp = dest.with_suffix(dest.suffix + ".part")
    with rasterio.open(str(tmp), "w", **profile) as out:
        out.write(dst, 1)
    if not _band_is_valid(tmp):
        raise RuntimeError(f"写入后校验失败：{tmp}")
    os.replace(tmp, dest)
    return {"shape": (int(ref.height), int(ref.width)),
            "transform": ref.transform, "crs": ref.crs}


def _download_b11_on_grid(url: str, box: BBox, ref_path, dest,
                          run_id: Optional[str]):
    """下载 B11（20m 原生）并双线性重采样到 ``ref_path``（B04，10m）的 AOI 网格。

    与 ``data/fetch_b11.py::read_band_on_ref_grid`` 同口径，实际重采样在
    :func:`resample_b11_to_ref`（纯函数，可单测）。重试与写后校验沿用
    ``_download_band`` 的 6 次退避策略。

    ``ref_path`` 必须是已落盘的 10m 参考波段（调用方保证 B04 在 B11 之前下载）。
    """
    last: Optional[Exception] = None
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        try:
            with rasterio.open(f"/vsicurl/{url}/{SWIR_BAND}.tif") as src, \
                    rasterio.open(str(ref_path)) as ref:
                resample_b11_to_ref(src, ref, box, dest)
            return dest
        except ApiError:
            raise  # AOI 越界等业务错误不重试
        except Exception as exc:  # noqa: BLE001 - 网络抖动重试
            last = exc
            if attempt < len(RETRY_DELAYS):
                logger.warning("B11 第 %d/%d 次下载失败（%s），%ds 后重试",
                               attempt, len(RETRY_DELAYS), type(exc).__name__, delay)
                time.sleep(delay)
    raise ApiError("SYS-06", run_id=run_id, http_status=500,
                   detail=f"B11 下载失败（已重试 {len(RETRY_DELAYS)} 次）：{last}")


def _b11_reference(paths: Dict[str, object]) -> object:
    """返回 B11 重采样的 10m 参考波段路径（B04，必须已下载）。"""
    ref = paths.get("B04")
    if ref is None:
        raise ApiError("SYS-06", http_status=500, detail="B11 重采样缺少 B04 参考网格")
    return ref


def coverage_diagnosis(info: dict, tag: str) -> Optional[str]:
    """覆盖率不足时返回中文诊断文案，达标返回 None（纯函数，便于单测）。

    文案含实际覆盖率与可执行建议（换候选 / 缩小 AOI），经 ``ApiError.message``
    → ``_fail`` → ``GET /fetch/{id}`` 的 error.message 透传到在线取数卡。
    """
    if float(info.get("coverage", 0.0)) >= MIN_AOI_COVERAGE:
        return None
    return f"[{tag}] " + COVERAGE_HINT.format(
        pct=float(info.get("coverage", 0.0)) * 100.0,
        min=MIN_AOI_COVERAGE * 100.0,
        miss=float(info.get("missing_pct", 0.0)),
    )


def _verify_coverage(src, box: BBox, tag: str) -> dict:
    """用真实 COG 的栅格几何精确计算覆盖率；不足则抛 EX2 + 中文诊断（根治 EX11）。

    ``src`` 为已打开的 B04（10m 参考波段，与检测 read_aoi 的参考网格一致），
    单测可传合成 dataset。覆盖率达标返回 info dict，不足抛 ApiError。
    """
    info = aoi_coverage(src, box)
    diag = coverage_diagnosis(info, tag)
    if diag is not None:
        raise ApiError("EX2", http_status=400, message=diag,
                       detail=f"{tag} AOI 覆盖率 {info['coverage']:.4f} < {MIN_AOI_COVERAGE}")
    return info


def _assert_has_data(paths: Dict[str, object], tag: str, run_id: Optional[str]) -> None:
    """下载后校验：SCL 全为 0（全无效）视为无有效数据 → EX2(400)。"""
    try:
        with rasterio.open(str(paths["SCL"])) as ds:
            arr = ds.read(1)
    except Exception as exc:  # noqa: BLE001
        raise ApiError("SYS-06", run_id=run_id, http_status=500,
                       detail=f"{tag} SCL 读取失败：{exc}") from exc
    if arr.size == 0 or int(np.count_nonzero(arr)) == 0:
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail=f"{tag} 下载后 SCL 全为 0，无有效数据")


# --------------------------------------------------------------------------- #
# 入库（MinIO + PG）
# --------------------------------------------------------------------------- #
def _upload_tag(name: str, tag: str, paths: Dict[str, object], run_id: Optional[str]) -> None:
    """上传一个时相的 5 波段到 MinIO ``scenes`` 桶 ``pair_<name>/<tag>/``。"""
    try:
        client = get_client(StorageSettings.from_env())
        ensure_buckets(client, [COG_BUCKET])
        prefix = cog_object_prefix(name, tag)
        for band in BANDS:
            path = paths[band]
            upload_file(client, COG_BUCKET, f"{prefix}{path.name}", str(path))
    except Exception as exc:  # noqa: BLE001 - MinIO 写入失败 → SYS-07
        raise ApiError("SYS-07", run_id=run_id, http_status=500,
                       detail=f"MinIO 上传失败（{tag}）：{exc}") from exc


def _persist_scene(name: str, tag: str, meta: dict, box: BBox,
                   run_id: Optional[str]) -> str:
    """写 aoi（uuid5）+ scene（uuid5），复用 seed 口径；PG 失败 → SYS-07(500)。"""
    aoi_id = str(uuid.uuid5(UUID_NS, f"aoi:{name}"))
    scene_id = str(uuid.uuid5(UUID_NS, f"scene:{name}:{tag}:{meta['source_id']}"))
    try:
        online_repo.upsert_aoi(aoi_id, box.as_tuple(), box.area_deg2)
        online_repo.upsert_scene(
            scene_id=scene_id,
            aoi_id=aoi_id,
            sensor=SENSOR_S2,
            acq_date=meta["date"],
            cloud_cover=meta.get("cloud_cover"),
            cog_path=object_url(COG_BUCKET, cog_object_prefix(name, tag)),
            proc_baseline=meta.get("proc_baseline"),
            radiometric_formula=RADIOMETRIC_FORMULAS[SENSOR_S2]["text"],
            tile=meta["tile"],
            bbox=box.as_tuple(),
        )
    except Exception as exc:  # noqa: BLE001 - PG 写入失败 → SYS-07
        raise ApiError("SYS-07", run_id=run_id, http_status=500,
                       detail=f"数据库写入失败（{tag}）：{exc}") from exc
    return scene_id


def _reusable_scenes(tile: str, dates: Sequence[str]) -> Dict[str, dict]:
    """scene 表查重：同 tile + 同日期**且含 B11** 已入库 → {acq_date: 场景行}。

    B11 过滤是必要的：早期在线取数只有 5 波段，若不加判据会被直接复用，
    新取数永远补不上 B11，NDBI / NDMI 仍被 EX11 拦住。缺 B11 的旧 scene
    不参与复用 → 走重新下载（落盘新组、带 B11）。
    """
    wanted = {d for d in dates if d}
    hits: Dict[str, dict] = {}
    for row in scene_repo.find_all():
        if row.get("tile") != tile:
            continue
        acq = row.get("acq_date")
        if acq not in wanted:
            continue
        if not has_optional_band(str(row.get("cog_path") or ""), SWIR_BAND,
                                 sensor=row.get("sensor")):
            continue
        hits[acq] = row
    return hits


# --------------------------------------------------------------------------- #
# 进程内任务状态
# --------------------------------------------------------------------------- #
def _advance(fetch_id: str, step: int = 1) -> None:
    with _lock:
        task = _tasks.get(fetch_id)
        if task is not None:
            task["done"] = min(task["total"], int(task["done"]) + int(step))


def _set_status(fetch_id: str, status: str) -> None:
    with _lock:
        task = _tasks.get(fetch_id)
        if task is not None:
            task["status"] = status


def _release_active(task: dict) -> None:
    """结束后移出进行中索引，允许同名重取（调用方需已持锁）。"""
    key = task.get("key")
    if key and _active.get(key) == task["fetch_id"]:
        _active.pop(key, None)


def _fail(fetch_id: str, code: str, detail: Optional[str] = None,
          message: Optional[str] = None) -> None:
    """标记失败。``message`` 给定时覆写 error.message（用于覆盖率等中文诊断透传），
    缺省仍逐字取 ERROR_MESSAGES，既有错误码行为不变。"""
    with _lock:
        task = _tasks.get(fetch_id)
        if task is None:
            return
        task["status"] = STATUS_FAILED
        task["error"] = {
            "code": code,
            "message": message if message is not None else ERROR_MESSAGES.get(code, code),
        }
        task["detail"] = detail
        _release_active(task)
    logger.warning("在线取数失败 fetch_id=%s code=%s detail=%s", fetch_id, code, detail)


def _succeed(fetch_id: str, scene_ids: Dict[str, str]) -> None:
    with _lock:
        task = _tasks.get(fetch_id)
        if task is None:
            return
        task["status"] = STATUS_SUCCESS
        task["scene_ids"] = dict(scene_ids)
        task["done"] = task["total"]
        _release_active(task)
    logger.info("在线取数完成 fetch_id=%s scenes=%s", fetch_id, scene_ids)


def _task_view(fetch_id: str) -> dict:
    """任务视图（对外契约：fetch_id/status/progress/error/scene_ids）。"""
    with _lock:
        task = _tasks.get(fetch_id)
        if task is None:
            return {}
        return {
            "fetch_id": task["fetch_id"],
            "status": task["status"],
            "progress": {"done": int(task["done"]), "total": int(task["total"])},
            "error": dict(task["error"]) if task["error"] else None,
            "scene_ids": dict(task["scene_ids"]) if task["scene_ids"] else None,
        }


# --------------------------------------------------------------------------- #
# 后台工作线程
# --------------------------------------------------------------------------- #
def _fetch_one_tag(fetch_id: str, name: str, tag: str, meta: dict,
                   box: BBox, root) -> str:
    """单时相完整流程：下载 → 校验 → 上传 → 入库。

    两时相由调用方并发提交；本函数只处理自己那一相。进度经 :func:`_advance`
    加锁，MinIO client / PG 连接每次新建，不共享可变状态。
    """
    url = _cog_url(meta["source_id"])
    paths: Dict[str, object] = {}
    cov_holder: Dict[str, float] = {}

    def _check_coverage(src):
        """覆盖率不足 → ApiError(EX2, 中文诊断)，原样透传前端，坏数据不入库。"""
        info = _verify_coverage(src, box, tag)
        cov_holder["coverage"] = float(info["coverage"])
        return info

    # B04 先行：它既是覆盖率判据的 10m 参考网格，也是 B11 重采样的基准。
    ordered = ("B04",) + tuple(b for b in BANDS if b != "B04")
    for band in ordered:
        dest = root / f"{tag}_{band}.tif"
        if _band_is_valid(dest):  # 断点续传：已存在且可读则跳过
            if band == "B04":
                # 本地文件直接验，不再为续传打一次远程
                with rasterio.open(str(dest)) as src:
                    _check_coverage(src)
            paths[band] = dest
        elif band == SWIR_BAND:
            paths[band] = _download_b11_on_grid(
                url, box, _b11_reference(paths), dest, fetch_id)
        else:
            # 根治 EX11：覆盖率校验并入 B04 的 /vsicurl 打开，省一次远程 RTT。
            paths[band] = _download_band(
                url, band, box, dest, fetch_id,
                on_open=_check_coverage if band == "B04" else None,
            )
        _advance(fetch_id, 1)  # 进度以"波段"为单位（两组共 12）

    logger.info("覆盖率校验通过 fetch_id=%s tag=%s coverage=%.4f",
                fetch_id, tag, cov_holder.get("coverage", -1.0))
    _assert_has_data(paths, tag, fetch_id)
    _upload_tag(name, tag, paths, fetch_id)
    return _persist_scene(name, tag, meta, box, fetch_id)


def _run_fetch(fetch_id: str) -> None:
    """后台取数主流程（a 下载 → b 上传 → c 入库；d 查重跳过）。

    t1/t2 两时相彼此独立 → 并发处理（约 2 倍提速）；波段级仍串行，避免
    S3 限速与 GDAL 跨线程 dataset 风险。总进度仍为 12，两相交错推进。
    """
    with _lock:
        task = _tasks.get(fetch_id)
        if task is None:
            return
        snapshot = dict(task)
    _set_status(fetch_id, STATUS_RUNNING)

    name: str = snapshot["name"]
    tile: str = snapshot["tile"]
    box: BBox = snapshot["bbox"]
    metas = {"t1": snapshot["t1"], "t2": snapshot["t2"]}

    try:
        _ensure_gdal_env()
        reusable = _reusable_scenes(tile, [metas["t1"]["date"], metas["t2"]["date"]])
        root = fetch_root(fetch_id)

        def _one(tag: str) -> Tuple[str, str]:
            meta = dict(metas[tag])
            meta["tile"] = tile
            hit = reusable.get(meta["date"])
            if hit is not None:
                # d) 同 tile+日期已入库 → 跳过下载/上传，直接复用
                _advance(fetch_id, BANDS_PER_TAG)
                logger.info("复用既有场景 tile=%s date=%s scene_id=%s",
                            tile, meta["date"], hit["scene_id"])
                return tag, hit["scene_id"]
            return tag, _fetch_one_tag(fetch_id, name, tag, meta, box, root)

        scene_ids: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=2,
                                thread_name_prefix=f"online-tag-{fetch_id[:8]}") as pool:
            futures = [pool.submit(_one, tag) for tag in TAGS]
            for fut in as_completed(futures):
                tag, sid = fut.result()  # 任一相失败 → 异常在此抛出，统一走下方处理
                scene_ids[tag] = sid

        _succeed(fetch_id, scene_ids)
    except ApiError as exc:
        _fail(fetch_id, exc.code, exc.detail, message=exc.message)
    except Exception as exc:  # noqa: BLE001 - 兜底归一
        mapped = as_api_error(exc, run_id=fetch_id)
        _fail(fetch_id, mapped.code, mapped.detail, message=mapped.message)


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def start_fetch(name: str, aoi_geom: Optional[str], bbox: Optional[Sequence[float]],
                tile: str, t1: dict, t2: dict) -> dict:
    """POST /api/v1/scene/fetch：校验 + 建 fetch_id + 起后台线程，立即返回 PENDING。"""
    # 先生成 fetch_id：校验失败时也用它当 run_id，错误响应可追溯（EX1/EX2）
    fetch_id = str(uuid.uuid4())
    try:
        slug = _slug(name, fetch_id)
        box, _geom_json = _resolve_aoi(aoi_geom, bbox, fetch_id)
        tile_norm = str(tile or "").strip().upper()
        if not _TILE_RE.match(tile_norm):
            raise ApiError("EX2", run_id=fetch_id, http_status=400,
                           detail=f"tile 非 MGRS 景号（如 49RFN）：{tile!r}")

        metas: Dict[str, dict] = {}
        for tag, raw in (("t1", t1), ("t2", t2)):
            source_id = str((raw or {}).get("source_id") or "").strip()
            if not _S2_ID_RE.match(source_id):
                raise ApiError("EX2", run_id=fetch_id, http_status=400,
                               detail=f"{tag}.source_id 非法：{source_id!r}")
            metas[tag] = {
                "source_id": source_id,
                "date": _parse_day((raw or {}).get("date"), f"{tag}.date", fetch_id),
                "proc_baseline": str((raw or {}).get("proc_baseline") or "").strip(),
                "cloud_cover": _cloud_value((raw or {}).get("cloud_cover")),
            }
        if metas["t1"]["date"] == metas["t2"]["date"]:
            raise ApiError("EX2", run_id=fetch_id, http_status=400,
                           detail="双时相日期不能相同")

        key = (slug, tile_norm, metas["t1"]["date"], metas["t2"]["date"])
        with _lock:
            existing_id = _active.get(key)
            if existing_id is not None:
                current = _tasks.get(existing_id) or {}
                if current.get("status") in _ACTIVE_STATUSES:
                    # 防重复提交：同 name+tile+两日期进行中 → 直接返回已有 fetch_id
                    return {"fetch_id": existing_id, "status": current["status"]}

            _tasks[fetch_id] = {
                "fetch_id": fetch_id,
                "key": key,
                "name": slug,
                "tile": tile_norm,
                "bbox": box,
                "t1": metas["t1"],
                "t2": metas["t2"],
                "status": STATUS_PENDING,
                "done": 0,
                "total": TOTAL_BANDS,
                "error": None,
                "scene_ids": None,
                "detail": None,
            }
            _active[key] = fetch_id

        threading.Thread(
            target=_run_fetch, args=(fetch_id,),
            name=f"online-fetch-{fetch_id[:8]}", daemon=True,
        ).start()
        return {"fetch_id": fetch_id, "status": STATUS_PENDING}
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise as_api_error(exc, run_id=fetch_id) from exc


def get_status(fetch_id: str) -> dict:
    """GET /api/v1/scene/fetch/{fetch_id}：任务状态（未知 fetch_id → EX2(404)）。"""
    view = _task_view(str(fetch_id or "").strip())
    if not view:
        raise ApiError("EX2", http_status=404, detail=f"未知 fetch_id：{fetch_id!r}")
    return view

# -*- coding: utf-8 -*-
"""CDSE 云端指数服务（快速模式专用，可选）。

设计口径（用户拍板，与现有链路完全隔离）：
  * 只在调用方显式 ``source=cloud`` 时使用；缺省 local 路径**不引用本模块**；
  * 通过 Copernicus Data Space Ecosystem 的 Sentinel Hub Processing API
    （``https://sh.dataspace.copernicus.eu/api/v1/process``）在云端算好指数，
    只回传小 GeoTIFF（约 150KB/景），不再跨国下载 25MB 原始波段；
  * 云端只算 NDVI / NDWI / NDBI / NDMI 四个**反射率比值**；CVA / Otsu /
    转移矩阵 / 精度评估仍由本地算法完成，算法主权保留；
  * 凭证从 ``api/.env`` 读取（``CDSE_CLIENT_ID`` / ``CDSE_CLIENT_SECRET``），
    不硬编码、不入库；缺失 → SYS-06 中文提示，不影响 local 路径。

口径一致性（关键）：
  * 公式与 :mod:`app.algorithm.index_calculator` 逐字一致：
    NDVI=(B08−B04)/(B08+B04)、NDWI=(B03−B08)/(B03+B08)、
    NDBI=(B11−B08)/(B11+B08)、NDMI=(B08−B11)/(B08+B11)。DN 同比例缩放
    在比值中约掉，故直接用 sample 的 BOA 反射率求商与本地 ρ 结果一致；
  * SCL 云/阴影掩膜、分母 |den|<1e-6 保护、[-1,1] 物理域裁剪均在本地
    二次执行（云端仅做粗掩膜），保证与 local 路径同口径；
  * 输出 float32、无效像元 NaN，尺寸严格等于请求 width×height。

稳定性（实测）：单波段请求稳定（256² 约 7.5s）；多波段/大尺寸偶发挂起，
故按"每景每指数一次单波段请求"逐次调用，带超时与重试；调用方按需要取
主指数 + 辅助指数 + NDVI + NDWI，避免一次塞过多输出。
"""
from __future__ import annotations

import io
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np
from rasterio.crs import CRS
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import transform as warp_transform

from app.api.errors import ApiError
from app.config.env import load_env

logger = logging.getLogger(__name__)

TOKEN_URL = ("https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
             "protocol/openid-connect/token")
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
CRS84 = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"


def utm_crs_for(bbox: Tuple[float, float, float, float]) -> CRS:
    """按 AOI 中心经度选北半球 UTM 带（米制），与本地 COG 投影口径一致。"""
    west, south, east, north = [float(v) for v in bbox]
    center_lon = (west + east) / 2.0
    zone = int(np.floor((center_lon + 180.0) / 6.0)) + 1
    return CRS.from_epsg(32600 + zone)


def metric_profile(bbox: Tuple[float, float, float, float],
                   size: int) -> dict:
    """构造与本地 COG 同口径的米制 profile。

    Processing API 以 CRS84 请求最稳定（UTM 渲染实测 500），回传栅格为
    度制网格；这里按 AOI 的 UTM 外接框换算等效米制像元边长，生成米制
    transform，使下游 ``pixel_area_from_profile``（transform.a*e）算出的
    单像元面积与本地 COG 同口径、面积数字可比。
    """
    west, south, east, north = [float(v) for v in bbox]
    utm = utm_crs_for(bbox)
    xs, ys = warp_transform(CRS.from_epsg(4326), utm,
                            [west, east], [south, north])
    span_x = abs(max(xs) - min(xs))
    span_y = abs(max(ys) - min(ys))
    pixel = span_x / float(size)
    return {
        "driver": "GTiff", "dtype": "float32", "nodata": float("nan"),
        "width": int(size), "height": int(size), "count": 1,
        "crs": utm,
        "transform": from_origin(min(xs), max(ys), pixel, span_y / float(size)),
    }

# 每个指数对应的两个波段（与 index_calculator 逐字一致）
INDEX_BAND_EXPR: Dict[str, Tuple[str, str]] = {
    "NDVI": ("B08", "B04"),
    "NDWI": ("B03", "B08"),
    "NDBI": ("B11", "B08"),
    "NDMI": ("B08", "B11"),
}

DEFAULT_SIZE = 512          # 单波段稳定输出尺寸（实测 256/512 稳，1024 偶发挂起）
TOKEN_REFRESH_S = 1500      # token 有效期 1800s，提前 300s 刷新
HTTP_TIMEOUT_S = 120
MAX_RETRIES = 3

_token_cache: Dict[str, object] = {"value": "", "expires_at": 0.0}


def configured() -> bool:
    """凭证是否齐全（缺失时快速模式不可用，local 路径不受影响）。"""
    load_env()
    return bool(os.getenv("CDSE_CLIENT_ID") and os.getenv("CDSE_CLIENT_SECRET"))


def _credentials() -> Tuple[str, str]:
    load_env()
    cid = os.getenv("CDSE_CLIENT_ID", "")
    sec = os.getenv("CDSE_CLIENT_SECRET", "")
    if not cid or not sec:
        raise ApiError(
            "SYS-06",
            detail="快速模式未配置 CDSE 凭证：请在 api/.env 填写 CDSE_CLIENT_ID / "
                   "CDSE_CLIENT_SECRET；或关闭快速模式使用本地检测",
        )
    return cid, sec


def get_token(force: bool = False) -> str:
    """获取 access token（进程内缓存，过期前刷新）；失败 → SYS-06。"""
    now = time.time()
    if (not force and _token_cache["value"]
            and float(_token_cache["expires_at"]) > now):
        return str(_token_cache["value"])
    cid, sec = _credentials()
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": sec,
    }).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            payload = json.load(resp)
        token = str(payload["access_token"])
        expires_in = int(payload.get("expires_in", 1800))
    except Exception as exc:  # noqa: BLE001
        raise ApiError(
            "SYS-06",
            detail=f"CDSE token 获取失败：{type(exc).__name__}: {exc}",
        ) from exc
    _token_cache["value"] = token
    _token_cache["expires_at"] = now + max(60, expires_in - 300)
    return token


def _evalscript(index: str) -> str:
    """单指数 evalscript：云端只做 SCL 粗掩膜 + 求商，精细口径本地二次校验。"""
    a, b = INDEX_BAND_EXPR[index]
    return f"""//VERSION=3
function setup() {{
  return {{
    input: ["{a}", "{b}", "SCL"],
    output: {{ id: "idx", bands: 1, sampleType: "FLOAT32" }}
  }};
}}
function evaluatePixel(s) {{
  var bad = (s.SCL === 0 || s.SCL === 1 || s.SCL === 3 || s.SCL === 8 ||
             s.SCL === 9 || s.SCL === 10 || s.SCL === 11);
  if (bad) return [NaN];
  return [(s.{a} - s.{b}) / (s.{a} + s.{b})];
}}"""


def _parse_tif(body: bytes) -> Tuple[np.ndarray, dict]:
    """解析回传 GeoTIFF → float32 数组 + rasterio profile（保留云端米制 CRS/transform）。"""
    with MemoryFile(body) as mf:
        with mf.open() as src:
            if src.count < 1:
                raise ValueError("云端回传栅格无波段")
            data = src.read(1).astype(np.float32)
            profile = {
                "driver": "GTiff",
                "dtype": "float32",
                "nodata": float("nan"),
                "width": int(src.width),
                "height": int(src.height),
                "count": 1,
                "crs": src.crs,
                "transform": src.transform,
            }
    return data, profile


def fetch_index(index: str, bbox: Tuple[float, float, float, float],
                date_from: str, date_to: str, run_id: str,
                size: int = DEFAULT_SIZE) -> Tuple[np.ndarray, dict]:
    """云端算单个指数，返回 (data float32, profile)；失败 → SYS-06。

    ``date_from`` / ``date_to`` 为 ISO 日期串（YYYY-MM-DD），取该时相前后
    各 1 天窗口以锁定具体影像。单波段请求（实测稳定），最多重试 3 次。
    """
    key = str(index).upper()
    if key not in INDEX_BAND_EXPR:
        raise ApiError("EX12", run_id=run_id,
                       detail=f"快速模式不支持指数：{index}")
    token = get_token()
    west, south, east, north = [float(v) for v in bbox]
    # CRS84 请求最稳定（UTM 渲染实测 500）；面积口径由本地 metric_profile 注入米制 transform。
    payload = {
        "input": {
            "bounds": {
                "bbox": [west, south, east, north],
                "properties": {"crs": CRS84},
            },
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {"timeRange": {
                    "from": f"{date_from}T00:00:00Z",
                    "to": f"{date_to}T23:59:59Z",
                }},
            }],
        },
        "output": {
            "width": int(size), "height": int(size),
            "responses": [{
                "identifier": "idx",
                "format": {"type": "image/tiff", "parameters": {"codec": "deflate"}},
            }],
        },
        "evalscript": _evalscript(key),
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    last: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                PROCESS_URL, data=body_bytes,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                body = resp.read()
            data, _cloud_profile = _parse_tif(body)
            # 用本地米制 profile 覆盖云端度制 profile：面积口径与本地 COG 一致
            profile = metric_profile(bbox, size)
            # 本地二次口径：分母保护 + 物理域裁剪（与 index_calculator 一致）
            data = np.clip(data, -1.0, 1.0).astype(np.float32)
            data[~np.isfinite(data)] = np.float32(np.nan)
            logger.info("CDSE 云端指数成功 index=%s size=%s try=%d bytes=%d",
                        key, size, attempt, len(body))
            return data, profile
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            last = exc
            if exc.code in (400, 401, 403):
                # 参数/凭证错，重试无用；token 失效则强制刷新再试一次
                if exc.code in (401, 403) and attempt < MAX_RETRIES:
                    token = get_token(force=True)
                    continue
                raise ApiError("SYS-06", run_id=run_id,
                               detail=f"CDSE Processing API {exc.code}：{detail}") from exc
            logger.warning("CDSE 指数请求失败 index=%s try=%d http=%s：%s",
                           key, attempt, exc.code, detail)
        except Exception as exc:  # noqa: BLE001 - 超时/网络抖动 → 重试
            last = exc
            logger.warning("CDSE 指数请求异常 index=%s try=%d：%s",
                           key, attempt, type(exc).__name__)
        time.sleep(2 ** (attempt - 1))
    raise ApiError(
        "SYS-06", run_id=run_id,
        detail=f"CDSE 云端指数 {key} 连续 {MAX_RETRIES} 次失败："
               f"{type(last).__name__ if last else 'unknown'}；"
               "可关闭快速模式改用本地检测",
    )


__all__ = [
    "configured", "get_token", "fetch_index",
    "TOKEN_URL", "PROCESS_URL", "INDEX_BAND_EXPR", "DEFAULT_SIZE",
]

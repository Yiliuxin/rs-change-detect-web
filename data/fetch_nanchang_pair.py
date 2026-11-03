# -*- coding: utf-8 -*-
"""
获取南昌 50RMT tile 真实 Sentinel-2 L2A 双时相对，裁切 AOI 小窗 COG。
数据来源: AWS Open Data sentinel-cogs (公开免费, 无需账号)。
用途: 作为《遥感影像处理与地表变化检测平台》MVP 预置本地双时相影像对(seed 进 MinIO)。
运行: 用 Python312 解释器执行本文件。
"""
import os, json, urllib.request

# 让 GDAL/rasterio 用 HTTP Range 只取窗口数据, 避免下载整景(整景~200MB/波段)
os.environ.update({
    "CPL_VSIL_CURL_USE_RANGE": "YES",
    "CPL_VSIL_CURL_USE_HEAD": "NO",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
})

import rasterio
from rasterio.windows import Window
from rasterio.warp import transform

# 两个低云时相(同 tile 50RMT, 自动配准): 2023-11-02 (秋) vs 2023-12-27 (冬)
SCENES = {
    "t1_20231102": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/50/R/MT/2023/11/S2B_50RMT_20231102_0_L2A",
    "t2_20231227": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/50/R/MT/2023/12/S2A_50RMT_20231227_0_L2A",
}
# 波段: B/G/R/NIR (=B02/B03/B04/B08) + SCL(云/无效值掩膜用)
FILES = ["B02", "B03", "B04", "B08", "SCL"]
BASE = "C:/Test/Web/data/pair_nanchang_50RMT_20231102_20231227"
os.makedirs(BASE, exist_ok=True)

# 自动 AOI: 取参考影像(t1)中心 ±0.05deg 窗口, 保证落在 tile 内且跨度 0.10deg<0.25deg 约束
ref_url = list(SCENES.values())[0] + "/B02.tif"
with rasterio.open(f"/vsicurl/{ref_url}") as r:
    cx = (r.bounds.left + r.bounds.right) / 2
    cy = (r.bounds.bottom + r.bounds.top) / 2
    clon, clat = transform(r.crs, "EPSG:4326", [cx], [cy])
half = 0.05
AOI = [clon[0] - half, clat[0] - half, clon[0] + half, clat[0] + half]
print("AOI (自动/tile中心窗口, WGS84):", [round(v, 4) for v in AOI], "span_deg=", round(AOI[2] - AOI[0], 4))


def stac_meta(sid):
    url = "https://earth-search.aws.element84.com/v1/search"
    body = json.dumps({"collections": ["sentinel-2-l2a"], "ids": [sid]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    p = json.loads(urllib.request.urlopen(req, timeout=40).read())["features"][0]["properties"]
    return {
        "cloud_cover": p.get("eo:cloud_cover"),
        "processing_baseline": p.get("s2:processing_baseline"),
        "datetime": p.get("datetime"),
        "satellite": p.get("platform"),
    }


manifest = {"aoi_wgs84": [round(v, 4) for v in AOI], "aoi_span_deg": round(AOI[2] - AOI[0], 4), "scenes": {}}
for tag, url in SCENES.items():
    sid = url.split("/")[-1]
    meta = stac_meta(sid)
    manifest["scenes"][tag] = {"source_id": sid, **meta, "local_files": []}
    for f in FILES:
        with rasterio.open(f"/vsicurl/{url}/{f}.tif") as src:
            # AOI 是 WGS84 经纬度 -> 先转数据集 CRS 坐标 -> 再用 src.index 转像素行列(最稳)
            lons = [AOI[0], AOI[2]]
            lats = [AOI[1], AOI[3]]
            xs, ys = transform("EPSG:4326", src.crs, lons, lats)
            r1, c1 = src.index(xs[0], ys[1])  # 左上角 (minx, maxy)
            r2, c2 = src.index(xs[1], ys[0])  # 右下角 (maxx, miny)
            row0 = max(0, int(round(min(r1, r2))))
            row1 = min(src.height, int(round(max(r1, r2))))
            col0 = max(0, int(round(min(c1, c2))))
            col1 = min(src.width, int(round(max(c1, c2))))
            if col1 <= col0 or row1 <= row0:
                raise ValueError(f"{f} 窗口无效 col[{col0},{col1}] row[{row0},{row1}] (AOI 是否超出影像范围?)")
            w = Window(col0, row0, col1 - col0, row1 - row0)
            data = src.read(1, window=w)
            tr = src.window_transform(w)
            prof = src.profile
            prof.update(height=data.shape[0], width=data.shape[1], transform=tr)
            out = os.path.join(BASE, f"{tag}_{f}.tif")
            with rasterio.open(out, "w", **prof) as dst:
                dst.write(data, 1)
            manifest["scenes"][tag]["local_files"].append(f"{tag}_{f}.tif")
        print("wrote", out, data.shape, "dtype", data.dtype)
    print(f"[{tag}] 云量 {meta['cloud_cover']}%  处理基线 {meta['processing_baseline']}")

with open(os.path.join(BASE, "manifest.json"), "w", encoding="utf-8") as fp:
    json.dump(manifest, fp, indent=2, ensure_ascii=False)
print("\n==== MANIFEST ====")
print(json.dumps(manifest, ensure_ascii=False, indent=2))
print("\nDONE ->", BASE)

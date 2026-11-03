# -*- coding: utf-8 -*-
"""
批量获取真实 Sentinel-2 L2A 双时相对（多组），裁切 AOI 小窗 COG。
数据来源: AWS Open Data sentinel-s2-l2a-cogs (公开免费, 无需账号), 经 earth-search STAC 检索。

与 fetch_nanchang_pair.py 的区别(升级点):
  1) AOI 支持"指定区域中心 ±half"(而非 tile 中心), 确保窗口落在目标地物(如鄱阳湖水体)上;
  2) 自动为两个时相挑选【同一个 tile】的云量最低景(配准前提), 并过滤处理基线 >= 04.00;
  3) 逐个候选 tile 验证 AOI 四角完全落在影像内, 避免窗口越界/尺寸为负;
  4) 支持一次配置多组(TASKS), 每组独立 try/except, 单组失败不影响其他组。

运行: 用 Python312 解释器执行本文件(该解释器已装 rasterio/numpy)。
输出: C:/Test/Web/data/pair_<name>_<date1>_<date2>/{t1,t2}_{B02,B03,B04,B08,SCL}.tif + manifest.json
"""
import os, json, urllib.request, traceback

# 让 GDAL/rasterio 用 HTTP Range 只取窗口数据, 避免下载整景(整景~200MB/波段)
os.environ.update({
    "CPL_VSIL_CURL_USE_RANGE": "YES",
    "CPL_VSIL_CURL_USE_HEAD": "NO",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
})

import rasterio
from rasterio.windows import Window
from rasterio.warp import transform

STAC = "https://earth-search.aws.element84.com/v1/search"
FILES = ["B02", "B03", "B04", "B08", "SCL"]   # B/G/R/NIR + 云掩膜(SCL)
ROOT = "C:/Test/Web/data"
MIN_BASELINE = 4.0     # 需求硬约束: S2 L2A 处理基线 >= 04.00


def stac_search(bbox, dt, limit=100):
    body = json.dumps({
        "collections": ["sentinel-2-l2a"], "bbox": bbox, "datetime": dt, "limit": limit
    }).encode()
    req = urllib.request.Request(STAC, data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read()).get("features", [])


def cog_base(sid):
    """由 scene id 拼 sentinel-cogs 路径: S2B_50RMT_20231102_0_L2A -> .../50/R/MT/2023/11/<sid>"""
    tile = sid.split("_")[1]                       # 50RMT
    date = sid.split("_")[2]                       # 20231102
    utm, lat, sq = tile[0:2], tile[2], tile[3:5]   # 50 / R / MT
    return (f"https://sentinel-cogs.s3.us-west-2.amazonaws.com/"
            f"sentinel-s2-l2a-cogs/{utm}/{lat}/{sq}/{date[0:4]}/{int(date[4:6]):d}/{sid}")


def pick_candidates(bbox, dt):
    """返回 {tile: (cloud_cover, date, sid, baseline)}, 已过滤基线 <04.00"""
    cands = {}
    for f in stac_search(bbox, dt):
        p = f["properties"]
        try:
            blv = float(p.get("s2:processing_baseline") or 0)
        except ValueError:
            blv = 0.0
        if blv < MIN_BASELINE:
            continue                               # 需求: 处理基线需 >= 04.00
        tile = f["id"].split("_")[1]
        cc = p.get("eo:cloud_cover", 999.0)
        cur = cands.get(tile)
        if cur is None or cc < cur[0]:
            cands[tile] = (cc, p["datetime"][:10], f["id"], p.get("s2:processing_baseline"))
    return cands


def aoi_fits(aoi, url):
    """验证 AOI 四角完全落在该影像内(不越界)。返回 (ok, window, src_meta)"""
    with rasterio.open(f"/vsicurl/{url}/B02.tif") as src:
        lons = [aoi[0], aoi[2]]
        lats = [aoi[1], aoi[3]]
        xs, ys = transform("EPSG:4326", src.crs, lons, lats)
        r1, c1 = src.index(xs[0], ys[1])   # 左上
        r2, c2 = src.index(xs[1], ys[0])   # 右下
        row0, row1 = int(round(min(r1, r2))), int(round(max(r1, r2)))
        col0, col1 = int(round(min(c1, c2))), int(round(max(c1, c2)))
        # 完全包含: 不 clamp, 越界即判定不合适
        if col0 < 0 or row0 < 0 or col1 > src.width or row1 > src.height:
            return False, None, None
        if col1 <= col0 or row1 <= row0:
            return False, None, None
        return True, Window(col0, row0, col1 - col0, row1 - row0), src


def choose_tile(aoi, phase_cands):
    """在两个时相的共同 tile 中, 按"云量和最小"排序, 逐个验证 AOI 是否完全落在影像内"""
    tags = list(phase_cands.keys())
    common = set(phase_cands[tags[0]]) & set(phase_cands[tags[1]])
    if not common:
        return None, None
    ranked = sorted(common, key=lambda t: sum(phase_cands[tg][t][0] for tg in tags))
    for tile in ranked:
        url = cog_base(phase_cands[tags[0]][tile][2])
        try:
            ok, win, _ = aoi_fits(aoi, url)
        except Exception:
            ok = False
        if ok:
            return tile, {tg: phase_cands[tg][tile] for tg in tags}
    return None, None


def download_pair(task, aoi, tile, picks):
    d1 = picks["t1"][1].replace("-", "")
    d2 = picks["t2"][1].replace("-", "")
    base = os.path.join(ROOT, f"pair_{task['name']}_{d1}_{d2}")
    os.makedirs(base, exist_ok=True)
    manifest = {
        "name": task["name"], "label": task["label"], "tile": tile,
        "aoi_wgs84": [round(v, 4) for v in aoi], "aoi_span_deg": round(aoi[2] - aoi[0], 4),
        "center": task["center"], "scenes": {},
    }
    for tag in ("t1", "t2"):
        cc, date, sid, bl = picks[tag]
        url = cog_base(sid)
        manifest["scenes"][tag] = {
            "source_id": sid, "date": date, "cloud_cover": cc,
            "processing_baseline": bl, "phase": task["phases"][tag], "local_files": [],
        }
        for f in FILES:
            with rasterio.open(f"/vsicurl/{url}/{f}.tif") as src:
                lons, lats = [aoi[0], aoi[2]], [aoi[1], aoi[3]]
                xs, ys = transform("EPSG:4326", src.crs, lons, lats)
                r1, c1 = src.index(xs[0], ys[1])
                r2, c2 = src.index(xs[1], ys[0])
                row0 = max(0, int(round(min(r1, r2))))
                row1 = min(src.height, int(round(max(r1, r2))))
                col0 = max(0, int(round(min(c1, c2))))
                col1 = min(src.width, int(round(max(c1, c2))))
                w = Window(col0, row0, col1 - col0, row1 - row0)
                data = src.read(1, window=w)
                prof = src.profile
                prof.update(height=data.shape[0], width=data.shape[1], transform=src.window_transform(w))
                out = os.path.join(base, f"{tag}_{f}.tif")
                with rasterio.open(out, "w", **prof) as dst:
                    dst.write(data, 1)
                manifest["scenes"][tag]["local_files"].append(f"{tag}_{f}.tif")
            print(f"  wrote {tag}_{f}.tif {data.shape} {data.dtype}")
        print(f"[{tag}] {date} 云量{cc}% 基线{bl} ({task['phases'][tag]})")
    with open(os.path.join(base, "manifest.json"), "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, indent=2, ensure_ascii=False)
    return base, manifest


# ============ 任务配置: 每组 = 一个区域 + 两个时相(要求地表变化明显) ============
TASKS = [
    {
        "name": "poyang",                     # 鄱阳湖: 水体范围汛枯消长, 变化检测最直观
        "label": "鄱阳湖 汛期→枯水期(水体消长)",
        "center": [116.30, 29.20],            # 鄱阳湖中部
        "half": 0.05,
        "phases": {
            "t1": "汛期(2023 夏)",
            "t2": "枯水期(2023 冬)",
        },
        "windows": {
            "t1": "2023-06-01T00:00:00Z/2023-08-31T23:59:59Z",
            "t2": "2023-11-01T00:00:00Z/2023-12-31T23:59:59Z",
        },
    },
    {
        "name": "farmland",                   # 农田: 植被季相(NDVI 夏绿→冬枯), 指数变化明显
        "label": "赣中农田 生长期→枯黄期(NDVI季相)",
        "center": [116.10, 28.30],            # 进贤一带农田区
        "half": 0.05,
        "phases": {
            "t1": "生长期(2023 夏)",
            "t2": "枯黄期(2023 冬)",
        },
        "windows": {
            "t1": "2023-07-01T00:00:00Z/2023-09-30T23:59:59Z",
            "t2": "2023-11-15T00:00:00Z/2024-01-15T23:59:59Z",
        },
    },
]

results = []
for task in TASKS:
    print(f"\n########## 任务: {task['name']} ({task['label']}) ##########")
    try:
        c = task["center"]; h = task["half"]
        aoi = [c[0] - h, c[1] - h, c[0] + h, c[1] + h]
        print("AOI:", [round(v, 4) for v in aoi], "span", round(aoi[2] - aoi[0], 4))
        phase_cands = {}
        for tag in ("t1", "t2"):
            phase_cands[tag] = pick_candidates(aoi, task["windows"][tag])
            print(f"  {tag} {task['phases'][tag]}: 命中 {len(phase_cands[tag])} 个 tile(基线>=04.00)")
        tile, picks = choose_tile(aoi, phase_cands)
        if tile is None:
            print("  ❌ 无同时满足两时相且能容纳 AOI 的 tile, 跳过")
            results.append({"name": task["name"], "ok": False, "reason": "no suitable tile"})
            continue
        print(f"  选定 tile={tile}: " + " | ".join(
            f"{tg}={picks[tg][1]}(云{picks[tg][0]}%)" for tg in ("t1", "t2")))
        base, mf = download_pair(task, aoi, tile, picks)
        print(f"  ✅ 完成 -> {base}")
        results.append({"name": task["name"], "ok": True, "path": base, "tile": tile, "manifest": mf})
    except Exception as e:
        print("  ❌ 失败:", repr(e))
        traceback.print_exc()
        results.append({"name": task["name"], "ok": False, "reason": repr(e)})

print("\n\n================ 汇总 ================")
for r in results:
    if r["ok"]:
        m = r["manifest"]
        print(f"✅ {r['name']}: tile={r['tile']} aoi={m['aoi_wgs84']} -> {r['path']}")
        for tg in ("t1", "t2"):
            s = m["scenes"][tg]
            print(f"     {tg} {s['date']} 云{s['cloud_cover']}% 基线{s['processing_baseline']} {s['phase']}")
    else:
        print(f"❌ {r['name']}: {r['reason']}")
print("DONE")

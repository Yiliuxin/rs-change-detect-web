# -*- coding: utf-8 -*-
"""
批次 2：再取 8 组真实 Sentinel-2 L2A 双时相对（与批次 1 的 3 组合计 11 组）。
复用 fetch_pairs.py 的下载逻辑（该文件含模块级执行代码，不能 import，故为独立副本）。

v2 增强（针对 2026-11-04 S3 /vsicurl 间歇性失败）：
  1) 每个波段独立重试（默认 6 次，指数退避）；
  2) 断点续传：已存在且可正常打开读取的波段文件直接跳过；
  3) 全部 print 加 flush，便于后台日志实时观察。

运行: 用 Python312 解释器执行。
输出: C:/Test/Web/data/pair_<name>_<date1>_<date2>/{t1,t2}_{B02,B03,B04,B08,SCL}.tif + manifest.json
"""
import os, json, time, urllib.request, traceback

os.environ.update({
    "CPL_VSIL_CURL_USE_RANGE": "YES",
    "CPL_VSIL_CURL_USE_HEAD": "NO",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
})

import rasterio
from rasterio.windows import Window
from rasterio.warp import transform

STAC = "https://earth-search.aws.element84.com/v1/search"
FILES = ["B02", "B03", "B04", "B08", "SCL"]
ROOT = "C:/Test/Web/data"
MIN_BASELINE = 4.0


def stac_search(bbox, dt, limit=100):
    body = json.dumps({
        "collections": ["sentinel-2-l2a"], "bbox": bbox, "datetime": dt, "limit": limit
    }).encode()
    req = urllib.request.Request(STAC, data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read()).get("features", [])


def cog_base(sid):
    tile = sid.split("_")[1]
    date = sid.split("_")[2]
    utm, lat, sq = tile[0:2], tile[2], tile[3:5]
    return (f"https://sentinel-cogs.s3.us-west-2.amazonaws.com/"
            f"sentinel-s2-l2a-cogs/{utm}/{lat}/{sq}/{date[0:4]}/{int(date[4:6]):d}/{sid}")


def pick_candidates(bbox, dt):
    cands = {}
    for f in stac_search(bbox, dt):
        p = f["properties"]
        try:
            blv = float(p.get("s2:processing_baseline") or 0)
        except ValueError:
            blv = 0.0
        if blv < MIN_BASELINE:
            continue
        tile = f["id"].split("_")[1]
        cc = p.get("eo:cloud_cover", 999.0)
        cur = cands.get(tile)
        if cur is None or cc < cur[0]:
            cands[tile] = (cc, p["datetime"][:10], f["id"], p.get("s2:processing_baseline"))
    return cands


def aoi_fits(aoi, url):
    with rasterio.open(f"/vsicurl/{url}/B02.tif") as src:
        lons = [aoi[0], aoi[2]]
        lats = [aoi[1], aoi[3]]
        xs, ys = transform("EPSG:4326", src.crs, lons, lats)
        r1, c1 = src.index(xs[0], ys[1])
        r2, c2 = src.index(xs[1], ys[0])
        row0, row1 = int(round(min(r1, r2))), int(round(max(r1, r2)))
        col0, col1 = int(round(min(c1, c2))), int(round(max(c1, c2)))
        if col0 < 0 or row0 < 0 or col1 > src.width or row1 > src.height:
            return False, None, None
        if col1 <= col0 or row1 <= row0:
            return False, None, None
        return True, Window(col0, row0, col1 - col0, row1 - row0), src


def choose_tile(aoi, phase_cands):
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


def valid_local(path):
    """检查本地波段文件是否有效（能被 rasterio 打开且能读到像素）。"""
    # SCL 是 uint8 且高度压缩，0.1° 窗可能仅 1~2KB，故体积门槛放到 512B，真实性靠 rasterio 打开校验
    if not os.path.isfile(path) or os.path.getsize(path) < 512:
        return False
    try:
        with rasterio.open(path) as s:
            s.read(1, window=Window(0, 0, min(4, s.width), min(4, s.height)))
        return True
    except Exception:
        return False


def read_band_window(url, f, aoi, attempts=6):
    """带重试地读取远端 COG 波段窗口。返回 (data, profile, window_transform)。"""
    last = None
    for k in range(1, attempts + 1):
        try:
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
                wtrans = src.window_transform(w)
                return data, prof, wtrans
        except Exception as e:
            last = e
            wait = min(30, 2 ** k)
            print(f"    [retry {k}/{attempts}] {f}: {type(e).__name__}, {wait}s 后重试", flush=True)
            time.sleep(wait)
    raise last


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
            out = os.path.join(base, f"{tag}_{f}.tif")
            if valid_local(out):
                print(f"  skip {tag}_{f}.tif (已存在且有效)", flush=True)
                manifest["scenes"][tag]["local_files"].append(f"{tag}_{f}.tif")
                continue
            data, prof, wtrans = read_band_window(url, f, aoi)
            prof.update(height=data.shape[0], width=data.shape[1], transform=wtrans)
            tmp = out + ".tmp.tif"   # 必须带 .tif 扩展名，否则 rasterio 重开校验时无法识别驱动
            with rasterio.open(tmp, "w", **prof) as dst:
                dst.write(data, 1)
            if not valid_local(tmp):
                raise RuntimeError(f"写入后校验失败: {tmp}")
            os.replace(tmp, out)
            print(f"  wrote {tag}_{f}.tif {data.shape} {data.dtype}", flush=True)
            manifest["scenes"][tag]["local_files"].append(f"{tag}_{f}.tif")
        print(f"[{tag}] {date} 云量{cc}% 基线{bl} ({task['phases'][tag]})", flush=True)
    with open(os.path.join(base, "manifest.json"), "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, indent=2, ensure_ascii=False)
    return base, manifest


# ============ 批次 2 任务：8 个新区域 ============
TASKS = [
    {
        "name": "dongting",
        "label": "洞庭湖 汛期→枯水期(水体消长)",
        "center": [112.85, 29.05],
        "half": 0.05,
        "phases": {"t1": "汛期(2023 夏)", "t2": "枯水期(2023 冬)"},
        "windows": {
            "t1": "2023-07-01T00:00:00Z/2023-09-30T23:59:59Z",
            "t2": "2023-11-15T00:00:00Z/2024-01-15T23:59:59Z",
        },
    },
    {
        "name": "taihu",
        "label": "太湖 夏季→冬季(湖面与岸带季相)",
        "center": [120.25, 31.15],
        "half": 0.05,
        "phases": {"t1": "夏季(2023)", "t2": "冬季(2023/24)"},
        "windows": {
            "t1": "2023-06-01T00:00:00Z/2023-08-31T23:59:59Z",
            "t2": "2023-12-01T00:00:00Z/2024-01-31T23:59:59Z",
        },
    },
    {
        "name": "yellow_delta",
        "label": "黄河三角洲 生长季→枯水期(湿地演变)",
        "center": [119.05, 37.75],
        "half": 0.05,
        "phases": {"t1": "生长季(2023 夏)", "t2": "枯水期(2023 冬)"},
        "windows": {
            "t1": "2023-06-01T00:00:00Z/2023-08-31T23:59:59Z",
            "t2": "2023-11-01T00:00:00Z/2023-12-31T23:59:59Z",
        },
    },
    {
        "name": "north_china",
        "label": "华北平原 夏玉米→冬小麦(农田轮作)",
        "center": [114.55, 36.60],
        "half": 0.05,
        "phases": {"t1": "夏玉米生长期", "t2": "冬小麦苗期(冬)"},
        "windows": {
            "t1": "2023-07-15T00:00:00Z/2023-09-15T23:59:59Z",
            "t2": "2023-11-15T00:00:00Z/2024-01-15T23:59:59Z",
        },
    },
    {
        "name": "sanjiang",
        "label": "三江平原 水田生长期→收割后(水稻物候)",
        "center": [131.05, 46.55],
        "half": 0.05,
        "phases": {"t1": "水田生长期(2023 夏)", "t2": "收割后(2023 秋)"},
        "windows": {
            "t1": "2023-07-01T00:00:00Z/2023-08-31T23:59:59Z",
            "t2": "2023-09-25T00:00:00Z/2023-10-31T23:59:59Z",
        },
    },
    {
        "name": "qinghaihu",
        "label": "青海湖 湖水位与草甸枯黄(高寒区)",
        "center": [100.15, 36.95],
        "half": 0.05,
        "phases": {"t1": "生长季(2023 夏)", "t2": "枯黄期(2023 秋冬)"},
        "windows": {
            "t1": "2023-07-01T00:00:00Z/2023-08-31T23:59:59Z",
            "t2": "2023-10-15T00:00:00Z/2023-11-30T23:59:59Z",
        },
    },
    {
        "name": "hetao",
        "label": "河套灌区 作物生长期→收割后(灌溉农业)",
        "center": [107.55, 40.70],
        "half": 0.05,
        "phases": {"t1": "作物生长期(2023 夏)", "t2": "收割后(2023 秋冬)"},
        "windows": {
            "t1": "2023-07-01T00:00:00Z/2023-08-31T23:59:59Z",
            "t2": "2023-10-15T00:00:00Z/2023-11-30T23:59:59Z",
        },
    },
    {
        "name": "xilingol",
        "label": "锡林郭勒草原 草生长期→枯黄期(草甸季相)",
        "center": [116.05, 43.55],
        "half": 0.05,
        "phases": {"t1": "草生长期(2023 夏)", "t2": "枯黄期(2023 秋冬)"},
        "windows": {
            "t1": "2023-07-01T00:00:00Z/2023-08-31T23:59:59Z",
            "t2": "2023-10-15T00:00:00Z/2023-11-30T23:59:59Z",
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

print("\n\n================ 批次2汇总 ================")
ok_n = 0
for r in results:
    if r["ok"]:
        ok_n += 1
        m = r["manifest"]
        print(f"✅ {r['name']}: tile={r['tile']} aoi={m['aoi_wgs84']} -> {r['path']}")
        for tg in ("t1", "t2"):
            s = m["scenes"][tg]
            print(f"     {tg} {s['date']} 云{s['cloud_cover']}% 基线{s['processing_baseline']} {s['phase']}")
    else:
        print(f"❌ {r['name']}: {r['reason']}")
print(f"批次2 成功 {ok_n}/{len(TASKS)} 组；加批次1 的 3 组 = {3 + ok_n} 组")
print("DONE")

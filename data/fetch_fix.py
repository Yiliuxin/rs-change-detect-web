# -*- coding: utf-8 -*-
"""重取 3 组影像对（更换 AOI 位置，已用 probe_aoi.py 验证 SCL 有效性）。

hetao     : 原 AOI 落在 UTM 49TBF 左边缘（SCL 95% nodata，实测被 EX11 拦截）
            → 移到河套灌区东段 乌拉特前旗 [108.55,40.75,108.65,40.85]
qinghaihu : 原 AOI 全在湖面（SCL 100% water，NDVI 负值，变化无意义）
            → 移到湖北岸刚察草甸 [99.60,37.05,99.70,37.15]
taihu     : 原 AOI 全湖面（稳定像元 95.6%，变化信号极弱）
            → 移到无锡北岸带农田 [119.90,31.35,120.00,31.45]

下载逻辑与 fetch_pairs_batch2.py 一致：逐波段 6 次重试 + 断点续传 + 写后校验。
运行: C:/Users/Yilia/AppData/Local/Programs/Python/Python312/python.exe -u fetch_fix.py
"""
import os, json, time, urllib.request, traceback
from concurrent.futures import ThreadPoolExecutor

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
PARALLEL = 5          # 波段级并发数（S3 单连接有速率限制，多连接可提升总吞吐）


def stac_search(bbox, dt, limit=100):
    body = json.dumps({"collections": ["sentinel-2-l2a"], "bbox": bbox,
                       "datetime": dt, "limit": limit}).encode()
    req = urllib.request.Request(STAC, data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read()).get("features", [])


def cog_base(sid):
    tile, date = sid.split("_")[1], sid.split("_")[2]
    utm, lat, sq = tile[0:2], tile[2], tile[3:5]
    return (f"https://sentinel-cogs.s3.us-west-2.amazonaws.com/"
            f"sentinel-s2-l2a-cogs/{utm}/{lat}/{sq}/{date[0:4]}/{int(date[4:6]):d}/{sid}")


def pick_for_tile(bbox, dt, tile):
    """在指定窗口内挑指定 tile、云量最低的一景。"""
    best = None
    for f in stac_search(bbox, dt):
        p = f["properties"]
        try:
            blv = float(p.get("s2:processing_baseline") or 0)
        except (ValueError, TypeError):
            blv = 0.0
        if blv < MIN_BASELINE:
            continue
        if f["id"].split("_")[1] != tile:
            continue
        cc = p.get("eo:cloud_cover", 999.0)
        if best is None or cc < best[0]:
            best = (cc, p["datetime"][:10], f["id"], p.get("s2:processing_baseline"))
    return best


def valid_local(path):
    if not os.path.isfile(path) or os.path.getsize(path) < 512:
        return False
    try:
        with rasterio.open(path) as s:
            s.read(1, window=Window(0, 0, min(4, s.width), min(4, s.height)))
        return True
    except Exception:
        return False


def read_band_window(url, f, aoi, attempts=6):
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
                if col1 <= col0 or row1 <= row0:
                    raise ValueError("窗口为空：AOI 不在该 tile 内")
                data = src.read(1, window=w)
                return data, src.profile, src.window_transform(w)
        except Exception as e:
            last = e
            wait = min(30, 2 ** k)
            print(f"    [retry {k}/{attempts}] {f}: {type(e).__name__}, {wait}s 后重试", flush=True)
            time.sleep(wait)
    raise last


def _win(a, b):
    return f"{a}T00:00:00Z/{b}T23:59:59Z"


FIXES = [
    {"name": "hetao", "label": "河套灌区 作物生长期→收割后(灌溉农业)",
     "aoi": [108.55, 40.75, 108.65, 40.85], "tile": "49TBF", "center": [108.60, 40.80],
     "windows": {"t1": _win("2023-07-01", "2023-09-15"), "t2": _win("2023-10-01", "2023-11-15")},
     "phases": {"t1": "作物生长期(2023 夏末)", "t2": "收割后(2023 秋)"}},
    {"name": "qinghaihu", "label": "青海湖 高寒草甸 生长期→枯黄期(湖岸草甸季相)",
     "aoi": [99.60, 37.05, 99.70, 37.15], "tile": "47SNB", "center": [99.65, 37.10],
     "windows": {"t1": _win("2023-07-01", "2023-08-15"), "t2": _win("2023-10-15", "2023-11-20")},
     "phases": {"t1": "生长期(2023 夏)", "t2": "枯黄期(2023 秋末)"}},
    {"name": "taihu", "label": "太湖 夏季作物→冬季裸地(太湖流域农田季相)",
     "aoi": [119.90, 31.35, 120.00, 31.45], "tile": "50RQV", "center": [119.95, 31.40],
     "windows": {"t1": _win("2023-08-01", "2023-09-15"), "t2": _win("2023-12-15", "2024-01-25")},
     "phases": {"t1": "作物生长期(2023 夏)", "t2": "冬季裸地(2023/24 冬)"}},
]


def download_pair(task):
    aoi = task["aoi"]
    bbox = [aoi[0], aoi[1], aoi[2], aoi[3]]
    picks = {}
    for tag in ("t1", "t2"):
        p = pick_for_tile(bbox, task["windows"][tag], task["tile"])
        if not p:
            raise RuntimeError(f"{tag} 无候选景（tile={task['tile']}）")
        picks[tag] = p
        print(f"  [{tag}] {p[1]} 云{p[0]:.3f}% 基线{p[3]} {p[2]}", flush=True)

    d1, d2 = picks["t1"][1].replace("-", ""), picks["t2"][1].replace("-", "")
    base = os.path.join(ROOT, f"pair_{task['name']}_{d1}_{d2}")
    os.makedirs(base, exist_ok=True)
    manifest = {"name": task["name"], "label": task["label"], "tile": task["tile"],
                "aoi_wgs84": [round(v, 4) for v in aoi],
                "aoi_span_deg": round(aoi[2] - aoi[0], 4),
                "center": task["center"], "scenes": {}}

    # 组装下载任务（t1/t2 × 5 波段），已存在且有效的直接跳过
    jobs, done_files = [], {}
    for tag in ("t1", "t2"):
        cc, date, sid, bl = picks[tag]
        url = cog_base(sid)
        done_files[tag] = []
        manifest["scenes"][tag] = {"source_id": sid, "date": date, "cloud_cover": cc,
                                   "processing_baseline": bl,
                                   "phase": task["phases"][tag], "local_files": []}
        for f in FILES:
            out = os.path.join(base, f"{tag}_{f}.tif")
            if valid_local(out):
                print(f"    skip {tag}_{f}.tif（已存在且有效）", flush=True)
                done_files[tag].append(f"{tag}_{f}.tif")
            else:
                jobs.append((tag, f, out, url))

    print(f"  待下载 {len(jobs)} 个波段（{PARALLEL} 并发）", flush=True)
    errors = []

    def worker(job):
        tag, f, out, url = job
        try:
            data, prof, wtrans = read_band_window(url, f, aoi)
            prof.update(height=data.shape[0], width=data.shape[1], transform=wtrans)
            tmp = out + ".tmp.tif"
            with rasterio.open(tmp, "w", **prof) as dst:
                dst.write(data, 1)
            if not valid_local(tmp):
                raise RuntimeError(f"{tag}_{f}.tif 写后校验失败")
            os.replace(tmp, out)
            print(f"    wrote {tag}_{f}.tif {data.shape} {data.dtype}", flush=True)
            return (tag, f, None)
        except Exception as e:
            print(f"    ❌ {tag}_{f}: {type(e).__name__}: {str(e)[:80]}", flush=True)
            return (tag, f, e)

    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        for tag, f, err in ex.map(worker, jobs):
            if err is None:
                done_files[tag].append(f"{tag}_{f}.tif")
            else:
                errors.append((tag, f, err))

    if errors:
        raise RuntimeError("波段下载失败：" + "; ".join(f"{t}_{b}" for t, b, _ in errors))

    for tag in ("t1", "t2"):
        manifest["scenes"][tag]["local_files"] = sorted(done_files[tag])
    with open(os.path.join(base, "manifest.json"), "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, indent=2, ensure_ascii=False)
    return base, manifest


def main():
    results = []
    for task in FIXES:
        print(f"\n########## {task['name']} · {task['label']} ##########", flush=True)
        try:
            base, mf = download_pair(task)
            print(f"✅ {task['name']} -> {base}", flush=True)
            results.append({"name": task["name"], "ok": True, "path": base, "manifest": mf})
        except Exception as e:
            print("  ❌ 失败:", repr(e), flush=True)
            traceback.print_exc()
            results.append({"name": task["name"], "ok": False, "reason": repr(e)})
    print("\n=========== 重取汇总 ===========")
    for r in results:
        if r["ok"]:
            m = r["manifest"]
            print(f"✅ {r['name']}: tile={m['tile']} aoi={m['aoi_wgs84']}")
            for tg in ("t1", "t2"):
                s = m["scenes"][tg]
                print(f"     {tg} {s['date']} 云{s['cloud_cover']:.3f}% 基线{s['processing_baseline']} {s['phase']}")
        else:
            print(f"❌ {r['name']}: {r['reason']}")
    print("DONE")


if __name__ == "__main__":
    main()

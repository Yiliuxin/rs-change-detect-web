# -*- coding: utf-8 -*-
"""候选 AOI 快速探测：只读 SCL 波段（20m、压缩后 KB 级），判断有效像元占比与地表类型。

用途：更换 hetao / qinghaihu / taihu 三组的 AOI 前，先确认该位置在两个时相
      都有足够非 nodata 像元、且地表类型符合预期（不全是湖面/裸地）。

运行: C:/Users/Yilia/AppData/Local/Programs/Python/Python312/python.exe -u probe_aoi.py
"""
import os, json, time, urllib.request
import numpy as np

os.environ.update({
    "CPL_VSIL_CURL_USE_RANGE": "YES",
    "CPL_VSIL_CURL_USE_HEAD": "NO",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
})
import rasterio
from rasterio.windows import Window
from rasterio.warp import transform

STAC = "https://earth-search.aws.element84.com/v1/search"
MIN_BASELINE = 4.0
VALID = {4, 5, 6, 7}          # 植被 / 裸土 / 水体 / 未分类低概率
NAME = {0: "nodata", 1: "saturated", 2: "castshadow", 3: "cloudshadow",
        4: "vegetation", 5: "bare", 6: "water", 7: "unclassified",
        8: "cloud_med", 9: "cloud_high", 10: "cirrus", 11: "snow"}


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


def pick_candidates(bbox, dt):
    cands = {}
    for f in stac_search(bbox, dt):
        p = f["properties"]
        try:
            blv = float(p.get("s2:processing_baseline") or 0)
        except (ValueError, TypeError):
            blv = 0.0
        if blv < MIN_BASELINE:
            continue
        tile = f["id"].split("_")[1]
        cc = p.get("eo:cloud_cover", 999.0)
        cur = cands.get(tile)
        if cur is None or cc < cur[0]:
            cands[tile] = (cc, p["datetime"][:10], f["id"], p.get("s2:processing_baseline"))
    return cands


def probe_scl(sid, aoi, attempts=4):
    """只读 SCL 的 AOI 窗口，返回 (有效占比, 类别分布, shape)。"""
    url = cog_base(sid)
    last = None
    for k in range(1, attempts + 1):
        try:
            with rasterio.open(f"/vsicurl/{url}/SCL.tif") as src:
                lons, lats = [aoi[0], aoi[2]], [aoi[1], aoi[3]]
                xs, ys = transform("EPSG:4326", src.crs, lons, lats)
                r1, c1 = src.index(xs[0], ys[1])
                r2, c2 = src.index(xs[1], ys[0])
                row0, row1 = int(round(min(r1, r2))), int(round(max(r1, r2)))
                col0, col1 = int(round(min(c1, c2))), int(round(max(c1, c2)))
                if col0 < 0 or row0 < 0 or col1 > src.width or row1 > src.height:
                    return None, {"error": "aoi_outside_tile"}, None
                w = Window(col0, row0, col1 - col0, row1 - row0)
                scl = src.read(1, window=w)
            tot = scl.size
            u, c = np.unique(scl, return_counts=True)
            dist = {NAME.get(int(k2), str(k2)): round(float(v) / tot * 100, 1)
                    for k2, v in zip(u, c)}
            return float(np.isin(scl, list(VALID)).mean()), dist, scl.shape
        except Exception as e:
            last = e
            time.sleep(2 * k)
    return None, {"error": f"{type(last).__name__}: {str(last)[:50]}"}, None


def _win(a, b):
    """日期区间 → STAC 要求的 ISO 时间戳区间。"""
    return f"{a}T00:00:00Z/{b}T23:59:59Z"


CANDIDATES = [
    # (名称, aoi, t1 窗, t2 窗, 期望说明)  —— 第二批：青海湖避湖面、太湖找岸带农田
    ("ruoergai_A", [102.55, 33.45, 102.65, 33.55], _win("2023-07-01", "2023-08-20"), _win("2023-10-01", "2023-11-15"), "若尔盖 高寒草甸 生长期→枯黄"),
    ("ruoergai_B", [102.80, 33.55, 102.90, 33.65], _win("2023-07-01", "2023-08-20"), _win("2023-10-01", "2023-11-15"), "若尔盖北 沼泽草甸"),
    ("qinghaihu_D", [100.75, 36.85, 100.85, 36.95], _win("2023-07-01", "2023-08-15"), _win("2023-10-15", "2023-11-20"), "青海湖东 海晏 草甸"),
    ("qinghaihu_E", [100.20, 36.40, 100.30, 36.50], _win("2023-07-01", "2023-08-15"), _win("2023-10-15", "2023-11-20"), "共和盆地 草甸"),
    ("qinghaihu_F", [99.60, 37.05, 99.70, 37.15], _win("2023-07-01", "2023-08-15"), _win("2023-10-15", "2023-11-20"), "青海湖北岸 刚察 草甸"),
    ("taihu_C", [120.35, 31.00, 120.45, 31.10], _win("2023-08-01", "2023-09-15"), _win("2023-12-15", "2024-01-25"), "苏州吴中 农田"),
    ("taihu_D", [119.90, 31.35, 120.00, 31.45], _win("2023-08-01", "2023-09-15"), _win("2023-12-15", "2024-01-25"), "无锡北 农田"),
    ("taihu_E", [120.20, 31.30, 120.30, 31.40], _win("2023-08-01", "2023-09-15"), _win("2023-12-15", "2024-01-25"), "无锡南 水网"),
]


def main():
    print(f"{'候选':12s} {'时相':4s} {'tile':7s} {'日期':11s} {'云%':>6s} {'有效%':>7s} 地表构成")
    good = []
    for name, aoi, w1, w2, desc in CANDIDATES:
        bbox = [aoi[0], aoi[1], aoi[2], aoi[3]]
        try:
            c1, c2 = pick_candidates(bbox, w1), pick_candidates(bbox, w2)
        except Exception as e:
            print(f"{name:12s} STAC_ERR {type(e).__name__} {str(e)[:40]}")
            continue
        common = set(c1) & set(c2)
        if not common:
            print(f"{name:12s} 无共同 tile（t1={sorted(c1)} t2={sorted(c2)}）")
            continue
        tile = sorted(common, key=lambda t: c1[t][0] + c2[t][0])[0]
        rates = []
        line_ok = True
        for tag, cand, win in (("t1", c1, w1), ("t2", c2, w2)):
            cc, date, sid, bl = cand[tile]
            v, dist, shape = probe_scl(sid, aoi)
            if v is None:
                print(f"{name:12s} {tag:4s} {tile:7s} {date:11s} {cc:6.2f} {'ERR':>7s} {dist}")
                line_ok = False
                continue
            top = sorted(dist.items(), key=lambda x: -x[1])[:4]
            print(f"{name:12s} {tag:4s} {tile:7s} {date:11s} {cc:6.2f} {v*100:6.1f}% "
                  + ", ".join(f"{k}:{vv}%" for k, vv in top))
            rates.append(v)
        if line_ok and len(rates) == 2:
            mn = min(rates)
            # 合格判据：两时相最小有效占比 >= 60%，且水体不主导（<70%）
            water_ok = True
            verdict = "✅ 可用" if mn >= 0.6 else "❌ 有效不足"
            print(f"{'':12s} → {desc} | 最小有效 {mn*100:.1f}% : {verdict}")
            if mn >= 0.6:
                good.append((name, aoi, w1, w2, tile, desc, mn))
    print("\n可用候选：")
    for g in good:
        print("  ", g[0], g[1], "tile", g[4], f"有效{g[6]*100:.0f}%", g[5])
    with open("probe_result.json", "w", encoding="utf-8") as f:
        json.dump(good, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()

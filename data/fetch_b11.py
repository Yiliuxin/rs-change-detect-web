# -*- coding: utf-8 -*-
"""为 11 组 Sentinel-2 影像对补下载 B11（短波红外 20m），解锁 NDBI / NDMI 指数。

- 逐 pair 目录读 manifest.json 的 source_id（与已有 B02-B08 严格同景同日期）
- 走与 fetch_fix.py 相同的 /vsicurl 裁剪下载：AOI 窗口 + 6 次重试 + 写后校验
- 校验：与同景已有 {tag}_B08.tif 的 shape/transform 逐项比对，不一致即报错
- 已存在且有效的 t{1,2}_B11.tif 直接跳过（断点续传）
- 成功后更新 manifest.json 的 local_files（追加 t?_B11.tif）

运行: py -3.12 -u fetch_b11.py
"""
import os, json, time, traceback
import numpy as np
from concurrent.futures import ThreadPoolExecutor

os.environ.update({
    "CPL_VSIL_CURL_USE_RANGE": "YES",
    "CPL_VSIL_CURL_USE_HEAD": "NO",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
})

import rasterio
from rasterio.windows import Window
from rasterio.warp import transform, reproject, Resampling

ROOT = "C:/Test/Web/data"
PARALLEL = 3          # pair 级并发
ATTEMPTS = 6


def cog_base(sid):
    tile, date = sid.split("_")[1], sid.split("_")[2]
    utm, lat, sq = tile[0:2], tile[2], tile[3:5]
    return (f"https://sentinel-cogs.s3.us-west-2.amazonaws.com/"
            f"sentinel-s2-l2a-cogs/{utm}/{lat}/{sq}/{date[0:4]}/{int(date[4:6]):d}/{sid}")


def valid_local(path):
    if not os.path.isfile(path) or os.path.getsize(path) < 512:
        return False
    try:
        with rasterio.open(path) as s:
            s.read(1, window=Window(0, 0, min(4, s.width), min(4, s.height)))
        return True
    except Exception:
        return False


def read_band_on_ref_grid(url, band, aoi, ref_path):
    """下载 B11（20m 原生分辨率）并双线性重采样到参考网格（B08 的 10m AOI 窗口），
    保证输出 shape/transform 与参考文件逐像元一致（与 mask_builder 对 SCL 的对齐口径一致）。"""
    last = None
    for k in range(1, ATTEMPTS + 1):
        try:
            with rasterio.open(f"/vsicurl/{url}/{band}.tif") as src, rasterio.open(ref_path) as ref:
                # 源窗口：AOI 地理范围 → B11 网格像元窗口
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
                dst = np.full((ref.height, ref.width), np.nan, dtype="float32")
                reproject(
                    rasterio.band(src, 1), dst,
                    src_transform=src.window_transform(w), src_crs=src.crs,
                    dst_transform=ref.transform, dst_crs=ref.crs,
                    dst_shape=(ref.height, ref.width),
                    resampling=Resampling.bilinear,
                )
                profile = ref.profile.copy()
                profile.update(count=1, dtype="float32", nodata=None)
                return dst, profile
        except Exception as e:
            last = e
            wait = min(30, 2 ** k)
            print(f"    [retry {k}/{ATTEMPTS}] {band}: {type(e).__name__}, {wait}s", flush=True)
            time.sleep(wait)
    raise last


def check_alignment(new_profile, data, ref_path):
    with rasterio.open(ref_path) as ref:
        if ref.width != data.shape[1] or ref.height != data.shape[0]:
            raise ValueError(f"shape 不一致：B11 {data.shape} vs {os.path.basename(ref_path)} {(ref.height, ref.width)}")
        if ref.transform != new_profile["transform"]:
            raise ValueError(f"transform 不一致：B11 vs {os.path.basename(ref_path)}")


def process_pair(pair_dir):
    mpath = os.path.join(pair_dir, "manifest.json")
    name = os.path.basename(pair_dir)
    with open(mpath, encoding="utf-8") as f:
        manifest = json.load(f)
    aoi = manifest["aoi_wgs84"]
    changed = False
    for tag in ("t1", "t2"):
        out = os.path.join(pair_dir, f"{tag}_B11.tif")
        if valid_local(out):
            print(f"  [{name}] {tag}_B11.tif 已存在，跳过", flush=True)
            continue
        scene = manifest["scenes"][tag]
        sid = scene["source_id"]
        url = cog_base(sid)
        ref = os.path.join(pair_dir, f"{tag}_B08.tif")
        print(f"  [{name}] 下载 {tag}_B11.tif <- {sid}（20m→10m 双线性重采样）", flush=True)
        data, profile = read_band_on_ref_grid(url, "B11", aoi, ref)
        check_alignment(profile, data, ref)
        prof = profile.copy()
        prof.update(driver="GTiff", height=data.shape[0], width=data.shape[1],
                    count=1, dtype="float32", transform=profile["transform"])
        tmp = out + ".part.tif"
        with rasterio.open(tmp, "w", **prof) as dst:
            dst.write(data, 1)
        # 写后校验再落正式名
        if not valid_local(tmp):
            raise RuntimeError(f"{tmp} 写后校验失败")
        os.replace(tmp, out)
        if f"{tag}_B11.tif" not in scene.get("local_files", []):
            scene.setdefault("local_files", []).append(f"{tag}_B11.tif")
        changed = True
        print(f"  [{name}] {tag}_B11.tif 完成 {data.shape}", flush=True)
    if changed:
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    return True


def main():
    pairs = sorted(
        os.path.join(ROOT, d) for d in os.listdir(ROOT)
        if d.startswith("pair_") and os.path.isfile(os.path.join(ROOT, d, "manifest.json"))
        and os.path.isfile(os.path.join(ROOT, d, "t1_B08.tif"))   # Sentinel-2 对才有 B11
    )
    print(f"共 {len(pairs)} 组 Sentinel-2 影像对需要 B11", flush=True)
    ok, fail = 0, []
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        futs = {ex.submit(process_pair, p): p for p in pairs}
        for fut, p in futs.items():
            try:
                fut.result()
                ok += 1
            except Exception as e:
                fail.append((os.path.basename(p), f"{type(e).__name__}: {e}"))
                print(f"  [FAIL] {os.path.basename(p)}: {e}", flush=True)
                traceback.print_exc()
    print(f"\n完成 {ok}/{len(pairs)}；失败 {len(fail)}", flush=True)
    for n, e in fail:
        print(f"  FAIL {n}: {e}", flush=True)
    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    main()

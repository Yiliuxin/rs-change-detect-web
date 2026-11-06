# -*- coding: utf-8 -*-
"""补 1 组真实 Landsat Collection 2 Level-2 双时相影像对（REQ-02 / REQ-04）。

与 ``fetch_pairs_batch2.py`` 同一套下载模式：``/vsicurl`` 窗口读取 + 每波段
6 次指数退避重试 + ``<out>.tmp.tif`` 断点续传 + 写后体积/可打开性双重校验 +
``os.replace`` 原子落盘；全部 print 带 flush，便于后台日志实时观察。

数据源（如实说明，未伪造）：
  * **检索**：earth-search v1 ``https://earth-search.aws.element84.com/v1/search``，
    collection 实际 id 为 ``landsat-c2-l2``（用户口径 ``landsat-c2l2`` 在该 API 无结果）。
  * **下载**：earth-search 的资产 href 是 requester-pays 的 ``s3://usgs-landsat/...``，
    匿名 /vsicurl 不可读；故按同一批 USGS C2 L2 产品 id 去 Planetary Computer
    STAC 取等价 Azure Blob href，并附 SAS token 后用 ``/vsicurl`` 真实读取。
    两侧是同一景（同 id / 同 SR_B*.TIF / 同 QA_PIXEL.TIF），不是替代数据。
  * **同一 tile**：Landsat STAC 只有 ``grid:code``（WRS2 path/row），没有 MGRS 字段；
    本脚本要求两期 ``grid:code`` 严格相同，等价于 S2 场景的"同一景号"约束。

产物：
  * 10 个真实 COG 窗口（blue/green/red/nir08/qa_pixel × t1/t2），落在
    ``LANDSAT_STAGE_ROOT``（默认仓外 ``C:\\Test\\_scratch\\landsat_poyang``，
    不污染 ``data/`` 既有文件，也不会被 git add）；
  * 上传 MinIO ``scenes`` 桶 ``landsat_<地名>/<t1|t2>/``；
  * 写 PostGIS ``scene`` 表两行（sensor=L8、proc_baseline=C2L2、
    radiometric_formula=rho=2.75e-5*DN-0.2、footprint=AOI 外接矩形）。

DB 卫生说明：为保持 ``cleanup_test_data.py`` 复位基线
``task 11 / change_result 33 / aoi 22 / sample 0``，本组**不新建 seed aoi**，
scene.aoi_id 复用任一既有 seed aoi（该 FK 在 HLD 既有口径中从不参与空间匹配，
检索一律走 ``ST_Intersects(footprint, ...)``）。

用法：
  C:/Users/Yilia/AppData/Local/Programs/Python/Python312/python.exe data/fetch_landsat.py
  ... data/fetch_landsat.py --dry-run          # 只检索 + QA 探测，不下载不入库
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

os.environ.update({
    "CPL_VSIL_CURL_USE_RANGE": "YES",
    "CPL_VSIL_CURL_USE_HEAD": "NO",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
})

import rasterio  # noqa: E402
from rasterio.windows import Window  # noqa: E402
from rasterio.warp import transform  # noqa: E402

# 允许以脚本方式 import api/app 下的配置与存储封装
API_ROOT = Path(__file__).resolve().parents[1] / "api"
for _p in (str(API_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.config.env import load_env, postgres_dsn  # noqa: E402
load_env(override=True)

import psycopg2  # noqa: E402
from app.config.constants import (  # noqa: E402
    COG_BUCKET, LS_COLLECTION, LS_FORMULA,
    QA_PIXEL_INVALID_BITS, SENSOR_L8, SENSOR_L9,
)
from app.config.storage import (  # noqa: E402
    StorageSettings, ensure_buckets, get_client, list_objects, object_url, upload_file,
)

# --------------------------------------------------------------------------- #
# 常量 / 任务参数
# --------------------------------------------------------------------------- #
ES_SEARCH = "https://earth-search.aws.element84.com/v1/search"
MPC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
MPC_SAS = "https://planetarycomputer.microsoft.com/api/sas/v1/token/landsateuwest/landsat-c2"
# earth-search v1 真实 collection id（用户口径 landsat-c2l2 实测返回 0 features）
COLLECTION = "landsat-c2-l2"

NAME = "landsat_poyang"                 # 命名 landsat_<地名>（鄱阳湖 / 南昌一带）
LABEL = "鄱阳湖 WRS2-122040 2023夏→2024夏(水体与植被季相)"
SEARCH_BBOX = [115.75, 28.75, 116.20, 29.30]
AOI = [115.90, 29.00, 116.00, 29.10]    # 0.10°×0.10°，两期 QA 有效比例 0.9474 / 0.8373
PREFERRED_GRID = "WRS2-122040"         # 同一 WRS2 path/row（Landsat 无 MGRS 字段）
WINDOWS = {
    "t1": "2023-06-01T00:00:00Z/2023-09-30T23:59:59Z",
    "t2": "2024-06-01T00:00:00Z/2024-09-30T23:59:59Z",
}
MAX_CLOUD = 10.0
ATTEMPTS = 6
# 内部统一波段键 → (STAC 资产键, 本地/MinIO 文件名后缀)
ASSETS = {
    "B02": ("blue", "SR_B2"),
    "B03": ("green", "SR_B3"),
    "B04": ("red", "SR_B4"),
    "B08": ("nir08", "SR_B5"),
    "SCL": ("qa_pixel", "QA_PIXEL"),
}
STAGE_ROOT = Path(os.getenv(
    "LANDSAT_STAGE_ROOT", r"C:\Test\_scratch\landsat_poyang"))
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # 与 seed_scenes.py 同命名空间

_TOKEN = {"value": "", "ts": 0.0}


# --------------------------------------------------------------------------- #
# HTTP / STAC
# --------------------------------------------------------------------------- #
def _http(url: str, payload: dict | None = None, attempts: int = 4) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    last = None
    for k in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"    [http retry {k}/{attempts}] {type(exc).__name__}: {exc}", flush=True)
            time.sleep(min(20, 2 ** k))
    raise last


def sas_token(force: bool = False) -> str:
    """取 Planetary Computer 只读 SAS token（30 分钟主动刷新，避免长任务过期）。"""
    now = time.time()
    if force or not _TOKEN["value"] or now - _TOKEN["ts"] > 1800:
        _TOKEN["value"] = _http(MPC_SAS)["token"]
        _TOKEN["ts"] = now
    return _TOKEN["value"]


def stac_candidates(dt: str) -> dict:
    """earth-search v1 检索：仅 LC08/LC09、云量 <10%，按 grid:code 取云量最低景。"""
    js = _http(ES_SEARCH, {
        "collections": [COLLECTION], "bbox": SEARCH_BBOX,
        "datetime": dt, "limit": 100,
        "query": {"eo:cloud_cover": {"lt": MAX_CLOUD}},
    })
    out = {}
    for f in js.get("features", []):
        sid = f["id"]
        if not sid.startswith(("LC08", "LC09")):
            continue
        p = f["properties"]
        cc = float(p.get("eo:cloud_cover") or 999.0)
        if cc >= MAX_CLOUD:
            continue
        grid = str(p.get("grid:code") or "")
        if not grid:
            continue
        platform = str(p.get("platform") or "")
        sensor = SENSOR_L9 if "9" in platform or sid.startswith("LC09") else SENSOR_L8
        rec = {"id": sid, "date": str(p.get("datetime"))[:10],
               "cloud_cover": cc, "grid": grid, "sensor": sensor,
               "collection": COLLECTION}
        cur = out.get(grid)
        if cur is None or cc < cur["cloud_cover"]:
            out[grid] = rec
    return out


def choose_pair():
    """取两期共同 grid:code 的一对（优先 PREFERRED_GRID），并强制同 tile / 低云。"""
    c1, c2 = stac_candidates(WINDOWS["t1"]), stac_candidates(WINDOWS["t2"])
    print(f"[stac] 2023 低云候选 {len(c1)} 个 tile；2024 低云候选 {len(c2)} 个 tile", flush=True)
    common = sorted(set(c1) & set(c2),
                    key=lambda g: (0 if g == PREFERRED_GRID else 1,
                                   c1[g]["cloud_cover"] + c2[g]["cloud_cover"]))
    if not common:
        raise RuntimeError("两期没有共同的 grid:code（同一 WRS2 tile），拒绝伪造配对")
    grid = common[0]
    t1, t2 = c1[grid], c2[grid]
    if t1["grid"] != t2["grid"]:
        raise RuntimeError("两期 tile 不一致")
    for tag, rec in (("t1", t1), ("t2", t2)):
        print(f"[stac] {tag}: id={rec['id']} date={rec['date']} "
              f"cloud={rec['cloud_cover']}% grid={rec['grid']} sensor={rec['sensor']}",
              flush=True)
    return grid, {"t1": t1, "t2": t2}


def mpc_items(ids) -> dict:
    """按同一 USGS 产品 id 取可匿名下载的 Azure Blob 资产 href。"""
    js = _http(MPC_SEARCH, {"collections": [COLLECTION], "ids": list(ids), "limit": 10})
    items = {f["id"]: f for f in js.get("features", [])}
    missing = [i for i in ids if i not in items]
    if missing:
        raise RuntimeError(f"下载镜像缺少同名 USGS 产品，拒绝替代数据：{missing}")
    return items


def signed_url(feature: dict, asset_key: str, force_token: bool = False) -> str:
    href = feature["assets"][asset_key]["href"]
    sep = "&" if "?" in href else "?"
    return "/vsicurl/" + href + sep + sas_token(force=force_token)


# --------------------------------------------------------------------------- #
# /vsicurl 窗口读取（照抄 fetch_pairs_batch2.py 模式）
# --------------------------------------------------------------------------- #
def window_for(src, aoi):
    xs, ys = transform("EPSG:4326", src.crs, [aoi[0], aoi[2]], [aoi[1], aoi[3]])
    r1, c1 = src.index(xs[0], ys[1])
    r2, c2 = src.index(xs[1], ys[0])
    row0 = max(0, int(round(min(r1, r2))))
    row1 = min(src.height, int(round(max(r1, r2))))
    col0 = max(0, int(round(min(c1, c2))))
    col1 = min(src.width, int(round(max(c1, c2))))
    if col1 <= col0 or row1 <= row0:
        raise ValueError("AOI 完全落在该景覆盖范围之外")
    return Window(col0, row0, col1 - col0, row1 - row0)


def valid_local(path) -> bool:
    """体积门槛 + rasterio 真实打开读取（写后校验，杜绝伪造下载成功）。"""
    p = Path(path)
    if not p.is_file() or p.stat().st_size < 512:
        return False
    try:
        with rasterio.open(str(p)) as s:
            s.read(1, window=Window(0, 0, min(4, s.width), min(4, s.height)))
        return True
    except Exception:  # noqa: BLE001
        return False


def read_band_window(feature, asset_key, aoi, attempts=ATTEMPTS):
    """带 6 次指数退避重试地读取远端 COG 波段窗口。"""
    last = None
    for k in range(1, attempts + 1):
        try:
            url = signed_url(feature, asset_key, force_token=(k > 1 and k % 3 == 1))
            with rasterio.open(url) as src:
                w = window_for(src, aoi)
                data = src.read(1, window=w)
                prof = dict(src.profile)
                wtrans = src.window_transform(w)
                return data, prof, wtrans
        except Exception as exc:  # noqa: BLE001
            last = exc
            wait = min(30, 2 ** k)
            print(f"    [retry {k}/{attempts}] {asset_key}: {type(exc).__name__}, "
                  f"{wait}s 后重试", flush=True)
            time.sleep(wait)
    raise last


def qa_valid_ratio(feature, aoi, attempts=ATTEMPTS):
    """真实读取 QA_PIXEL 窗口，按常量源位定义统计有效像元占比（不伪造）。"""
    data, _prof, _w = read_band_window(feature, "qa_pixel", aoi, attempts=attempts)
    import numpy as np
    qa = data.astype(np.uint32)
    bad = np.zeros(qa.shape, dtype=bool)
    for bit in QA_PIXEL_INVALID_BITS:
        bad |= (qa & (np.uint32(1) << np.uint32(bit))) != 0
    return float((~bad).mean())


def download_bands(items, picks, stage: Path, log: dict) -> dict:
    """下载 10 个 COG 窗口；逐波段记录耗时 / 体积 / 形状并写后校验。"""
    files = {"t1": [], "t2": []}
    for tag in ("t1", "t2"):
        rec = picks[tag]
        feat = items[rec["id"]]
        for band, (asset_key, suffix) in ASSETS.items():
            out = stage / f"{tag}_{suffix}.tif"
            if valid_local(out):
                print(f"  skip {out.name}（已存在且有效，断点续传）", flush=True)
                files[tag].append(out)
                log["bands"].append({"tag": tag, "band": band, "file": out.name,
                                     "bytes": out.stat().st_size, "elapsed_sec": 0.0,
                                     "resumed": True})
                continue
            t0 = time.time()
            data, prof, wtrans = read_band_window(feat, asset_key, AOI)
            prof.update(height=data.shape[0], width=data.shape[1], transform=wtrans)
            # .tmp.tif 必须带 .tif 扩展名，否则 rasterio 重开校验时无法识别驱动
            tmp = Path(str(out) + ".tmp.tif")
            with rasterio.open(str(tmp), "w", **prof) as dst:
                dst.write(data, 1)
            if not valid_local(tmp):
                raise RuntimeError(f"写后校验失败（体积/可打开性），拒绝落盘：{tmp}")
            os.replace(str(tmp), str(out))
            elapsed = round(time.time() - t0, 2)
            size = out.stat().st_size
            print(f"  wrote {out.name} shape={data.shape} dtype={data.dtype} "
                  f"bytes={size} elapsed={elapsed}s", flush=True)
            files[tag].append(out)
            log["bands"].append({"tag": tag, "band": band, "asset": asset_key,
                                 "file": out.name, "bytes": size,
                                 "elapsed_sec": elapsed, "shape": list(data.shape),
                                 "dtype": str(data.dtype), "resumed": False})
    if sum(len(v) for v in files.values()) != len(ASSETS) * 2:
        raise RuntimeError("波段数不足 10，拒绝上传与入库")
    return files


# --------------------------------------------------------------------------- #
# MinIO / PostGIS
# --------------------------------------------------------------------------- #
def upload(files: dict, log: dict):
    client = get_client(StorageSettings.from_env())
    ensure_buckets(client, [COG_BUCKET])
    uploaded = []
    for tag in ("t1", "t2"):
        for p in files[tag]:
            obj = f"{NAME}/{tag}/{p.name}"
            upload_file(client, COG_BUCKET, obj, str(p))
            uploaded.append(obj)
            print(f"  [up] {obj} ({p.stat().st_size} bytes)", flush=True)
    log["minio_objects"] = uploaded
    log["minio_scene_objects"] = len(
        [o for o in list_objects(client, COG_BUCKET, prefix=f"{NAME}/")])
    return uploaded


def _resolve_existing_seed_aoi(cur) -> str:
    """复用任一既有 seed aoi 作 FK，避免新增第 12 个 seed aoi 改变 cleanup 复位基线。"""
    cur.execute(
        "SELECT aoi_id::text FROM scene WHERE cog_path LIKE %s LIMIT 1",
        ("minio://scenes/pair_%/%",),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("未找到既有 seed aoi；拒绝新建以保住 DB 复位基线")
    return str(row[0])


def upsert_scenes(grid, picks, log: dict):
    minx, miny, maxx, maxy = AOI
    scene_ids = []
    conn = psycopg2.connect(postgres_dsn())
    try:
        with conn.cursor() as cur:
            aoi_id = _resolve_existing_seed_aoi(cur)
            log["reused_aoi_id"] = aoi_id
            for tag in ("t1", "t2"):
                rec = picks[tag]
                sid = str(uuid.uuid5(NS, f"scene:{NAME}:{tag}:{rec['id']}"))
                cog = object_url(COG_BUCKET, f"{NAME}/{tag}/")
                cur.execute(
                    """
                    INSERT INTO scene (scene_id, aoi_id, sensor, acq_date, cloud_cover,
                                       cog_path, proc_baseline, radiometric_formula,
                                       tile, footprint)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            ST_MakeEnvelope(%s, %s, %s, %s, 4326))
                    ON CONFLICT (scene_id) DO UPDATE SET
                        cloud_cover = EXCLUDED.cloud_cover,
                        cog_path = EXCLUDED.cog_path,
                        proc_baseline = EXCLUDED.proc_baseline,
                        radiometric_formula = EXCLUDED.radiometric_formula,
                        tile = EXCLUDED.tile,
                        footprint = EXCLUDED.footprint
                    RETURNING scene_id
                    """,
                    (sid, aoi_id, rec["sensor"], rec["date"], rec["cloud_cover"],
                     cog, LS_COLLECTION, LS_FORMULA, grid,
                     minx, miny, maxx, maxy),
                )
                scene_ids.append(cur.fetchone()[0])
                print(f"  [db] {tag} scene_id={sid} acq_date={rec['date']} "
                      f"sensor={rec['sensor']} cog_path={cog}", flush=True)
        conn.commit()
    finally:
        conn.close()
    log["scene_ids"] = [str(s) for s in scene_ids]
    return scene_ids


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="真实下载并入库 1 组 Landsat C2 L2 双时相 COG")
    ap.add_argument("--dry-run", action="store_true", help="只检索 + QA 探测，不下载 / 上传 / 入库")
    ap.add_argument("--stage-root", default=str(STAGE_ROOT), help="COG 窗口落盘目录（仓外）")
    args = ap.parse_args()
    stage = Path(args.stage_root)

    t_start = time.time()
    log = {"name": NAME, "label": LABEL, "collection": COLLECTION,
           "aoi_wgs84": AOI, "search_bbox": SEARCH_BBOX,
           "stac_url": ES_SEARCH, "download_mirror": MPC_SEARCH,
           "attempts_per_band": ATTEMPTS, "bands": []}
    grid, picks = choose_pair()
    log["grid"] = grid
    log["scenes"] = picks

    items = mpc_items([picks["t1"]["id"], picks["t2"]["id"]])
    for tag in ("t1", "t2"):
        ratio = qa_valid_ratio(items[picks[tag]["id"]], AOI)
        print(f"[qa] {tag} AOI 有效像元占比 = {ratio:.4f}", flush=True)
        log.setdefault("qa_valid_ratio", {})[tag] = ratio
        if ratio < 0.5:
            raise RuntimeError(f"{tag} AOI 有效像元占比 {ratio:.4f} < 0.50，拒绝入库")
    if args.dry_run:
        print("[dry-run] 检索与 QA 探测完成；未下载、未上传、未入库", flush=True)
        return 0

    stage.mkdir(parents=True, exist_ok=True)
    print(f"[download] 落盘目录 {stage}（仓外，不污染 data/）", flush=True)
    files = download_bands(items, picks, stage, log)

    manifest = {
        "name": NAME, "label": LABEL, "tile": grid,
        "aoi_wgs84": AOI, "aoi_span_deg": round(AOI[2] - AOI[0], 4),
        "collection": COLLECTION, "sensor": picks["t1"]["sensor"],
        "proc_baseline": LS_COLLECTION, "radiometric_formula": LS_FORMULA,
        "assets": {k: list(v) for k, v in ASSETS.items()},
        "scenes": {tag: dict(picks[tag], local_files=[p.name for p in files[tag]])
                   for tag in ("t1", "t2")},
    }
    (stage / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print("[minio] 上传 10 个 COG 到 scenes 桶", flush=True)
    upload(files, log)
    print("[db] 写入 scene 表两行", flush=True)
    upsert_scenes(grid, picks, log)

    log["total_elapsed_sec"] = round(time.time() - t_start, 2)
    log["total_bytes"] = sum(b["bytes"] for b in log["bands"])
    (stage / "download_report.json").write_text(
        json.dumps(log, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print("\n============ Landsat 下载汇总 ============", flush=True)
    print(f"tile={grid}  时相={picks['t1']['date']}({picks['t1']['cloud_cover']}%) → "
          f"{picks['t2']['date']}({picks['t2']['cloud_cover']}%)", flush=True)
    print(f"COG 数={len(log['bands'])}  总体积={log['total_bytes']} bytes  "
          f"总耗时={log['total_elapsed_sec']}s", flush=True)
    for b in log["bands"]:
        print(f"  {b['file']:<22} {b['bytes']:>9} bytes  {b['elapsed_sec']:>6}s", flush=True)
    print(f"报告：{stage / 'download_report.json'}", flush=True)
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

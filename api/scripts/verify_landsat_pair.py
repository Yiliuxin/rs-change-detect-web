# -*- coding: utf-8 -*-
"""Landsat C2 L2 影像对全链路验收（风格同 ``verify_all_pairs.py``）。

链路：POST /aoi/select → POST /result/change → POST /report/export
数据源：``scene`` 表 ``cog_path LIKE 'minio://scenes/landsat_%'`` 的真实入库行，
时相 / 云量 / tile 一律从库里读，不在脚本里硬编码或伪造。

用法：
  cd C:/Test/Web/api
  C:/Users/Yilia/AppData/Local/Programs/Python/Python312/python.exe scripts/verify_landsat_pair.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.env import load_env  # noqa: E402
load_env(override=True)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.repository.db import db_cursor  # noqa: E402
from app.service.common import RUNS_ROOT  # noqa: E402

AOI = [115.90, 29.00, 116.00, 29.10]
FORMATS = ["geotiff", "png", "pdf"]


def cleanup_runs(run_ids):
    """自建自清：删掉本脚本产生的 task / change_result / sample / aoi，保持 DB 复位基线。

    ``cleanup_test_data.py`` 会为每个 ``cog_path`` 组保留最新一条 SUCCESS run；
    若不清理，Landsat 组会多留 1 条 kept task，导致复位计数从 11/33/22 漂到 12/36/23。
    """
    import shutil
    if not run_ids:
        return
    with db_cursor() as cur:
        cur.execute("SELECT aoi_id::text FROM task WHERE run_id = ANY(%s::uuid[])",
                    (run_ids,))
        aoi_ids = [r["aoi_id"] for r in cur.fetchall()]
        cur.execute("DELETE FROM change_result WHERE run_id = ANY(%s::uuid[])", (run_ids,))
        cur.execute("DELETE FROM sample WHERE run_id = ANY(%s::uuid[])", (run_ids,))
        cur.execute("DELETE FROM task WHERE run_id = ANY(%s::uuid[])", (run_ids,))
        cur.execute(
            "DELETE FROM aoi WHERE aoi_id = ANY(%s::uuid[])"
            " AND aoi_id NOT IN (SELECT aoi_id FROM scene)", (aoi_ids,),
        )
    for rid in run_ids:
        shutil.rmtree(RUNS_ROOT / rid, ignore_errors=True)
    print(f"[cleanup] 已清理本轮 {len(run_ids)} 个 run 的 DB 行与产物目录", flush=True)


def load_pair():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT acq_date::text AS acq_date, cloud_cover::text AS cloud_cover,
                   sensor, tile, cog_path
            FROM scene WHERE cog_path LIKE 'minio://scenes/landsat_%'
            ORDER BY acq_date
            """
        )
        rows = [dict(r) for r in cur.fetchall()]
    if len(rows) != 2:
        raise RuntimeError(f"期望 2 条 Landsat scene，实际 {len(rows)}；请先运行 data/fetch_landsat.py")
    return rows


def polygon(aoi):
    return {"type": "Polygon", "coordinates": [[
        [aoi[0], aoi[1]], [aoi[2], aoi[1]], [aoi[2], aoi[3]],
        [aoi[0], aoi[3]], [aoi[0], aoi[1]],
    ]]}


def run_chain(client, rows, method="otsu"):
    t1, t2 = rows[0]["acq_date"], rows[1]["acq_date"]
    t0 = time.time()
    print(f"\n[1] POST /api/v1/aoi/select  t1={t1} t2={t2} aoi={AOI}", flush=True)
    r1 = client.post("/api/v1/aoi/select", json={
        "aoi_geom": json.dumps(polygon(AOI)), "t1": t1, "t2": t2,
    })
    print(f"    -> {r1.status_code} {r1.json() if r1.status_code != 200 else r1.json()['run_id']}",
          flush=True)
    assert r1.status_code == 200, r1.text
    matched = r1.json()["matched_scenes"]
    print(f"    matched sensor={[s['sensor'] for s in matched]} "
          f"baseline={[s['proc_baseline'] for s in matched]}", flush=True)
    run_id = r1.json()["run_id"]

    print(f"[2] POST /api/v1/result/change  method={method} index=NDVI", flush=True)
    r2 = client.post("/api/v1/result/change",
                     json={"run_id": run_id, "index": "NDVI", "method": method})
    if r2.status_code != 200:
        print(f"    -> {r2.status_code} {r2.json()}", flush=True)
        raise RuntimeError(f"result/change 失败：{r2.text}")
    j = r2.json()
    snap = j["params_snapshot"]
    print(f"    -> 200 valid_ratio={j['valid_ratio']:.4f} threshold={j['threshold']:.4f} "
          f"sensor={snap['sensor']} formula={snap['formula']}", flush=True)
    print(f"    stats={[(s['change_class'], s['pixel_count'], round(s['area_ha'], 3)) for s in j['stats']]}",
          flush=True)
    print(f"    artifacts={sorted(j['artifacts'])}", flush=True)

    print("[3] POST /api/v1/report/export  formats=geotiff/png/pdf", flush=True)
    r3 = client.post("/api/v1/report/export",
                     json={"run_id": run_id, "formats": FORMATS})
    assert r3.status_code == 200, r3.text
    files = r3.json()["files"]
    for f in files:
        got = client.get(f["url"])
        print(f"    {f['format']:<8} size={f['size']:>8} url={f['url']} GET={got.status_code}",
              flush=True)
        assert got.status_code == 200 and f["size"] > 0
    print(f"    attribution={r3.json()['attribution']}", flush=True)
    elapsed = round(time.time() - t0, 2)
    print(f"[OK] 全链路通过 method={method}，耗时 {elapsed}s", flush=True)
    return {"run_id": run_id, "method": method, "valid_ratio": j["valid_ratio"],
            "threshold": j["threshold"], "stats": j["stats"], "files": len(files),
            "elapsed_sec": elapsed}


def main():
    rows = load_pair()
    print("| 组 | tile | 时相 | 云量% | sensor | 基线 | cog_path |", flush=True)
    print("|---|---|---|---|---|---|---|", flush=True)
    for r in rows:
        print(f"| landsat_poyang | {r['tile']} | {r['acq_date']} | {r['cloud_cover']} "
              f"| {r['sensor']} | C2L2 | {r['cog_path']} |", flush=True)
    results = []
    run_ids = []
    try:
        with TestClient(app) as c:
            for method in ("otsu", "regression"):
                item = run_chain(c, rows, method=method)
                results.append(item)
                run_ids.append(item["run_id"])
    finally:
        cleanup_runs(run_ids)
    print("\n" + json.dumps(results, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

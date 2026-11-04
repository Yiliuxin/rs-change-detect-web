# -*- coding: utf-8 -*-
"""对所有预置影像对跑一遍完整链路，输出 Markdown 验收表。

链路：POST /aoi/select → POST /result/change → POST /report/export
（精度评估单独由 pytest 的 EX10 用例覆盖，此处不重复触发以免拖慢）

兼容两种 manifest 结构：
  批次 1: scenes = {"t1_20231102": {...}, "t2_20231227": {...}}（日期在键名）
  批次 2: scenes = {"t1": {"date": "...", ...}, "t2": {...}}

用法：
  cd C:/Test/Web/api
  C:/Users/Yilia/AppData/Local/Programs/Python/Python312/python.exe scripts/verify_all_pairs.py
"""
import sys, json, pathlib, re, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config.env import load_env
load_env(override=True)

from fastapi.testclient import TestClient
from app.main import app

DATA = pathlib.Path(r"C:\Test\Web\data")


def _phase(m, tag):
    sc = m["scenes"]
    if tag in sc:
        return sc[tag], tag
    for k, v in sc.items():
        if k.startswith(tag):
            return v, k
    raise KeyError(f"manifest 缺少 {tag}")


def _date(v, k):
    hit = re.search(r"(20\d{2})(\d{2})(\d{2})", k)
    if hit:
        return f"{hit.group(1)}-{hit.group(2)}-{hit.group(3)}"
    return v.get("date") or (v.get("datetime") or "")[:10]


def collect():
    rows = []
    for d in sorted(DATA.glob("pair_*")):
        mf = d / "manifest.json"
        if not mf.exists():
            continue
        m = json.loads(mf.read_text(encoding="utf-8"))
        name = m.get("name") or d.name.split("_")[1]
        v1, k1 = _phase(m, "t1")
        v2, k2 = _phase(m, "t2")
        rows.append({
            "name": name, "label": m.get("label") or name, "dir": d.name,
            "aoi": m["aoi_wgs84"], "tile": m.get("tile") or "-",
            "t1": _date(v1, k1), "t2": _date(v2, k2),
            "cloud": (v1.get("cloud_cover"), v2.get("cloud_cover")),
        })
    return rows


def main():
    rows = collect()
    print(f"发现 {len(rows)} 组影像对\n")
    c = TestClient(app)

    hdr = ("| 组 | 名称 | tile | 时相 | 云量% | valid_ratio | 阈值 | 增强(像素/占比) "
           "| 退化 | 稳定 | 三件套 | 耗时 |")
    sep = "|---|---|---|---|---|---|---|---|---|---|---|---|"
    print(hdr)
    print(sep)

    ok = 0
    fails = []
    for r in rows:
        a = r["aoi"]
        poly = {"type": "Polygon", "coordinates": [[
            [a[0], a[1]], [a[2], a[1]], [a[2], a[3]], [a[0], a[3]], [a[0], a[1]]]]}
        t0 = time.time()
        r1 = c.post("/api/v1/aoi/select",
                    json={"aoi_geom": json.dumps(poly), "t1": r["t1"], "t2": r["t2"]})
        if r1.status_code != 200:
            fails.append((r["name"], f"aoi/select {r1.status_code} {r1.json().get('code')}"))
            print(f"| {r['name']} | {r['label']} | {r['tile']} | {r['t1']}→{r['t2']} | - | - | - | - | - | - | ❌ {r1.json().get('code')} | - |")
            continue
        run = r1.json()["run_id"]
        r2 = c.post("/api/v1/result/change",
                    json={"run_id": run, "index": "NDVI", "block_size": 256})
        dt = time.time() - t0
        if r2.status_code != 200:
            code = r2.json().get("code")
            fails.append((r["name"], f"result/change {r2.status_code} {code}"))
            print(f"| {r['name']} | {r['label']} | {r['tile']} | {r['t1']}→{r['t2']} "
                  f"| {r['cloud'][0]:.2f}/{r['cloud'][1]:.2f} | - | - | - | - | - | ❌ {code} | {dt:.1f}s |")
            continue
        j = r2.json()
        st = {s["change_class"]: s for s in j["stats"]}
        tot = sum(s["pixel_count"] for s in j["stats"]) or 1
        e, dg, s3 = st.get(1, {}), st.get(2, {}), st.get(3, {})

        r3 = c.post("/api/v1/report/export", json={"run_id": run, "formats": ["geotiff", "png", "pdf"]})
        trio = "3/3" if r3.status_code == 200 and len(r3.json().get("files", [])) == 3 else f"❌{r3.status_code}"
        if r3.status_code != 200:
            fails.append((r["name"], f"report/export {r3.status_code} {r3.json().get('code')}"))

        print(f"| {r['name']} | {r['label']} | {r['tile']} | {r['t1']}→{r['t2']} "
              f"| {r['cloud'][0]:.2f}/{r['cloud'][1]:.2f} | {j['valid_ratio']:.4f} | {j['threshold']:.4f} "
              f"| {e.get('pixel_count',0)}/{e.get('pixel_count',0)/tot*100:.1f}% "
              f"| {dg.get('pixel_count',0)}/{dg.get('pixel_count',0)/tot*100:.1f}% "
              f"| {s3.get('pixel_count',0)}/{s3.get('pixel_count',0)/tot*100:.1f}% "
              f"| {trio} | {dt:.1f}s |")
        ok += 1

    print(f"\n通过 {ok}/{len(rows)} 组")
    if fails:
        print("失败明细：")
        for n, why in fails:
            print(f"  - {n}: {why}")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

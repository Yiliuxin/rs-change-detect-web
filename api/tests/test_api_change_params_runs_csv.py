# -*- coding: utf-8 -*-
"""接口测：变化检测参数扩展 + 历史 run API + 面积 CSV（REQ-07/13/14）。

覆盖：
  * EX12 错误码文案/HTTP 映射，index 非法、threshold 越界、分页参数非法边界；
  * NDWI 真链路（预置鄱阳湖影像对）+ 手动阈值跳过 Otsu + 阈值来源快照；
  * 双时相真彩合成 t1/t2_composite.png 落盘并进入产物清单；
  * 带 index 小写后缀的归档产物互不覆盖且 URL 可达；
  * /runs 分页与字段、详情参数快照全量 + 全部产物 URL、EX2 404；
  * CSV 的 UTF-8 BOM、表头、增强/退化/稳定三行内容与 DB 口径一致。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.algorithm.index_calculator import ndvi, ndwi
from app.api.errors import CODE_HTTP
from app.config.constants import ERROR_MESSAGES, EXPORT_FORMATS, S2_FORMULA
from app.service.common import ARTIFACT_MEDIA_TYPES, run_dir
from _helpers import (
    POYANG_T1, POYANG_T2, assert_error_body, poyang_geojson,
)

CHANGE_URL = "/api/v1/result/change"
RUNS_URL = "/api/v1/runs"
CSV_URL = "/api/v1/report/area_stats/{run_id}.csv"
UNKNOWN_RUN = "00000000-0000-0000-0000-0000000000ff"
MANUAL_T = 0.18


@pytest.fixture(scope="module")
def ndwi_ready(client):
    """真实跑一次「预置鄱阳湖影像对 → NDWI + 手动阈值」全链路（module 级只跑一次）。"""
    a = client.post("/api/v1/aoi/select", json={
        "aoi_geom": poyang_geojson(), "t1": POYANG_T1, "t2": POYANG_T2,
    })
    assert a.status_code == 200, a.text
    run_id = a.json()["run_id"]
    r = client.post(CHANGE_URL, json={
        "run_id": run_id, "index": "NDWI", "threshold": MANUAL_T,
    })
    assert r.status_code == 200, r.text
    return {"run_id": run_id, "body": r.json(), "aoi": a.json()}


# --------------------------------------------------------------------------- #
# 1. EX12 错误码
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-07")
def test_ex12_message_and_http_mapping():
    """EX12 文案逐字取常量、HTTP=400，且既有错误码映射未被改动。"""
    assert ERROR_MESSAGES["EX12"] == "请求参数非法，请检查输入后重试"
    assert CODE_HTTP["EX12"] == 400
    assert CODE_HTTP["EX2"] == 404 and CODE_HTTP["EX7"] == 409
    assert CODE_HTTP["EX11"] == 400


@pytest.mark.req("REQ-07")
@pytest.mark.parametrize("bad_index", ["EVI", "", "NDVI2", "ndwi2", "SAVI"])
def test_ex12_illegal_index(client, change_ready, bad_index):
    """非法 index → 400 EX12（中文提示来自常量），不改动 task 状态。"""
    r = client.post(CHANGE_URL, json={
        "run_id": change_ready["run_id"], "index": bad_index,
    })
    body = assert_error_body(r, "EX12", 400)
    assert body["run_id"] == change_ready["run_id"]


@pytest.mark.req("REQ-07")
@pytest.mark.parametrize("bad_threshold", [-1.0, 1.0, -1.5, 2.0, 1e9])
def test_ex12_threshold_out_of_open_interval(client, change_ready, bad_threshold):
    """threshold 须落在 (-1, 1) 开区间：-1 / 1 及越界值均 → EX12。"""
    r = client.post(CHANGE_URL, json={
        "run_id": change_ready["run_id"], "threshold": bad_threshold,
    })
    assert_error_body(r, "EX12", 400)


@pytest.mark.req("REQ-14")
@pytest.mark.parametrize("qs", ["limit=0", "limit=101", "offset=-1"])
def test_ex12_illegal_pagination(client, qs):
    """分页参数越界 → 400 EX12（不带 run_id 的错误体亦合法）。"""
    r = client.get(f"{RUNS_URL}?{qs}")
    assert_error_body(r, "EX12", 400, run_id_required=False)


# --------------------------------------------------------------------------- #
# 2. 缺省 NDVI 行为不变 + 双时相真彩合成
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-07")
@pytest.mark.req("REQ-14")
def test_default_ndvi_behaviour_unchanged(client, change_ready):
    """不传 index/threshold：主名 URL、Otsu 阈值、快照字段与历史一致。"""
    body = change_ready["change_body"]
    rid = change_ready["run_id"]
    assert body["index"] == "NDVI"
    assert body["threshold_source"] == "otsu"
    assert body["change_geotiff"] == f"/api/v1/artifacts/{rid}/change.tif"
    assert body["change_png"] == f"/api/v1/artifacts/{rid}/change.png"
    snap = body["params_snapshot"]
    assert snap["index"] == "NDVI" and snap["formula"] == S2_FORMULA
    assert snap["threshold"] == pytest.approx(body["threshold"])
    assert snap["threshold_source"] == "otsu"
    assert snap["index_bands"] == ["B04", "B08"]
    assert body["stats"] and [s["change_class"] for s in body["stats"]] == [1, 2, 3]


@pytest.mark.req("REQ-10")
def test_both_phase_true_color_composites_in_manifest(client, change_ready):
    """t1/t2_composite.png 真实 PNG 落盘、出现在产物清单且同源 URL 可达。"""
    rid = change_ready["run_id"]
    out = run_dir(rid)
    manifest = change_ready["change_body"]["artifacts"]
    assert manifest
    assert set(manifest) <= set(ARTIFACT_MEDIA_TYPES)
    for name in ("t1_composite.png", "t2_composite.png"):
        path = out / name
        assert path.is_file() and path.stat().st_size > 0
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert name in manifest
        got = client.get(manifest[name])
        assert got.status_code == 200 and got.content[:8] == b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# 3. NDWI 真链路 + 手动阈值
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-06")
def test_ndwi_pure_function_is_mcfeeters_formula():
    """NDWI=(G−NIR)/(G+NIR)，与 NDVI 不是同一个函数（真链路波段分派依据）。"""
    g = np.array([[0.03, 0.10], [0.20, 0.05]], dtype=np.float32)
    n = np.array([[0.30, 0.08], [0.10, 0.02]], dtype=np.float32)
    want = np.clip((g - n) / (g + n), -1.0, 1.0)
    assert np.allclose(ndwi(g, n), want, atol=1e-6)
    assert not np.allclose(ndwi(g, n), ndvi(g, n), atol=1e-3)


@pytest.mark.req("REQ-07")
@pytest.mark.req("REQ-14")
def test_ndwi_real_pipeline_with_manual_threshold(ndwi_ready):
    """预置影像对真跑 NDWI：绿+近红外波段、手动阈值覆盖 Otsu、三类统计齐全。"""
    body = ndwi_ready["body"]
    snap = body["params_snapshot"]
    assert body["run_id"] == ndwi_ready["run_id"]
    assert body["index"] == "NDWI" and snap["index"] == "NDWI"
    assert snap["index_bands"] == ["B03", "B08"]
    assert body["threshold"] == pytest.approx(MANUAL_T)
    assert body["threshold_source"] == "manual"
    assert snap["threshold"] == pytest.approx(MANUAL_T)
    assert snap["threshold_source"] == "manual"
    assert snap["otsu_threshold"] == pytest.approx(MANUAL_T)
    assert snap["otsu_fallback"] is False
    assert "手动" in str(snap["otsu_reason"])
    assert [s["change_class"] for s in body["stats"]] == [1, 2, 3]
    assert snap["formula"] == S2_FORMULA


@pytest.mark.req("REQ-07")
@pytest.mark.req("REQ-12")
def test_ndwi_artifacts_suffixed_reachable_and_isolated(client, ndwi_ready, change_ready):
    """NDWI 产物带 ndwi 后缀、与主名内容一致；NDVI run 只有 ndvi 归档（互不覆盖）。"""
    rid = ndwi_ready["run_id"]
    body = ndwi_ready["body"]
    assert body["change_geotiff"].endswith("/change_ndwi.tif")
    assert body["change_png"].endswith("/change_ndwi.png")
    out = run_dir(rid)
    for name in ("change_ndwi.tif", "change_ndwi.png", "report_ndwi.pdf",
                 "t1_composite.png", "t2_composite.png"):
        assert (out / name).is_file() and (out / name).stat().st_size > 0
        assert name in body["artifacts"]
        got = client.get(body["artifacts"][name])
        assert got.status_code == 200 and len(got.content) > 0
    assert (out / "change_ndwi.tif").read_bytes() == (out / "change.tif").read_bytes()
    # 不同 index 的归档文件名分离，互不覆盖
    assert "change_ndvi.tif" in change_ready["change_body"]["artifacts"]
    assert "change_ndwi.tif" not in change_ready["change_body"]["artifacts"]
    assert "change_ndvi.tif" not in body["artifacts"]


@pytest.mark.req("REQ-12")
def test_ndwi_run_report_export_still_works(client, ndwi_ready):
    """NDWI + 手动阈值后，/report/export 三件套仍可导出且 URL 同源可达。"""
    r = client.post("/api/v1/report/export", json={
        "run_id": ndwi_ready["run_id"], "formats": list(EXPORT_FORMATS),
    })
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    assert len(files) == len(EXPORT_FORMATS)
    for f in files:
        assert f["size"] > 0
        assert client.get(f["url"]).status_code == 200


# --------------------------------------------------------------------------- #
# 4. 面积统计 CSV
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-08")
@pytest.mark.req("REQ-11")
def test_area_stats_csv_bom_header_and_three_rows(client, ndwi_ready):
    """UTF-8 BOM + 中文表头 + 增强/退化/稳定三行，数值与 change 响应一致。"""
    rid = ndwi_ready["run_id"]
    r = client.get(CSV_URL.format(run_id=rid))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.content.startswith(b"\xef\xbb\xbf")          # \ufeff BOM
    lines = r.content.decode("utf-8-sig").splitlines()
    assert lines[0] == "类别,像元数,面积_公顷"
    assert len(lines) == 4
    assert [ln.split(",")[0] for ln in lines[1:]] == ["增强", "退化", "稳定"]

    stats = {int(s["change_class"]): s for s in ndwi_ready["body"]["stats"]}
    for code, line in zip((1, 2, 3), lines[1:]):
        label, pixels, area = line.split(",")
        assert label in ("增强", "退化", "稳定")
        assert int(pixels) == stats[code]["pixel_count"]
        assert float(area) == pytest.approx(stats[code]["area_ha"], abs=1e-3)


@pytest.mark.req("REQ-08")
def test_area_stats_csv_ex2_unknown_run(client):
    r = client.get(CSV_URL.format(run_id=UNKNOWN_RUN))
    assert_error_body(r, "EX2", 404)


# --------------------------------------------------------------------------- #
# 5. 历史 run 列表 / 详情
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-14")
def test_runs_list_default_limit_and_pagination(client, ndwi_ready, change_ready):
    """默认 limit=20；两页不重叠；按 created_at 倒序；字段与三类面积合计齐全。"""
    d = client.get(RUNS_URL)
    assert d.status_code == 200 and d.json()["limit"] == 20 and d.json()["offset"] == 0

    p1 = client.get(f"{RUNS_URL}?limit=3&offset=0")
    p2 = client.get(f"{RUNS_URL}?limit=3&offset=3")
    assert p1.status_code == 200 and p2.status_code == 200
    b1, b2 = p1.json(), p2.json()
    assert b1["total"] >= 4 and b1["total"] == b2["total"]
    assert b1["limit"] == 3 and b1["offset"] == 0 and b2["offset"] == 3
    ids1 = [it["run_id"] for it in b1["items"]]
    ids2 = [it["run_id"] for it in b2["items"]]
    assert ids1 and not (set(ids1) & set(ids2))
    dates = [it["created_at"] for it in b1["items"]]
    assert all(dates[i] >= dates[i + 1] for i in range(len(dates) - 1))

    required = {"run_id", "aoi_name", "bbox", "t1", "t2", "t1_source",
                "t2_source", "created_at", "threshold", "total_area_ha"}
    for item in b1["items"]:
        assert required <= set(item)
        assert item["run_id"] and item["aoi_name"]
        assert item["bbox"] and len(item["bbox"]) == 4
        assert item["total_area_ha"] >= 0.0


@pytest.mark.req("REQ-14")
def test_runs_detail_full_snapshot_and_all_artifact_urls(client, ndwi_ready):
    """详情 = params_snapshot 全量 + 全部产物下载 URL（复用 artifact_url）。"""
    rid = ndwi_ready["run_id"]
    r = client.get(f"{RUNS_URL}/{rid}")
    assert r.status_code == 200, r.text
    b = r.json()
    snap = b["params_snapshot"]
    for key in ("formula", "index", "index_bands", "threshold", "threshold_source",
                "otsu_reason", "block_size", "valid_ratio", "stats",
                "pixel_area_m2", "artifacts", "scene_cog_paths"):
        assert key in snap, key
    assert snap == ndwi_ready["body"]["params_snapshot"]
    assert b["threshold"] == pytest.approx(MANUAL_T)
    assert b["index"] == "NDWI" and b["threshold_source"] == "manual"
    assert b["t1"] == POYANG_T1 and b["t2"] == POYANG_T2
    assert b["total_area_ha"] == pytest.approx(
        sum(float(s["area_ha"]) for s in snap["stats"]), abs=1e-3)

    names = {a["name"]: a for a in b["artifacts"]}
    assert {"change.tif", "change.png", "report.pdf", "change_ndwi.tif",
            "change_ndwi.png", "report_ndwi.pdf",
            "t1_composite.png", "t2_composite.png"} <= set(names)
    for name, item in names.items():
        assert item["url"] == f"/api/v1/artifacts/{rid}/{name}"
        assert item["size"] > 0
        assert name in ARTIFACT_MEDIA_TYPES
        assert client.get(item["url"]).status_code == 200


@pytest.mark.req("REQ-14")
def test_runs_detail_ex2_unknown_run(client):
    r = client.get(f"{RUNS_URL}/{UNKNOWN_RUN}")
    assert_error_body(r, "EX2", 404)

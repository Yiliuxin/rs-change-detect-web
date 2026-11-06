# -*- coding: utf-8 -*-
"""PDF 报告增强单测（REQ-12 / REQ-15 / REQ-11）。

覆盖：
  * matplotlib 三类面积柱状图 PNG 真实落盘、进入产物白名单且 URL 可达；
  * PDF 内嵌图像资源、ASCII 可检索的真实统计数字；
  * 独立署名页逐字复用 NF6/NF7 的 Copernicus / USGS 常量文案；
  * method=regression 时差值法 / 回归法阈值与面积对比表；
  * requirements.txt 固定 matplotlib 版本。

接口测用 module 级 fixture 只真跑一次，teardown 自行删除 aoi / task /
change_result / sample 与 runs 目录，不依赖全局 cleanup 脚本。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.algorithm.writers import attribution_lines, render_area_chart
from app.config.constants import (
    ATTRIBUTION_LS, ATTRIBUTION_S2, ATTRIBUTION_S2_MODIFIED,
    EXPORT_FORMATS, METHOD_REGRESSION,
)
from app.repository.db import db_cursor
from app.service.common import ARTIFACT_MEDIA_TYPES, run_dir
from _helpers import POYANG_T1, POYANG_T2, poyang_geojson

CHANGE_URL = "/api/v1/result/change"
EXPORT_URL = "/api/v1/report/export"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _cleanup(run_id: str, aoi_id: str) -> None:
    with db_cursor() as cur:
        cur.execute("DELETE FROM change_result WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM sample WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM task WHERE run_id = %s", (run_id,))
        cur.execute(
            "DELETE FROM aoi WHERE aoi_id = %s"
            " AND aoi_id NOT IN (SELECT aoi_id FROM scene)", (aoi_id,),
        )
    shutil.rmtree(run_dir(run_id), ignore_errors=True)


@pytest.fixture(scope="module")
def pdf_flow(client):
    """真实跑「AOI → regression 检测 → 三件套导出」一次，teardown 自建自清。"""
    a = client.post("/api/v1/aoi/select", json={
        "aoi_geom": poyang_geojson(), "t1": POYANG_T1, "t2": POYANG_T2,
    })
    assert a.status_code == 200, a.text
    run_id, aoi_id = a.json()["run_id"], a.json()["aoi_id"]
    try:
        r = client.post(CHANGE_URL, json={
            "run_id": run_id, "method": METHOD_REGRESSION,
        })
        assert r.status_code == 200, r.text
        e = client.post(EXPORT_URL, json={
            "run_id": run_id, "formats": list(EXPORT_FORMATS),
        })
        assert e.status_code == 200, e.text
        yield {"run_id": run_id, "body": r.json(), "export": e.json()}
    finally:
        _cleanup(run_id, aoi_id)


# --------------------------------------------------------------------------- #
# 1. 纯函数 / 依赖清单
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-12")
def test_requirements_pins_matplotlib_version():
    """requirements.txt 固定 matplotlib 版本（本机实测 3.9.1 与 numpy 2.2.6 兼容）。"""
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert "matplotlib==3.9.1" in req
    assert "reportlab==5.1.0" in req


@pytest.mark.req("REQ-15")
def test_attribution_lines_are_verbatim_nf6_nf7_constants():
    """署名逐字取自常量源，不重写、不翻译。"""
    lines = attribution_lines({"year": "2024"})
    assert lines == [
        ATTRIBUTION_S2.format(year="2024"),
        ATTRIBUTION_S2_MODIFIED.format(year="2024"),
        ATTRIBUTION_LS,
    ]
    assert lines[2] == "Courtesy of USGS/NASA"


@pytest.mark.req("REQ-12")
def test_render_area_chart_creates_real_png(tmp_path):
    """matplotlib 生成真实 PNG 柱状图（PNG 魔数 + 非零体积）。"""
    stats = [
        {"change_class": 1, "pixel_count": 10, "area_ha": 1.5},
        {"change_class": 2, "pixel_count": 20, "area_ha": 3.0},
        {"change_class": 3, "pixel_count": 30, "area_ha": 4.5},
    ]
    p = Path(render_area_chart(stats, tmp_path / "area_chart.png"))
    assert p.is_file() and p.stat().st_size > 1000
    assert p.read_bytes()[:8] == PNG_MAGIC
    # 缺类记 0，不伪造数值、不抛异常
    p2 = Path(render_area_chart([], tmp_path / "empty_chart.png"))
    assert p2.read_bytes()[:8] == PNG_MAGIC


# --------------------------------------------------------------------------- #
# 2. 真链路产物抽验
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-12")
def test_area_chart_png_artifact_exists_and_reachable(client, pdf_flow):
    """面积图作为独立产物落盘、进入白名单与清单，且同源 URL 可达。"""
    rid, body = pdf_flow["run_id"], pdf_flow["body"]
    out = run_dir(rid)
    chart = out / "area_chart.png"
    assert chart.is_file() and chart.stat().st_size > 1000
    assert chart.read_bytes()[:8] == PNG_MAGIC
    assert "area_chart.png" in ARTIFACT_MEDIA_TYPES
    assert ARTIFACT_MEDIA_TYPES["area_chart.png"] == "image/png"
    assert "area_chart.png" in body["artifacts"]
    got = client.get(body["artifacts"]["area_chart.png"])
    assert got.status_code == 200 and got.content[:8] == PNG_MAGIC


@pytest.mark.req("REQ-12")
def test_pdf_embeds_chart_image_and_real_stat_numbers(pdf_flow):
    """PDF 内嵌图像资源 + ASCII 可检索的真实三类统计数字（不伪造）。"""
    rid, body = pdf_flow["run_id"], pdf_flow["body"]
    pdf_path = run_dir(rid) / "report_reg.pdf"
    assert pdf_path.is_file() and pdf_path.stat().st_size > 10000
    raw = pdf_path.read_bytes()
    assert b"/Subtype /Image" in raw               # 柱状图真实嵌入，不是占位文本
    assert b"AREA CHART" in raw
    assert b"VALID_RATIO" in raw
    for s in body["stats"]:
        assert f"PIXELS={int(s['pixel_count'])}".encode() in raw
        assert f"AREA_HA={float(s['area_ha']):.4f}".encode() in raw


@pytest.mark.req("REQ-15")
def test_pdf_signature_page_verbatim_copernicus_and_usgs(pdf_flow):
    """独立署名页逐字包含 Copernicus 两条 + USGS 一条常量文案。"""
    raw = (run_dir(pdf_flow["run_id"]) / "report_reg.pdf").read_bytes()
    assert b"DATA ATTRIBUTION" in raw
    assert b"Copernicus Sentinel data" in raw
    assert b"Contains modified Copernicus Sentinel data" in raw
    assert ATTRIBUTION_LS.encode("ascii") in raw
    year = str(pdf_flow["body"]["params_snapshot"].get("t1") or "")[:4] or "2023"
    assert ATTRIBUTION_S2.format(year=year).split(" [")[0].encode() in raw
    assert ATTRIBUTION_S2_MODIFIED.format(year=year).split(" [")[0].encode() in raw


@pytest.mark.req("REQ-11")
def test_pdf_regression_comparison_table_thresholds_and_areas(pdf_flow):
    """method=regression 的 PDF 附差值法 / 回归法阈值与面积对比表。"""
    body = pdf_flow["body"]
    assert body["method"] == METHOD_REGRESSION
    snap = body["params_snapshot"]
    cmp_ = snap["comparison"]
    raw = (run_dir(pdf_flow["run_id"]) / "report_reg.pdf").read_bytes()
    assert b"METHOD COMPARISON" in raw
    assert f"DIFF_T={float(cmp_['difference']['threshold']):.4f}".encode() in raw
    assert f"REG_T={float(cmp_['regression']['threshold']):.4f}".encode() in raw
    diff_area = sum(float(s["area_ha"]) for s in cmp_["difference"]["stats"])
    reg_area = sum(float(s["area_ha"]) for s in cmp_["regression"]["stats"])
    assert f"DIFF_TOTAL_AREA_HA={diff_area:.4f}".encode() in raw
    assert f"REG_TOTAL_AREA_HA={reg_area:.4f}".encode() in raw


@pytest.mark.req("REQ-15")
def test_report_export_attribution_uses_verbatim_constants(pdf_flow):
    """/report/export 响应与 PDF 署名页同源，三条文案逐字一致。"""
    attribution = pdf_flow["export"]["attribution"]
    for text in attribution_lines({"year": str(POYANG_T1)[:4]}):
        assert text in attribution
    assert ATTRIBUTION_LS in attribution
    assert len(pdf_flow["export"]["files"]) == len(EXPORT_FORMATS)
    assert all(f["size"] > 0 for f in pdf_flow["export"]["files"])

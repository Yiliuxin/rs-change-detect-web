# -*- coding: utf-8 -*-
"""批次 A：POST /api/v1/report/export 多格式自动打包 zip（REQ-12）。

覆盖：≥2 格式时 files[] 追加 zip 且内容与所选产物一致；单格式不含 zip；
zip 走同源产物端点（白名单 + run_id 精确匹配）可下载、重复导出不残留旧成员。
"""
from __future__ import annotations

import zipfile

import pytest

from app.config.constants import EXPORT_FORMATS
from app.service.common import export_zip_name, run_dir
from _helpers import export_products, export_zip_entries

URL = "/api/v1/report/export"


@pytest.mark.req("REQ-12")
def test_multi_format_export_adds_zip_bundle(client, change_ready):
    """4 种格式 → files[] 多一条 format='zip'，包内逐字节复用既有产物。"""
    rid = change_ready["run_id"]
    r = client.post(URL, json={"run_id": rid, "formats": list(EXPORT_FORMATS)})
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    products = export_products(files)
    assert len(products) == len(EXPORT_FORMATS)

    zips = export_zip_entries(files)
    assert len(zips) == 1
    z = zips[0]
    name = export_zip_name(rid)
    assert z["filename"] == name
    assert z["url"] == f"/api/v1/artifacts/{rid}/{name}"
    assert z["size"] and z["size"] > 0

    disk = run_dir(rid) / name
    assert disk.is_file() and disk.stat().st_size == z["size"]
    with zipfile.ZipFile(disk) as zf:
        assert zf.testzip() is None
        assert set(zf.namelist()) == {f["filename"] for f in products}
        for f in products:
            assert zf.read(f["filename"]) == (run_dir(rid) / f["filename"]).read_bytes()


@pytest.mark.req("REQ-12")
def test_single_format_export_has_no_zip(client, change_ready):
    """单格式行为不变：files[] 只有该格式，且不重新生成打包文件。"""
    rid = change_ready["run_id"]
    disk = run_dir(rid) / export_zip_name(rid)
    before = disk.stat().st_mtime_ns if disk.is_file() else None
    r = client.post(URL, json={"run_id": rid, "formats": ["png"]})
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    assert [f["format"] for f in files] == ["png"]
    assert export_zip_entries(files) == []
    after = disk.stat().st_mtime_ns if disk.is_file() else None
    assert after == before


@pytest.mark.req("REQ-12")
def test_zip_bundle_url_is_reachable_and_only_selected_formats(client, change_ready):
    """2 格式导出 → zip URL 200 / application/zip，且包内不含上一次的旧成员。"""
    rid = change_ready["run_id"]
    r = client.post(URL, json={"run_id": rid, "formats": ["png", "pdf"]})
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    assert len(export_products(files)) == 2
    z = export_zip_entries(files)[0]

    got = client.get(z["url"])
    assert got.status_code == 200, (z["url"], got.status_code)
    assert got.headers["content-type"].startswith("application/zip")
    assert got.content[:2] == b"PK"
    assert len(got.content) == z["size"]

    with zipfile.ZipFile(run_dir(rid) / z["filename"]) as zf:
        assert set(zf.namelist()) == {"change.png", "report.pdf"}

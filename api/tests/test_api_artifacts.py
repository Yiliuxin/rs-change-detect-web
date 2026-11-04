# -*- coding: utf-8 -*-
"""接口测：产物经 /api/v1/artifacts 同源 HTTP 暴露（REQ-10 / REQ-12）。

覆盖：
  - /result/change 的 change_png / change_geotiff 改为同源相对 URL；
  - PNG 端点 GET 200 且 Content-Type=image/png（浏览器可直接叠加，不再 file:///）；
  - /report/export 的 files[].url 同源可达（<a download> 可触发，不再 minio://）；
  - UUID / 白名单 / 目录穿越防护；
  - scene.cog_path 仍为 minio://scenes/，storage.object_url() 口径未被改动。
"""
from __future__ import annotations

import pytest

from app.config.constants import COG_BUCKET, EXPORT_FORMATS
from app.config.storage import object_url
from app.service.common import ARTIFACT_URL_PREFIX, run_dir

URL = "/api/v1/artifacts"
_FILE_BY_FORMAT = {"geotiff": "change.tif", "png": "change.png", "pdf": "report.pdf"}


@pytest.mark.req("REQ-10")
@pytest.mark.req("REQ-12")
def test_change_response_exposes_same_origin_artifact_urls(client, change_ready):
    """/result/change 只暴露同源相对 URL；产物本体仍在 api/runs/<run_id>/ 落盘。"""
    body = change_ready["change_body"]
    rid = change_ready["run_id"]
    assert body["change_png"] == f"{URL}/{rid}/change.png"
    assert body["change_geotiff"] == f"{URL}/{rid}/change.tif"
    assert body["change_png"].startswith(ARTIFACT_URL_PREFIX + "/")
    assert body["change_png"].startswith("/api/")          # 相对同源，非绝对磁盘路径/自定义 scheme
    assert "://" not in body["change_png"] and ":\\" not in body["change_png"]
    # change_result 表登记的是本地绝对路径：此处只验证磁盘仍落盘（schema / 仓储层不改）
    assert (run_dir(rid) / "change.tif").is_file()
    assert (run_dir(rid) / "change.png").is_file()


@pytest.mark.req("REQ-10")
def test_artifact_png_is_http_200_image_png(client, change_ready):
    """UI-03 image source 能直接取到真实 PNG 字节。"""
    r = client.get(change_ready["change_body"]["change_png"])
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png")
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.req("REQ-12")
def test_report_export_file_urls_are_same_origin_and_reachable(client, change_ready):
    """三件套 URL 同源、GET 200、字节非空，且不出现 minio:// 或本地绝对路径。"""
    r = client.post("/api/v1/report/export", json={
        "run_id": change_ready["run_id"], "formats": list(EXPORT_FORMATS),
    })
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    assert len(files) == len(EXPORT_FORMATS)
    for f in files:
        assert f["url"] == f"{URL}/{change_ready['run_id']}/{_FILE_BY_FORMAT[f['format']]}"
        assert f["url"].startswith(ARTIFACT_URL_PREFIX + "/")
        assert "minio://" not in f["url"] and "file://" not in f["url"]
        got = client.get(f["url"])
        assert got.status_code == 200, (f["url"], got.status_code)
        assert len(got.content) > 0


@pytest.mark.req("REQ-10")
@pytest.mark.req("REQ-12")
def test_artifact_endpoint_rejects_bad_uuid_filename_and_traversal(client, change_ready):
    """UUID 校验 + filename 白名单 + 目录穿越防护。"""
    rid = change_ready["run_id"]
    bad_urls = (
        f"{URL}/not-a-uuid/change.png",
        f"{URL}/{rid}/run_report.json",          # 不在白名单
        f"{URL}/{rid}/../../constants.py",       # 穿越尝试
        f"{URL}/../{rid}/change.png",            # 穿越尝试（路径规范化后落到别处）
    )
    for u in bad_urls:
        r = client.get(u)
        assert r.status_code in (400, 404, 422), (u, r.status_code, r.text[:200])


@pytest.mark.req("REQ-02")
def test_cog_path_and_object_url_convention_unchanged(client):
    """storage.object_url() 未改：scene.cog_path 仍是 minio://scenes/，resolve_bands 不受影响。"""
    r = client.get("/api/v1/scene/match")
    assert r.status_code == 200, r.text
    scenes = r.json()["scenes"]
    assert len(scenes) >= 6  # 预置影像对可扩容（离线 6→22 景），口径校验与数量解耦
    assert all(s["cog_path"].startswith("minio://scenes/") for s in scenes)
    assert object_url(COG_BUCKET, "pair_poyang/t1/") == "minio://scenes/pair_poyang/t1/"

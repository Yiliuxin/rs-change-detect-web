# -*- coding: utf-8 -*-
"""接口测：在线取数（B 级扩展）POST /scene/discover + /scene/fetch + GET /scene/fetch/{id}。

重点：
  * **不打真网**：STAC 调用全部 monkeypatch ``online_fetch._stac_search``（或 httpx.post）；
  * 覆盖参数校验 / EX1 / EX2 / SYS-06 与响应结构（candidates/t1/t2/combined_cloud/run_id）；
  * /scene/fetch 只测"线程启动前"的校验与 404 分支，绝不触发真实下载线程。
"""
from __future__ import annotations

import uuid

import pytest

from app.config.constants import AOI_MAX_SPAN_DEG, ERROR_MESSAGES, S2_BASELINE_MIN
from app.service import online_fetch
from _helpers import assert_error_body

DISCOVER_URL = "/api/v1/scene/discover"
FETCH_URL = "/api/v1/scene/fetch"

# 洞庭湖 AOI（跨度 0.1° ≤ 0.25°，与真实验收口径一致）
DONGTING_BBOX = [112.8, 29.0, 112.9, 29.1]
T1_WINDOW = ["2023-07-01", "2023-09-30"]
T2_WINDOW = ["2023-11-15", "2024-01-15"]

# sentinel-2-l2a 场景标识格式：S2B_<tile>_<YYYYMMDD>_<n>_L2A
SID_DONGTING_T1 = "S2B_49RFN_20230805_0_L2A"
SID_DONGTING_T2 = "S2B_49RFN_20231123_0_L2A"


def _feature(tile: str, day: str, cloud: float, baseline: str) -> dict:
    """构造 STAC feature（只保留 discover 用到的字段）。"""
    sid = f"S2B_{tile}_{day.replace('-', '')}_0_L2A"
    return {
        "id": sid,
        "properties": {
            "datetime": f"{day}T03:22:31Z",
            "eo:cloud_cover": cloud,
            "s2:processing_baseline": baseline,
        },
    }


def _fake_stac(t1_features, t2_features):
    """按时相窗口分派的假 STAC 检索（替换 ``online_fetch._stac_search``）。"""

    def _search(bbox, dt, run_id=None):
        return list(t1_features) if "2023-07-01" in dt else list(t2_features)

    return _search


@pytest.mark.req("REQ-02")
@pytest.mark.req("REQ-04")
def test_discover_happy_path_returns_common_tile(client, monkeypatch):
    """共同 tile + 基线过滤 + 综合云量升序 + 响应结构。"""
    t1 = [
        _feature("49RFN", "2023-08-05", 12.5, "05.00"),
        _feature("49RFP", "2023-08-11", 30.0, "04.00"),
        _feature("49RFQ", "2023-08-20", 1.0, "03.00"),  # 基线 <04.00 → 过滤（REQ-04）
    ]
    t2 = [
        _feature("49RFN", "2023-11-23", 5.5, "05.00"),
        _feature("49RFP", "2023-12-07", 8.0, "04.00"),
        _feature("49RFZ", "2023-12-19", 2.0, "05.00"),  # 仅 t2 有 → 非共同 tile
    ]
    monkeypatch.setattr(online_fetch, "_stac_search", _fake_stac(t1, t2))

    r = client.post(DISCOVER_URL, json={
        "bbox": DONGTING_BBOX, "t1_window": T1_WINDOW, "t2_window": T2_WINDOW,
    })
    assert r.status_code == 200, r.text
    body = r.json()

    # run_id 可追溯（UUID 形态）
    assert str(uuid.UUID(body["run_id"])) == body["run_id"]
    tiles = [c["tile"] for c in body["candidates"]]
    assert tiles == ["49RFN", "49RFP"], body  # 综合云量 18.0 < 38.0
    assert "49RFQ" not in tiles and "49RFZ" not in tiles

    top = body["candidates"][0]
    assert set(top) == {"tile", "t1", "t2", "combined_cloud"}
    assert top["t1"] == {
        "source_id": SID_DONGTING_T1,
        "date": "2023-08-05",
        "cloud_cover": 12.5,
        "proc_baseline": "05.00",
    }
    assert top["t2"]["source_id"] == SID_DONGTING_T2
    assert top["t2"]["date"] == "2023-11-23"
    assert top["combined_cloud"] == 18.0
    assert body["candidates"][1]["combined_cloud"] == 38.0
    # 候选基线全部 ≥04.00（与常量源 S2_BASELINE_MIN 对齐）
    for cand in body["candidates"]:
        for tag in ("t1", "t2"):
            assert float(cand[tag]["proc_baseline"]) >= float(S2_BASELINE_MIN)


@pytest.mark.req("REQ-04")
def test_discover_ex2_when_all_baselines_too_low(client, monkeypatch):
    """全部候选基线 <04.00 → 过滤后无候选 → EX2(404)。"""
    low = [_feature("49RFN", "2023-08-05", 10.0, "03.00")]
    monkeypatch.setattr(online_fetch, "_stac_search", _fake_stac(low, low))

    r = client.post(DISCOVER_URL, json={
        "aoi_geom": None, "bbox": DONGTING_BBOX,
        "t1_window": T1_WINDOW, "t2_window": T2_WINDOW,
    })
    assert_error_body(r, "EX2", 404)


@pytest.mark.req("REQ-02")
def test_discover_ex2_when_no_common_tile(client, monkeypatch):
    """两时相 tile 无交集 → EX2(404)。"""
    t1 = [_feature("49RFN", "2023-08-05", 10.0, "05.00")]
    t2 = [_feature("49RFP", "2023-11-23", 10.0, "05.00")]
    monkeypatch.setattr(online_fetch, "_stac_search", _fake_stac(t1, t2))

    r = client.post(DISCOVER_URL, json={
        "bbox": DONGTING_BBOX, "t1_window": T1_WINDOW, "t2_window": T2_WINDOW,
    })
    assert_error_body(r, "EX2", 404)


@pytest.mark.req("REQ-01")
def test_discover_ex1_when_aoi_span_too_large(client, monkeypatch):
    """AOI 跨度 > 0.25° → EX1(400)，中文文案取自常量源。"""
    def _boom(*_a, **_k):  # 不应被调用（EX1 在检索前拦截）
        raise AssertionError("EX1 场景不应调用 STAC")

    monkeypatch.setattr(online_fetch, "_stac_search", _boom)
    span = AOI_MAX_SPAN_DEG + 0.15

    r = client.post(DISCOVER_URL, json={
        "bbox": [112.0, 29.0, 112.0 + span, 29.1],
        "t1_window": T1_WINDOW, "t2_window": T2_WINDOW,
    })
    assert_error_body(r, "EX1", 400)


def test_discover_sys06_when_stac_unreachable(client, monkeypatch):
    """STAC 不可达 → SYS-06(500)，文案为 constants 新增条目。"""
    import httpx as _httpx

    def _raise(*_a, **_k):
        raise _httpx.ConnectError("stac down")

    monkeypatch.setattr(online_fetch.httpx, "post", _raise)
    assert "SYS-06" in ERROR_MESSAGES

    r = client.post(DISCOVER_URL, json={
        "bbox": DONGTING_BBOX, "t1_window": T1_WINDOW, "t2_window": T2_WINDOW,
    })
    assert_error_body(r, "SYS-06", 500)


def test_discover_rejects_inverted_window(client, monkeypatch):
    """窗口 end ≤ start → EX2(400)（不臆造文案，复用"调整时相"）。"""
    monkeypatch.setattr(online_fetch, "_stac_search", _fake_stac([], []))
    r = client.post(DISCOVER_URL, json={
        "bbox": DONGTING_BBOX,
        "t1_window": ["2023-09-30", "2023-07-01"], "t2_window": T2_WINDOW,
    })
    assert_error_body(r, "EX2", 400)


def test_discover_requires_aoi_or_bbox(client):
    """aoi_geom 与 bbox 均缺失 → 422（请求体结构不合规，与既有 5 接口一致）。"""
    r = client.post(DISCOVER_URL, json={
        "t1_window": T1_WINDOW, "t2_window": T2_WINDOW,
    })
    assert r.status_code == 422, r.text


def test_fetch_rejects_name_outside_slug_pattern(client):
    """name 非 [a-z0-9_] → 422（Pydantic pattern 拦截，不起线程）。"""
    r = client.post(FETCH_URL, json={
        "name": "DongTing-Lake",
        "bbox": DONGTING_BBOX, "tile": "49RFN",
        "t1": {"source_id": SID_DONGTING_T1, "date": "2023-08-05", "proc_baseline": "05.00"},
        "t2": {"source_id": SID_DONGTING_T2, "date": "2023-11-23", "proc_baseline": "05.00"},
    })
    assert r.status_code == 422, r.text


@pytest.mark.req("REQ-01")
def test_fetch_ex1_when_aoi_span_too_large(client):
    """fetch 复核 AOI 跨度（服务端再校验）→ EX1(400)，且不创建任务。"""
    span = AOI_MAX_SPAN_DEG + 0.15
    r = client.post(FETCH_URL, json={
        "name": "dongting_online",
        "bbox": [112.0, 29.0, 112.0 + span, 29.1], "tile": "49RFN",
        "t1": {"source_id": SID_DONGTING_T1, "date": "2023-08-05", "proc_baseline": "05.00"},
        "t2": {"source_id": SID_DONGTING_T2, "date": "2023-11-23", "proc_baseline": "05.00"},
    })
    assert_error_body(r, "EX1", 400)


def test_fetch_status_unknown_id_returns_ex2(client):
    """未知 fetch_id → EX2(404)。"""
    r = client.get(f"{FETCH_URL}/{uuid.uuid4()}")
    assert_error_body(r, "EX2", 404, run_id_required=False)


def test_fetch_status_contract_fields(client, monkeypatch):
    """GET 契约字段：fetch_id/status/progress{0,10}/error/scene_ids（注入假任务，不下载）。"""
    fetch_id = str(uuid.uuid4())
    monkeypatch.setitem(online_fetch._tasks, fetch_id, {
        "fetch_id": fetch_id, "key": None, "name": "dongting_online", "tile": "49RFN",
        "bbox": None, "t1": {}, "t2": {},
        "status": online_fetch.STATUS_RUNNING,
        "done": 3, "total": online_fetch.TOTAL_BANDS,
        "error": None, "scene_ids": None, "detail": None,
    })
    r = client.get(f"{FETCH_URL}/{fetch_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fetch_id"] == fetch_id
    assert body["status"] == "RUNNING"
    assert body["progress"] == {"done": 3, "total": 10}
    assert body["error"] is None and body["scene_ids"] is None

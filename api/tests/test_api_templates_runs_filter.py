# -*- coding: utf-8 -*-
"""批次 D 接口测：检测参数模板 CRUD + /runs 历史搜索过滤（NF9 / REQ-14 扩展）。

覆盖：
  * 模板 CRUD：创建 / 列表 created_at 倒序 / 删除；数据按 user_id 隔离；
  * 重名 → TMPL-01(409)；他人删除或模板不存在 → TMPL-02(404)（不泄漏存在性）；
  * 口径参数白名单：多余键一律丢弃不入库；index/method/block_size/threshold
    非法值按 EX1 口径报错（400，{code,message,run_id}，中文提示）；
  * /runs 可选过滤：q 命中与不命中、index 枚举过滤、days 边界与非法值（EX12）、
    无参数时与旧行为完全一致（老前端不带参数不受影响）。

测试卫生：沿用 conftest 的 AUTH_DISABLED=1 直通 + 本文件 auth_on 局部启用 JWT
（风格与 test_api_auth.py 一致）；注册用户名带 pytest_tmpl_ 前缀，用例末尾一律
清理（FK ON DELETE CASCADE 连带删除模板），run 数据靠 test_data 标记，不触碰
真实历史。
"""
from __future__ import annotations

import uuid

import pytest

from app.main import app  # noqa: F401 - 与既有接口测一致：导入即注册路由
from _helpers import assert_error_body

TEMPLATES_URL = "/api/v1/templates"
RUNS_URL = "/api/v1/runs"

VALID_PARAMS = {"index": "NDVI", "method": "otsu", "block_size": 256, "threshold": 0.15}


@pytest.fixture()
def auth_on(monkeypatch):
    """临时启用 JWT 网关：main.AUTH_DISABLED 在 conftest 环境下为 True，这里切 False。"""
    import app.main as main_mod
    assert main_mod.AUTH_DISABLED is True
    monkeypatch.setattr(main_mod, "AUTH_DISABLED", False)
    yield
    monkeypatch.undo()


def _register(client, username, password="pw123456"):
    return client.post("/api/v1/auth/register",
                       json={"username": username, "password": password})


def _new_user(client) -> dict:
    """注册一个带 pytest_tmpl_ 前缀的新用户，返回 {user_id, username, auth 头}。"""
    username = "pytest_tmpl_" + uuid.uuid4().hex[:8]
    r = _register(client, username)
    assert r.status_code == 200, r.text
    body = r.json()
    return {"user_id": body["user_id"], "username": username,
            "token": body["token"], "auth": {"Authorization": f"Bearer {body['token']}"}}


def _cleanup_user(username):
    """测试后清理：解除 run 归属 + 删用户（detect_template 经 FK 级联删除）。"""
    from app.repository.db import db_cursor
    with db_cursor() as cur:
        cur.execute("UPDATE task SET user_id = NULL WHERE user_id IN "
                    "(SELECT user_id FROM users WHERE username = %s)", (username,))
        cur.execute("DELETE FROM users WHERE username = %s", (username,))


def _assert_ex1_param(resp, fragment: str) -> dict:
    """EX1 口径参数错误：{code,message,run_id} 结构、400、中文提示含具体参数说明。"""
    body = resp.json()
    assert resp.status_code == 400, (resp.status_code, body)
    assert {"code", "message", "run_id"} <= set(body.keys()), body
    assert body["code"] == "EX1", body
    assert fragment in body["message"], body
    assert body["message"] != "EX1", body  # 中文提示，非裸错误码
    return body


# --------------------------------------------------------------------------- #
# 1. 模板 CRUD：创建 / 列表倒序 / 删除
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-14")
def test_template_crud_create_list_order_and_delete(client, auth_on):
    """创建 → 列表按 created_at 倒序 → 删除后列表不再含该模板；按人隔离。"""
    user = _new_user(client)
    try:
        thresholds = [0.1, 0.2, 0.3]
        ids = []
        for name, thr in zip(["tmpl_first", "tmpl_second", "tmpl_third"], thresholds):
            r = client.post(TEMPLATES_URL, headers=user["auth"],
                            json={"name": name,
                                  "params": {**VALID_PARAMS, "threshold": thr}})
            assert r.status_code == 200, r.text
            body = r.json()
            ids.append(body["id"])
            assert body["name"] == name and body["user_id"] == user["user_id"]
            assert body["params"]["threshold"] == pytest.approx(thr)
            assert body["created_at"]

        r = client.get(TEMPLATES_URL, headers=user["auth"])
        assert r.status_code == 200, r.text
        lst = r.json()
        assert lst["total"] == 3
        # created_at 倒序：最后创建的排最前
        assert [it["name"] for it in lst["items"]] == ["tmpl_third", "tmpl_second", "tmpl_first"]

        # 删除最后创建的模板
        r = client.delete(f"{TEMPLATES_URL}/{ids[-1]}", headers=user["auth"])
        assert r.status_code == 200 and r.json() == {"id": ids[-1], "deleted": True}
        lst2 = client.get(TEMPLATES_URL, headers=user["auth"]).json()
        assert lst2["total"] == 2
        assert ids[-1] not in [it["id"] for it in lst2["items"]]

        # 再删已删过的 → 404 TMPL-02
        r = client.delete(f"{TEMPLATES_URL}/{ids[-1]}", headers=user["auth"])
        assert r.status_code == 404 and r.json()["code"] == "TMPL-02"
    finally:
        _cleanup_user(user["username"])


@pytest.mark.req("REQ-14")
def test_template_duplicate_name_tmpl01(client, auth_on):
    """同用户重名 → 409 TMPL-01（中文文案）；不同用户同名不冲突（按人隔离）。"""
    user = _new_user(client)
    other = _new_user(client)
    try:
        r = client.post(TEMPLATES_URL, headers=user["auth"],
                        json={"name": "同名模板", "params": VALID_PARAMS})
        assert r.status_code == 200, r.text
        r = client.post(TEMPLATES_URL, headers=user["auth"],
                        json={"name": "同名模板", "params": VALID_PARAMS})
        body = r.json()
        assert r.status_code == 409 and body["code"] == "TMPL-01"
        assert {"code", "message", "run_id"} <= set(body.keys())
        assert body["message"] != "TMPL-01"  # 中文提示
        # 另一个用户用同名 → 200（按人隔离，互不影响）
        r = client.post(TEMPLATES_URL, headers=other["auth"],
                        json={"name": "同名模板", "params": VALID_PARAMS})
        assert r.status_code == 200, r.text
    finally:
        _cleanup_user(user["username"])
        _cleanup_user(other["username"])


# --------------------------------------------------------------------------- #
# 2. 口径参数枚举校验（EX1 口径）与白名单过滤
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-14")
@pytest.mark.parametrize("key,bad,fragment", [
    ("index", "EVI", "index"),
    ("index", "savi", "index"),
    ("method", "deep_learning", "method"),
    ("block_size", 128, "block_size"),
    ("block_size", "abc", "block_size"),
    ("threshold", 2.0, "threshold"),
])
def test_template_invalid_param_ex1(client, auth_on, key, bad, fragment):
    """指数/方法/分块尺寸/阈值非法值 → 400 EX1 口径（{code,message,run_id}，中文提示）。"""
    user = _new_user(client)
    try:
        r = client.post(TEMPLATES_URL, headers=user["auth"],
                        json={"name": "bad_params", "params": {key: bad}})
        _assert_ex1_param(r, fragment)
    finally:
        _cleanup_user(user["username"])


@pytest.mark.req("REQ-14")
def test_template_extra_keys_dropped(client, auth_on):
    """白名单外键一律丢弃不入库；入库 params 仅含 index/method/block_size/threshold。"""
    from app.config.constants import TEMPLATE_PARAM_KEYS
    user = _new_user(client)
    try:
        dirty = {
            **VALID_PARAMS,
            "formula": "rho=DN/10000",       # 伪造成快照派生字段
            "sensor": "S2",
            "aoi_wgs84": [116.2, 29.1],
            "run_id": "spoofed-run-id",
            "evil_drop": {"nested": True},
        }
        r = client.post(TEMPLATES_URL, headers=user["auth"],
                        json={"name": "白名单过滤", "params": dirty})
        assert r.status_code == 200, r.text
        params = r.json()["params"]
        assert set(params) == set(TEMPLATE_PARAM_KEYS), params
        assert params["index"] == "NDVI" and params["method"] == "otsu"
        assert params["block_size"] == 256 and params["threshold"] == pytest.approx(0.15)
        # 数据库侧复核：JSONB 中同样只有白名单键
        from app.repository.db import db_cursor
        with db_cursor() as cur:
            cur.execute("SELECT params FROM detect_template WHERE id::text = %s",
                        (r.json()["id"],))
            row = cur.fetchone()
        assert set(row["params"]) == set(TEMPLATE_PARAM_KEYS)
    finally:
        _cleanup_user(user["username"])


@pytest.mark.req("REQ-14")
def test_template_delete_other_user_404(client, auth_on):
    """他人删除 A 的模板 → 404 TMPL-02（不泄漏存在性）；A 本人删除成功。"""
    owner = _new_user(client)
    stranger = _new_user(client)
    try:
        r = client.post(TEMPLATES_URL, headers=owner["auth"],
                        json={"name": "仅本人可删", "params": VALID_PARAMS})
        assert r.status_code == 200, r.text
        tid = r.json()["id"]

        # 他人视角：列表看不到，删除 404
        lst = client.get(TEMPLATES_URL, headers=stranger["auth"]).json()
        assert not any(it["id"] == tid for it in lst["items"])
        r = client.delete(f"{TEMPLATES_URL}/{tid}", headers=stranger["auth"])
        assert r.status_code == 404 and r.json()["code"] == "TMPL-02"

        # 不存在的模板 → 同样 404 TMPL-02
        r = client.delete(f"{TEMPLATES_URL}/00000000-0000-0000-0000-0000000000dd",
                          headers=owner["auth"])
        assert r.status_code == 404 and r.json()["code"] == "TMPL-02"

        # 本人删除 → 200
        r = client.delete(f"{TEMPLATES_URL}/{tid}", headers=owner["auth"])
        assert r.status_code == 200 and r.json()["deleted"] is True
    finally:
        _cleanup_user(owner["username"])
        _cleanup_user(stranger["username"])


# --------------------------------------------------------------------------- #
# 3. /runs 可选过滤：q / index / days；缺省行为与旧版完全一致
# --------------------------------------------------------------------------- #
@pytest.mark.req("REQ-14")
def test_runs_filter_q_hit_and_miss(client, change_ready):
    """q=run_id 前缀 → 命中该 run；q=组名文本 → 命中；q=不可能串 → total=0。"""
    run_id = change_ready["run_id"]
    plain = client.get(RUNS_URL).json()
    hit = client.get(f"{RUNS_URL}?q={run_id[:8]}").json()
    assert hit["total"] >= 1 and hit["total"] <= plain["total"]
    assert any(it["run_id"] == run_id for it in hit["items"])
    # 组名文本（params_snapshot.scene_cog_paths 含 pair_poyang）也可命中
    hit_name = client.get(f"{RUNS_URL}?q=poyang").json()
    assert hit_name["total"] >= 1
    assert any(it["run_id"] == run_id for it in hit_name["items"])
    miss = client.get(f"{RUNS_URL}?q=zzqqxx_no_such_run_prefix").json()
    assert miss["total"] == 0 and miss["items"] == []


@pytest.mark.req("REQ-14")
def test_runs_filter_index_days_and_errors(client, change_ready):
    """index=NDVI 命中本 run；index=NDWI 不含本 run；days=1 含当日 run；
    非法 index / days 越界 → 400 EX12；q+index+days 组合过滤生效。"""
    run_id = change_ready["run_id"]
    ndvi = client.get(f"{RUNS_URL}?index=NDVI").json()
    assert ndvi["total"] >= 1 and any(it["run_id"] == run_id for it in ndvi["items"])
    ndwi = client.get(f"{RUNS_URL}?index=NDWI").json()
    assert not any(it["run_id"] == run_id for it in ndwi["items"])
    days1 = client.get(f"{RUNS_URL}?days=1").json()
    assert days1["total"] >= 1 and any(it["run_id"] == run_id for it in days1["items"])
    combo = client.get(f"{RUNS_URL}?q={run_id[:8]}&index=NDVI&days=1").json()
    assert any(it["run_id"] == run_id for it in combo["items"])
    # 非法枚举 / 越界 → EX12（400，{code,message,run_id}）
    assert_error_body(client.get(f"{RUNS_URL}?index=EVI"), "EX12", 400, run_id_required=False)
    assert_error_body(client.get(f"{RUNS_URL}?days=0"), "EX12", 400, run_id_required=False)
    assert_error_body(client.get(f"{RUNS_URL}?days=3651"), "EX12", 400, run_id_required=False)


@pytest.mark.req("REQ-14")
def test_runs_no_filters_backward_compatible(client, change_ready):
    """不带任何过滤参数时与旧行为完全一致：total / items / limit / offset 不变。"""
    plain = client.get(RUNS_URL).json()
    explicit = client.get(f"{RUNS_URL}?limit=20&offset=0").json()
    assert plain["limit"] == 20 and plain["offset"] == 0
    assert plain == explicit
    # 过滤条件均为收窄口径：过滤后 total 只会 ≤ 全量 total
    for url in (f"{RUNS_URL}?q={change_ready['run_id'][:8]}",
                f"{RUNS_URL}?index=NDVI",
                f"{RUNS_URL}?days=1"):
        filtered = client.get(url).json()
        assert filtered["limit"] == 20 and filtered["offset"] == 0  # 分页字段不受影响
        assert filtered["total"] <= plain["total"]

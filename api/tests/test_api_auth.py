# -*- coding: utf-8 -*-
"""注册/登录扩展测试：AUTH-* 错误码、JWT 网关、数据按人隔离。

既有 166 条用例经 conftest 的 ``AUTH_DISABLED=1`` 直通；本文件把
``app.state`` 无关的全局 ``AUTH_DISABLED`` 临时切回启用态（monkeypatch
环境变量 + 重载 main.AUTH_DISABLED 常量），验证真实门禁与隔离逻辑。

数据卫生：注册的用户名带 ``pytest_auth_`` 前缀，用例末尾一律清理。
"""
from __future__ import annotations

import uuid

import pytest

from app.main import app

pytestmark = pytest.mark.usefixtures("client")


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


def _cleanup_user(username):
    """测试后清理：解除该用户的 run 归属（还原 NULL），删除用户本身。
    绝不删除 task——测试用户可能临时认领过真实历史 run。"""
    from app.repository.db import db_cursor
    with db_cursor() as cur:
        cur.execute("UPDATE task SET user_id = NULL WHERE user_id IN "
                    "(SELECT user_id FROM users WHERE username = %s)", (username,))
        cur.execute("DELETE FROM users WHERE username = %s", (username,))


def test_register_login_me(client, auth_on):
    username = "pytest_auth_" + uuid.uuid4().hex[:8]
    try:
        r = _register(client, username)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["username"] == username and body["token"]
        token = body["token"]

        # /auth/me 带 token → 用户信息
        r = client.get("/api/v1/auth/me",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and r.json()["username"] == username

        # /runs 带 token → 200（首个注册者可能认领历史遗留 run，仅验证可达性）
        r = client.get("/api/v1/runs",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and isinstance(r.json()["total"], int)

        # 第二个注册者：不认领任何 run → total == 0（数据按人隔离）
        username2 = "pytest_auth_" + uuid.uuid4().hex[:8]
        try:
            r2 = _register(client, username2)
            assert r2.status_code == 200 and r2.json()["claimed_runs"] == 0
            r = client.get("/api/v1/runs",
                           headers={"Authorization": f"Bearer {r2.json()['token']}"})
            assert r.status_code == 200 and r.json()["total"] == 0
        finally:
            _cleanup_user(username2)

        # 登录：正确密码 200；错误密码 401 AUTH-02
        r = client.post("/api/v1/auth/login",
                        json={"username": username, "password": "pw123456"})
        assert r.status_code == 200 and r.json()["token"]
        r = client.post("/api/v1/auth/login",
                        json={"username": username, "password": "wrong-pw"})
        assert r.status_code == 401 and r.json()["code"] == "AUTH-02"
    finally:
        _cleanup_user(username)


def test_register_rejects_bad_input(client, auth_on):
    # 用户名格式非法 → AUTH-05(400)；重复用户名 → AUTH-04(409)
    username = "pytest_auth_" + uuid.uuid4().hex[:8]
    try:
        r = _register(client, "bad name!")
        assert r.status_code == 400 and r.json()["code"] == "AUTH-05"
        r = _register(client, "ab")
        assert r.status_code == 400 and r.json()["code"] == "AUTH-05"
        assert _register(client, username).status_code == 200
        r = _register(client, username)
        assert r.status_code == 409 and r.json()["code"] == "AUTH-04"
    finally:
        _cleanup_user(username)


def test_gateway_rejects_without_token(client, auth_on):
    # 无 Authorization → 401 AUTH-01；坏 token → 同样 401
    for headers in ({}, {"Authorization": "Bearer not-a-jwt"}):
        r = client.get("/api/v1/runs", headers=headers)
        assert r.status_code == 401 and r.json()["code"] == "AUTH-01"
    # 注册/登录本身放行
    r = client.post("/api/v1/auth/login",
                    json={"username": "no_such_user_xx", "password": "whatever"})
    assert r.status_code == 401 and r.json()["code"] == "AUTH-02"


def test_run_isolation_between_users(client, auth_on, change_ready):
    """已有 run 归属 A；B 用户看不到列表与详情（404 不泄漏存在性）。"""
    a = "pa_a_" + uuid.uuid4().hex[:10]
    b = "pa_b_" + uuid.uuid4().hex[:10]
    run_id = change_ready["run_id"]
    try:
        ra = _register(client, a)
        rb = _register(client, b)
        assert ra.status_code == 200 and rb.status_code == 200
        # 显式把本用例的 run 归属给 a：注册时「首个用户认领历史遗留 run」依赖
        # 全库 users 为空，而演示库可能存在常驻账号，故与全局 DB 状态解耦；
        # teardown 仍把 user_id 还原为 NULL，隔离断言语义完全不变。
        from app.repository.db import db_cursor
        with db_cursor() as cur:
            cur.execute("UPDATE task SET user_id = %s WHERE run_id::text = %s",
                        (ra.json()["user_id"], run_id))
        ta = client.get("/api/v1/runs",
                        headers={"Authorization": f"Bearer {ra.json()['token']}"}).json()
        assert any(it["run_id"] == run_id for it in ta["items"])
        # b 用户：列表无该 run；详情 404
        tb = client.get("/api/v1/runs",
                        headers={"Authorization": f"Bearer {rb.json()['token']}"}).json()
        assert not any(it["run_id"] == run_id for it in tb["items"])
        r = client.get(f"/api/v1/runs/{run_id}",
                       headers={"Authorization": f"Bearer {rb.json()['token']}"})
        assert r.status_code == 404 and r.json()["code"] == "EX2"
        # b 对他人 run 提交样本 → AUTH-03(403)
        r = client.post("/api/v1/accuracy/sample",
                        headers={"Authorization": f"Bearer {rb.json()['token']}"},
                        json={"run_id": run_id, "samples": []})
        assert r.status_code == 403 and r.json()["code"] == "AUTH-03"
    finally:
        # 归属还原：task 归还给遗留态，避免污染其他用例基线
        from app.repository.db import db_cursor
        with db_cursor() as cur:
            cur.execute("UPDATE task SET user_id = NULL WHERE run_id::text = %s", (run_id,))
        _cleanup_user(a)
        _cleanup_user(b)

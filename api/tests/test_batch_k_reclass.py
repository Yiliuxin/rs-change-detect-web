# -*- coding: utf-8 -*-
"""批次 K 接口测：阈值滑块单倍数只读复算 POST /api/v1/result/reclass。

覆盖任务书验收：
  * 与批次 E sensitivity 同档完全一致（0.8× / 1.2× 面积与过渡带逐项相等）；
  * 他人 run → 404 EX2；倍数越界 / 非网格 / 非数值 → 400 EX12；
  * 缓存（experiments.json）被篡改也不影响结果，且 reclass 不写该文件；
  * 响应结构：阈值 4 位口径 / 三类面积 / 过渡带 / 直方图阈值线 / 内存 PNG。

测试卫生：复算类走合成 run（monkeypatch task_repo.get_task 与 run_dir 指向
tmp_path），只权限用例复用 conftest 的 change_ready 真实 run。
"""
from __future__ import annotations

import uuid

import pytest

from app.service import experiment_service as ES

RECLASS_URL = "/api/v1/result/reclass"
SENS_URL = "/api/v1/result/sensitivity"
BASE_T = 0.20
PIXEL_AREA_M2 = 100.0
H = W = 64
DELTAS = (-0.17, -0.19, -0.21, -0.23, -0.25)
STRIP_ROWS = 10


def _write_synth_products(out):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    t1 = np.full((H, W), 0.30, dtype=np.float32)
    t2 = t1.copy()
    for i, dv in enumerate(DELTAS):
        r0 = 2 + i * 12
        t2[r0:r0 + STRIP_ROWS, :] = t1[r0:r0 + STRIP_ROWS, :] + dv
    aux1 = np.full((H, W), 0.10, dtype=np.float32)
    transform = from_origin(116.25, 29.25, 0.0001, 0.0001)
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 2,
        "width": W, "height": H, "crs": "EPSG:4326",
        "transform": transform, "nodata": float("nan"),
    }
    for name, primary, auxiliary in (
        ("index_t1.tif", t1, aux1), ("index_t2.tif", t2, aux1 + 0.01),
    ):
        with rasterio.open(str(out / name), "w", **profile) as ds:
            ds.write(primary, 1)
            ds.write(auxiliary, 2)


def _synth_snapshot():
    degraded_px = sum(STRIP_ROWS * W for dv in DELTAS if abs(dv) > BASE_T)
    return {
        "index": "NDVI", "method": "otsu",
        "threshold": BASE_T, "threshold_source": "manual",
        "pixel_area_m2": PIXEL_AREA_M2,
        "stats": [
            {"change_class": 1, "pixel_count": 0, "area_ha": 0.0},
            {"change_class": 2, "pixel_count": degraded_px,
             "area_ha": degraded_px * PIXEL_AREA_M2 / 10000.0},
            {"change_class": 3, "pixel_count": H * W - degraded_px,
             "area_ha": (H * W - degraded_px) * PIXEL_AREA_M2 / 10000.0},
        ],
        "transition_band": {"ratio": 0.125},
        "index_histogram": {
            "name": "delta NDVI", "counts": [1, 2, 3], "edges": [0, 1, 2, 3],
            "threshold": BASE_T, "band_low": 0.15, "band_high": 0.25,
        },
        "test_data": True,
    }


@pytest.fixture()
def synth_run(tmp_path, monkeypatch):
    run_id = str(uuid.uuid4())
    root = tmp_path / "runs" / run_id
    root.mkdir(parents=True, exist_ok=True)
    _write_synth_products(root)
    snap = _synth_snapshot()
    monkeypatch.setattr(
        ES.task_repo, "get_task",
        lambda rid: {"run_id": rid, "params_snapshot": snap, "user_id": None},
    )
    monkeypatch.setattr(ES, "run_dir", lambda rid: root)
    return {"run_id": run_id, "dir": root, "snapshot": snap}


@pytest.fixture()
def auth_on(monkeypatch):
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "AUTH_DISABLED", False)
    yield


def _register(client, username):
    return client.post("/api/v1/auth/register",
                       json={"username": username, "password": "pw123456"})


def _cleanup_user(username):
    from app.repository.db import db_cursor
    with db_cursor() as cur:
        cur.execute("UPDATE task SET user_id = NULL WHERE user_id IN "
                    "(SELECT user_id FROM users WHERE username = %s)", (username,))
        cur.execute("DELETE FROM users WHERE username = %s", (username,))


# --------------------------------------------------------------------------- #
# 1. 口径锚点：与批次 E sensitivity 同档逐项一致
# --------------------------------------------------------------------------- #
def test_reclass_matches_sensitivity_same_scale(client, synth_run):
    rid = synth_run["run_id"]
    sens = client.post(SENS_URL, json={"run_id": rid})
    assert sens.status_code == 200, sens.text
    sweep = {float(s["scale"]): s for s in sens.json()["sweep"]}

    for scale in (0.8, 1.2):
        resp = client.post(RECLASS_URL, json={"run_id": rid, "scale": scale})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["scale"] == pytest.approx(scale)
        assert body["base_threshold"] == pytest.approx(BASE_T)
        assert body["threshold"] == pytest.approx(scale * BASE_T)
        assert [int(a["change_class"]) for a in body["stats"]] == [1, 2, 3]
        anchor = sweep[scale]
        assert [float(a["area_ha"]) for a in body["stats"]] == pytest.approx(
            [float(a["area_ha"]) for a in anchor["areas"]], rel=1e-9)
        assert body["transition_pct"] == pytest.approx(
            float(anchor["transition_pct"]), rel=1e-9)


# --------------------------------------------------------------------------- #
# 2. 他人 run → 404 EX2（不泄漏存在性，与批次 E 同款归属校验）
# --------------------------------------------------------------------------- #
def test_reclass_other_users_run_returns_404(client, auth_on, change_ready):
    a = "pa_k_a_" + uuid.uuid4().hex[:8]
    b = "pa_k_b_" + uuid.uuid4().hex[:8]
    rid = change_ready["run_id"]
    try:
        ra, rb = _register(client, a), _register(client, b)
        assert ra.status_code == 200 and rb.status_code == 200, (ra.text, rb.text)
        from app.repository.db import db_cursor
        with db_cursor() as cur:
            cur.execute("UPDATE task SET user_id = %s WHERE run_id::text = %s",
                        (ra.json()["user_id"], rid))
        headers = {"Authorization": "Bearer " + rb.json()["token"]}
        resp = client.post(RECLASS_URL, headers=headers,
                           json={"run_id": rid, "scale": 0.8})
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "EX2"
    finally:
        from app.repository.db import db_cursor
        with db_cursor() as cur:
            cur.execute("UPDATE task SET user_id = NULL WHERE run_id::text = %s", (rid,))
        _cleanup_user(a)
        _cleanup_user(b)


# --------------------------------------------------------------------------- #
# 3. 倍数越界 / 非网格 / 非数值 → 400 EX12（不是 422）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scale", [0.5, 1.5, 0.0, 0.67, "abc", None])
def test_reclass_invalid_scale_returns_400(client, synth_run, scale):
    resp = client.post(RECLASS_URL,
                       json={"run_id": synth_run["run_id"], "scale": scale})
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "EX12"
    assert body["run_id"] == synth_run["run_id"]
    assert body["message"] and body["message"] != "EX12"


# --------------------------------------------------------------------------- #
# 4. 缓存不影响结果 + 严格只读（experiments.json 不被 reclass 读写）
# --------------------------------------------------------------------------- #
def test_reclass_is_cache_independent_and_read_only(client, synth_run, monkeypatch):
    rid = synth_run["run_id"]
    assert client.post(SENS_URL, json={"run_id": rid}).status_code == 200

    tampered = b'{"version":1,"sensitivity":{"tampered":true}}'
    cache_file = synth_run["dir"] / "experiments.json"
    cache_file.write_bytes(tampered)

    calls = {"n": 0}
    original = ES.recompute

    def counting(ctx, *args, **kwargs):
        calls["n"] += 1
        return original(ctx, *args, **kwargs)

    monkeypatch.setattr(ES, "recompute", counting)
    r1 = client.post(RECLASS_URL, json={"run_id": rid, "scale": 0.8})
    r2 = client.post(RECLASS_URL, json={"run_id": rid, "scale": 0.8})
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert calls["n"] == 2                       # 每次都复算，绝不命中缓存
    assert r1.json()["stats"] == r2.json()["stats"]
    assert cache_file.read_bytes() == tampered   # 缓存文件逐字节不变
    assert synth_run["snapshot"] == ES.task_repo.get_task(rid)["params_snapshot"]


# --------------------------------------------------------------------------- #
# 5. 响应结构：阈值 / 过渡带 / 直方图联动 / 内存 PNG
# --------------------------------------------------------------------------- #
def test_reclass_response_shape(client, synth_run):
    resp = client.post(RECLASS_URL,
                       json={"run_id": synth_run["run_id"], "scale": 1.2})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == synth_run["run_id"]
    assert body["transition_pct"] == pytest.approx(
        float(body["transition_band"]["ratio"]) * 100.0, rel=1e-9)
    assert body["threshold"] == pytest.approx(1.2 * BASE_T)
    assert f"{body['threshold']:.4f}" == f"{1.2 * BASE_T:.4f}"
    hist = body["index_histogram"]
    assert hist["counts"] and hist["edges"]
    assert hist["threshold"] == pytest.approx(body["threshold"])
    assert 0 < hist["band_low"] < hist["band_high"]
    assert body["change_png_data"].startswith("data:image/png;base64,")
    assert len(body["change_png_data"]) > 200

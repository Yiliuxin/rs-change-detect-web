# -*- coding: utf-8 -*-
"""批次 E 接口/算法测：方法对比 / 阈值敏感性（NF9 只读实验，REQ-07/11/14）。

覆盖用户验收清单：
  * compare 变体配置校验：全同基准 → 400 EX1 / 超 4 个 → 400 / 他人 run → 404 EX2；
  * agreement_pct 在合成 2×2 分类图上的手工核算（分母 = 两轮共同有效像元）；
  * sensitivity 五档面积边界：阈值增大退化面积变化方向符合预期（不强单调断言）；
  * experiments.json 缓存复用：第二次请求不重算、force=true 重算；
  * compare ≥2 变体响应结构 / 数字实时算出 / 缓存落盘。

测试卫生：除 3 条权限/校验用例复用 session 级 ``change_ready`` 真实 run 外，
复算类一律走 ``synth_run`` 合成 run（monkeypatch ``task_repo.get_task`` 与
``run_dir`` 指向 tmp_path），不触发真实 COG 下载、不写 DB、不碰真实历史运行，
保证用例秒级、确定性、可复现。
"""
from __future__ import annotations

import json
import uuid

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.config.constants import COMPARE_MAX_VARIANTS, SENSITIVITY_SCALES
from app.service import experiment_service as ES

COMPARE_URL = "/api/v1/result/compare"
SENS_URL = "/api/v1/result/sensitivity"

# 合成 run 口径（全部取自常量/显式构造，禁止硬编码后端结论数字）
BASE_T = 0.20
PIXEL_AREA_M2 = 100.0
H = W = 64
DELTAS = (-0.17, -0.19, -0.21, -0.23, -0.25)   # 5 个退化条带，跨 0.8T~1.2T
STRIP_ROWS = 10
BASE_BAND_RATIO = 0.125                          # 快照里的基准过渡带占比


def _degraded_count(threshold: float) -> int:
    """按对称阈值 T 直接数出应判为退化的条带像元数（不含形态学）。"""
    return sum(STRIP_ROWS * W for dv in DELTAS if abs(dv) > threshold)


def _write_synth_products(out) -> tuple:
    """写 64×64 双时相双波段指数栅格 + 基准三类图（合成 run）。"""
    t1 = np.full((H, W), 0.30, dtype=np.float32)
    t2 = t1.copy()
    for i, dv in enumerate(DELTAS):
        r0 = 2 + i * 12
        t2[r0:r0 + STRIP_ROWS, :] = t1[r0:r0 + STRIP_ROWS, :] + dv
    aux1 = np.full((H, W), 0.10, dtype=np.float32)
    aux2 = aux1 + 0.01
    transform = from_origin(116.25, 29.25, 0.0001, 0.0001)
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 2,
        "width": W, "height": H, "crs": "EPSG:4326",
        "transform": transform, "nodata": float("nan"),
    }
    for name, primary, auxiliary in (
        ("index_t1.tif", t1, aux1), ("index_t2.tif", t2, aux2),
    ):
        with rasterio.open(str(out / name), "w", **profile) as ds:
            ds.write(primary, 1)
            ds.write(auxiliary, 2)

    base_change = np.full((H, W), 3, dtype=np.uint8)
    for i, dv in enumerate(DELTAS):
        if abs(dv) > BASE_T:
            r0 = 2 + i * 12
            base_change[r0:r0 + STRIP_ROWS, :] = 2
    cprofile = {**profile, "dtype": "uint8", "count": 1, "nodata": 0}
    with rasterio.open(str(out / "change.tif"), "w", **cprofile) as ds:
        ds.write(base_change, 1)
    return t1, t2, base_change


def _synth_snapshot() -> dict:
    """基准参数快照：面积口径 / 阈值 / 三类统计 / 过渡带占比齐备。"""
    degraded_px = _degraded_count(BASE_T)
    stable_px = H * W - degraded_px
    stats = [
        {"change_class": 1, "pixel_count": 0, "area_ha": 0.0},
        {"change_class": 2, "pixel_count": degraded_px,
         "area_ha": degraded_px * PIXEL_AREA_M2 / 10000.0},
        {"change_class": 3, "pixel_count": stable_px,
         "area_ha": stable_px * PIXEL_AREA_M2 / 10000.0},
    ]
    return {
        "index": "NDVI", "method": "otsu",
        "threshold": BASE_T, "threshold_source": "manual",
        "pixel_area_m2": PIXEL_AREA_M2,
        "stats": stats,
        "transition_band": {"ratio": BASE_BAND_RATIO},
        "test_data": True,
    }


@pytest.fixture()
def synth_run(tmp_path, monkeypatch):
    """合成 run：只读实验所需产物全部落 tmp_path，不碰真实 runs / DB。"""
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
    """局部启用 JWT 网关（风格与 test_api_auth.py / 批次 D 一致）。"""
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
# 1~3. compare 变体配置校验 / 归属隔离（真实 run，不触发复算，快）
# --------------------------------------------------------------------------- #
def test_compare_all_same_variants_returns_ex1(client, change_ready):
    """所有变体与基准 (index,method) 全同 → 400 EX1，{code,message,run_id} 齐全。"""
    rid = change_ready["run_id"]
    resp = client.post(COMPARE_URL, json={"run_id": rid, "variants": [{}]})
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert {"code", "message", "run_id"} <= set(body)
    assert body["code"] == "EX1"
    assert body["run_id"] == rid
    assert "至少一项" in body["message"] and body["message"] != "EX1"


def test_compare_over_four_variants_returns_400(client, change_ready):
    """超过 COMPARE_MAX_VARIANTS 个变体 → 400 EX12（不是 Pydantic 422）。"""
    rid = change_ready["run_id"]
    variants = [{"index": "NDWI", "method": "regression"}
                for _ in range(COMPARE_MAX_VARIANTS + 1)]
    resp = client.post(COMPARE_URL, json={"run_id": rid, "variants": variants})
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "EX12"
    assert body["run_id"] == rid


def test_compare_other_users_run_returns_404(client, auth_on, change_ready):
    """他人 run → 404 EX2（不泄漏存在性，与 GET /runs/{id} 同口径）。"""
    a = "pa_e_a_" + uuid.uuid4().hex[:8]
    b = "pa_e_b_" + uuid.uuid4().hex[:8]
    rid = change_ready["run_id"]
    try:
        ra, rb = _register(client, a), _register(client, b)
        assert ra.status_code == 200 and rb.status_code == 200, (ra.text, rb.text)
        from app.repository.db import db_cursor
        with db_cursor() as cur:
            cur.execute("UPDATE task SET user_id = %s WHERE run_id::text = %s",
                        (ra.json()["user_id"], rid))
        headers = {"Authorization": "Bearer " + rb.json()["token"]}
        resp = client.post(
            COMPARE_URL, headers=headers,
            json={"run_id": rid,
                  "variants": [{"index": "NDWI", "method": "regression"}]},
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "EX2"
        # 敏感性端点同款归属校验
        resp2 = client.post(SENS_URL, headers=headers, json={"run_id": rid})
        assert resp2.status_code == 404, resp2.text
        assert resp2.json()["code"] == "EX2"
    finally:
        from app.repository.db import db_cursor
        with db_cursor() as cur:
            cur.execute("UPDATE task SET user_id = NULL WHERE run_id::text = %s", (rid,))
        _cleanup_user(a)
        _cleanup_user(b)


# --------------------------------------------------------------------------- #
# 4. agreement_pct 在合成 2×2 分类图上的手工核算
# --------------------------------------------------------------------------- #
def test_agreement_pct_hand_calculated_on_synthetic_2x2():
    """纯函数：分母恒为两轮共同有效像元，逐像元一致率手工核算。

    base = [[1,2],[3,0]]；variant = [[1,2],[2,3]]
    共同有效 = (0,0)(0,1)(1,0) 共 3 个（base[1,1]=0 为无效，排除）；
    一致 = 1==1、2==2 共 2 个，3!=2 不一致 → 2/3 = 66.666…%。
    再叠加 valid_mask 排除 (0,1) → 分母 2、一致 1 → 50%。
    """
    base = np.array([[1, 2], [3, 0]], dtype=np.uint8)
    variant = np.array([[1, 2], [2, 3]], dtype=np.uint8)
    assert ES.agreement_pct(base, variant) == pytest.approx(200.0 / 3.0, rel=1e-9)

    mask = np.array([[True, False], [True, False]], dtype=bool)
    assert ES.agreement_pct(base, variant, valid_mask=mask) == pytest.approx(50.0)

    # 无共同有效像元 / 基准图缺失 / 形状不符：如实返回 0.0，不编造一致率
    assert ES.agreement_pct(np.zeros((2, 2), dtype=np.uint8), variant) == 0.0
    assert ES.agreement_pct(None, variant) == 0.0
    assert ES.agreement_pct(base, np.zeros((3, 3), dtype=np.uint8)) == 0.0


# --------------------------------------------------------------------------- #
# 5. sensitivity 五档与退化面积变化方向
# --------------------------------------------------------------------------- #
def test_sensitivity_five_scales_and_direction(client, synth_run):
    """五档 t = scale·T；阈值增大时退化面积不增且首尾严格下降（不强单调断言）。"""
    resp = client.post(SENS_URL, json={"run_id": synth_run["run_id"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["base_threshold"] == pytest.approx(BASE_T)
    assert body["computed_at"]
    sweep = body["sweep"]
    assert [float(s["scale"]) for s in sweep] == [float(x) for x in SENSITIVITY_SCALES]
    for s in sweep:
        assert s["t"] == pytest.approx(s["scale"] * BASE_T)
        assert [int(a["change_class"]) for a in s["areas"]] == [1, 2, 3]
        assert all(a["area_ha"] >= 0 for a in s["areas"])
        assert 0.0 <= s["transition_pct"] <= 100.0
    degraded = [float(s["areas"][1]["area_ha"]) for s in sweep]
    assert all(degraded[i + 1] <= degraded[i] + 1e-6 for i in range(len(degraded) - 1)), degraded
    assert degraded[0] > degraded[-1], degraded
    # 数字实时算出：总面积守恒（形态学不改有效像元总数），且不是硬编码常量
    totals = [sum(float(a["area_ha"]) for a in s["areas"]) for s in sweep]
    expected_total = H * W * PIXEL_AREA_M2 / 10000.0
    assert all(t == pytest.approx(expected_total, rel=1e-6) for t in totals), totals


# --------------------------------------------------------------------------- #
# 6. experiments.json 缓存复用 / force=true 重算
# --------------------------------------------------------------------------- #
def test_experiments_cache_reuse_and_force(client, synth_run, monkeypatch):
    """第二次请求命中缓存不重算；force=true 重算；两接口共用同一缓存文件。"""
    calls = {"n": 0}
    original = ES.recompute

    def counting(ctx, *args, **kwargs):
        calls["n"] += 1
        return original(ctx, *args, **kwargs)

    # compare / sensitivity 内部都是模块级直接调用 recompute，可安全计数
    monkeypatch.setattr(ES, "recompute", counting)

    rid = synth_run["run_id"]
    payload = {"run_id": rid, "variants": [
        {"index": "NDWI", "method": "otsu", "threshold_mode": "otsu"},
        {"index": "NDVI", "method": "regression", "threshold_mode": "otsu"},
    ]}
    first = client.post(COMPARE_URL, json=payload)
    assert first.status_code == 200, first.text
    assert calls["n"] == 2, calls

    second = client.post(COMPARE_URL, json=payload)
    assert second.status_code == 200
    assert second.json()["computed_at"] == first.json()["computed_at"]
    assert calls["n"] == 2, calls                    # 命中缓存，0 次重算

    forced = client.post(COMPARE_URL, json={**payload, "force": True})
    assert forced.status_code == 200
    assert calls["n"] == 4, calls                    # force=true → 2 个变体重算

    s1 = client.post(SENS_URL, json={"run_id": rid})
    assert s1.status_code == 200, s1.text
    assert calls["n"] == 9, calls                    # 五档 → +5
    s2 = client.post(SENS_URL, json={"run_id": rid})
    assert s2.json()["computed_at"] == s1.json()["computed_at"]
    assert calls["n"] == 9, calls
    s3 = client.post(SENS_URL, json={"run_id": rid, "force": True})
    assert s3.status_code == 200
    assert calls["n"] == 14, calls                   # force=true → +5

    cache_file = synth_run["dir"] / "experiments.json"
    assert cache_file.is_file()
    cache = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cache["version"] == 1
    assert cache["compare"] and cache["sensitivity"]
    assert len(cache["sensitivity"]["sweep"]) == len(SENSITIVITY_SCALES)


# --------------------------------------------------------------------------- #
# 7. compare ≥2 变体响应结构 + agreement 端到端手工复核 + 缓存落盘
# --------------------------------------------------------------------------- #
def test_compare_two_variants_response_shape(client, synth_run):
    """两个变体全部实时算出；agreement_pct 与纯函数手工核算一致；只写缓存文件。"""
    rid = synth_run["run_id"]
    resp = client.post(COMPARE_URL, json={"run_id": rid, "variants": [
        {"index": "NDWI", "method": "otsu"},
        {"index": "NDVI", "method": "regression"},
    ]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    base = body["base"]
    assert base["run_id"] == rid
    assert base["index"] == "NDVI" and base["method"] == "otsu"
    assert base["threshold"] == pytest.approx(BASE_T)
    assert base["pixel_area_m2"] == pytest.approx(PIXEL_AREA_M2)
    assert base["transition_pct"] == pytest.approx(BASE_BAND_RATIO * 100.0)
    assert [int(a["change_class"]) for a in base["areas"]] == [1, 2, 3]

    assert len(body["variants"]) == 2
    assert body["computed_at"]
    for v in body["variants"]:
        assert [int(a["change_class"]) for a in v["areas"]] == [1, 2, 3]
        assert v["threshold"] >= 0
        assert 0.0 <= v["transition_pct"] <= 100.0
        assert 0.0 <= v["agreement_pct"] <= 100.0
        assert v["config"]["index"] in ("NDVI", "NDWI")
        assert v["config"]["method"] in ("otsu", "regression")
        assert v["config"]["threshold_mode"] == "otsu"
        assert all(a["area_ha"] >= 0 for a in v["areas"])

    # 端到端手工复核：同一套纯函数重算第一个变体的分类图，与基准落盘三类图比对
    ctx = ES.load_context(rid)
    again = ES.recompute(ctx, "NDWI", "otsu")
    hand = ES.agreement_pct(ctx.base_change, again["change"], again["valid"])
    assert body["variants"][0]["agreement_pct"] == pytest.approx(hand)

    cache = json.loads((synth_run["dir"] / "experiments.json").read_text(encoding="utf-8"))
    assert len(cache["compare"]) == 1
    assert (synth_run["dir"] / "index_t1.tif").is_file()      # 只读复用，未改写
    assert not (synth_run["dir"] / "change.png").exists()     # 未落盘任何产物

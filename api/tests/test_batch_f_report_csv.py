# -*- coding: utf-8 -*-
"""批次 F · PDF「实验分析」章节 + 只读 CSV 导出（REQ-12/14）。

覆盖：
  * PDF 章节 4 例：compare+sensitivity / 两者皆无 / 只有 compare / 只有 sensitivity；
  * 缓存注入：多条 compare 取 computed_at 最新一条、sensitivity=null 如实省略、缓存不被改写；
  * 敏感性自动结论文案逐字对齐前端 sensConclusion（含无可比较相邻档兜底）；
  * CSV 两接口：命中 200 + BOM + 列名 + 行数、未命中 404 EX3、不存在 / 他人 run 404 EX2、
    force 忽略不重算、缓存未命中时绝不写缓存文件、缺字段如实标「—」。

测试卫生：tmp_path 造实验缓存 + monkeypatch task_repo.get_task / run_dir，
不依赖真实 COG、不写真实 runs、不碰真实历史；任何既有测试断言均不改。
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.algorithm import writers
from app.config import constants as C
from app.service import experiment_service as ES, report_service as RS

COMPARE_CSV_URL = "/api/v1/result/compare/csv"
SENS_CSV_URL = "/api/v1/result/sensitivity/csv"

BASE_T = 0.38671875
COMPARE_HEADER = "类型,指数,方法,阈值方式,阈值T,增强_ha,退化_ha,稳定_ha,过渡带占比_%,一致率_%"
SENS_HEADER = "倍数,阈值T,增强_ha,退化_ha,稳定_ha,过渡带占比_%"
NO_ADJ = "本次扫描未返回可比较的相邻档，无法给出敏感区间（如实说明，不作定性结论）。"

# 与造缓存时完全一致的合法变体（基准 NDVI/otsu，至少一个与基准不同）
VARIANTS = [
    {"index": "NDWI", "method": "regression"},
    {"index": "NDVI", "method": "otsu"},
]
CMP_AT = "2026-11-18T01:25:40.625859+00:00"
SENS_AT = "2026-11-18T01:25:43.490824+00:00"


def _areas(a1, a2, a3):
    return [
        {"change_class": 1, "pixel_count": int(a1 * 100), "area_ha": a1},
        {"change_class": 2, "pixel_count": int(a2 * 100), "area_ha": a2},
        {"change_class": 3, "pixel_count": int(a3 * 100), "area_ha": a3},
    ]


def _compare_response(computed_at=CMP_AT, n_variants=2):
    variants = []
    for i in range(n_variants):
        variants.append({
            "config": {"index": "NDWI" if i == 0 else "NDVI",
                       "method": "regression" if i == 0 else "otsu",
                       "threshold_mode": "otsu", "threshold": None},
            "threshold": 0.32421875 + i * 0.01,
            "areas": _areas(531.93 + i, 1051.33, 5212.88),
            "transition_pct": 14.5203,
            "agreement_pct": 58.4544,
        })
    return {
        "base": {
            "run_id": "x", "index": "NDVI", "method": "otsu",
            "threshold": BASE_T, "threshold_source": "otsu",
            "threshold_mode": "otsu", "areas": _areas(2334.06, 97.33, 4364.75),
            "transition_pct": 10.674, "pixel_area_m2": 100.0, "valid_count": 679614,
        },
        "variants": variants,
        "computed_at": computed_at,
    }


def _sensitivity_response(computed_at=SENS_AT):
    sweep = []
    for scale in C.SENSITIVITY_SCALES:
        sweep.append({
            "t": float(scale) * BASE_T,
            "scale": float(scale),
            "areas": _areas(2419.22 / float(scale), 175.61 * float(scale), 4201.31),
            "transition_pct": 8.2282 * float(scale),
        })
    return {"base_threshold": BASE_T, "sweep": sweep, "computed_at": computed_at}


@pytest.fixture()
def cached_run(tmp_path, monkeypatch):
    """合成 run：experiments.json 落 tmp_path，patch get_task / run_dir（不碰真实 runs / DB）。"""
    run_id = str(uuid.uuid4())
    root = tmp_path / "runs" / run_id
    root.mkdir(parents=True, exist_ok=True)
    snapshot = {"index": "NDVI", "method": "otsu", "threshold": BASE_T,
                "threshold_source": "otsu", "test_data": True}
    monkeypatch.setattr(
        ES.task_repo, "get_task",
        lambda rid: ({"run_id": rid, "params_snapshot": snapshot, "user_id": None}
                     if rid == run_id else None),
    )
    monkeypatch.setattr(ES, "run_dir", lambda rid: root)
    return {"run_id": run_id, "dir": root}


def _cache_file(cached_run):
    return cached_run["dir"] / C.EXPERIMENTS_CACHE_FILE


def _write_cache(cached_run, *, compare=None, sensitivity=None):
    payload = {
        "version": C.EXPERIMENTS_CACHE_VERSION,
        C.EXPERIMENTS_CACHE_COMPARE_KEY: compare or {},
        C.EXPERIMENTS_CACHE_SENSITIVITY_KEY: sensitivity,
    }
    _cache_file(cached_run).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return _cache_file(cached_run)


def _signature(run_id, variants):
    """与 compare() 完全相同的签名口径（_load_meta → validate_variants → _signature）。"""
    meta = ES._load_meta(run_id)
    return ES._signature(ES.validate_variants(variants, meta, run_id=run_id))


def _base_report(experiments):
    return {
        "run_id": "batch-f-test", "year": 2024, "valid_ratio": 0.92,
        "stats": [
            {"change_class": 1, "pixel_count": 10, "area_ha": 1.0},
            {"change_class": 2, "pixel_count": 20, "area_ha": 2.0},
            {"change_class": 3, "pixel_count": 30, "area_ha": 3.0},
        ],
        "experiments": experiments,
    }


def _write_pdf(tmp_path, experiments):
    out = tmp_path / ("report_%s.pdf" % uuid.uuid4().hex[:8])
    meta = writers.write_pdf(_base_report(experiments), out)
    return meta, out.read_bytes()


# --------------------------------------------------------------------------- #
# 1. PDF 章节 4 例 + 缓存注入（compare+sens / 都无 / 只有 compare / 只有 sens）
# --------------------------------------------------------------------------- #
def test_pdf_section_both_compare_and_sensitivity(tmp_path, cached_run):
    cmp_resp = _compare_response()
    sens_resp = _sensitivity_response()
    _write_cache(cached_run, compare={"sigA": cmp_resp}, sensitivity=sens_resp)
    sections = RS.experiment_sections_from_cache(cached_run["run_id"])
    assert sections["compare"] == cmp_resp and sections["sensitivity"] == sens_resp
    meta, pdf_bytes = _write_pdf(tmp_path, sections)
    assert meta["experiments_section"] is True
    assert meta["experiments_compare_rows"] == 1 + len(cmp_resp["variants"])
    assert meta["experiments_sensitivity_rows"] == len(C.SENSITIVITY_SCALES)
    assert meta["experiments_conclusion"] == ES.sensitivity_conclusion(sens_resp["sweep"])
    assert meta["experiments_conclusion"].startswith("结果对阈值敏感区间在 ±")
    assert len(meta["experiments_sources"]) == 2
    assert all("computed_at=" in s for s in meta["experiments_sources"])
    assert b"EXPERIMENT ANALYSIS" in pdf_bytes
    assert b"EXPERIMENT ANALYSIS SOURCE COMPARE" in pdf_bytes
    assert b"EXPERIMENT ANALYSIS SOURCE SENSITIVITY" in pdf_bytes


def test_pdf_section_absent_when_no_experiments(tmp_path, cached_run):
    _write_cache(cached_run)  # compare={} sensitivity=None
    sections = RS.experiment_sections_from_cache(cached_run["run_id"])
    assert sections == {"compare": None, "sensitivity": None}
    meta, pdf_bytes = _write_pdf(tmp_path, sections)
    assert meta["experiments_section"] is False
    assert meta["experiments_compare_rows"] == 0
    assert meta["experiments_sensitivity_rows"] == 0
    assert meta["experiments_conclusion"] == ""
    assert meta["experiments_sources"] == []
    # 连标题都不打：PDF 字节里不得出现 EXPERIMENT ANALYSIS
    assert b"EXPERIMENT ANALYSIS" not in pdf_bytes


def test_pdf_section_compare_only(tmp_path, cached_run):
    cmp_resp = _compare_response()
    _write_cache(cached_run, compare={"sigB": cmp_resp})  # sensitivity=null
    sections = RS.experiment_sections_from_cache(cached_run["run_id"])
    assert sections["sensitivity"] is None
    meta, pdf_bytes = _write_pdf(tmp_path, sections)
    assert meta["experiments_section"] is True
    assert meta["experiments_compare_rows"] == 1 + len(cmp_resp["variants"])
    assert meta["experiments_sensitivity_rows"] == 0
    assert meta["experiments_conclusion"] == ""
    assert len(meta["experiments_sources"]) == 1
    assert b"EXPERIMENT ANALYSIS" in pdf_bytes
    assert b"EXPERIMENT ANALYSIS SOURCE SENSITIVITY" not in pdf_bytes


def test_pdf_section_sensitivity_only(tmp_path, cached_run):
    sens_resp = _sensitivity_response()
    _write_cache(cached_run, sensitivity=sens_resp)  # compare={}
    sections = RS.experiment_sections_from_cache(cached_run["run_id"])
    assert sections["compare"] is None
    meta, pdf_bytes = _write_pdf(tmp_path, sections)
    assert meta["experiments_section"] is True
    assert meta["experiments_compare_rows"] == 0
    assert meta["experiments_sensitivity_rows"] == len(C.SENSITIVITY_SCALES)
    assert meta["experiments_conclusion"].startswith("结果对阈值敏感区间在 ±")
    assert len(meta["experiments_sources"]) == 1
    assert b"EXPERIMENT ANALYSIS" in pdf_bytes
    assert b"EXPERIMENT ANALYSIS SOURCE COMPARE" not in pdf_bytes


def test_latest_compare_by_computed_at_and_cache_untouched(tmp_path, cached_run):
    old = _compare_response("2026-01-01T00:00:00+00:00", n_variants=1)
    new = _compare_response("2026-12-31T23:59:59+00:00", n_variants=2)
    path = _write_cache(cached_run, compare={"sig-old": old, "sig-new": new})
    sections = RS.experiment_sections_from_cache(cached_run["run_id"])
    assert sections["compare"]["computed_at"] == new["computed_at"]
    assert sections["sensitivity"] is None
    # 只读：缓存文件仍含两条，未被改写
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw[C.EXPERIMENTS_CACHE_COMPARE_KEY]) == 2
    meta, _ = _write_pdf(tmp_path, sections)
    assert meta["experiments_compare_rows"] == 1 + len(new["variants"])


# --------------------------------------------------------------------------- #
# 2. 敏感性自动结论文案：逐字移植前端 sensConclusion
# --------------------------------------------------------------------------- #
def test_sensitivity_conclusion_verbatim_from_real_cache_values():
    # 数值取自 api/runs/0cdc51cc-.../experiments.json 的 sensitivity.sweep（如实实测）
    sweep = [
        {"scale": 0.8, "areas": _areas(2419.22, 175.61, 4201.31)},
        {"scale": 0.9, "areas": _areas(2376.67, 133.16, 4286.31)},
        {"scale": 1.0, "areas": _areas(2334.06, 97.33, 4364.75)},
        {"scale": 1.1, "areas": _areas(2282.03, 71.60, 4442.51)},
        {"scale": 1.2, "areas": _areas(2024.28, 52.88, 4718.98)},
    ]
    # 相邻档「增强+退化」合计：2594.83 / 2509.83 / 2431.39 / 2353.63 / 2077.16
    # 最陡 1.1→1.2，delta=276.47，dev=0.2 → ±20%
    expected = ("结果对阈值敏感区间在 ±20%：最陡档 1.1×→1.2×，"
                "增强＋退化面积合计变化 276.47 ha（数字全部取自本次五档实测扫描）。")
    assert ES.sensitivity_conclusion(sweep) == expected
    best = ES.sensitivity_best_interval(sweep)
    assert best["from"] == 1.1 and best["to"] == 1.2
    assert best["delta"] == pytest.approx(276.47)
    # 乱序输入也按 scale 升序比较（与前端渲染前排序一致），且不改动原 list 顺序
    shuffled = [sweep[4], sweep[0], sweep[3], sweep[1], sweep[2]]
    assert ES.sensitivity_conclusion(shuffled) == expected
    assert shuffled[0]["scale"] == 1.2


def test_sensitivity_conclusion_no_comparable_adjacent_scales():
    assert ES.sensitivity_conclusion([]) == NO_ADJ
    assert ES.sensitivity_conclusion([{"scale": 1.0, "areas": []}]) == NO_ADJ


# --------------------------------------------------------------------------- #
# 3. CSV 只读接口：命中 / 未命中 / 不存在 / 他人 / force 忽略 / 缺字段
# --------------------------------------------------------------------------- #
def test_compare_csv_hit_200_bom_header_rows(client, cached_run):
    rid = cached_run["run_id"]
    sig = _signature(rid, VARIANTS)
    cmp_resp = _compare_response(n_variants=2)
    _write_cache(cached_run, compare={sig: cmp_resp})
    resp = client.post(COMPARE_CSV_URL, json={"run_id": rid, "variants": VARIANTS})
    assert resp.status_code == 200, resp.text
    assert resp.content.startswith(b"\xef\xbb\xbf")
    assert resp.headers["content-type"].startswith("text/csv")
    assert f'compare_{rid[:8]}.csv' in resp.headers["content-disposition"]
    lines = resp.content.decode("utf-8-sig").rstrip("\r\n").split("\r\n")
    assert lines[0] == COMPARE_HEADER
    assert len(lines) == 1 + 1 + len(cmp_resp["variants"])   # 表头 + 基准 + 每个变体
    assert lines[1].startswith("基准,") and lines[1].endswith(",—")
    assert lines[2].startswith("变体,")


def test_sensitivity_csv_hit_200_bom_header_five_rows(client, cached_run):
    rid = cached_run["run_id"]
    _write_cache(cached_run, sensitivity=_sensitivity_response())
    resp = client.post(SENS_CSV_URL, json={"run_id": rid})
    assert resp.status_code == 200, resp.text
    assert resp.content.startswith(b"\xef\xbb\xbf")
    assert resp.headers["content-type"].startswith("text/csv")
    assert f'sensitivity_{rid[:8]}.csv' in resp.headers["content-disposition"]
    lines = resp.content.decode("utf-8-sig").rstrip("\r\n").split("\r\n")
    assert lines[0] == SENS_HEADER
    assert len(lines) == 1 + len(C.SENSITIVITY_SCALES)
    assert lines[1].startswith("0.8×,")


def test_compare_csv_cache_miss_returns_ex3_and_no_cache_file(client, cached_run, monkeypatch):
    rid = cached_run["run_id"]
    assert not _cache_file(cached_run).exists()
    calls = {"compare": 0, "write": 0}
    monkeypatch.setattr(ES, "compare",
                        lambda *a, **k: calls.update(compare=calls["compare"] + 1))
    monkeypatch.setattr(ES, "write_cache",
                        lambda *a, **k: calls.update(write=calls["write"] + 1))
    resp = client.post(COMPARE_CSV_URL, json={"run_id": rid, "variants": VARIANTS})
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "EX3" and body["run_id"] == rid
    assert "请先在页面执行实验" in body["message"]
    assert not _cache_file(cached_run).exists()             # 缓存不存在时不被创建
    assert calls == {"compare": 0, "write": 0}              # 不重算 / 不写缓存


def test_sensitivity_csv_cache_miss_returns_ex3_and_no_recompute(
        client, cached_run, monkeypatch):
    rid = cached_run["run_id"]
    calls = {"n": 0}
    monkeypatch.setattr(ES, "sensitivity",
                        lambda *a, **k: calls.update(n=calls["n"] + 1))
    resp = client.post(SENS_CSV_URL, json={"run_id": rid})
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "EX3"
    assert "请先在页面执行实验" in resp.json()["message"]
    assert not _cache_file(cached_run).exists()
    assert calls["n"] == 0


def test_csv_missing_run_returns_ex2(client, cached_run):
    rid = str(uuid.uuid4())   # patched get_task → None
    for url, payload in (
        (COMPARE_CSV_URL, {"run_id": rid, "variants": VARIANTS}),
        (SENS_CSV_URL, {"run_id": rid}),
    ):
        resp = client.post(url, json=payload)
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "EX2"


def test_csv_other_users_run_returns_ex2(client, cached_run, monkeypatch):
    import app.api.deps as deps
    rid = cached_run["run_id"]
    # 让 task 归属他人且当前用户 id 非空 → AUTH-03 被实验端点统一转 EX2(404)
    monkeypatch.setattr(deps, "current_user_id", lambda request: "user-me")
    monkeypatch.setattr(
        ES.task_repo, "get_task",
        lambda r: {"run_id": r, "params_snapshot": {}, "user_id": "someone-else"},
    )
    for url, payload in (
        (COMPARE_CSV_URL, {"run_id": rid, "variants": VARIANTS}),
        (SENS_CSV_URL, {"run_id": rid}),
    ):
        resp = client.post(url, json=payload)
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "EX2"


def test_csv_force_ignored_never_recomputes(client, cached_run, monkeypatch):
    rid = cached_run["run_id"]
    sig = _signature(rid, VARIANTS)
    _write_cache(cached_run, compare={sig: _compare_response(n_variants=2)},
                 sensitivity=_sensitivity_response())
    calls = {"compare": 0, "sensitivity": 0}
    monkeypatch.setattr(ES, "compare",
                        lambda *a, **k: calls.update(compare=calls["compare"] + 1))
    monkeypatch.setattr(ES, "sensitivity",
                        lambda *a, **k: calls.update(sensitivity=calls["sensitivity"] + 1))
    r1 = client.post(COMPARE_CSV_URL,
                     json={"run_id": rid, "variants": VARIANTS, "force": True})
    r2 = client.post(SENS_CSV_URL, json={"run_id": rid, "force": True})
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert calls == {"compare": 0, "sensitivity": 0}


def test_csv_missing_fields_render_dash(client, cached_run):
    rid = cached_run["run_id"]
    sig = _signature(rid, VARIANTS)
    minimal = {"base": {"index": "NDVI"},
               "variants": [{"config": {}}],
               "computed_at": CMP_AT}
    _write_cache(cached_run, compare={sig: minimal})
    resp = client.post(COMPARE_CSV_URL, json={"run_id": rid, "variants": VARIANTS})
    assert resp.status_code == 200, resp.text
    lines = resp.content.decode("utf-8-sig").rstrip("\r\n").split("\r\n")
    assert lines[0] == COMPARE_HEADER
    assert lines[1].count("—") >= 7      # 缺字段如实标「—」，绝不补算

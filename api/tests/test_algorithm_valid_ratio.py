# -*- coding: utf-8 -*-
"""REQ-05：合成 SCL 栅格四段有效像元占比（EX3 / EX11 / EX5 / OK）。

用户硬约束 3：必须真实构造 valid_ratio≈5% 的合成栅格专门命中 EX11
（终止门槛已放宽到 VALID_RATIO_MIN=0.10，不构造永远走不到）；EX3 / EX5
同样用合成 mask 覆盖，不指望现有 seed（鄱阳湖 valid_ratio≈0.98 只走 OK 档）。

所有分支阈值从 constants.VALID_RATIO_MIN / VALID_RATIO_WARN 读取，
目标占比亦由常量推导，测试里不写死 0.10 / 0.50 来判定分支。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.algorithm.mask_builder import build_masks
from app.algorithm.mask_pipeline import (
    compute_valid_ratio, judge_valid_ratio, run_mask_pipeline,
)
from app.config.constants import ERROR_MESSAGES, VALID_RATIO_MIN, VALID_RATIO_WARN

# 四档目标占比全部由常量推导（不写死业务阈值）
R_ALL_CLOUD = 0.0                                                    # 0%
R_EX11 = VALID_RATIO_MIN / 2.0                                       # ≈5%
R_EX5 = VALID_RATIO_MIN + (VALID_RATIO_WARN - VALID_RATIO_MIN) / 2.0  # 30%
R_OK = VALID_RATIO_WARN + (1.0 - VALID_RATIO_WARN) * 0.6             # 80%

CASES = [
    pytest.param(R_ALL_CLOUD, "EX3", True, False, id="00pct_all_cloud_EX3"),
    pytest.param(R_EX11, "EX11", True, False, id="05pct_insufficient_EX11"),
    pytest.param(R_EX5, "EX5", False, True, id="30pct_warn_EX5"),
    pytest.param(R_OK, "OK", False, False, id="80pct_pass_OK"),
]


def _direct_verdict(scl):
    """磁盘合成栅格 → build_masks → compute_valid_ratio → judge_valid_ratio。"""
    masks = build_masks(scl["array"])
    ratio = compute_valid_ratio(masks.valid)
    verdict = judge_valid_ratio(ratio, mask=masks.valid)
    return masks, ratio, verdict


@pytest.mark.req("REQ-05")
@pytest.mark.parametrize("target,code,terminate,warn", CASES)
def test_synthetic_scl_four_segments(scl_factory, target, code, terminate, warn):
    """4 档真实合成 SCL 文件 → 四段判定 code / terminate / warn。"""
    scl = scl_factory(target)
    assert scl["path"].is_file() and scl["path"].stat().st_size > 0
    assert scl["array"].shape == (100, 100)
    assert scl["array"].dtype == np.uint8

    masks, ratio, v = _direct_verdict(scl)
    assert ratio == pytest.approx(target, abs=0.005), (target, ratio)
    if code == "EX11":
        # 硬约束 3：≈5% 有效，专门命中 EX11 终止档
        assert 0.04 < ratio < 0.06, ratio
    assert v.code == code, (ratio, v.as_dict())
    assert v.terminate is terminate and v.warn is warn, v.as_dict()
    assert v.total == 100 * 100
    assert v.valid_count == int(masks.valid.sum())
    if code != "OK":
        assert v.message == ERROR_MESSAGES[code]
    else:
        assert isinstance(v.message, str) and v.message


@pytest.mark.req("REQ-05")
@pytest.mark.parametrize("target,code,terminate,warn", CASES)
def test_run_mask_pipeline_same_verdict(scl_factory, target, code, terminate, warn):
    """同一合成栅格文件走完整流水线：SCL 重采样对齐 → 掩膜 → 四段判定。"""
    scl = scl_factory(target)
    masks, verdict = run_mask_pipeline(scl["path"], scl["profile"])
    _, direct_ratio, direct = _direct_verdict(scl)
    assert verdict.code == code == direct.code
    assert verdict.terminate is terminate and verdict.warn is warn
    assert verdict.valid_ratio == pytest.approx(direct_ratio, abs=0.01)
    assert verdict.total == 100 * 100
    assert masks.valid.shape == (100, 100)
    assert int(masks.valid.sum()) == verdict.valid_count


@pytest.mark.req("REQ-05")
def test_ex11_is_reachable_with_synthetic_5pct(scl_factory):
    """专门证明 EX11 在终止门槛放宽后仍可达（不是死分支）。"""
    scl = scl_factory(R_EX11)
    masks = build_masks(scl["array"])
    ratio = compute_valid_ratio(masks.valid)
    assert 0.0 < ratio < VALID_RATIO_MIN, ratio
    assert int(masks.cloud.sum()) == pytest.approx(0.95 * masks.valid.size, rel=0.01)
    v = judge_valid_ratio(ratio, mask=masks.valid)
    assert v.code == "EX11" and v.terminate is True and v.warn is False


@pytest.mark.req("REQ-05")
def test_judge_boundaries_are_mutually_exclusive_and_exhaustive():
    """VALID_RATIO_MIN / VALID_RATIO_WARN 恰好命中时四段互斥且穷尽。"""
    eps = VALID_RATIO_MIN * 1e-3
    boundaries = [
        (0.0, "EX3", True, False),
        (VALID_RATIO_MIN - eps, "EX11", True, False),
        (VALID_RATIO_MIN, "EX5", False, True),                 # 恰好命中 → 可继续
        (VALID_RATIO_WARN - eps, "EX5", False, True),
        (VALID_RATIO_WARN, "OK", False, False),                 # 恰好命中 → 通过
        (1.0, "OK", False, False),
    ]
    for ratio, code, terminate, warn in boundaries:
        v = judge_valid_ratio(ratio)
        assert (v.code, v.terminate, v.warn) == (code, terminate, warn), (ratio, v.as_dict())
        assert not (v.terminate and v.warn)     # 终止档与告警档互斥

    seen = {judge_valid_ratio(float(r)).code for r in np.linspace(0.0, 1.0, 1001)}
    assert seen == {"EX3", "EX11", "EX5", "OK"}
    for bad in (-0.01, 1.01):
        with pytest.raises(ValueError):
            judge_valid_ratio(bad)


@pytest.mark.req("REQ-05")
def test_valid_ratio_constants_match_hld_37():
    """对照 HLD 3.7 常量口径（可用 approx 断言边界值，但不用于判分支）。"""
    assert VALID_RATIO_MIN == pytest.approx(0.10)
    assert VALID_RATIO_WARN == pytest.approx(0.50)
    assert 0.0 < VALID_RATIO_MIN < VALID_RATIO_WARN <= 1.0
    assert ERROR_MESSAGES["EX3"] and ERROR_MESSAGES["EX11"] and ERROR_MESSAGES["EX5"]

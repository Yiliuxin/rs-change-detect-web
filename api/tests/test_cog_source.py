# -*- coding: utf-8 -*-
"""COG 源解析测试（REQ-02 / REQ-03 / SYS-01）。

重点回归：**同一 run_id 内切换影像对必须各自下载**。
曾经的缺陷是缓存目录只按 ``run_id/_cog/<tag>`` 区分，而不同影像对的波段文件名
完全相同（都是 ``t1_B02.tif``），导致第二个影像对命中第一个的文件，
表现为 clip_window 离谱、valid_ratio=0、检测直接 EX3。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.service import cog_source
from app.service.cog_source import BANDS, parse_cog_path, resolve_bands


class _FakeClient:
    """记录下载次数；把对象名写进本地文件，便于断言没串文件。"""

    def __init__(self):
        self.downloads = []

    def fget_object(self, bucket, obj, local):
        self.downloads.append(obj)
        Path(local).write_text(obj, encoding="utf-8")


@pytest.fixture()
def fake_minio(monkeypatch, tmp_path):
    client = _FakeClient()

    monkeypatch.setattr(cog_source, "get_client", lambda settings: client)
    monkeypatch.setattr(cog_source, "StorageSettings", type("S", (), {"from_env": staticmethod(lambda: None)}))
    monkeypatch.setattr(
        cog_source, "list_objects",
        lambda c, bucket, prefix="": [f"{prefix}{tag}_{b}.tif" for tag in ("t1", "t2") for b in BANDS],
    )
    monkeypatch.setattr(cog_source, "RUNS_ROOT", tmp_path)
    return client


def test_parse_cog_path_variants():
    assert parse_cog_path("minio://scenes/pair_poyang/t1/") == ("scenes", "pair_poyang/t1/", "pair_poyang", "t1")
    assert parse_cog_path("minio://scenes/pair_nanchang/t2/")[2] == "pair_nanchang"
    # 末尾无斜杠也应归一到带斜杠
    assert parse_cog_path("minio://scenes/pair_x/t1")[1].endswith("/")


def test_switch_pair_in_same_run_does_not_reuse_cache(fake_minio, tmp_path):
    """同一 run_id 下两个影像对必须落在不同目录、内容不串。"""
    run_id = "run-switch"
    a = resolve_bands("minio://scenes/pair_aaa/t1/", run_id)
    b = resolve_bands("minio://scenes/pair_bbb/t1/", run_id)

    assert "pair_aaa" in str(a["B02"])
    assert "pair_bbb" in str(b["B02"])
    assert a["B02"] != b["B02"]

    # 各自文件内容必须来自各自前缀（若复用缓存，b 的内容会是 a 的）
    assert "pair_aaa" in a["B02"].read_text(encoding="utf-8")
    assert "pair_bbb" in b["B02"].read_text(encoding="utf-8")

    # 两次解析共 10 个波段，一次都不能少
    assert len(fake_minio.downloads) == len(BANDS) * 2


def test_same_pair_second_call_hits_cache(fake_minio):
    """同一影像对重复解析应命中本地缓存，不重复下载。"""
    run_id = "run-cache"
    first = resolve_bands("minio://scenes/pair_ccc/t1/", run_id)
    n_after_first = len(fake_minio.downloads)
    second = resolve_bands("minio://scenes/pair_ccc/t1/", run_id)

    assert first == second
    assert len(fake_minio.downloads) == n_after_first == len(BANDS)


def test_t1_and_t2_of_same_pair_are_separated(fake_minio):
    """同一影像对的 t1 / t2 也要分目录，否则两个时相互相覆盖。"""
    run_id = "run-tags"
    t1 = resolve_bands("minio://scenes/pair_ddd/t1/", run_id)
    t2 = resolve_bands("minio://scenes/pair_ddd/t2/", run_id)
    assert t1["B04"].parent != t2["B04"].parent
    assert t1["B04"].parent.name == "t1"
    assert t2["B04"].parent.name == "t2"


def test_missing_band_raises_sys01(fake_minio, monkeypatch):
    """缺波段 → SYS-01（不允许静默降级）。"""
    monkeypatch.setattr(cog_source, "list_objects", lambda c, bucket, prefix="": [f"{prefix}t1_B02.tif"])
    from app.api.errors import ApiError

    with pytest.raises(ApiError) as e:
        resolve_bands("minio://scenes/pair_eee/t1/", "run-miss")
    assert e.value.code == "SYS-01"

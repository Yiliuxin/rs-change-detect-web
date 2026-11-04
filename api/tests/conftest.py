# -*- coding: utf-8 -*-
"""pytest 全局夹具（api/tests/conftest.py）。

用户硬约束 1 在此逐条落实：
  * 把 ``C:\\Test\\Web\\api`` 与 ``api/tests`` 插入 ``sys.path``，保证 ``app.*``
    与 ``_helpers`` 可导入（只用 Python 3.12 指定解释器运行，见报告）；
  * **显式调用** ``load_env(override=True)``，使 ``api/.env`` 的
    ``MINIO_ENDPOINT=localhost:19000`` / ``PG_DSN`` 生效（覆盖进程已有值）；
  * ``client`` 用 ``fastapi.testclient.TestClient`` **进程内**调用，不依赖
    8000 端口的共享 uvicorn，也不启动/停止任何服务。

另外提供：合成 SCL 栅格工厂（scl_factory）与 session 级、只跑一次的
全链路前置 fixture（change_ready），避免每个用例重复触发 /result/change。
"""
from __future__ import annotations

import sys
from pathlib import Path

# —— sys.path：api/ 根目录 + tests/ 目录 ——
API_ROOT = Path(__file__).resolve().parents[1]      # C:\Test\Web\api
TESTS_DIR = Path(__file__).resolve().parent         # C:\Test\Web\api\tests
for _p in (str(API_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# —— 硬约束 1：显式加载 api/.env（override=True） ——
from app.config.env import load_env  # noqa: E402
load_env(override=True)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from _helpers import (  # noqa: E402
    POYANG_T1, POYANG_T2, make_change_flow, make_scl_raster, poyang_geojson,
)

__all__ = ["API_ROOT", "TESTS_DIR", "client", "scl_factory", "change_ready"]


@pytest.fixture(scope="session")
def client():
    """进程内 TestClient（session 级复用；lifespan 只执行一次 ensure_buckets）。"""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def scl_factory(tmp_path):
    """合成 SCL 栅格工厂：make(valid_ratio=...) -> {path, array, profile}。

    真实用 numpy 生成 100×100 uint8 SCL 数组（SCL=4 植被为有效，SCL=8/9
    中/高概率云为无效），并用 rasterio 写成 GeoTIFF 落 tmp_path；固定随机
    种子，结果确定性、可复现。
    """
    counter = {"n": 0}

    def make(valid_ratio: float, size: int = 100, seed: int = 20240607):
        counter["n"] += 1
        return make_scl_raster(
            tmp_path / f"scl_{counter['n']:02d}_{valid_ratio:.4f}.tif",
            valid_ratio=valid_ratio, size=size, seed=seed,
        )

    return make


@pytest.fixture(scope="session")
def change_ready(client):
    """**只跑一次**的完整前置流程（性能：/result/change 真实下载 COG + 计算）。

    顺序（用户硬约束 2）：
        ① POST /api/v1/aoi/select     -> aoi_id / run_id / matched_scenes（task 入库）
        ② POST /api/v1/result/change  -> change.tif / change.png 落盘
    各测试随后才发起 ③ POST /api/v1/accuracy/sample、④ POST /api/v1/report/export。
    """
    result = make_change_flow(client, poyang_geojson(), POYANG_T1, POYANG_T2)
    if result["aoi_status"] != 200:
        pytest.fail(f"前置 ① /aoi/select 失败：{result['aoi_status']} {result['aoi_body']}")
    if result["change_status"] != 200:
        pytest.fail(
            f"前置 ② /result/change 失败：{result['change_status']} {result['change_body']}"
        )
    return result

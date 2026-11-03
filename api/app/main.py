# -*- coding: utf-8 -*-
"""rs-change-api 应用入口（FastAPI）。

对应 HLD 2.1 服务层：提供 5 个 REST 接口（2.2.3），前缀统一 ``/api/v1``；
表现层为 http://localhost:5173 的 Vue + MapLibre 前端，CORS 仅放开该来源。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import install as install_error_handlers
from app.api.v1 import accuracy, aoi, change, report, scene
from app.config.storage import COG_BUCKET, RESULT_BUCKET, ensure_buckets, get_client

# API 版本前缀（HLD 2.2.3）
API_V1_PREFIX = "/api/v1"
# 前端开发服务器来源（HLD 2.1 表现层）
WEB_ORIGIN = "http://localhost:5173"

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动时确保 ``scenes`` / ``results`` 两桶存在（HLD 3.2 数据层，REQ-02/REQ-12）。

    存储不可用时仅告警、不抛错，保证 /health 与只读接口仍能启动；
    结果导出接口会在真正写桶时以 EX6 形式报告失败。
    """
    try:
        ready = ensure_buckets(get_client(), [COG_BUCKET, RESULT_BUCKET])
        logger.info("MinIO 桶就绪：%s", ready)
    except Exception as exc:  # noqa: BLE001 - 存储故障不应阻断服务层启动
        logger.warning("MinIO 桶校验失败（/report/export 将不可用）：%s", exc)
    yield


app = FastAPI(
    title="rs-change-api",
    version="0.1.0",
    description="遥感影像处理与地表变化检测 Web 平台 · 服务层（HLD 2.2.3）",
    lifespan=lifespan,
)

# CORS：仅允许本地前端开发服务器（表现层 → 服务层）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEB_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 5 个 REST 路由（HLD 2.2.3.1 ~ 2.2.3.5）
for _router in (aoi.router, scene.router, change.router, accuracy.router, report.router):
    app.include_router(_router, prefix=API_V1_PREFIX)

# 统一异常封装：所有异常输出 {code, message, run_id}（HLD 第 5 章 / EX1–EX11、SYS-01–05）
install_error_handlers(app)


@app.get("/health", tags=["Ops"], summary="健康检查")
def health() -> dict:
    """健康检查：返回 {"status": "ok"}。"""
    return {"status": "ok"}

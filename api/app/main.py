# -*- coding: utf-8 -*-
"""rs-change-api 应用入口（FastAPI）。

对应 HLD 2.1 服务层：提供 5 个 REST 接口（2.2.3），前缀统一 ``/api/v1``；
表现层为 http://localhost:5173 的 Vue + MapLibre 前端，CORS 仅放开该来源。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import accuracy, aoi, change, report, scene

# API 版本前缀（HLD 2.2.3）
API_V1_PREFIX = "/api/v1"
# 前端开发服务器来源（HLD 2.1 表现层）
WEB_ORIGIN = "http://localhost:5173"

app = FastAPI(
    title="rs-change-api",
    version="0.1.0",
    description="遥感影像处理与地表变化检测 Web 平台 · 服务层（HLD 2.2.3）",
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


@app.get("/health", tags=["Ops"], summary="健康检查")
def health() -> dict:
    """健康检查：返回 {"status": "ok"}。"""
    return {"status": "ok"}

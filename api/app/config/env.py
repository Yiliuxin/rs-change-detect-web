# -*- coding: utf-8 -*-
"""环境变量加载与数据库连接串（读取 api/.env）。

加载时机：导入 ``app.config`` 包（含 ``app.config.constants`` / ``app.config.storage``）
即会自动执行一次 :func:`load_env`，因此各模块的 ``os.getenv`` 都能读到 ``api/.env``。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# api/ 根目录（本文件位于 api/app/config/env.py）
API_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = API_ROOT / ".env"


def load_env(override: bool = False) -> bool:
    """加载 ``api/.env``，返回该文件是否存在。

    参数：
        override：True 时用 .env 覆盖进程已有的环境变量；默认 False（进程环境优先，
                  便于 CI / 部署时用真实环境变量覆盖本地文件）。
    """
    return load_dotenv(dotenv_path=DOTENV_PATH, override=override)


def postgres_dsn() -> str:
    """组装 PostgreSQL DSN：优先 ``PG_DSN``，缺失时由 ``PG*`` 离散变量拼接。"""
    dsn = os.getenv("PG_DSN")
    if dsn:
        return dsn
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    db = os.getenv("PGDATABASE", "rs_change")
    user = os.getenv("PGUSER", "postgres")
    password = os.getenv("PGPASSWORD", "postgres")
    return f"host={host} port={port} dbname={db} user={user} password={password}"


# 导入即加载（幂等）
load_env()

__all__ = ["API_ROOT", "DOTENV_PATH", "load_env", "postgres_dsn"]

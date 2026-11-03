# -*- coding: utf-8 -*-
"""数据库连接与参数化 SQL 基础设施（数据访问层，REQ-02/13/14）。

规则：
  - DSN 一律经 ``app.config.env.postgres_dsn()`` 读取（api/.env），禁止硬编码；
  - 全部 SQL 使用 ``%s`` 占位符参数化，杜绝拼接（HLD 3.2）；
  - 不写任何 DDL（表结构归 migrations/，只读）；
  - JSONB 参数统一用 ``psycopg2.extras.Json`` 适配。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from app.config.env import load_env, postgres_dsn

# 导入即确保 api/.env 已加载（幂等）
load_env()

__all__ = ["Json", "db_conn", "db_cursor", "as_json"]


def as_json(obj: Any) -> Json:
    """把 Python 对象包装为 PG JSONB 参数。"""
    return Json(obj)


@contextmanager
def db_conn() -> Iterator[Any]:
    """连接上下文：正常退出 commit，异常 rollback，最终 close（REQ-13/14）。"""
    conn = psycopg2.connect(postgres_dsn())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def db_cursor() -> Iterator[RealDictCursor]:
    """字典游标上下文（返回 ``RealDictRow``，可直接 ``dict(row)``）。"""
    with db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur

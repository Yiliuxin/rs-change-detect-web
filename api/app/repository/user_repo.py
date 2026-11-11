# -*- coding: utf-8 -*-
"""用户数据访问（注册/登录扩展）。

规则与既有仓储一致：只写参数化 DML，不写 DDL（表结构归 migrations/006）；
连接经 ``app.repository.db.db_cursor``（commit/rollback 由上下文管理）。
"""
from __future__ import annotations

import uuid
from typing import Optional

from app.repository.db import db_cursor

__all__ = ["insert_user", "get_user", "get_user_by_username", "count_users",
           "claim_orphan_tasks"]


def insert_user(user_id: str, username: str, password_hash: str,
                display_name: Optional[str]) -> dict:
    """新建用户，返回全字段行；username 冲突由唯一约束抛异常（上层 → AUTH-04）。"""
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (user_id, username, password_hash, display_name)
            VALUES (%s, %s, %s, %s)
            RETURNING user_id::text AS user_id, username, display_name, created_at
            """,
            (user_id, username, password_hash, display_name),
        )
        return dict(cur.fetchone())


def get_user(user_id: str) -> Optional[dict]:
    """按 user_id 取用户；不存在返回 None。"""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT user_id::text AS user_id, username, display_name,
                   password_hash, created_at
            FROM users WHERE user_id::text = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_user_by_username(username: str) -> Optional[dict]:
    """按用户名取用户（登录校验用）；不存在返回 None。"""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT user_id::text AS user_id, username, display_name,
                   password_hash, created_at
            FROM users WHERE username = %s
            """,
            (username,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def count_users() -> int:
    """当前用户总数（首个注册者判定用）。"""
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM users")
        return int(cur.fetchone()["n"])


def claim_orphan_tasks(user_id: str) -> int:
    """把历史遗留（user_id IS NULL）的 task 归到首个注册用户名下，返回条数。"""
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE task SET user_id = %s
            WHERE user_id IS NULL
            """,
            (user_id,),
        )
        return cur.rowcount

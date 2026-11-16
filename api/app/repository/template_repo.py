# -*- coding: utf-8 -*-
"""检测参数模板数据访问（批次 D，NF9 参数快照模板化）。

规则与既有仓储一致：只写参数化 DML，不写 DDL（表结构归 migrations/007）；
连接经 ``app.repository.db.db_cursor``（commit/rollback 由上下文管理）；
JSONB 参数统一用 ``psycopg2.extras.Json`` 适配；user_id 全程参与过滤（按人隔离）。
"""
from __future__ import annotations

from typing import List, Optional

import psycopg2

from app.repository.db import Json, db_cursor

__all__ = ["insert_template", "list_templates", "get_template", "delete_template"]


def insert_template(template_id: str, user_id: str, name: str, params: dict) -> Optional[dict]:
    """新建模板，返回全字段行；同用户重名由唯一约束冲突 → 返回 None（上层 → TMPL-01）。"""
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO detect_template (id, user_id, name, params)
                VALUES (%s::uuid, %s::uuid, %s, %s)
                RETURNING id::text AS id, user_id::text AS user_id,
                          name, params, created_at
                """,
                (template_id, user_id, name, Json(params)),
            )
            return dict(cur.fetchone())
    except psycopg2.errors.UniqueViolation:
        # uq_detect_template_user_name 命中：db_cursor 上下文对异常做 rollback，
        # 不留脏数据；上层据此报 TMPL-01(409)。
        return None


def list_templates(user_id: str) -> List[dict]:
    """按 ``created_at`` 倒序返回该用户的全部模板（按人隔离）。"""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id::text AS id, user_id::text AS user_id, name, params, created_at
            FROM detect_template
            WHERE user_id::text = %s
            ORDER BY created_at DESC, id DESC
            """,
            (user_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_template(template_id: str, user_id: str) -> Optional[dict]:
    """按 id 取本人模板；不存在或属他人（user_id 过滤）返回 None（上层 → TMPL-02 404）。"""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id::text AS id, user_id::text AS user_id, name, params, created_at
            FROM detect_template
            WHERE id::text = %s AND user_id::text = %s
            """,
            (template_id, user_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def delete_template(template_id: str, user_id: str) -> bool:
    """删除本人模板；命中 0 行（不存在或属他人）返回 False（上层 → TMPL-02 404）。"""
    with db_cursor() as cur:
        cur.execute(
            "DELETE FROM detect_template WHERE id::text = %s AND user_id::text = %s",
            (template_id, user_id),
        )
        return cur.rowcount > 0

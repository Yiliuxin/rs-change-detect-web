# -*- coding: utf-8 -*-
"""极简迁移执行器：按序号顺序应用 / 回滚 api/migrations/*.sql。

用法：
    python migrations/migrate.py up      # 应用全部 up（001→005）
    python migrations/migrate.py down    # 逆序回滚（005→001）
    python migrations/migrate.py status  # 打印待执行脚本清单

依赖：psycopg2 + PostgreSQL/PostGIS；连接串取自 api/.env（见 app.config.env.postgres_dsn）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import psycopg2

# 允许以脚本方式直接运行：把 api/ 加入搜索路径，复用 app.config.env 的 .env 加载与 DSN 组装
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.env import postgres_dsn  # noqa: E402

HERE = Path(__file__).resolve().parent
UP_RE = re.compile(r"^\d{3}_.*\.up\.sql$")
DOWN_RE = re.compile(r"^\d{3}_.*\.down\.sql$")


def _scripts(pattern) -> list[Path]:
    return sorted(HERE.glob(pattern), key=lambda p: p.name)


def run(paths: list[Path]) -> None:
    conn = psycopg2.connect(postgres_dsn())
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            for p in paths:
                sql = p.read_text(encoding="utf-8")
                cur.execute(sql)
                print(f"[ok] {p.name}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "up"
    if cmd == "up":
        run(_scripts("*.up.sql"))
    elif cmd == "down":
        run(sorted(HERE.glob("*.down.sql"), key=lambda p: p.name, reverse=True))
    elif cmd == "status":
        for p in _scripts("*.up.sql"):
            print("pending/up  :", p.name)
        for p in sorted(HERE.glob("*.down.sql"), key=lambda p: p.name, reverse=True):
            print("pending/down:", p.name)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

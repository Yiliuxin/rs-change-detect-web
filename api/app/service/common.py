# -*- coding: utf-8 -*-
"""服务层共用小工具（可复现三要素 / 时间，REQ-14）。"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# api/app/service/common.py → parents[3] = 工程根 C:\Test\Web
REPO_ROOT = Path(__file__).resolve().parents[3]
# 产物落盘根：api/runs/<run_id>/（与算法 _smoke 一致，且被 .gitignore 忽略）
RUNS_ROOT = Path(__file__).resolve().parents[2] / "runs"
# 本地影像回退根：C:\Test\Web\data\pair_*
DATA_ROOT = REPO_ROOT / "data"

__all__ = ["REPO_ROOT", "RUNS_ROOT", "DATA_ROOT", "git_short_commit", "utc_now", "run_dir"]


def git_short_commit() -> Optional[str]:
    """只读取 ``git rev-parse --short HEAD``（REQ-12 可复现 commit）。失败返回 None。"""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT), stderr=subprocess.DEVNULL, timeout=5,
        )
        return out.decode("utf-8", "ignore").strip() or None
    except Exception:
        return None


def utc_now() -> datetime:
    """带时区当前时间（task.started_at / finished_at）。"""
    return datetime.now(timezone.utc)


def run_dir(run_id: str) -> Path:
    """返回并创建 ``api/runs/<run_id>/`` 产物目录。"""
    d = RUNS_ROOT / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d

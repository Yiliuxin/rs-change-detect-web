# -*- coding: utf-8 -*-
"""测试数据清理脚本（交付卫生 / REQ-13 异步化配套）。

用途：把自动化测试往真实库 / 产物目录写入的数据复位，仅保留每个影像对
（``scene.cog_path`` 的 ``pair_*``）最新一条 SUCCESS run（即 11 条真实 run）。

执行顺序（FK 依赖）：change_result → sample → task → aoi。
非保留的 ``api/runs/<run_id>`` 目录移到 ``--trash-dir``（默认
``C:\\Test\\_scratch\\run_trash``），不直接删除，便于回滚。

用法：
    python api/scripts/cleanup_test_data.py            # 真实执行
    python api/scripts/cleanup_test_data.py --dry-run  # 只打印，不动数据
    python api/scripts/cleanup_test_data.py --trash-dir D:/trash
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# 让脚本可直接以 ``python api/scripts/cleanup_test_data.py`` 运行（无需先 cd）
API_ROOT = Path(__file__).resolve().parents[1]  # C:\Test\Web\api
for _p in (str(API_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.repository.db import db_cursor  # noqa: E402
from app.service.common import RUNS_ROOT  # noqa: E402
from app.service.cog_source import parse_cog_path  # noqa: E402

DEFAULT_TRASH = Path(r"C:\Test\_scratch\run_trash")


def _counts() -> dict:
    """返回各表当前行数（task / change_result / aoi / sample）。"""
    with db_cursor() as cur:
        out = {}
        for tbl in ("task", "change_result", "aoi", "sample"):
            cur.execute(f"SELECT COUNT(*) AS n FROM {tbl}")
            out[tbl] = int(cur.fetchone()["n"])
        return out


def _load_tasks() -> list:
    """取全部 task 及其配对名（scene.cog_path 的 pair_* 段）。"""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT t.run_id::text AS run_id,
                   t.aoi_id::text AS aoi_id,
                   t.status,
                   t.finished_at,
                   t.started_at,
                   s.cog_path AS cog_path
            FROM task t
            LEFT JOIN scene s ON s.scene_id = t.scene_t1_id
            ORDER BY t.run_id
            """
        )
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        pair = ""
        if r.get("cog_path"):
            try:
                pair = parse_cog_path(r["cog_path"])[2]
            except Exception:
                pair = ""
        r["pair"] = pair
    return rows


def _choose_kept(tasks: list) -> set:
    """保留规则：每个影像对保留最新一条 SUCCESS run（按 finished_at/started_at 倒序）。"""
    best: dict = {}
    for t in tasks:
        if t["status"] != "SUCCESS":
            continue
        key = t["pair"]
        cur_best = best.get(key)
        if cur_best is None:
            best[key] = t
        else:
            fa, fb = cur_best.get("finished_at"), t.get("finished_at")
            sa, sb = cur_best.get("started_at"), t.get("started_at")
            if (fb, sb) > (fa, sa):  # 较晚的优先
                best[key] = t
    return {t["run_id"] for t in best.values()}


def _delete_non_kept(tasks: list, kept: set) -> dict:
    """按 FK 顺序删除非保留数据；返回各表删除行数。"""
    non_kept = [t for t in tasks if t["run_id"] not in kept]
    non_kept_ids = [t["run_id"] for t in non_kept]
    deleted = {"change_result": 0, "sample": 0, "task": 0, "aoi": 0}
    if not non_kept_ids:
        return deleted
    placeholders = ",".join(["%s"] * len(non_kept_ids))
    with db_cursor() as cur:
        cur.execute(
            f"DELETE FROM change_result WHERE run_id IN ({placeholders})",
            non_kept_ids,
        )
        deleted["change_result"] = cur.rowcount
        cur.execute(
            f"DELETE FROM sample WHERE run_id IN ({placeholders})",
            non_kept_ids,
        )
        deleted["sample"] = cur.rowcount
        cur.execute(
            f"DELETE FROM task WHERE run_id IN ({placeholders})",
            non_kept_ids,
        )
        deleted["task"] = cur.rowcount
        # 删除真正孤儿的 aoi：既不被任何 task 引用、也不拥有任何 scene
        # （注意：seed aoi 拥有 scene，且 scene 被 task 以 RESTRICT 引用，不能删；
        #  task 使用的随机 aoi 仍被保留 task 引用，也不能删）。
        cur.execute(
            "DELETE FROM aoi WHERE aoi_id NOT IN (SELECT aoi_id FROM task)"
            " AND aoi_id NOT IN (SELECT aoi_id FROM scene)"
        )
        deleted["aoi"] = cur.rowcount
    return deleted


def _move_runs(kept: set, trash_dir: Path) -> list:
    """把 api/runs 下非保留目录移到 trash_dir；返回被移走的目录名列表。"""
    moved: list = []
    if not RUNS_ROOT.exists():
        return moved
    trash_dir.mkdir(parents=True, exist_ok=True)
    for entry in sorted(RUNS_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in kept:
            continue
        dest = trash_dir / entry.name
        try:
            shutil.move(str(entry), str(dest))
            moved.append(entry.name)
        except Exception as exc:  # noqa: BLE001 - 移动失败不应中断其余清理
            print(f"  [warn] 移动 {entry.name} 失败：{exc}", file=sys.stderr)
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description="复位测试数据，保留每影像对最新 SUCCESS run")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不改动数据")
    parser.add_argument("--trash-dir", default=str(DEFAULT_TRASH),
                        help=f"非保留 runs 目录移至此（默认 {DEFAULT_TRASH}）")
    args = parser.parse_args()
    trash_dir = Path(args.trash_dir)

    before = _counts()
    tasks = _load_tasks()
    kept = _choose_kept(tasks)

    print("=" * 64)
    print(f"运行前计数：{before}")
    print(f"影像对数量：{len({t['pair'] for t in tasks})}")
    print(f"保留 run 数：{len(kept)}（每个影像对最新一条 SUCCESS）")
    print("-" * 64)
    print("保留清单：")
    for t in sorted(tasks, key=lambda x: (x["pair"], str(x["finished_at"]))):
        mark = "KEEP" if t["run_id"] in kept else "del "
        print(f"  [{mark}] {t['run_id']}  pair={t['pair'] or '<none>'}  "
              f"status={t['status']}  finished={t['finished_at']}")
    print("-" * 64)

    if args.dry_run:
        print("[dry-run] 未执行任何删除 / 移动。")
        return 0

    deleted = _delete_non_kept(tasks, kept)
    moved = _move_runs(kept, trash_dir)
    after = _counts()

    print(f"删除行数：{deleted}")
    print(f"移走 runs 目录数：{len(moved)} -> {trash_dir}")
    print(f"运行后计数：{after}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

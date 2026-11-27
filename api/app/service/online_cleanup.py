# -*- coding: utf-8 -*-
"""在线取数数据清理（用户主动删除自己取数的影像对，不碰预置数据）。

背景：在线取数（POST /scene/fetch）把影像下载到 MinIO 的 ``pair_<name>/`` 前缀并
入库 scene/aoi，检测还会产生 task / change_result / sample。用户需要能在界面上清掉
这些历史取数数据。本模块封装「列组 → 按组删 → 一键清空」的完整链路。

安全口径（三重护栏，缺一不可）：
  1. **预置白名单**：``PRESET_GROUP_NAMES`` 内的组一律拒绝删除（EX2）。seed 与在线
     取数都用 uuid5，无法靠 id 版本区分，故以组名为唯一判据源。
  2. **只认在线组**：清空只删不在白名单内的 ``pair_*`` 组；landsat_* 等非 pair 组
     即便不在白名单也不碰。
  3. **级联顺序**：先删 task（下游 change_result/sample 由 FK CASCADE 自动清），再删
     scene（RESTRICT 要求），再删 aoi（仅删无引用者），最后删 MinIO 对象与本地
     ``runs/<run_id>/`` 产物目录。DB 删除与 MinIO 删除分步执行：DB 成功后再删对象，
     对象删除失败不回滚 DB（残留对象无害，下次可再清），但如实计数返回。

不打真网例外：MinIO 对象删除走 ``get_client``，与取数同一客户端口径；删除失败
记入 ``minio_errors`` 而不是抛 500（数据已删，抛错无益且会误导用户重试）。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from minio.deleteobjects import DeleteObject

from app.api.errors import ApiError
from app.config.constants import COG_BUCKET, PRESET_GROUP_NAMES
from app.config.storage import StorageSettings, get_client, list_objects
from app.repository import online_repo
from app.service.common import RUNS_ROOT

__all__ = [
    "list_online_groups", "delete_group", "delete_all",
]


def _is_preset(group: str) -> bool:
    """是否预置组（受保护，禁止删除）。"""
    return str(group) in PRESET_GROUP_NAMES


def _assert_deletable(group: str) -> None:
    """删除前置校验：预置组或非 pair 组 → EX2(400) 拒绝。"""
    g = str(group or "")
    if _is_preset(g):
        raise ApiError(
            "EX2", http_status=400,
            detail=f"预置影像对组 {g!r} 受保护，不允许删除",
            message=f"预置影像对组 {g!r} 受保护，不允许删除",
        )
    if not g.startswith("pair_"):
        raise ApiError(
            "EX2", http_status=400,
            detail=f"组名 {g!r} 不是在线取数组（应为 pair_<name>）",
            message=f"组名 {g!r} 不是在线取数组（应为 pair_<name>）",
        )


def _index_source(group: str) -> Optional[str]:
    """探测组的取数口径（cloud=云端指数成品 / local=原始波段）。"""
    from app.config.constants import COG_BUCKET
    from app.service.cog_source import has_cloud_indices

    try:
        if has_cloud_indices(f"minio://{COG_BUCKET}/{group}/t1/"):
            return "cloud"
    except Exception:  # noqa: BLE001 - 探测失败按 local 处理，不影响删除功能
        pass
    return "local"


def list_online_groups() -> Dict[str, object]:
    """列出可删除的在线取数组（已剔除预置白名单与非 pair 组）。

    返回 ``{"groups": [{group, scene_count, t1_date, t2_date, index_source}],
    "total": n}``；``index_source`` 供前端区分云端指数与原始波段取数。
    """
    groups = [
        g for g in online_repo.list_fetch_groups()
        if g["group"].startswith("pair_") and not _is_preset(g["group"])
    ]
    for g in groups:
        g["index_source"] = _index_source(str(g["group"]))
    return {"groups": groups, "total": len(groups)}


def _delete_group_db(group: str) -> Dict[str, object]:
    """删除单组的 DB 记录（级联顺序），返回统计。"""
    scene_ids = online_repo.group_scene_ids(group)
    aoi_ids = online_repo.group_aoi_ids(group)
    tasks = online_repo.delete_tasks_for_scenes(scene_ids)
    scenes = online_repo.delete_scenes(scene_ids)
    aois = online_repo.delete_aois(aoi_ids)
    return {
        "group": group,
        "deleted_tasks": tasks,
        "deleted_scenes": scenes,
        "deleted_aois": aois,
        "scene_ids": scene_ids,
    }


def _delete_minio(prefix: str) -> List[str]:
    """删 MinIO 对象，返回删除失败的对象名列表（空=全成功）。"""
    try:
        client = get_client(StorageSettings.from_env())
        targets = list_objects(client, COG_BUCKET, prefix=prefix, recursive=True)
        if not targets:
            return []
        errors = list(client.remove_objects(
            COG_BUCKET, [DeleteObject(t) for t in targets]))
        return [str(getattr(e, "object_name", e)) for e in errors]
    except Exception:  # noqa: BLE001 - 对象已失联不阻断 DB 清理，如实计数
        return [prefix]


def _delete_result_dirs(run_ids: List[str]) -> int:
    """删除这些 run 的本地产物目录（api/runs/<run_id>/），返回成功删除数。"""
    import shutil

    n = 0
    for rid in run_ids:
        d = RUNS_ROOT / rid
        if d.is_dir():
            try:
                shutil.rmtree(d)
                n += 1
            except OSError:
                pass
    return n


def _finish_delete(stats: List[Dict[str, object]], run_ids: List[str]) -> Dict[str, object]:
    """收尾：删 MinIO 对象（按每组的 pair_<name>/ 前缀）+ 本地产物目录。"""
    minio_errors: List[str] = []
    for s in stats:
        g = str(s["group"])
        # 组名即 pair_<name>，对象前缀就是 group + "/"
        minio_errors.extend(_delete_minio(g + "/"))
    deleted_dirs = _delete_result_dirs(run_ids)
    return {
        "deleted_groups": [s["group"] for s in stats],
        "deleted_tasks": sum(int(s["deleted_tasks"]) for s in stats),
        "deleted_scenes": sum(int(s["deleted_scenes"]) for s in stats),
        "deleted_aois": sum(int(s["deleted_aois"]) for s in stats),
        "deleted_result_dirs": deleted_dirs,
        "minio_errors": minio_errors,
    }


def delete_group(group: str) -> Dict[str, object]:
    """删除单个在线取数组（含其检测任务与结果）。组不存在 → no-op 成功。

    ``run_ids`` 取自被删 scene 关联的 task，用于清理本地产物目录；必须在删
    task 之前采集，否则 FK CASCADE 后无法反查。
    """
    _assert_deletable(group)
    from app.repository.db import db_cursor

    # 先采集 run_id（删 task 前）
    with db_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT t.run_id::text AS rid FROM task t "
            "JOIN scene s ON t.scene_t1_id=s.scene_id OR t.scene_t2_id=s.scene_id "
            "WHERE split_part(s.cog_path, '/', 4) = %s",
            (group,),
        )
        run_ids = [str(r["rid"]) for r in cur.fetchall()]

    stats = [_delete_group_db(group)]
    result = _finish_delete(stats, run_ids)
    result["deleted_runs"] = run_ids
    return result


def delete_all() -> Dict[str, object]:
    """一键清空全部在线取数组（保预置）。无在线组 → 空统计成功。"""
    groups = [g["group"] for g in list_online_groups()["groups"]]
    if not groups:
        return {
            "deleted_groups": [], "deleted_tasks": 0, "deleted_scenes": 0,
            "deleted_aois": 0, "deleted_result_dirs": 0,
            "minio_errors": [], "deleted_runs": [],
        }

    from app.repository.db import db_cursor

    all_run_ids: List[str] = []
    with db_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT t.run_id::text AS rid FROM task t "
            "JOIN scene s ON t.scene_t1_id=s.scene_id OR t.scene_t2_id=s.scene_id "
            "WHERE split_part(s.cog_path, '/', 4) = ANY(%s)",
            (groups,),
        )
        all_run_ids = [str(r["rid"]) for r in cur.fetchall()]

    stats = [_delete_group_db(g) for g in groups]
    result = _finish_delete(stats, all_run_ids)
    result["deleted_runs"] = all_run_ids
    return result

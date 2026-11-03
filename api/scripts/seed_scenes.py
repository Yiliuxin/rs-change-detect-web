# -*- coding: utf-8 -*-
"""真实影像 seed：3 组本地影像对 → MinIO(scenes) + PostgreSQL/PostGIS(scene 表)。

用法：
    python scripts/seed_scenes.py                 # 上传 + 入库 + 打印 seed 报告
    python scripts/seed_scenes.py --dry-run       # 只扫描并打印计划，不上传不入库

兼容性说明（任务要点）：
  南昌组 manifest 的时相 key 为旧格式 ``t1_20231102`` / ``t2_20231227``，
  鄱阳湖 / 农田组为新格式 ``t1`` / ``t2``。本脚本以 ``^t([12])`` 正则归一为 t1 / t2，
  两种格式同时兼容；日期优先取 scene["date"]，缺失时依次回退 datetime、key 内嵌日期、source_id。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 允许以脚本方式导入 app 包

from app.config.constants import (  # noqa: E402
    COG_BUCKET,
    RADIOMETRIC_FORMULAS,
    SENSOR_S2,
)
from app.config.storage import (  # noqa: E402
    StorageSettings,
    ensure_buckets,
    get_client,
    list_objects,
    object_url,
    upload_file,
)

DATA_ROOT = Path(os.getenv("DATA_ROOT", r"C:\Test\Web\data"))
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # UUIDv5 命名空间（可复现）

TAG_RE = re.compile(r"^t([12])")
DATE8_RE = re.compile(r"(\d{4})(\d{2})(\d{2})")


# --------------------------------------------------------------------------- #
# Manifest 解析
# --------------------------------------------------------------------------- #
@dataclass
class ScenePlan:
    """单个时相的 seed 计划。"""

    tag: str
    date: str
    sensor: str
    cloud_cover: Optional[float]
    proc_baseline: Optional[str]
    source_id: str
    files: List[Path] = field(default_factory=list)
    object_names: List[str] = field(default_factory=list)


@dataclass
class GroupPlan:
    """单组影像对的 seed 计划。"""

    name: str
    tile: str
    bbox: Tuple[float, float, float, float]
    aoi_id: uuid.UUID
    scenes: List[ScenePlan] = field(default_factory=list)


def normalize_tag(key: str) -> str:
    """兼容 t1 / t1_20231102 两种时相 key，归一为 t1 / t2。"""
    m = TAG_RE.match(key)
    return f"t{m.group(1)}" if m else key


def resolve_date(scn: Dict, key: str) -> str:
    """按 date → datetime → key 内嵌日期 → source_id 内嵌日期 的优先序解析日期。"""
    if scn.get("date"):
        return str(scn["date"])
    if scn.get("datetime"):
        return str(scn["datetime"])[:10]
    for text in (key, str(scn.get("source_id", ""))):
        m = DATE8_RE.search(text)
        if m:
            return "-".join(m.groups())
    raise ValueError(f"无法解析时相日期：key={key!r}, source_id={scn.get('source_id')!r}")


def parse_group(pair_dir: Path) -> GroupPlan:
    """解析一组 pair_* 目录的 manifest.json，产出 seed 计划。"""
    manifest = json.loads((pair_dir / "manifest.json").read_text(encoding="utf-8"))

    # 目录名格式：pair_<name>_<tile>_<d1>_<d2>；manifest 有 name/tile 时以 manifest 为准
    parts = pair_dir.name.split("_")
    name = str(manifest.get("name") or (parts[1] if len(parts) > 1 else pair_dir.name))
    tile = str(manifest.get("tile") or (parts[2] if len(parts) > 2 else ""))

    bbox = tuple(float(v) for v in manifest["aoi_wgs84"])  # (minx, miny, maxx, maxy)
    if len(bbox) != 4:
        raise ValueError(f"{pair_dir.name}: aoi_wgs84 需为 [minx, miny, maxx, maxy]")

    plan = GroupPlan(
        name=name,
        tile=tile,
        bbox=bbox,  # type: ignore[arg-type]
        aoi_id=uuid.uuid5(NS, f"aoi:{name}"),
    )

    for key, scn in manifest["scenes"].items():
        tag = normalize_tag(key)
        files = [pair_dir / f for f in scn.get("local_files", [])]
        missing = [str(f) for f in files if not f.exists()]
        if missing:
            raise FileNotFoundError(f"{pair_dir.name}/{key} 缺少文件：{missing}")
        plan.scenes.append(
            ScenePlan(
                tag=tag,
                date=resolve_date(scn, key),
                sensor=SENSOR_S2,  # 预置数据均为 Sentinel-2 L2A
                cloud_cover=scn.get("cloud_cover"),
                proc_baseline=scn.get("processing_baseline"),
                source_id=str(scn.get("source_id", "")),
                files=files,
                object_names=[f"pair_{name}/{tag}/{f.name}" for f in files],
            )
        )

    plan.scenes.sort(key=lambda s: s.tag)
    return plan


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #
def dsn() -> str:
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    db = os.getenv("PGDATABASE", "rs_change")
    user = os.getenv("PGUSER", "postgres")
    password = os.getenv("PGPASSWORD", "postgres")
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def upsert_aoi(cur, plan: GroupPlan) -> None:
    minx, miny, maxx, maxy = plan.bbox
    cur.execute(
        """
        INSERT INTO aoi (aoi_id, aoi_geom, area_deg2)
        VALUES (%s, ST_MakeEnvelope(%s, %s, %s, %s, 4326), %s)
        ON CONFLICT (aoi_id) DO UPDATE SET area_deg2 = EXCLUDED.area_deg2
        """,
        (str(plan.aoi_id), minx, miny, maxx, maxy, (maxx - minx) * (maxy - miny)),
    )


def upsert_scene(cur, plan: GroupPlan, scn: ScenePlan) -> str:
    scene_id = str(uuid.uuid5(NS, f"scene:{plan.name}:{scn.tag}:{scn.source_id}"))
    minx, miny, maxx, maxy = plan.bbox
    formula = RADIOMETRIC_FORMULAS[scn.sensor]["text"]
    cur.execute(
        """
        INSERT INTO scene (scene_id, aoi_id, sensor, acq_date, cloud_cover, cog_path,
                           proc_baseline, radiometric_formula, tile, footprint)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
        ON CONFLICT (scene_id) DO UPDATE SET
            cloud_cover = EXCLUDED.cloud_cover,
            cog_path = EXCLUDED.cog_path,
            proc_baseline = EXCLUDED.proc_baseline
        RETURNING scene_id
        """,
        (
            scene_id,
            str(plan.aoi_id),
            scn.sensor,
            scn.date,
            scn.cloud_cover,
            object_url(COG_BUCKET, f"pair_{plan.name}/{scn.tag}/"),
            scn.proc_baseline,
            formula,
            plan.tile,
            minx, miny, maxx, maxy,
        ),
    )
    return cur.fetchone()[0]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="seed 真实影像到 MinIO 与 scene 表")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不上传不入库")
    args = ap.parse_args()

    pair_dirs = sorted(p for p in DATA_ROOT.glob("pair_*") if (p / "manifest.json").exists())
    if not pair_dirs:
        print(f"[error] 未找到任何 pair_* 目录：{DATA_ROOT}")
        return 1

    plans = [parse_group(d) for d in pair_dirs]

    # 1) MinIO 上传
    uploaded: List[str] = []
    if not args.dry_run:
        client = get_client(StorageSettings.from_env())
        ensure_buckets(client, [COG_BUCKET])
        for plan in plans:
            for scn in plan.scenes:
                for f, obj in zip(scn.files, scn.object_names):
                    uploaded.append(upload_file(client, COG_BUCKET, obj, str(f)))
                    print(f"[up]   {obj}")
        print(f"[minio] scenes 桶对象总数：{len(list_objects(client, COG_BUCKET))}")

    # 2) 数据库写入
    written: List[Tuple[str, str, str]] = []  # (group, tag, scene_id)
    if not args.dry_run:
        conn = psycopg2.connect(dsn())
        try:
            with conn.cursor() as cur:
                for plan in plans:
                    upsert_aoi(cur, plan)
                    for scn in plan.scenes:
                        written.append((plan.name, scn.tag, upsert_scene(cur, plan, scn)))
            conn.commit()
        finally:
            conn.close()

    # 3) seed 报告
    lines: List[str] = ["# seed 报告：3 组真实影像对", ""]
    for plan in plans:
        lines.append(f"## pair_{plan.name}（tile={plan.tile}, aoi_id={plan.aoi_id}）")
        lines.append("")
        lines.append("| 时相 | 日期 | 文件数 | 文件名 | MinIO 对象路径 |")
        lines.append("|---|---|---|---|---|")
        for scn in plan.scenes:
            names = "、".join(f.name for f in scn.files)
            objs = "、".join(object_url(COG_BUCKET, o) for o in scn.object_names)
            lines.append(f"| {scn.tag} | {scn.date} | {len(scn.files)} | {names} | {objs} |")
        n = sum(1 for g, _, _ in written if g == plan.name)
        lines.append("")
        lines.append(f"- scene 表写入：**{n}** 条（t1/t2 各 1 条）")
        lines.append(f"- 上传对象：**{sum(len(s.files) for s in plan.scenes)}** 个")
        lines.append("")
    lines.append(f"合计：scene 记录 {len(written)} 条，MinIO 对象 {len(uploaded)} 个")

    report = "\n".join(lines)
    out = Path(__file__).resolve().parent / "seed_report.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[report] 已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

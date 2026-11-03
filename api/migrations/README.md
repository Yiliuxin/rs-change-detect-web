# 数据库迁移说明（HLD 3.2）

- 目标库：PostgreSQL + PostGIS（本地开发容器 `rs-pg`，库名 `rs_change`）
- 执行器：`python migrations/migrate.py up | down | status`
- 连接参数由环境变量组装：`PGHOST / PGPORT / PGDATABASE / PGUSER / PGPASSWORD`

## 脚本清单（按依赖顺序）

| 序号 | 表 | up 脚本 | down 脚本 | 对应 REQ |
|---|---|---|---|---|
| 001 | aoi | `001_create_aoi.up.sql` | `001_create_aoi.down.sql` | REQ-01 |
| 002 | scene | `002_create_scene.up.sql` | `002_create_scene.down.sql` | REQ-02/04 |
| 003 | task | `003_create_task.up.sql` | `003_create_task.down.sql` | REQ-13/14 |
| 004 | sample | `004_create_sample.up.sql` | `004_create_sample.down.sql` | REQ-09 |
| 005 | change_result | `005_create_change_result.up.sql` | `005_create_change_result.down.sql` | REQ-08/10/12 |

说明：`sample` 与 `change_result` 均以 `run_id` 外键引用 `task`，故 `task`（003）先于二者创建；
回滚时按 005→001 逆序执行，`down.sql` 均可完整回滚。

## 约束与索引

- CHECK：`scene.sensor IN (S2,L8,L9)`、`sample.ref_class IN (增强,退化,稳定)`、
  `change_result.change_class IN (1,2,3)`、`task.status`、`task.scene_t1_id <> scene_t2_id`
- 外键：`scene.aoi_id → aoi`、`task.aoi_id → aoi`、`task.scene_t1/t2_id → scene`、
  `sample.run_id → task`、`change_result.run_id → task`
- 索引：GIST（`aoi.aoi_geom`、`scene.footprint`、`sample.geom`）+ BTree（`aoi_id / acq_date / run_id / status`）

## 对 HLD 3.2 的补充字段（已标注）

- `scene.tile`：MGRS 景号（50RMT / 50RMS），seed 写入需要，HLD 表 3-2 未列
- `scene.footprint`：影像覆盖范围，支撑 `ST_Intersects` 空间相交检索（HLD 2.2.2.1 功能一），HLD 表 3-2 未列

## HLD 3.3–3.6 落地结论

按 HLD 原文：**不使用**存储过程（3.3）、**不使用**视图（3.4）、**不使用**触发器（3.5）、
**不设计**自定义数据库函数（3.6）。全部数据访问由服务层参数化 SQL 完成，面积换算在算法层按像元分辨率计算。

## seed

```
python scripts/seed_scenes.py            # 上传 3 组真实影像对 + 写 scene 表
python scripts/seed_scenes.py --dry-run  # 只打印计划
```

验证口径：`psql` 查到 6 条 `scene` 记录（3 组 × 2 时相）；MinIO `scenes` 桶列出 30 个对象。

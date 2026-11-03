# -*- coding: utf-8 -*-
"""数据访问层（PostgreSQL + PostGIS / MinIO）。

以参数化 SQL 完成 5 表读写，不使用存储过程/视图/触发器（HLD 3.3–3.6）。
骨架阶段仅占位，后续落 scene_repo.py / aoi_repo.py 与迁移脚本 api/migrations。
"""

# -*- coding: utf-8 -*-
"""配置包：常量、环境变量与对象存储连接的唯一入口。

导入本包（或其任意子模块，如 ``app.config.constants`` / ``app.config.storage``）
会自动加载 ``api/.env``，保证后续 ``os.getenv`` 能读到本地开发配置。
"""
from app.config.env import load_env  # noqa: F401  导入即加载 api/.env

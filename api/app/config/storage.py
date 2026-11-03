# -*- coding: utf-8 -*-
"""MinIO 对象存储客户端封装（数据层）。

约定两个桶（HLD 3.2 数据层 / REQ-02、REQ-12）：
  - ``scenes``  ：预置双时相 COG（B02/B03/B04/B08/SCL）
  - ``results`` ：结果 GeoTIFF / PNG / PDF
桶名取自常量源 ``app.config.constants``，禁止就地写死。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterator, List, Optional

from minio import Minio
from minio.datatypes import Object as MinioObject
from minio.error import S3Error

from app.config.constants import COG_BUCKET, RESULT_BUCKET


@dataclass(frozen=True)
class StorageSettings:
    """MinIO 连接配置（环境变量可覆盖，默认值对应本地开发容器）。"""

    endpoint: str = "localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    secure: bool = False
    region: str = "us-east-1"

    @classmethod
    def from_env(cls) -> "StorageSettings":
        return cls(
            endpoint=os.getenv("MINIO_ENDPOINT", "localhost:9000"),
            access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
            secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
            secure=os.getenv("MINIO_SECURE", "false").strip().lower() == "true",
            region=os.getenv("MINIO_REGION", "us-east-1"),
        )


def get_client(settings: Optional[StorageSettings] = None) -> Minio:
    """构造 MinIO 客户端（不校验连通性，校验见 :func:`ensure_buckets`）。"""
    s = settings or StorageSettings.from_env()
    return Minio(
        endpoint=s.endpoint,
        access_key=s.access_key,
        secret_key=s.secret_key,
        secure=s.secure,
        region=s.region,
    )


def ensure_buckets(client: Minio, buckets: Optional[List[str]] = None) -> List[str]:
    """确保桶存在，缺失则创建；返回实际存在的桶名清单。"""
    targets = list(buckets or (COG_BUCKET, RESULT_BUCKET))
    existing: List[str] = []
    for name in targets:
        try:
            if not client.bucket_exists(name):
                client.make_bucket(name)
            existing.append(name)
        except S3Error as exc:  # pragma: no cover - 依赖本地 MinIO 可用性
            raise RuntimeError(f"MinIO 桶 {name!r} 创建/校验失败：{exc}") from exc
    return existing


def upload_file(
    client: Minio,
    bucket: str,
    object_name: str,
    file_path: str,
    content_type: str = "image/tiff",
) -> str:
    """上传本地文件到对象存储，返回对象名。"""
    result = client.fput_object(bucket, object_name, file_path, content_type=content_type)
    return result.object_name


def list_objects(client: Minio, bucket: str, prefix: str = "", recursive: bool = True) -> List[str]:
    """列出桶内对象名（默认递归展开）。"""
    items: Iterator[MinioObject] = client.list_objects(bucket, prefix=prefix, recursive=recursive)
    return [obj.object_name for obj in items]


def object_url(bucket: str, object_name: str) -> str:
    """返回统一的对象路径表示（库内 cog_path 亦用该口径登记）。"""
    return f"minio://{bucket}/{object_name}"


__all__ = [
    "COG_BUCKET",
    "RESULT_BUCKET",
    "StorageSettings",
    "get_client",
    "ensure_buckets",
    "upload_file",
    "list_objects",
    "object_url",
]

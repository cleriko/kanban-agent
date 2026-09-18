from __future__ import annotations

from functools import lru_cache

from ...config import Settings, get_settings
from .base import ObjectStorage
from .local import LocalObjectStorage
from .s3 import S3ObjectStorage

__all__ = ["ObjectStorage", "LocalObjectStorage", "S3ObjectStorage", "get_storage"]


@lru_cache(maxsize=1)
def get_storage(settings: Settings | None = None) -> ObjectStorage:
    settings = settings or get_settings()
    if settings.storage_backend == "s3":
        return S3ObjectStorage(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            region=settings.s3_region,
        )
    return LocalObjectStorage(settings.storage_path)

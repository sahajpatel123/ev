from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import settings


class LocalObjectStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    async def delete(self, key: str) -> None:
        (self.root / key).unlink(missing_ok=True)


class S3ObjectStore:
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        import boto3  # optional dependency

        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        self.bucket = bucket

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type or "application/octet-stream")

    async def get(self, key: str) -> bytes:
        resp = self.client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()

    async def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def get_object_store():
    if settings.object_store_backend == "s3":
        return S3ObjectStore(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
        )
    return LocalObjectStore(settings.storage_root)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


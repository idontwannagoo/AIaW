from functools import lru_cache
from urllib.parse import urlparse, urlunparse

import boto3
from botocore.client import Config

from .config import (
    S3_ACCESS_KEY,
    S3_BUCKET,
    S3_ENDPOINT,
    S3_PUBLIC_ENDPOINT,
    S3_REGION,
    S3_SECRET_KEY,
)


@lru_cache(maxsize=1)
def _client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=Config(signature_version="s3v4"),
    )


def ensure_bucket() -> None:
    client = _client()
    try:
        client.head_bucket(Bucket=S3_BUCKET)
    except Exception:
        client.create_bucket(Bucket=S3_BUCKET)


def _rewrite_public(url: str) -> str:
    """Rewrite host portion from internal endpoint to public endpoint for browser use."""
    if S3_ENDPOINT == S3_PUBLIC_ENDPOINT:
        return url
    parsed = urlparse(url)
    public = urlparse(S3_PUBLIC_ENDPOINT)
    return urlunparse(parsed._replace(scheme=public.scheme, netloc=public.netloc))


def presign_put(key: str, mime_type: str, expires: int = 900) -> str:
    url = _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": mime_type},
        ExpiresIn=expires,
    )
    return _rewrite_public(url)


def presign_get(key: str, expires: int = 3600) -> str:
    url = _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires,
    )
    return _rewrite_public(url)


def head_object(key: str) -> bool:
    try:
        _client().head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except Exception:
        return False

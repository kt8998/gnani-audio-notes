"""
Object storage via the S3 API (boto3). In production this is Supabase Storage's
S3-compatible endpoint; any S3-compatible bucket works.

The browser uploads directly to the bucket with a short-lived *presigned* URL,
so large files never pass through our API server. The worker downloads with
our own credentials. The bucket stays private.
"""

from functools import lru_cache
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import get_settings

UPLOAD_URL_EXPIRES_SECONDS = 15 * 60
DOWNLOAD_URL_EXPIRES_SECONDS = 60 * 60


@lru_cache
def get_client():
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url or None,
        aws_access_key_id=s.s3_access_key_id,
        aws_secret_access_key=s.s3_secret_access_key,
        region_name=s.s3_region,
        # Path-style URLs (endpoint/bucket/key): Supabase's S3 endpoint requires them,
        # and they work with every S3-compatible service.
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def bucket() -> str:
    return get_settings().s3_bucket


def presign_upload(key: str, content_type: str) -> str:
    """URL the browser can PUT the file to. It must send the same Content-Type header."""
    return get_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket(), "Key": key, "ContentType": content_type},
        ExpiresIn=UPLOAD_URL_EXPIRES_SECONDS,
    )


def presign_download(key: str) -> str:
    """Short-lived URL for playing the original audio in the browser."""
    return get_client().generate_presigned_url(
        "get_object", Params={"Bucket": bucket(), "Key": key}, ExpiresIn=DOWNLOAD_URL_EXPIRES_SECONDS
    )


def object_size(key: str) -> int | None:
    """Size in bytes, or None if the object doesn't exist (e.g. the upload never finished)."""
    try:
        return get_client().head_object(Bucket=bucket(), Key=key)["ContentLength"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def download_to(key: str, path: Path) -> None:
    get_client().download_file(bucket(), key, str(path))

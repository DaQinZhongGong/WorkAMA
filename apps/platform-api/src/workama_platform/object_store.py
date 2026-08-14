from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

import httpx

from workama_platform.core import settings


def _signing_key(secret: str, date: str, region: str = "us-east-1") -> bytes:
    key = hmac.new(("AWS4" + secret).encode(), date.encode(), hashlib.sha256).digest()
    key = hmac.new(key, region.encode(), hashlib.sha256).digest()
    key = hmac.new(key, b"s3", hashlib.sha256).digest()
    return hmac.new(key, b"aws4_request", hashlib.sha256).digest()


async def request(method: str, bucket: str, key: str | None = None, data: bytes = b"") -> httpx.Response:
    endpoint = settings.minio_endpoint if "://" in settings.minio_endpoint else f"http://{settings.minio_endpoint}"
    parsed = urlparse(endpoint)
    path = f"/{quote(bucket)}" + (f"/{quote(key, safe='/')}" if key else "")
    now = datetime.now(UTC); amz_date = now.strftime("%Y%m%dT%H%M%SZ"); date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(data).hexdigest()
    canonical_headers = f"host:{parsed.netloc}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical = f"{method}\n{path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    scope = f"{date}/us-east-1/s3/aws4_request"
    string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    signature = hmac.new(_signing_key(settings.minio_secret_key, date), string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {"x-amz-date": amz_date, "x-amz-content-sha256": payload_hash, "Authorization": f"AWS4-HMAC-SHA256 Credential={settings.minio_access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"}
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.request(method, f"{parsed.scheme}://{parsed.netloc}{path}", headers=headers, content=data)


async def put_object(bucket: str, key: str, data: bytes) -> None:
    created = await request("PUT", bucket)
    if created.status_code not in {200, 409}:
        raise RuntimeError(f"object bucket unavailable: {created.status_code}")
    response = await request("PUT", bucket, key, data)
    response.raise_for_status()


async def get_object(bucket: str, key: str) -> bytes:
    response = await request("GET", bucket, key)
    response.raise_for_status()
    return response.content


async def delete_object(bucket: str, key: str) -> None:
    response = await request("DELETE", bucket, key)
    if response.status_code not in {200, 204, 404}:
        response.raise_for_status()

from __future__ import annotations

import hashlib
import hmac
import tempfile
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from .base import ObjectStorage


class S3ObjectStorage(ObjectStorage):
    """S3-compatible storage (MinIO, Garage, Backblaze, AWS).

    Signs requests with SigV4 directly over httpx rather than pulling in boto3 —
    four operations do not justify that dependency, and this keeps the image small.
    """

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str,
                 region: str = "us-east-1") -> None:
        self._endpoint = endpoint.rstrip("/")
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region

    def _url(self, key: str) -> str:
        return f"{self._endpoint}/{self._bucket}/{quote(key)}"

    # --- SigV4 ------------------------------------------------------------

    def _sign(self, method: str, key: str, payload_hash: str,
              content_type: str | None = None) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        host = self._endpoint.split("://", 1)[-1]
        canonical_uri = f"/{self._bucket}/{quote(key)}"

        headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        if content_type:
            headers["content-type"] = content_type

        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        canonical_request = "\n".join(
            [method, canonical_uri, "", canonical_headers, signed_headers, payload_hash])

        scope = f"{date_stamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical_request.encode()).hexdigest()])

        def sign(key_bytes: bytes, message: str) -> bytes:
            return hmac.new(key_bytes, message.encode(), hashlib.sha256).digest()

        signing_key = sign(sign(sign(sign(f"AWS4{self._secret_key}".encode(), date_stamp),
                                     self._region), "s3"), "aws4_request")
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()

        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")
        return headers

    # --- Operations -------------------------------------------------------

    async def put(self, key: str, data: AsyncIterator[bytes], content_type: str) -> int:
        # Buffer to a temporary file: SigV4 needs the payload hash up front, and
        # streaming chunked signatures are not worth the complexity here.
        written = 0
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            digest = hashlib.sha256()
            async for chunk in data:
                tmp.write(chunk)
                digest.update(chunk)
                written += len(chunk)
            payload_hash = digest.hexdigest()
            path = Path(tmp.name)

        try:
            headers = self._sign("PUT", key, payload_hash, content_type)
            async with httpx.AsyncClient(timeout=600) as client:
                with path.open("rb") as handle:
                    response = await client.put(self._url(key), content=handle.read(), headers=headers)
            response.raise_for_status()
        finally:
            path.unlink(missing_ok=True)
        return written

    async def get_path(self, key: str) -> str:
        headers = self._sign("GET", key, hashlib.sha256(b"").hexdigest())
        suffix = Path(key).suffix or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            async with httpx.AsyncClient(timeout=600) as client:
                async with client.stream("GET", self._url(key), headers=headers) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        tmp.write(chunk)
            return tmp.name

    async def delete(self, key: str) -> None:
        headers = self._sign("DELETE", key, hashlib.sha256(b"").hexdigest())
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.delete(self._url(key), headers=headers)
        if response.status_code not in (204, 404):
            response.raise_for_status()

    async def delete_prefix(self, prefix: str) -> None:
        # Only ever one known object per meeting today; listing would add an API
        # surface with no current caller.
        await self.delete(f"{prefix}original.m4a")

    async def exists(self, key: str) -> bool:
        headers = self._sign("HEAD", key, hashlib.sha256(b"").hexdigest())
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.head(self._url(key), headers=headers)
        return response.status_code == 200

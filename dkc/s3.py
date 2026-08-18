"""Literal conditional requests for an S3-compatible storage boundary.

The client intentionally implements only the small part of the S3 API used by
the repository protocol.  In particular, a write is one signed ``PutObject``
request with exactly one precondition.  There is no multipart, copy, or
unconditional-write entry point for callers to select accidentally.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .storage import ObjectMetadata

__all__ = [
    "HttpResponse",
    "ListedObject",
    "NotFound",
    "PreconditionFailed",
    "S3Client",
    "S3Credentials",
    "S3Endpoint",
    "RemoteObject",
    "RemoteStoreError",
    "SignedRequest",
    "exact_http_request",
]


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}[A-Za-z0-9]$")
_ETAG_RE = re.compile(r'^"[^"\x00-\x1f\x7f]+"$')
_UNRESERVED = "/-_.~"


class RemoteStoreError(RuntimeError):
    """A bounded, sanitized remote object-store failure."""

    def __init__(self, operation: str, status: int, detail: str = "") -> None:
        suffix = f": {detail}" if detail else ""
        super().__init__(f"{operation} failed with HTTP {status}{suffix}")
        self.operation = operation
        self.status = status


class PreconditionFailed(RemoteStoreError):
    """The remote equivalent of a lost HTTP 412 conditional write."""

    def __init__(self, operation: str) -> None:
        super().__init__(operation, 412, "conditional precondition lost")


class NotFound(RemoteStoreError):
    """An authoritative S3 read found no object at the exact key."""

    def __init__(self, operation: str) -> None:
        super().__init__(operation, 404, "object is absent")


@dataclass(frozen=True, repr=False)
class S3Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,128}", self.access_key_id):
            raise ValueError("access key ID is empty or unsafe")
        if (
            not self.secret_access_key
            or self.secret_access_key != self.secret_access_key.strip()
            or _CONTROL_RE.search(self.secret_access_key)
        ):
            raise ValueError("secret access key is empty or unsafe")
        if self.session_token is not None and not re.fullmatch(
            r"[A-Za-z0-9._~+/=-]{8,8192}", self.session_token
        ):
            raise ValueError("session token is empty or unsafe")


@dataclass(frozen=True, repr=False)
class S3Endpoint:
    base_url: str
    bucket: str
    region: str
    addressing_style: str

    @classmethod
    def validated(
        cls, base_url: str, bucket: str, region: str, addressing_style: str
    ) -> S3Endpoint:
        if (
            not _BUCKET_RE.fullmatch(bucket)
            or ".." in bucket
            or ".-" in bucket
            or "-." in bucket
        ):
            raise ValueError("S3 bucket name is unsafe")
        if not _REGION_RE.fullmatch(region):
            raise ValueError("S3 signing region is unsafe")
        if addressing_style not in ("path", "virtual"):
            raise ValueError("S3 addressing style must be path or virtual")
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("S3 endpoint must be one exact HTTPS origin")
        return cls(f"https://{parsed.netloc}", bucket, region, addressing_style)


@dataclass(frozen=True, repr=False)
class SignedRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    canonical_request: str


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class RemoteObject:
    body: bytes
    metadata: ObjectMetadata
    etag: str


@dataclass(frozen=True)
class ListedObject:
    key: str
    size: int

    def __post_init__(self) -> None:
        _safe_key(self.key)
        if self.size < 0:
            raise ValueError("listed object size must not be negative")


Transport = Callable[[SignedRequest], HttpResponse]


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def exact_http_request(
    request: urllib.request.Request,
    timeout: int,
    *,
    max_response_bytes: int | None = None,
) -> HttpResponse:
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            if max_response_bytes is None:
                body = response.read()
            else:
                body = response.read(max_response_bytes + 1)
                if len(body) > max_response_bytes:
                    raise RuntimeError("exact HTTP response exceeded its size bound")
            return HttpResponse(
                response.status, dict(response.headers.items()), body
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, dict(exc.headers.items()), exc.read(4096))


def _safe_key(value: str) -> None:
    parts = value.split("/")
    if (
        not value
        or value.startswith("/")
        or value.endswith("/")
        or "*" in value
        or "\\" in value
        or _CONTROL_RE.search(value)
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ValueError(f"unsafe object key: {value!r}")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _etag(headers: Mapping[str, str], operation: str) -> str:
    value = _header(headers, "etag")
    if value is None or not _ETAG_RE.fullmatch(value):
        raise RemoteStoreError(operation, 502, "missing or unsafe ETag")
    return value


def _quote(value: str, *, keep_slash: bool = False) -> str:
    return urllib.parse.quote(value, safe=_UNRESERVED if keep_slash else "-_.~")


def _canonical_query(values: Mapping[str, str]) -> str:
    return "&".join(
        f"{_quote(key)}={_quote(value)}" for key, value in sorted(values.items())
    )


def _normalize_header_value(value: str) -> str:
    if _CONTROL_RE.search(value):
        raise ValueError("request header contains a control character")
    return " ".join(value.strip().split())


def _sign(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


class S3Client:
    """Small SigV4 S3 client with a conditional-only write surface."""

    def __init__(
        self,
        endpoint: S3Endpoint,
        credentials: S3Credentials,
        *,
        transport: Transport | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1 or max_attempts > 8:
            raise ValueError("max_attempts must be between 1 and 8")
        self.endpoint = endpoint
        self._credentials = credentials
        self._transport = transport or self._urlopen
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleeper = sleeper
        self._max_attempts = max_attempts

    def signed_request(
        self,
        method: str,
        key: str | None,
        *,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None,
    ) -> SignedRequest:
        parsed_endpoint = urllib.parse.urlsplit(self.endpoint.base_url)
        if parsed_endpoint.hostname is None:
            raise AssertionError("validated S3 endpoint lost its hostname")
        request_base_url = self.endpoint.base_url
        request_host = parsed_endpoint.netloc
        if key is not None:
            _safe_key(key)
            if self.endpoint.addressing_style == "path":
                path = f"/{self.endpoint.bucket}/{key}"
            else:
                port = f":{parsed_endpoint.port}" if parsed_endpoint.port else ""
                request_host = f"{self.endpoint.bucket}.{parsed_endpoint.hostname}{port}"
                request_base_url = f"https://{request_host}"
                path = f"/{key}"
        else:
            if self.endpoint.addressing_style == "path":
                path = f"/{self.endpoint.bucket}"
            else:
                port = f":{parsed_endpoint.port}" if parsed_endpoint.port else ""
                request_host = f"{self.endpoint.bucket}.{parsed_endpoint.hostname}{port}"
                request_base_url = f"https://{request_host}"
                path = "/"
        canonical_uri = _quote(path, keep_slash=True)
        canonical_query = _canonical_query(query or {})
        payload_hash = hashlib.sha256(body).hexdigest()
        now = self._clock().astimezone(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        short_date = now.strftime("%Y%m%d")
        request_headers: dict[str, str] = {
            "host": request_host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if self._credentials.session_token is not None:
            request_headers["x-amz-security-token"] = self._credentials.session_token
        for name, value in (headers or {}).items():
            lowered = name.lower()
            if lowered in request_headers or not re.fullmatch(r"[a-z0-9-]+", lowered):
                raise ValueError(f"duplicate or unsafe request header: {name!r}")
            request_headers[lowered] = _normalize_header_value(value)
        canonical_headers = "".join(
            f"{name}:{_normalize_header_value(value)}\n"
            for name, value in sorted(request_headers.items())
        )
        signed_headers = ";".join(sorted(request_headers))
        canonical_request = "\n".join(
            (
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_hash,
            )
        )
        scope = f"{short_date}/{self.endpoint.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            (
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            )
        )
        date_key = _sign(
            f"AWS4{self._credentials.secret_access_key}".encode("utf-8"), short_date
        )
        region_key = _sign(date_key, self.endpoint.region)
        service_key = _sign(region_key, "s3")
        signing_key = _sign(service_key, "aws4_request")
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        request_headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._credentials.access_key_id}/{scope},"
            f"SignedHeaders={signed_headers},Signature={signature}"
        )
        url = f"{request_base_url}{canonical_uri}"
        if canonical_query:
            url = f"{url}?{canonical_query}"
        return SignedRequest(method, url, request_headers, bytes(body), canonical_request)

    @staticmethod
    def _urlopen(request: SignedRequest) -> HttpResponse:
        wire_headers = {key: value for key, value in request.headers.items()}
        req = urllib.request.Request(
            request.url,
            data=request.body if request.method in ("PUT", "POST") else None,
            headers=wire_headers,
            method=request.method,
        )
        try:
            return exact_http_request(req, 120)
        except urllib.error.URLError:
            raise RemoteStoreError(request.method, 0, "transport unavailable") from None

    def _send(
        self,
        operation: str,
        method: str,
        key: str | None,
        *,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None,
        accepted: tuple[int, ...] = (200,),
        retry_throttled: bool = False,
    ) -> HttpResponse:
        for attempt in range(1, self._max_attempts + 1):
            request = self.signed_request(
                method, key, body=body, headers=headers, query=query
            )
            response = self._transport(request)
            if response.status in accepted:
                return response
            if response.status == 412:
                raise PreconditionFailed(operation)
            if response.status == 404:
                raise NotFound(operation)
            if (
                retry_throttled
                and response.status == 429
                and attempt < self._max_attempts
            ):
                raw_delay = _header(response.headers, "retry-after")
                delay = (
                    float(raw_delay)
                    if raw_delay and raw_delay.isdigit()
                    else 2 ** (attempt - 1)
                )
                self._sleeper(min(delay, 10.0))
                continue
            # Remote bodies are untrusted and may reflect request values. Keep
            # them out of exceptions and workflow output entirely.
            raise RemoteStoreError(operation, response.status)
        raise AssertionError("bounded request loop exhausted unexpectedly")

    def put(
        self,
        key: str,
        body: bytes,
        metadata: ObjectMetadata,
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> str:
        if if_none_match == (if_match is not None):
            raise ValueError("a write requires exactly one conditional precondition")
        request_headers = {
            "cache-control": metadata.cache_control,
            "content-type": metadata.content_type,
        }
        if if_none_match:
            request_headers["if-none-match"] = "*"
        else:
            if if_match is None or not _ETAG_RE.fullmatch(if_match):
                raise ValueError("If-Match requires one opaque quoted ETag")
            request_headers["if-match"] = if_match
        response = self._send(
            f"PutObject {key}",
            "PUT",
            key,
            body=body,
            headers=request_headers,
            accepted=(200,),
            retry_throttled=True,
        )
        return _etag(response.headers, f"PutObject {key}")

    def get(self, key: str) -> RemoteObject:
        response = self._send(f"GetObject {key}", "GET", key, accepted=(200,))
        content_type = _header(response.headers, "content-type")
        cache_control = _header(response.headers, "cache-control")
        if content_type is None or cache_control is None:
            raise RemoteStoreError(
                f"GetObject {key}", 502, "required HTTP metadata is absent"
            )
        return RemoteObject(
            response.body,
            ObjectMetadata(content_type, cache_control),
            _etag(response.headers, f"GetObject {key}"),
        )

    def get_optional(self, key: str) -> RemoteObject | None:
        try:
            return self.get(key)
        except NotFound:
            return None

    def delete(self, key: str) -> None:
        self._send(f"DeleteObject {key}", "DELETE", key, accepted=(200, 204))

    def list_objects(
        self, prefix: str, *, page_size: int | None = None
    ) -> tuple[ListedObject, ...]:
        if prefix:
            _safe_key(prefix.rstrip("/"))
        if page_size is not None and not 1 <= page_size <= 1000:
            raise ValueError("S3 list page size must be between 1 and 1000")
        continuation: str | None = None
        objects: list[ListedObject] = []
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if page_size is not None:
                query["max-keys"] = str(page_size)
            if continuation is not None:
                query["continuation-token"] = continuation
            response = self._send(
                f"ListObjectsV2 {prefix}", "GET", None, query=query, accepted=(200,)
            )
            try:
                root = ET.fromstring(response.body)
            except ET.ParseError as exc:
                raise RemoteStoreError(
                    f"ListObjectsV2 {prefix}", 502, "invalid XML response"
                ) from exc
            namespace = ""
            if root.tag.startswith("{"):
                namespace = root.tag.split("}", 1)[0] + "}"
            for item in root.findall(f"{namespace}Contents"):
                value = item.findtext(f"{namespace}Key")
                if value is None or not value.startswith(prefix):
                    raise RemoteStoreError(
                        f"ListObjectsV2 {prefix}", 502, "response escaped requested prefix"
                    )
                _safe_key(value)
                raw_size = item.findtext(f"{namespace}Size")
                if raw_size is None or not raw_size.isdecimal():
                    raise RemoteStoreError(
                        f"ListObjectsV2 {prefix}", 502, "invalid object size"
                    )
                objects.append(ListedObject(value, int(raw_size)))
            truncated = root.findtext(f"{namespace}IsTruncated")
            if truncated == "false":
                break
            if truncated != "true":
                raise RemoteStoreError(
                    f"ListObjectsV2 {prefix}", 502, "invalid truncation marker"
                )
            next_token = root.findtext(f"{namespace}NextContinuationToken")
            if not next_token or next_token == continuation:
                raise RemoteStoreError(
                    f"ListObjectsV2 {prefix}", 502, "missing continuation token"
                )
            continuation = next_token
        keys = [item.key for item in objects]
        if len(keys) != len(set(keys)):
            raise RemoteStoreError(
                f"ListObjectsV2 {prefix}", 502, "duplicate object key"
            )
        return tuple(sorted(objects, key=lambda item: item.key))

    def list_keys(
        self, prefix: str, *, page_size: int | None = None
    ) -> tuple[str, ...]:
        return tuple(
            item.key for item in self.list_objects(prefix, page_size=page_size)
        )

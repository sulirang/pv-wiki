"""Stateless provenance receipts for content returned by the Exa MCP.

The receipt proves only that the trusted Exa gateway returned a particular
UTF-8 content body for a particular normalized public URL.  It deliberately
contains no timestamp, run identifier, lease, retry state, or database state.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import stat
import struct
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .render import validate_public_http_url


EVIDENCE_HMAC_KEY_FILE_ENV = "PV_WIKI_EVIDENCE_HMAC_KEY_FILE"
RECEIPT_VERSION = "pvwiki-evidence-v1"
MIN_HMAC_KEY_BYTES = 32
MAX_HMAC_KEY_FILE_BYTES = 4096
_MESSAGE_DOMAIN = b"pv-wiki/evidence-receipt\x00"
_SIGNATURE_BYTES = hashlib.sha256().digest_size


class EvidenceReceiptError(ValueError):
    """Raised for an invalid key, URL, content body, or receipt."""


def _validated_key(value: Any) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise EvidenceReceiptError("evidence HMAC key must be bytes")
    key = bytes(value)
    if len(key) < MIN_HMAC_KEY_BYTES:
        raise EvidenceReceiptError(
            f"evidence HMAC key must contain at least {MIN_HMAC_KEY_BYTES} bytes"
        )
    if len(key) > MAX_HMAC_KEY_FILE_BYTES:
        raise EvidenceReceiptError("evidence HMAC key is too large")
    return key


def load_evidence_hmac_key(
    *,
    environ: Mapping[str, str] | None = None,
) -> bytes:
    """Load the independent receipt key from an owner-only regular file."""

    active_environ = os.environ if environ is None else environ
    path_value = active_environ.get(EVIDENCE_HMAC_KEY_FILE_ENV, "").strip()
    if not path_value:
        raise EvidenceReceiptError(
            f"{EVIDENCE_HMAC_KEY_FILE_ENV} must name a protected key file"
        )
    path = Path(path_value).expanduser()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceReceiptError(
            f"{EVIDENCE_HMAC_KEY_FILE_ENV} cannot be read"
        ) from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise EvidenceReceiptError(
                f"{EVIDENCE_HMAC_KEY_FILE_ENV} must be a regular file"
            )
        if os.name == "posix" and stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise EvidenceReceiptError(
                f"{EVIDENCE_HMAC_KEY_FILE_ENV} must have mode 0600"
            )
        if file_stat.st_size > MAX_HMAC_KEY_FILE_BYTES:
            raise EvidenceReceiptError(
                f"{EVIDENCE_HMAC_KEY_FILE_ENV} is too large"
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            key = stream.read(MAX_HMAC_KEY_FILE_BYTES + 1)
    except OSError as exc:
        raise EvidenceReceiptError(
            f"{EVIDENCE_HMAC_KEY_FILE_ENV} cannot be read"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _validated_key(key)


def resolve_evidence_hmac_key(
    key: bytes | bytearray | memoryview | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bytes:
    """Resolve an injected test key or the production file-backed key."""

    if key is not None:
        return _validated_key(key)
    return load_evidence_hmac_key(environ=environ)


def normalize_evidence_url(url: Any) -> str:
    if not isinstance(url, str):
        raise EvidenceReceiptError("evidence URL must be a string")
    try:
        return validate_public_http_url(url)
    except ValueError as exc:
        raise EvidenceReceiptError("evidence URL must be public HTTP(S)") from exc


def _content_bytes(content: Any) -> bytes:
    if not isinstance(content, str):
        raise EvidenceReceiptError("evidence content must be a string")
    return content.encode("utf-8")


def _message(url: str, content_digest: bytes) -> bytes:
    url_bytes = url.encode("utf-8")
    # Fixed domain/version, an explicit URL byte length, and a fixed-size hash
    # make the signed field boundaries unambiguous.
    return b"".join(
        (
            _MESSAGE_DOMAIN,
            b"\x01",
            struct.pack(">Q", len(url_bytes)),
            url_bytes,
            content_digest,
        )
    )


def content_sha256(content: Any) -> str:
    return hashlib.sha256(_content_bytes(content)).hexdigest()


def issue_evidence_receipt(
    key: bytes | bytearray | memoryview,
    *,
    url: Any,
    content: Any,
) -> str:
    active_key = _validated_key(key)
    normalized_url = normalize_evidence_url(url)
    digest = hashlib.sha256(_content_bytes(content)).digest()
    signature = hmac.new(active_key, _message(normalized_url, digest), hashlib.sha256)
    encoded = base64.urlsafe_b64encode(signature.digest()).rstrip(b"=").decode("ascii")
    return f"{RECEIPT_VERSION}.{encoded}"


def verify_evidence_receipt(
    key: bytes | bytearray | memoryview,
    *,
    url: Any,
    content: Any,
    receipt: Any,
) -> str:
    """Verify a receipt in constant time and return the normalized URL."""

    active_key = _validated_key(key)
    normalized_url = normalize_evidence_url(url)
    if not isinstance(receipt, str):
        raise EvidenceReceiptError("evidence receipt is required")
    prefix = f"{RECEIPT_VERSION}."
    if not receipt.startswith(prefix):
        raise EvidenceReceiptError("evidence receipt is malformed")
    encoded = receipt[len(prefix) :]
    if len(encoded) != 43:
        raise EvidenceReceiptError("evidence receipt is malformed")
    try:
        supplied = base64.b64decode(
            encoded + "=",
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise EvidenceReceiptError("evidence receipt is malformed") from exc
    if len(supplied) != _SIGNATURE_BYTES:
        raise EvidenceReceiptError("evidence receipt is malformed")
    digest = hashlib.sha256(_content_bytes(content)).digest()
    expected = hmac.new(
        active_key,
        _message(normalized_url, digest),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied, expected):
        raise EvidenceReceiptError("evidence receipt is invalid")
    return normalized_url


__all__ = [
    "EVIDENCE_HMAC_KEY_FILE_ENV",
    "EvidenceReceiptError",
    "content_sha256",
    "issue_evidence_receipt",
    "load_evidence_hmac_key",
    "normalize_evidence_url",
    "resolve_evidence_hmac_key",
    "verify_evidence_receipt",
]

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
import json
import os
import re
import stat
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .render import validate_public_http_url


EVIDENCE_HMAC_KEY_FILE_ENV = "PV_WIKI_EVIDENCE_HMAC_KEY_FILE"
RECEIPT_VERSION = "pvwiki-evidence-v1"
RECEIPT_V2_VERSION = "pvwiki-evidence-v2"
MIN_HMAC_KEY_BYTES = 32
MAX_HMAC_KEY_FILE_BYTES = 4096
_MESSAGE_DOMAIN = b"pv-wiki/evidence-receipt\x00"
_V2_MESSAGE_DOMAIN = b"pv-wiki/evidence-receipt-v2\x00"
_SIGNATURE_BYTES = hashlib.sha256().digest_size
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_V2_FIELDS = frozenset(
    {
        "version",
        "requested_url",
        "final_url",
        "redirect_chain",
        "content_sha256",
        "artifact_sha256",
        "parser_metadata",
        "target_models",
        "parameter_rows",
        "signature",
    }
)
_PARSER_FIELDS = frozenset(
    {
        "contract_version",
        "page_count",
        "extracted_pages",
        "truncated",
    }
)
_PARAMETER_ROW_FIELDS = frozenset(
    {
        "parameter_id",
        "model",
        "source_label",
        "value",
        "unit",
        "section",
        "page",
        "order",
        "model_quote",
        "quote",
        "table_title",
        "value_state",
    }
)


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


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvidenceReceiptError(
            "evidence receipt contains non-canonical JSON"
        ) from exc


def _v2_text(value: Any, name: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceReceiptError(f"{name} must be a non-empty string")
    normalized = " ".join(value.replace("\x00", "").split())
    if not normalized or len(normalized) > limit:
        raise EvidenceReceiptError(f"{name} is invalid")
    return normalized


def _v2_optional_text(value: Any, name: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise EvidenceReceiptError(f"{name} must be a string")
    normalized = " ".join(value.replace("\x00", "").split())
    if len(normalized) > limit:
        raise EvidenceReceiptError(f"{name} is invalid")
    return normalized


def _v2_payload(
    receipt: Mapping[str, Any],
    *,
    require_signature: bool,
) -> dict[str, Any]:
    unknown = set(receipt) - _V2_FIELDS
    required = _V2_FIELDS if require_signature else _V2_FIELDS - {"signature"}
    missing = required - set(receipt)
    if unknown or missing:
        raise EvidenceReceiptError("evidence receipt v2 fields are invalid")
    if receipt.get("version") != RECEIPT_V2_VERSION:
        raise EvidenceReceiptError("evidence receipt v2 version is invalid")
    requested_url = normalize_evidence_url(receipt.get("requested_url"))
    final_url = normalize_evidence_url(receipt.get("final_url"))
    chain_raw = receipt.get("redirect_chain")
    if (
        isinstance(chain_raw, (str, bytes, bytearray))
        or not isinstance(chain_raw, Sequence)
        or not 1 <= len(chain_raw) <= 5
    ):
        raise EvidenceReceiptError("redirect_chain is invalid")
    redirect_chain = [normalize_evidence_url(item) for item in chain_raw]
    if redirect_chain[0] != requested_url or redirect_chain[-1] != final_url:
        raise EvidenceReceiptError("redirect_chain endpoints are invalid")
    if len(set(redirect_chain)) != len(redirect_chain):
        raise EvidenceReceiptError("redirect_chain must not repeat URLs")
    digests: dict[str, str] = {}
    for field in ("content_sha256", "artifact_sha256"):
        value = receipt.get(field)
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            raise EvidenceReceiptError(f"{field} is invalid")
        digests[field] = value
    parser_raw = receipt.get("parser_metadata")
    if not isinstance(parser_raw, Mapping) or set(parser_raw) != _PARSER_FIELDS:
        raise EvidenceReceiptError("parser_metadata fields are invalid")
    page_count = parser_raw.get("page_count")
    extracted_pages = parser_raw.get("extracted_pages")
    truncated = parser_raw.get("truncated")
    if (
        isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or not 1 <= page_count <= 500
        or isinstance(extracted_pages, bool)
        or not isinstance(extracted_pages, int)
        or not 1 <= extracted_pages <= page_count
        or not isinstance(truncated, bool)
    ):
        raise EvidenceReceiptError("parser_metadata values are invalid")
    parser_metadata = {
        "contract_version": _v2_text(
            parser_raw.get("contract_version"),
            "parser_metadata.contract_version",
            limit=100,
        ),
        "page_count": page_count,
        "extracted_pages": extracted_pages,
        "truncated": truncated,
    }
    targets_raw = receipt.get("target_models")
    if (
        isinstance(targets_raw, (str, bytes, bytearray))
        or not isinstance(targets_raw, Sequence)
        or len(targets_raw) > 16
    ):
        raise EvidenceReceiptError("target_models is invalid")
    target_models = [
        _v2_text(item, "target_models item", limit=300)
        for item in targets_raw
    ]
    if len(set(target_models)) != len(target_models):
        raise EvidenceReceiptError("target_models must be unique")
    rows_raw = receipt.get("parameter_rows")
    if (
        isinstance(rows_raw, (str, bytes, bytearray))
        or not isinstance(rows_raw, Sequence)
        or len(rows_raw) > 500
    ):
        raise EvidenceReceiptError("parameter_rows is invalid")
    rows: list[dict[str, Any]] = []
    for index, row_raw in enumerate(rows_raw):
        if not isinstance(row_raw, Mapping) or set(row_raw) != _PARAMETER_ROW_FIELDS:
            raise EvidenceReceiptError(
                f"parameter_rows[{index}] fields are invalid"
            )
        expected_id = f"p{index + 1:03d}"
        if row_raw.get("parameter_id") != expected_id:
            raise EvidenceReceiptError(
                "parameter_rows must use server-assigned sequential IDs"
            )
        page = row_raw.get("page")
        order = row_raw.get("order")
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or not 1 <= page <= page_count
            or isinstance(order, bool)
            or not isinstance(order, int)
            or order < 1
        ):
            raise EvidenceReceiptError(
                f"parameter_rows[{index}] position is invalid"
            )
        row = {}
        for key in _PARAMETER_ROW_FIELDS - {"page", "order"}:
            text_loader = (
                _v2_text
                if key
                in {
                    "parameter_id",
                    "model",
                    "source_label",
                    "value",
                    "model_quote",
                    "quote",
                    "value_state",
                }
                else _v2_optional_text
            )
            row[key] = text_loader(
                row_raw.get(key),
                f"parameter_rows[{index}].{key}",
                limit=1000,
            )
        row["page"] = page
        row["order"] = order
        rows.append(row)
    payload = {
        "version": RECEIPT_V2_VERSION,
        "requested_url": requested_url,
        "final_url": final_url,
        "redirect_chain": redirect_chain,
        **digests,
        "parser_metadata": parser_metadata,
        "target_models": target_models,
        "parameter_rows": rows,
    }
    if require_signature:
        signature = receipt.get("signature")
        if not isinstance(signature, str):
            raise EvidenceReceiptError("evidence receipt v2 signature is invalid")
        payload["signature"] = signature
    return payload


def issue_evidence_receipt_v2(
    key: bytes | bytearray | memoryview,
    *,
    requested_url: Any,
    final_url: Any,
    redirect_chain: Sequence[Any],
    content: Any,
    artifact_sha256: Any,
    parser_metadata: Mapping[str, Any],
    target_models: Sequence[Any],
    parameter_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Issue a strict self-describing receipt for locally parsed PDF evidence."""

    active_key = _validated_key(key)
    unsigned = _v2_payload(
        {
            "version": RECEIPT_V2_VERSION,
            "requested_url": requested_url,
            "final_url": final_url,
            "redirect_chain": list(redirect_chain),
            "content_sha256": content_sha256(content),
            "artifact_sha256": artifact_sha256,
            "parser_metadata": dict(parser_metadata),
            "target_models": list(target_models),
            "parameter_rows": [dict(item) for item in parameter_rows],
        },
        require_signature=False,
    )
    signature = hmac.new(
        active_key,
        _V2_MESSAGE_DOMAIN + _canonical_json_bytes(unsigned),
        hashlib.sha256,
    ).digest()
    return {
        **unsigned,
        "signature": base64.urlsafe_b64encode(signature)
        .rstrip(b"=")
        .decode("ascii"),
    }


def verify_evidence_receipt_v2(
    key: bytes | bytearray | memoryview,
    *,
    content: Any,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify a strict v2 receipt and return its normalized signed payload."""

    active_key = _validated_key(key)
    normalized = _v2_payload(receipt, require_signature=True)
    supplied_text = normalized.pop("signature")
    if len(supplied_text) != 43:
        raise EvidenceReceiptError("evidence receipt v2 signature is invalid")
    try:
        supplied = base64.b64decode(
            supplied_text + "=",
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise EvidenceReceiptError(
            "evidence receipt v2 signature is invalid"
        ) from exc
    expected = hmac.new(
        active_key,
        _V2_MESSAGE_DOMAIN + _canonical_json_bytes(normalized),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied, expected):
        raise EvidenceReceiptError("evidence receipt v2 is invalid")
    if normalized["content_sha256"] != content_sha256(content):
        raise EvidenceReceiptError("evidence receipt v2 content hash is invalid")
    return normalized


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
    if isinstance(receipt, Mapping):
        payload = verify_evidence_receipt_v2(
            active_key,
            content=content,
            receipt=receipt,
        )
        if payload["final_url"] != normalized_url:
            raise EvidenceReceiptError("evidence receipt v2 URL is invalid")
        return normalized_url
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
    "RECEIPT_V2_VERSION",
    "content_sha256",
    "issue_evidence_receipt",
    "issue_evidence_receipt_v2",
    "load_evidence_hmac_key",
    "normalize_evidence_url",
    "resolve_evidence_hmac_key",
    "verify_evidence_receipt",
    "verify_evidence_receipt_v2",
]

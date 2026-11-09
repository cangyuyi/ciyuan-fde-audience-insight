from __future__ import annotations

import json
import re
from typing import Any

# Deliberately conservative intake guard, not a full DLP/PII detector.
_DIRECT_IDENTIFIERS = (
    re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
)
_SENSITIVE_KEYS = {
    "name",
    "real_name",
    "phone",
    "mobile",
    "email",
    "address",
    "id_card",
    "身份证",
    "手机号",
    "电话",
    "姓名",
    "住址",
    "收货地址",
    "买家id",
    "buyer_id",
    "user_id",
    "用户id",
}


def contains_direct_identifier(value: str) -> bool:
    return any(pattern.search(value) for pattern in _DIRECT_IDENTIFIERS)


def metadata_has_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).strip().casefold() in _SENSITIVE_KEYS:
                return True
            if metadata_has_sensitive_key(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(metadata_has_sensitive_key(child) for child in value)
    return False


def metadata_contains_direct_identifier(value: Any) -> bool:
    """Check textual JSON values without treating high-precision floats as ID numbers."""
    if isinstance(value, dict):
        return any(
            metadata_contains_direct_identifier(key) or metadata_contains_direct_identifier(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(metadata_contains_direct_identifier(child) for child in value)
    if isinstance(value, str):
        return contains_direct_identifier(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return contains_direct_identifier(str(value))
    return False


def safe_json_size(value: Any, limit: int = 8_000) -> bool:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")) <= limit
    except (TypeError, ValueError):
        return False

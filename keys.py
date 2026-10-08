"""WireGuard keys are canonical Base64-encoded 32-byte values."""

import base64
import binascii


def validate_key(value: str) -> str:
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Expected a Base64-encoded 32-byte WireGuard key") from exc
    if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("Expected a canonical Base64-encoded 32-byte WireGuard key")
    return value

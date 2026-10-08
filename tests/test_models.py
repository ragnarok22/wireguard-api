"""Public contracts, key canonicalization, configuration and stable errors."""

import base64
import tomllib
from ipaddress import IPv4Address
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from configuration import client_config
from errors import (
    ConflictError,
    ControlPlaneError,
    InputError,
    NotFoundError,
    StorageError,
    WireGuardError,
)
from keys import validate_key
from models import ConfigTemplate, ErrorResponse, PeerCreate
from settings import Settings
from version import VERSION

KEY = base64.b64encode(bytes(range(32))).decode("ascii")


@pytest.mark.parametrize("raw", [bytes(32), bytes(range(32)), b"\xff" * 32])
def test_keys_accept_exact_canonical_32_bytes(raw: bytes) -> None:
    encoded = base64.b64encode(raw).decode("ascii")
    assert validate_key(encoded) == encoded


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not base64",
        "é",
        KEY + "\n",
        KEY.rstrip("="),
        KEY + "=",
        base64.b64encode(bytes(31)).decode(),
        base64.b64encode(bytes(33)).decode(),
        KEY[:-2] + "9=",
        KEY.replace("A", "_", 1),
    ],
)
def test_keys_reject_noncanonical_or_wrong_length(value: str) -> None:
    with pytest.raises(ValueError, match="WireGuard key"):
        validate_key(value)


def test_create_modes_and_address_serialization() -> None:
    generated = PeerCreate.model_validate(
        {"key_mode": "generated", "public_key": None, "address": "10.13.13.2"}
    )
    assert generated.public_key is None
    assert generated.address == IPv4Address("10.13.13.2")
    assert '"address":"10.13.13.2"' in generated.model_dump_json()
    external = PeerCreate(key_mode="external", public_key=KEY)
    assert external.public_key == KEY
    assert external.address is None


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"key_mode": "unknown"},
        {"key_mode": True},
        {"key_mode": 1},
        {"key_mode": "external"},
        {"key_mode": "external", "public_key": None},
        {"key_mode": "generated", "public_key": KEY},
        {"key_mode": "external", "public_key": "bad"},
        {"key_mode": "external", "public_key": 123},
        {"key_mode": "generated", "address": "::1"},
        {"key_mode": "generated", "address": "10.0.0.1/32"},
        {"key_mode": "generated", "private_key": KEY},
        {"key_mode": "generated", "preshared_key": KEY},
        {"key_mode": "generated", "allowed_ips": ["0.0.0.0/0"]},
    ],
)
def test_create_rejects_invalid_modes_keys_and_extra_fields(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        PeerCreate.model_validate(values)


@pytest.mark.parametrize(
    "values",
    [
        {"key_mode": b"generated"},
        {"key_mode": "external", "public_key": KEY.encode()},
        {"key_mode": "generated", "address": int(IPv4Address("10.13.13.2"))},
        {"key_mode": "generated", "address": True},
    ],
    ids=["bytes-mode", "bytes-public-key", "integer-address", "boolean-address"],
)
def test_create_rejects_invalid_field_types(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PeerCreate.model_validate(values)


def test_configuration_full_ipv4_template() -> None:
    settings = Settings(
        api_token=SecretStr("test-secret"),
        server_endpoint="node.example.org:443",
        dns=IPv4Address("9.9.9.9"),
    )
    expected = (
        "[Interface]\nPrivateKey = <YOUR_PRIVATE_KEY>\n"
        "Address = 10.13.13.2/32\nDNS = 9.9.9.9\n\n"
        f"[Peer]\nPublicKey = {KEY}\nEndpoint = node.example.org:443\n"
        "AllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n"
    )
    rendered = client_config(settings, "10.13.13.2", KEY, "<YOUR_PRIVATE_KEY>")
    assert rendered == expected
    assert "::/0" not in rendered
    assert "PresharedKey" not in rendered
    assert ConfigTemplate(config=rendered).model_dump() == {"config": expected}


def test_version_matches_project_metadata() -> None:
    metadata = tomllib.loads(
        Path(__file__).parents[1].joinpath("pyproject.toml").read_text()
    )
    assert isinstance(VERSION, str)
    assert VERSION == metadata["project"]["version"]


@pytest.mark.parametrize(
    ("error_type", "status", "code"),
    [
        (ControlPlaneError, 503, "unavailable"),
        (WireGuardError, 503, "wireguard_unavailable"),
        (StorageError, 503, "storage_unavailable"),
        (ConflictError, 409, "conflict"),
        (NotFoundError, 404, "not_found"),
        (InputError, 422, "invalid_input"),
    ],
)
def test_stable_error_contract(
    error_type: type[ControlPlaneError], status: int, code: str
) -> None:
    error = error_type("Safe public message")
    assert isinstance(error, ControlPlaneError)
    assert error.status_code == status
    assert error.code == code
    assert ErrorResponse(code=error.code, detail=str(error)).model_dump() == {
        "code": code,
        "detail": "Safe public message",
    }

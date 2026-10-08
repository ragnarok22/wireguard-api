"""Validate configuration at the trust boundary, including environment loading."""

from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

import settings as settings_module
from settings import Settings, interface_name


def configured(**overrides: object) -> Settings:
    return Settings.model_validate(
        {"api_token": "test-secret", "server_endpoint": "node.example.org:51820"}
        | overrides
    )


def test_defaults_secrets_and_immutability() -> None:
    settings = configured()
    assert isinstance(settings.api_token, SecretStr)
    assert settings.api_token.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(settings)
    assert "test-secret" not in settings.model_dump_json()
    assert settings.server_address == IPv4Interface("10.13.13.1/24")
    assert settings.dns == IPv4Address("1.1.1.1")
    assert settings.data_dir == Path("/config")
    assert settings.egress_interface is None
    with pytest.raises(ValidationError, match="frozen"):
        settings.interface = "wg1"


@pytest.mark.parametrize("missing", ["api_token", "server_endpoint"])
def test_required_fields(missing: str) -> None:
    values = {"api_token": "test-secret", "server_endpoint": "node.example.org"}
    del values[missing]
    with pytest.raises(ValidationError, match="Field required"):
        Settings.model_validate(values)


@pytest.mark.parametrize("token", ["", " \t\n", "default_token_change_me"])
def test_reject_empty_or_default_tokens(token: str) -> None:
    with pytest.raises(ValidationError, match="nonempty, non-default"):
        configured(api_token=token)


@pytest.mark.parametrize(
    "values,secret,field",
    [
        (
            {"api_token": "diagnostic-secret-marker"},
            "diagnostic-secret-marker",
            "server_endpoint",
        ),
        (
            {
                "api_token": "default_token_change_me",
                "server_endpoint": "node.example.org",
            },
            "default_token_change_me",
            "api_token",
        ),
        (
            {
                "api_token": "diagnostic-secret-marker",
                "server_endpoint": "vpn.example.com",
            },
            "diagnostic-secret-marker",
            "server_endpoint",
        ),
    ],
)
def test_startup_validation_diagnostics_never_disclose_token(
    values: dict[str, str], secret: str, field: str
) -> None:
    with pytest.raises(ValidationError) as error:
        Settings.model_validate(values)
    diagnostic = str(error.value)
    assert field in diagnostic
    assert secret not in diagnostic


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("NODE.Example.ORG", "node.example.org:51820"),
        ("192.0.2.10", "192.0.2.10:51820"),
        ("192.0.2.10:1", "192.0.2.10:1"),
        ("node.example.org:65535", "node.example.org:65535"),
        ("node.example.org:00080", "node.example.org:80"),
        ("node-1.example.org:51820", "node-1.example.org:51820"),
    ],
)
def test_endpoint_normalization(endpoint: str, expected: str) -> None:
    assert configured(server_endpoint=endpoint).server_endpoint == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        ":51820",
        "node.example.org:",
        "node.example.org:0",
        "node.example.org:65536",
        "node.example.org:-1",
        "node.example.org:1.5",
        "node.example.org:１２",
        "node.example.org:abc",
        "node.example.org:80:90",
        "node.example.org\nPostUp=evil",
        "node.example.org:80\n",
        "node.example.org\r:80",
        "node.example.org :80",
        "https://node.example.org",
        "[2001:db8::1]:51820",
        "2001:db8::1",
        "vpn.example.com",
        "256.0.0.1:80",
        "1.2.3:80",
        "192.168.01.1:80",
        "-node.example.org",
        "node_.example.org",
        "node..org",
        "node.org.",
        "a" * 64 + ".org",
        ".".join(["a" * 63] * 4),
    ],
)
def test_reject_endpoint_injection_and_invalid_hosts(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        configured(server_endpoint=endpoint)


@pytest.mark.parametrize("endpoint", ["VPN.EXAMPLE.COM", "Vpn.Example.Com:51820"])
def test_placeholder_endpoint_cannot_bypass_validation_with_case(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="real IPv4 host"):
        configured(server_endpoint=endpoint)


@pytest.mark.parametrize("name", ["wg0", "eth0.1", "enp0s3-1", "a" * 15, "_wg"])
def test_valid_interface_names(name: str) -> None:
    assert interface_name(name) == name
    settings = configured(interface=name, egress_interface=name)
    assert settings.interface == settings.egress_interface == name


@pytest.mark.parametrize("name", ["", "-wg0", "a" * 16, "wg 0", "wg0\n", "wg0;id", "é"])
@pytest.mark.parametrize("field", ["interface", "egress_interface"])
def test_reject_invalid_interfaces(name: str, field: str) -> None:
    with pytest.raises(ValidationError, match="Linux interface"):
        configured(**{field: name})


def test_explicit_no_egress() -> None:
    assert configured(egress_interface=None).egress_interface is None


@pytest.mark.parametrize("address", ["10.0.0.1/16", "10.0.0.1/30", "10.0.0.2/30"])
def test_valid_pool_boundaries(address: str) -> None:
    assert str(configured(server_address=address).server_address) == address


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1/15",
        "10.0.0.0/24",
        "10.0.0.255/24",
        "10.0.0.1/31",
        "10.0.0.1/32",
        "2001:db8::1/64",
        "bad",
    ],
)
def test_reject_unusable_server_pool(address: str) -> None:
    with pytest.raises(ValidationError):
        configured(server_address=address)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("listen_port", 0),
        ("listen_port", 65536),
        ("command_timeout", 0),
        ("command_timeout", 61),
        ("reconcile_interval", 0),
        ("reconcile_interval", 301),
        ("snapshot_ttl", -1),
        ("snapshot_ttl", 31),
        ("command_timeout", float("inf")),
        ("snapshot_ttl", float("nan")),
        ("dns", "::1"),
        ("surprise", True),
    ],
)
def test_reject_invalid_limits_and_unknown_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        configured(**{field: value})


def test_valid_limit_boundaries_and_data_directory(tmp_path: Path) -> None:
    settings = configured(
        listen_port=65535,
        command_timeout=60,
        reconcile_interval=300,
        snapshot_ttl=0,
        data_dir=tmp_path,
    )
    assert settings.data_dir == tmp_path
    assert settings.snapshot_ttl == 0
    assert configured(listen_port=1, snapshot_ttl=30).listen_port == 1


def test_load_environment_explicitly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environment = {
        "API_TOKEN": "environment-secret",
        "SERVER_ENDPOINT": "Node.Example.org:443",
        "WG_INTERFACE": "wg1",
        "WG_SERVER_ADDRESS": "10.4.0.1/16",
        "WG_LISTEN_PORT": "443",
        "WG_DATA_DIR": str(tmp_path),
        "CLIENT_DNS": "9.9.9.9",
        "WG_EGRESS_INTERFACE": "eth0",
        "WG_COMMAND_TIMEOUT": "2.5",
        "WG_RECONCILE_INTERVAL": "20",
        "WG_SNAPSHOT_TTL": "0.5",
    }
    monkeypatch.setattr(settings_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(settings_module.os, "environ", environment)
    loaded = Settings.load()
    assert loaded.api_token.get_secret_value() == "environment-secret"
    assert loaded.server_endpoint == "node.example.org:443"
    assert loaded.interface == "wg1"
    assert loaded.server_address == IPv4Interface("10.4.0.1/16")
    assert loaded.listen_port == 443
    assert loaded.data_dir == tmp_path
    assert loaded.dns == IPv4Address("9.9.9.9")
    assert loaded.egress_interface == "eth0"
    assert loaded.command_timeout == 2.5
    assert loaded.reconcile_interval == 20
    assert loaded.snapshot_ttl == 0.5
    monkeypatch.setattr(
        settings_module.os,
        "environ",
        {
            "API_TOKEN": "environment-secret",
            "SERVER_ENDPOINT": "node.example.org",
        },
    )
    assert Settings.load().interface == "wg0"

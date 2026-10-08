"""Validated node configuration, loaded explicitly rather than during import."""

import os
import re
from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path
from typing import Self

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


def interface_name(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,14}", value) is None:
        raise ValueError("Expected a Linux interface name of at most 15 characters")
    return value


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    api_token: SecretStr
    server_endpoint: str
    interface: str = "wg0"
    server_address: IPv4Interface = IPv4Interface("10.13.13.1/24")
    listen_port: int = Field(default=51820, ge=1, le=65535)
    data_dir: Path = Path("/config")
    dns: IPv4Address = IPv4Address("1.1.1.1")
    egress_interface: str | None = None
    command_timeout: float = Field(default=5, gt=0, le=60)
    reconcile_interval: float = Field(default=5, gt=0, le=300)
    snapshot_ttl: float = Field(default=1, ge=0, le=30)

    @field_validator("api_token")
    @classmethod
    def token_required(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if not token.strip() or token == "default_token_change_me":
            raise ValueError("API_TOKEN must be a nonempty, non-default secret")
        return value

    @field_validator("interface")
    @classmethod
    def valid_interface(cls, value: str) -> str:
        return interface_name(value)

    @field_validator("egress_interface")
    @classmethod
    def valid_egress(cls, value: str | None) -> str | None:
        return None if value is None else interface_name(value)

    @field_validator("server_address")
    @classmethod
    def valid_network(cls, value: IPv4Interface) -> IPv4Interface:
        network = value.network
        if network.prefixlen < 16 or value.ip in (
            network.network_address,
            network.broadcast_address,
        ):
            raise ValueError("Expected a usable server address in an IPv4 /16–/30 pool")
        return value

    @field_validator("server_endpoint")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        host, separator, port = value.partition(":")
        if not separator:
            port = "51820"
        if (
            not host
            or len(host) > 253
            or any(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                is None
                for label in host.split(".")
            )
            or not port.isascii()
            or not port.isdecimal()
            or not 1 <= int(port) <= 65535
            or host == "vpn.example.com"
        ):
            raise ValueError(
                "SERVER_ENDPOINT must be a real IPv4 host or hostname:port"
            )
        # Numeric dotted hosts must be valid IPv4, rather than a mistyped address.
        if re.fullmatch(r"[0-9.]+", host):
            IPv4Address(host)
        return f"{host.lower()}:{int(port)}"

    @classmethod
    def load(cls) -> Self:
        load_dotenv()
        names = {
            "api_token": "API_TOKEN",
            "server_endpoint": "SERVER_ENDPOINT",
            "interface": "WG_INTERFACE",
            "server_address": "WG_SERVER_ADDRESS",
            "listen_port": "WG_LISTEN_PORT",
            "data_dir": "WG_DATA_DIR",
            "dns": "CLIENT_DNS",
            "egress_interface": "WG_EGRESS_INTERFACE",
            "command_timeout": "WG_COMMAND_TIMEOUT",
            "reconcile_interval": "WG_RECONCILE_INTERVAL",
            "snapshot_ttl": "WG_SNAPSHOT_TTL",
        }
        return cls.model_validate(
            {
                field: os.environ[name]
                for field, name in names.items()
                if name in os.environ
            }
        )

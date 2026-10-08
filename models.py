"""Explicit public contracts: configuration and observations never contain PSKs."""

from ipaddress import IPv4Address
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator, model_validator

from keys import validate_key
from storage import OperationRecord
from wireguard import PeerStats


class PeerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_mode: Literal["generated", "external"]
    public_key: StrictStr | None = None
    address: IPv4Address | None = None

    @field_validator("address", mode="before")
    @classmethod
    def address_type(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, IPv4Address)):
            raise ValueError("Expected an IPv4 address string")
        return value

    @field_validator("public_key")
    @classmethod
    def valid_key(cls, value: str | None) -> str | None:
        return None if value is None else validate_key(value)

    @model_validator(mode="after")
    def valid_mode(self) -> Self:
        if (self.key_mode == "external") != (self.public_key is not None):
            raise ValueError("external mode requires a key; generated mode forbids one")
        return self


class PeerView(BaseModel):
    id: str
    public_key: str
    address: str
    state: Literal["pending", "active", "deleting"]
    created_at: float
    applied: bool
    observation: PeerStats | None


class PeerPage(BaseModel):
    items: list[PeerView]
    next_cursor: str | None


class CreateResult(BaseModel):
    peer: PeerView
    operation: OperationRecord
    private_key: str | None = None
    client_config: str | None = None
    replayed: bool = False


class ConfigTemplate(BaseModel):
    config: str


class ServerView(BaseModel):
    public_key: str
    endpoint: str
    interface: str
    address: str
    pool: str
    capacity: int
    reserved: int
    available: int


class ErrorResponse(BaseModel):
    code: str
    detail: str


class SampleInfo(BaseModel):
    sampled_at: float
    age_seconds: float
    interval_seconds: float | None


class PeerCounts(BaseModel):
    registered: int
    active: int
    pending: int
    deleting: int
    applied: int
    observed: int
    unmanaged: int


class HandshakeSummary(BaseModel):
    recent: int
    never: int
    latest_at: int | None
    window_seconds: int


class TrafficSummary(BaseModel):
    scope: Literal["current_interface"] = "current_interface"
    rx_bytes: int
    tx_bytes: int
    rx_bytes_per_second: float | None
    tx_bytes_per_second: float | None


class PoolSummary(BaseModel):
    network: str
    capacity: int
    reserved: int
    available: int


class VpnStats(BaseModel):
    version: str
    uptime_seconds: float
    sample: SampleInfo
    peers: PeerCounts
    handshakes: HandshakeSummary
    traffic: TrafficSummary
    pool: PoolSummary
    pending_operations: int


class CpuInfo(BaseModel):
    source: Literal["cgroup_v1", "cgroup_v2", "unavailable"]
    status: Literal["available", "warming_up", "unavailable"]
    capacity_cores: float | None
    total_usage_seconds: float | None
    used_cores: float | None
    usage_percent: float | None


class MemoryInfo(BaseModel):
    source: Literal["cgroup_v1", "cgroup_v2", "unavailable"]
    used_bytes: int | None
    limit_bytes: int | None
    capacity_bytes: int | None
    usage_percent: float | None


class DiskInfo(BaseModel):
    scope: Literal["data_filesystem"] = "data_filesystem"
    total_bytes: int
    used_bytes: int
    free_bytes: int
    usage_percent: float


class RuntimeInfo(BaseModel):
    os: str
    kernel: str
    architecture: str
    python_version: str
    api_version: str
    uptime_seconds: float


class SystemInfo(BaseModel):
    status: Literal["available", "partial"]
    resource_scope: Literal["current_cgroup"] = "current_cgroup"
    sample: SampleInfo
    cpu: CpuInfo
    memory: MemoryInfo
    disk: DiskInfo | None
    runtime: RuntimeInfo

"""Explicit public contracts: configuration and observations never contain PSKs."""

from ipaddress import IPv4Address
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from keys import validate_key
from storage import OperationRecord
from wireguard import PeerStats


class PeerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_mode: Literal["generated", "external"]
    public_key: str | None = None
    address: IPv4Address | None = None

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

"""Authenticated, versioned HTTP contracts over the peer service."""

import hmac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from errors import InputError
from models import (
    ConfigTemplate,
    CreateResult,
    ErrorResponse,
    PeerCreate,
    PeerPage,
    PeerView,
    ServerView,
    SystemInfo,
    VpnStats,
)
from service import PeerService
from storage import OperationRecord
from telemetry import Telemetry


def build_router(service: PeerService, telemetry: Telemetry) -> APIRouter:
    scheme = APIKeyHeader(name="X-API-Token", auto_error=False)

    async def authenticate(token: Annotated[str | None, Depends(scheme)]) -> None:
        if token is None:
            raise HTTPException(status_code=401, detail="API token required")
        if not hmac.compare_digest(
            token.encode(), service.settings.api_token.get_secret_value().encode()
        ):
            raise HTTPException(status_code=403, detail="Invalid authentication token")

    router = APIRouter(
        prefix="/v1",
        dependencies=[Depends(authenticate)],
        responses={
            code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 503)
        },
    )

    @router.get("/peers", response_model=PeerPage, tags=["peers"])
    def list_peers(
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        after: UUID | None = None,
    ) -> PeerPage:
        return service.list(limit, None if after is None else str(after))

    @router.post(
        "/peers",
        response_model=CreateResult,
        status_code=201,
        responses={200: {"model": CreateResult}, 202: {"model": CreateResult}},
        tags=["peers"],
    )
    def create_peer(
        body: PeerCreate,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9_.:-]{1,128}$"),
        ],
    ) -> CreateResult:
        if request.query_params:
            raise InputError("Peer creation does not accept query parameters")
        result = service.create(body, idempotency_key)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Location"] = f"/v1/peers/{result.peer.id}"
        if result.operation.status == "pending":
            response.status_code = 202
            response.headers["Retry-After"] = "5"
        elif result.replayed:
            response.status_code = 200
        return result

    @router.get("/peers/{peer_id}", response_model=PeerView, tags=["peers"])
    def get_peer(peer_id: UUID) -> PeerView:
        return service.get(str(peer_id))

    @router.delete(
        "/peers/{peer_id}",
        status_code=204,
        response_model=None,
        responses={202: {"model": OperationRecord}},
        tags=["peers"],
    )
    def delete_peer(peer_id: UUID) -> Response:
        operation = service.delete(str(peer_id))
        if operation.status == "pending":
            return JSONResponse(
                content=operation_to_json(operation),
                status_code=202,
                headers={
                    "Location": f"/v1/operations/{operation.id}",
                    "Retry-After": "5",
                },
            )
        return Response(status_code=204)

    @router.get(
        "/peers/{peer_id}/config-template",
        response_model=ConfigTemplate,
        tags=["peers"],
    )
    def template(peer_id: UUID) -> ConfigTemplate:
        return ConfigTemplate(config=service.template(str(peer_id)))

    @router.get("/server", response_model=ServerView, tags=["server"])
    def server() -> ServerView:
        return service.server()

    @router.get("/stats", response_model=VpnStats, tags=["statistics"])
    def stats(
        response: Response,
        handshake_window_seconds: Annotated[int, Query(ge=1, le=3600)] = 180,
    ) -> VpnStats:
        response.headers["Cache-Control"] = "no-store"
        return telemetry.stats(handshake_window_seconds)

    @router.get("/system", response_model=SystemInfo, tags=["statistics"])
    def system(response: Response) -> SystemInfo:
        response.headers["Cache-Control"] = "no-store"
        return telemetry.system()

    @router.get(
        "/operations/{operation_id}",
        response_model=OperationRecord,
        tags=["operations"],
    )
    def operation(operation_id: UUID) -> OperationRecord:
        return service.operation(str(operation_id))

    return router


def operation_to_json(operation: OperationRecord) -> dict[str, str | float | None]:
    # This explicit projection makes the error/secret boundary reviewable.
    return {
        "id": operation.id,
        "peer_id": operation.peer_id,
        "kind": operation.kind,
        "status": operation.status,
        "public_key": operation.public_key,
        "address": operation.address,
        "error": operation.error,
        "created_at": operation.created_at,
        "request_key": operation.request_key,
        "fingerprint": operation.fingerprint,
    }

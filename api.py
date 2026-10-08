"""Application composition. Run with ``uvicorn api:create_app --factory``."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from errors import ControlPlaneError
from health import ReadinessStatus, readiness
from metrics import Metrics, MetricsMiddleware
from models import ErrorResponse
from routes import build_router
from service import PeerService
from settings import Settings
from storage import Store
from version import VERSION
from wireguard import WireGuard

logger = logging.getLogger(__name__)


async def reconciliation_loop(service: PeerService, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=service.settings.reconcile_interval
            )
        except TimeoutError:
            try:
                await run_in_threadpool(service.reconcile)
            except ControlPlaneError:
                logger.warning("WireGuard reconciliation will be retried")


def create_app(
    settings: Settings | None = None, service: PeerService | None = None
) -> FastAPI:
    configured = settings or (
        service.settings if service is not None else Settings.load()
    )
    if service is None:
        service = PeerService(
            configured,
            Store(
                configured.data_dir / "peers.sqlite3",
                configured.interface,
                str(configured.server_address),
                configured.data_dir / "peers.json",
            ),
            WireGuard(configured.interface, configured.command_timeout),
        )
    node = service
    collector = Metrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await run_in_threadpool(node.initialize)
        stop = asyncio.Event()
        task = asyncio.create_task(reconciliation_loop(node, stop))
        try:
            yield
        finally:
            stop.set()
            await task

    app = FastAPI(title="WireGuard API", version=VERSION, lifespan=lifespan)
    app.state.service = node
    app.state.metrics = collector
    app.add_middleware(MetricsMiddleware, metrics=collector)
    app.include_router(build_router(node))

    @app.exception_handler(ControlPlaneError)
    async def control_error(request: Request, exc: ControlPlaneError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "detail": str(exc)},
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": "http_error", "detail": str(exc.detail)},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Validation payloads can contain submitted credentials. Publish locations,
        # never echo values or the body, and keep the public error schema stable.
        locations = ", ".join(
            ".".join(map(str, error["loc"])) for error in exc.errors()
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": "invalid_input",
                "detail": f"Invalid request: {locations}",
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled control-plane error (%s)", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "detail": "Internal Server Error"},
        )

    @app.get("/livez", tags=["monitoring"])
    async def live() -> dict[str, str]:
        return {"status": "alive", "version": VERSION}

    @app.get(
        "/readyz",
        response_model=ReadinessStatus,
        responses={503: {"model": ReadinessStatus}},
        tags=["monitoring"],
    )
    def ready(response: Response) -> ReadinessStatus:
        result = readiness(node)
        response.status_code = 200 if result.status == "ready" else 503
        return result

    @app.get(
        "/metrics", response_class=Response, responses={503: {"model": ErrorResponse}}
    )
    def metrics() -> Response:
        try:
            snapshot = node.snapshot()
            pending = node.store.pending_count()
        except ControlPlaneError:
            snapshot = None
            pending = -1
        return Response(
            content=collector.render(snapshot, pending),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    return app

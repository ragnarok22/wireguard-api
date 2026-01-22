import logging
import os
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from wireguard import WireGuard, WireGuardError

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wireguard-api")

load_dotenv()

# Token uses by master to send commands to this node
TOKEN = os.getenv("API_TOKEN")
WG_INTERFACE = os.getenv("WG_INTERFACE", "wg0")

app = FastAPI(title="Wireguard API", version="0.3.0")
wg = WireGuard(interface=WG_INTERFACE)


# --- Exception Handlers ---
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )


# --- Dependencies ---
header_scheme = APIKeyHeader(name="X-API-Token")


async def get_token_header(x_api_token: Annotated[str, Depends(header_scheme)]):
    if x_api_token != TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid authentication token",
        )


# --- Models ---


class Peer(BaseModel):
    public_key: str
    preshared_key: str = "(hidden)"
    endpoint: str
    allowed_ips: list[str]
    latest_handshake: str
    transfer_rx: str
    transfer_tx: str
    persistent_keepalive: str


class PeerCreate(BaseModel):
    public_key: str | None = Field(
        None,
        description="Public key of the peer. If not provided, one will be generated.",
    )
    allowed_ips: list[str] = Field(
        ..., description="List of allowed IPs for this peer."
    )


class PeerResponse(BaseModel):
    public_key: str
    allowed_ips: list[str]
    # If we generated keys, we return private key (ONLY ONCE)
    private_key: str | None = None


# --- Endpoints ---


@app.get("/peers", dependencies=[Depends(get_token_header)], response_model=list[dict])
async def list_peers():
    peers = wg.list_peers()
    # Convert dict to list response
    result = []
    for pub_key, data in peers.items():
        data["public_key"] = pub_key
        result.append(data)
    return result


@app.post(
    "/peers",
    dependencies=[Depends(get_token_header)],
    status_code=status.HTTP_201_CREATED,
)
async def create_peer(peer: PeerCreate):
    priv_key = None
    pub_key = peer.public_key

    if not pub_key:
        priv_key, pub_key = wg.gen_keys()

    try:
        wg.create_peer(pub_key, peer.allowed_ips)
    except WireGuardError as e:
        logger.error(f"Failed to create peer: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e

    return PeerResponse(
        public_key=pub_key, allowed_ips=peer.allowed_ips, private_key=priv_key
    )


@app.get("/peers/{public_key}", dependencies=[Depends(get_token_header)])
async def get_peer(public_key: str):
    peers = wg.list_peers()
    if public_key not in peers:
        raise HTTPException(status_code=404, detail="Peer not found")

    data = peers[public_key]
    data["public_key"] = public_key
    return data


@app.delete(
    "/peers/{public_key}",
    dependencies=[Depends(get_token_header)],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_peer(public_key: str):
    # Check existence
    peers = wg.list_peers()
    if public_key not in peers:
        raise HTTPException(status_code=404, detail="Peer not found")

    try:
        wg.delete_peer(public_key)
    except WireGuardError as e:
        logger.error(f"Failed to delete peer: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
    return None


@app.get("/peers/{public_key}/config", dependencies=[Depends(get_token_header)])
async def get_peer_config(public_key: str):
    """
    Returns a basic configuration for the client.
    """
    peers = wg.list_peers()
    if public_key not in peers:
        raise HTTPException(status_code=404, detail="Peer not found")

    # peer_data = peers[public_key]
    # allowed_ips = ",".join(peer_data['allowed_ips'])

    server_pub_key = os.getenv("SERVER_PUBLIC_KEY", "SERVER_PUB_KEY_PLACEHOLDER")
    server_endpoint = os.getenv("SERVER_ENDPOINT", "vpn.example.com:51820")

    config = f"""
# Client config (partial) - Add your PrivateKey to [Interface]

[Peer]
PublicKey = {server_pub_key}
Endpoint = {server_endpoint}
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
"""
    return {
        "config": config.strip(),
        "note": "Add your private key to the interface section found in POST response",
    }

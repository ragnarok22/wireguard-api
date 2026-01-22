import logging
import os
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
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
    allowed_ips: list[str] | None = Field(
        None, description="Allowed IPs. If None, one will be allocated automatically."
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
    response_model=None,
)
async def create_peer(peer: PeerCreate, format: str = "json"):
    priv_key = None
    pub_key = peer.public_key

    if not pub_key:
        priv_key, pub_key = wg.gen_keys()

    if not peer.allowed_ips:
        try:
            # 1. Get current subnet (e.g. 10.13.13.1/24)
            subnet = wg.get_interface_subnet()
            # 2. Get list of used IPs from existing peers
            peers = wg.list_peers()
            used_ips = set()
            for p in peers.values():
                # Each peer has a list of allowed_ips (str) like "10.0.0.2/32,..."
                for ip_cidr in p.get("allowed_ips", []):
                    # Store just the IP part, ignoring /32
                    used_ips.add(ip_cidr.split("/")[0])

            # 3. Allocate next
            new_ip = wg.allocate_next_ip(subnet, used_ips)
            # Assign as /32 (single host)
            peer.allowed_ips = [f"{new_ip}/32"]
            logger.info(f"Allocated new IP {new_ip} for peer {pub_key}")

        except WireGuardError as e:
            logger.error(f"Failed to allocate IP: {e}")
            raise HTTPException(
                status_code=500, detail=f"IP Allocation failed: {e}"
            ) from e

    try:
        wg.create_peer(pub_key, peer.allowed_ips)
    except WireGuardError as e:
        logger.error(f"Failed to create peer: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e

    if format == "config":
        if not priv_key:
            # We can't generate full config if we didn't generate the keys
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot generate config when public_key is provided. "
                    "Private key is unknown."
                ),
            )

        server_pub_key = os.getenv("SERVER_PUBLIC_KEY", "SERVER_PUB_KEY_PLACEHOLDER")
        if server_pub_key == "SERVER_PUB_KEY_PLACEHOLDER":
            # Try to fetch real public key from interface
            try:
                # wg show wg0 public-key
                server_pub_key = wg._run(["wg", "show", WG_INTERFACE, "public-key"])
            except Exception as e:
                logger.warning(f"Could not fetch server public key: {e}")

        server_endpoint = os.getenv("SERVER_ENDPOINT", "vpn.example.com:51820")

        # Taking the first allowed IP as the Interface Address (usually /32)
        # If multiple are passed, we might just list them or take first.
        # Standard WireGuard config takes 'Address'.
        address = peer.allowed_ips[0]

        config_content = f"""[Interface]
PrivateKey = {priv_key}
Address = {address}
DNS = 1.1.1.1

[Peer]
PublicKey = {server_pub_key}
Endpoint = {server_endpoint}
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
"""
        return Response(
            content=config_content, media_type="text/plain", status_code=201
        )

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
    if server_pub_key == "SERVER_PUB_KEY_PLACEHOLDER":
        # Try to fetch real public key from interface
        try:
            # wg show wg0 public-key
            server_pub_key = wg._run(["wg", "show", WG_INTERFACE, "public-key"])
        except Exception as e:
            logger.warning(f"Could not fetch server public key: {e}")

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

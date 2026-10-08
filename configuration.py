"""Client configuration rendering with one explicit IPv4 routing policy."""

from settings import Settings


def client_config(
    settings: Settings, address: str, server_key: str, private_key: str
) -> str:
    return (
        "[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"Address = {address}/32\n"
        f"DNS = {settings.dns}\n\n"
        "[Peer]\n"
        f"PublicKey = {server_key}\n"
        f"Endpoint = {settings.server_endpoint}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        "PersistentKeepalive = 25\n"
    )

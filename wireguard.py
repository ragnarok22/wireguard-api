import logging
import subprocess

logger = logging.getLogger(__name__)


class WireGuardError(Exception):
    pass


class WireGuard:
    def __init__(self, interface: str = "wg0"):
        self.interface = interface

    def _run(self, cmd: list[str]) -> str:
        try:
            # shell=False is safer. We pass list of arguments.
            result = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
            return result.strip()
        except subprocess.CalledProcessError as e:
            logger.error(f"Command failed: {cmd}, output: {e.output}")
            raise WireGuardError(f"WireGuard command failed: {e.output}") from e
        except FileNotFoundError as e:
            # For development/testing/mocking purposes where 'wg' might not exist
            logger.warning(
                "wg command not found. Returning mock data or raising error."
            )
            if cmd[0] == "wg" and "show" in cmd:
                return ""
            raise WireGuardError("WireGuard command not found") from e

    def list_peers(self) -> dict[str, dict]:
        """
        Parses `wg show <interface> dump` to get peer list.
        Returns dict keyed by public_key.
        """
        try:
            output = self._run(["wg", "show", self.interface, "dump"])
        except WireGuardError:
            return {}

        peers = {}
        lines = output.splitlines()
        # Format: public_key, preshared_key, endpoint, allowed_ips, latest_handshake
        # transfer_rx, transfer_tx, persistent_keepalive

        for line in lines:
            parts = line.split()
            if len(parts) < 8:
                # might be interface line (4 parts)
                continue

            public_key = parts[0]
            # Verify if it looks like a pubkey (base64, 44 chars) - simple check
            if not public_key.endswith("="):
                continue

            peers[public_key] = {
                "preshared_key": parts[1],
                "endpoint": parts[2],
                "allowed_ips": parts[3].split(","),
                "latest_handshake": parts[4],
                "transfer_rx": parts[5],
                "transfer_tx": parts[6],
                "persistent_keepalive": parts[7],
            }
        return peers

    def create_peer(self, public_key: str, allowed_ips: list[str]) -> None:
        """
        Adds a peer to the interface.
        """
        ips_str = ",".join(allowed_ips)
        self._run(
            ["wg", "set", self.interface, "peer", public_key, "allowed-ips", ips_str]
        )

    def delete_peer(self, public_key: str) -> None:
        """
        Removes a peer.
        """
        self._run(["wg", "set", self.interface, "peer", public_key, "remove"])

    def gen_keys(self) -> tuple[str, str]:
        """
        Generates (private_key, public_key) pair.
        """
        # wg genkey | tee privatekey | wg pubkey > publickey
        # We can do this in python to avoid pipe complexity or run separate commands
        priv_key = self._run(["wg", "genkey"])

        # Pipe priv_key to 'wg pubkey' stdin
        process = subprocess.Popen(
            ["wg", "pubkey"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        pub_key, stderr = process.communicate(input=priv_key)

        if process.returncode != 0:
            raise WireGuardError(f"Failed to generate pubkey: {stderr}")

        return priv_key.strip(), pub_key.strip()

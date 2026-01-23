import ipaddress
import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


class WireGuardError(Exception):
    pass


class WireGuard:
    def __init__(
        self, interface: str = "wg0", storage_path: str = "/config/peers.json"
    ):
        self.interface = interface
        self.storage_path = storage_path
        # Ensure directory exists if possible, though /config is usually a volume
        storage_dir = os.path.dirname(self.storage_path)
        if storage_dir and not os.path.exists(storage_dir):
            try:
                os.makedirs(storage_dir, exist_ok=True)
            except OSError as e:
                logger.warning(f"Could not create storage directory {storage_dir}: {e}")

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

    def _add_peer_to_interface(self, public_key: str, allowed_ips: list[str]) -> None:
        """
        Internal method to just run the command to add peer to interface.
        """
        ips_str = ",".join(allowed_ips)
        self._run(
            ["wg", "set", self.interface, "peer", public_key, "allowed-ips", ips_str]
        )

    def create_peer(self, public_key: str, allowed_ips: list[str]) -> None:
        """
        Adds a peer to the interface and saves to storage.
        """
        self._add_peer_to_interface(public_key, allowed_ips)
        self.save_peer_to_storage(public_key, allowed_ips)

    def delete_peer(self, public_key: str) -> None:
        """
        Removes a peer and deletes from storage.
        """
        self._run(["wg", "set", self.interface, "peer", public_key, "remove"])
        self.remove_peer_from_storage(public_key)

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

    def get_interface_subnet(self) -> str:
        """
        Returns the subnet CIDR of the WireGuard interface (e.g., "10.13.13.1/24").
        """
        # ip -o -f inet addr show <interface>
        try:
            output = self._run(
                ["ip", "-o", "-f", "inet", "addr", "show", self.interface]
            )
            # Parse CIDR from output
            # Split by whitespace, find the part with '/'
            for part in output.split():
                if "/" in part:
                    return part
            raise WireGuardError(f"Could not find CIDR in output: {output}")
        except Exception as e:
            raise WireGuardError(
                f"Failed to get subnet for {self.interface}: {e}"
            ) from e

    def allocate_next_ip(self, subnet_cidr: str, used_ips: set[str]) -> str:
        """
        Finds the next available IP in the subnet.
        """
        try:
            network = ipaddress.ip_network(subnet_cidr, strict=False)
        except ValueError as e:
            raise WireGuardError(f"Invalid subnet CIDR: {subnet_cidr}") from e

        # Host iterator (excludes network address and broadcast address)
        for ip in network.hosts():
            ip_str = str(ip)
            # Check if this IP is the assigned interface IP (gateway for peers)
            server_ip = subnet_cidr.split("/")[0]
            if ip_str == server_ip:
                continue

            if ip_str not in used_ips:
                return ip_str

        raise WireGuardError("No available IPs in subnet")

    # --- Persistence Methods ---

    def load_peers_from_storage(self) -> dict:
        if not os.path.exists(self.storage_path):
            return {}
        try:
            with open(self.storage_path) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load peers from storage: {e}")
            return {}

    def save_peer_to_storage(self, public_key: str, allowed_ips: list[str]) -> None:
        peers = self.load_peers_from_storage()
        peers[public_key] = {"allowed_ips": allowed_ips}
        # We could store more meta-data here if needed
        self._write_storage(peers)

    def remove_peer_from_storage(self, public_key: str) -> None:
        peers = self.load_peers_from_storage()
        if public_key in peers:
            del peers[public_key]
            self._write_storage(peers)

    def _write_storage(self, peers: dict) -> None:
        try:
            with open(self.storage_path, "w") as f:
                json.dump(peers, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write peers to storage: {e}")

    def restore_peers(self) -> None:
        """
        Restores peers from storage to the WireGuard interface.
        Should be called on startup.
        """
        logger.info(f"Restoring peers from {self.storage_path}")
        stored_peers = self.load_peers_from_storage()

        # Get currently active peers to avoid duplicates or errors
        # But 'wg set' is generally idempotent for adding peers.

        count = 0
        for public_key, data in stored_peers.items():
            allowed_ips = data.get("allowed_ips", [])
            try:
                self._add_peer_to_interface(public_key, allowed_ips)
                count += 1
            except WireGuardError as e:
                logger.error(f"Failed to restore peer {public_key}: {e}")

        logger.info(f"Restored {count} peers.")

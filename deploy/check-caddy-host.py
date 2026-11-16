"""Validate the real Caddyfile with and without an optional host TCP gateway.

Run after `docker pull caddy:2.11.4`. Uses synthetic domains and an isolated
temporary configuration; never starts a listener or contacts a certificate issuer.
"""

import json
from pathlib import Path
import subprocess
import tempfile

DEPLOY = Path(__file__).resolve().parent
IMAGE = "caddy:2.11.4"
OVERRIDE = """{
    http_port 18080
    https_port 18443
    default_bind 127.0.0.1
    servers {
        protocols h1 h2
        listener_wrappers {
            proxy_protocol {
                timeout 2s
                fallback_policy require
            }
            tls
        }
    }
}
http:// {
}
"""


def adapt(directory: Path) -> dict:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--mount",
            f"type=bind,src={directory},dst=/etc/caddy,readonly",
            "-e",
            "MAAT_HOST=api.maat.example.test",
            "-e",
            "MAAT_ADMIN_HOST=admin.maat.example.test",
            IMAGE,
            "caddy",
            "adapt",
            "--config",
            "/etc/caddy/Caddyfile",
            "--adapter",
            "caddyfile",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def values(node, key):
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                yield value
            yield from values(value, key)
    elif isinstance(node, list):
        for child in node:
            yield from values(child, key)


def main():
    with tempfile.TemporaryDirectory(prefix="maat-caddy-check-") as temp:
        root = Path(temp)
        (root / "Caddyfile").write_text((DEPLOY / "Caddyfile").read_text())
        overrides = root / "host-overrides"
        overrides.mkdir()
        default = adapt(root)
        default_servers = default["apps"]["http"]["servers"]
        assert list(values(default_servers, "listen")) == [[":443"]]
        assert not list(values(default_servers, "listener_wrappers"))

        (overrides / "gateway.caddy").write_text(OVERRIDE)
        proxied = adapt(root)
        servers = proxied["apps"]["http"]["servers"]
        listeners = {item for group in values(servers, "listen") for item in group}
        assert listeners == {"127.0.0.1:18080", "127.0.0.1:18443"}, listeners
        for server in servers.values():
            assert server["protocols"] == ["h1", "h2"]
            wrappers = server["listener_wrappers"]
            assert wrappers[0]["wrapper"] == "proxy_protocol"
            assert wrappers[0]["fallback_policy"] == "REQUIRE"
            assert not wrappers[0].get("allow")
            assert wrappers[1]["wrapper"] == "tls"

        # The public routes and WireGuard gate must not be rewritten by the gateway.
        assert (
            list(values(servers, "remote_ip"))
            == list(values(default_servers, "remote_ip"))
            == [{"ranges": ["10.8.0.0/24"]}]
        )
        assert list(values(servers, "upstreams")) == list(
            values(default_servers, "upstreams")
        )
        assert list(values(servers, "host")) == list(values(default_servers, "host"))
        assert proxied["apps"]["http"]["http_port"] == 18080
        assert proxied["apps"]["http"]["https_port"] == 18443
    print(
        "Caddy default and proxied configurations preserve Maat routes and its WireGuard gate."
    )


if __name__ == "__main__":
    main()

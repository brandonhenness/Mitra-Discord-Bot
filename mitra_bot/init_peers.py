"""Provision an offline CA and a separate installation bundle for every node."""
from __future__ import annotations

import argparse
import hashlib
import shutil
import ssl
import subprocess
import uuid
from pathlib import Path

import tomli_w

from mitra_bot.peer_config import Peer, PeerConfig


def find_openssl() -> str:
    executable = shutil.which("openssl")
    if executable:
        return executable
    git_openssl = Path("C:/Program Files/Git/usr/bin/openssl.exe")
    if git_openssl.exists():
        return str(git_openssl)
    raise RuntimeError("OpenSSL is required for certificate provisioning. Install OpenSSL or Git for Windows.")


def provision(output: Path, nodes: dict[str, str], *, allow_power: bool = False) -> None:
    if not 2 <= len(nodes) <= 65:
        raise ValueError("Provide between 2 and 65 unique nodes")
    for node, host in nodes.items():
        Peer(node_id=node, host=host, fingerprint="0" * 64)
    executable = find_openssl()
    output = output.resolve()
    # Never overwrite private keys or an existing configuration.
    output.mkdir(parents=True, exist_ok=False)

    def run(*args):
        subprocess.run([executable, *map(str, args)], check=True, capture_output=True, timeout=60)

    ca_key = output / "OFFLINE-CA.key"
    ca_cert = output / "ca.crt"
    run("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "3650",
        "-keyout", ca_key, "-out", ca_cert, "-subj", "/CN=Mitra Private Network CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    fingerprints = {}
    for node in nodes:
        folder = output / node
        folder.mkdir()
        shutil.copyfile(ca_cert, folder / "ca.crt")
        run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", folder / "node.key",
            "-out", folder / "node.csr", "-subj", f"/CN={node}")
        extensions = folder / "extensions.cnf"
        extensions.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth,clientAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n", encoding="ascii")
        run("x509", "-req", "-in", folder / "node.csr", "-CA", ca_cert, "-CAkey", ca_key,
            "-set_serial", str(uuid.uuid4().int), "-days", "365", "-out", folder / "node.crt",
            "-extfile", extensions)
        certificate = ssl.PEM_cert_to_DER_cert((folder / "node.crt").read_text())
        fingerprints[node] = hashlib.sha256(certificate).hexdigest()
    network_id = uuid.uuid4().hex
    for node in nodes:
        cfg = PeerConfig(
            enabled=True, network_id=network_id, node_id=node, listen_host="0.0.0.0",
            state_owner=next(iter(nodes)),
            ca_file="ca.crt", cert_file="node.crt", key_file="node.key",
            peers=[Peer(node_id=other, host=host, fingerprint=fingerprints[other], allow_power=allow_power)
                   for other, host in nodes.items() if other != node],
        )
        (output / node / "peer-network.toml").write_text(tomli_w.dumps(cfg.model_dump()), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory for private bundles")
    parser.add_argument("--node", action="append", required=True, help="Unique server-id=reachable-host (repeat for every server)")
    parser.add_argument("--allow-power", action="store_true", help="Grant every peer power control on every other node")
    args = parser.parse_args()
    nodes = {}
    for item in args.node:
        node, separator, host = item.partition("=")
        if not separator or node in nodes:
            parser.error("Each --node must be a unique server-id=reachable-host")
        nodes[node] = host
    provision(args.output, nodes, allow_power=args.allow_power)
    print("Private network created. Give each server only its own folder. Keep OFFLINE-CA.key offline.")
    print("Use the SAME Discord bot token on every node. Two nodes work without a witness.")


if __name__ == "__main__":
    main()

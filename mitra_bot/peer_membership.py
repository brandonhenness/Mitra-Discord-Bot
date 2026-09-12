"""Prepare reviewed peer membership bundles without changing running nodes."""
import argparse
import hashlib
import shutil
import ssl
import subprocess
import uuid
from pathlib import Path

import tomli_w

from mitra_bot.init_peers import OPENSSL_CONFIG, find_openssl
from mitra_bot.peer_config import Peer, PeerConfig
from mitra_bot.storage.config_store import tomllib
from mitra_bot.private_files import private_directory


def apply_membership(plan, local):
    """Apply only reviewed trust-list changes, retaining this machine's settings."""
    plan, local = Path(plan).resolve(), Path(local).resolve()
    proposed = PeerConfig.model_validate(tomllib.loads(plan.read_text(encoding="utf-8")))
    current = PeerConfig.model_validate(tomllib.loads(local.read_text(encoding="utf-8")))
    if (proposed.node_id, proposed.network_id, proposed.resolved_state_owner) != (current.node_id, current.network_id, current.resolved_state_owner):
        raise ValueError("The membership plan belongs to another node, network or owner")
    local_cert = local.parent/current.cert_file
    plan_cert = plan.parent/proposed.cert_file
    if ssl.PEM_cert_to_DER_cert(local_cert.read_text()) != ssl.PEM_cert_to_DER_cert(plan_cert.read_text()):
        raise ValueError("Membership plan changes this node's identity")
    current.peers = proposed.peers
    current.state_owner = proposed.resolved_state_owner
    current = PeerConfig.model_validate(current.model_dump())
    from mitra_bot.migrate_legacy_cache import _atomic_write_text
    backup = local.with_name(local.name + ".before-membership-" + uuid.uuid4().hex[:8])
    with backup.open("xb") as stream:
        stream.write(local.read_bytes())
    _atomic_write_text(local, tomli_w.dumps(current.model_dump()))
    return backup


def prepare(root, output, *, add=None, remove=None):
    root, output = Path(root).resolve(), Path(output).resolve()
    if bool(add) == bool(remove):
        raise ValueError("Choose exactly one add or remove operation")
    configs = {p.parent.name: PeerConfig.model_validate(tomllib.loads(p.read_text(encoding="utf-8")))
               for p in root.glob("*/peer-network.toml")}
    if not configs or any(name != cfg.node_id for name, cfg in configs.items()):
        raise ValueError("Use the complete original provisioning folder")
    first = next(iter(configs.values()))
    owner = first.resolved_state_owner
    for cfg in configs.values():
        if not cfg.enabled or cfg.network_id != first.network_id or cfg.resolved_state_owner != first.resolved_state_owner:
            raise ValueError("Bundles disagree about network identity or state owner")
        if {cfg.node_id, *(p.node_id for p in cfg.peers)} != set(configs):
            raise ValueError("Provisioning folder is incomplete or membership differs")
        if (cfg.ca_file, cfg.cert_file, cfg.key_file) != ("ca.crt", "node.crt", "node.key"):
            raise ValueError("Membership provisioning requires standard bundle certificate paths")
    if remove and (remove not in configs or remove == first.resolved_state_owner):
        raise ValueError("Cannot remove an unknown node or the state owner; recover the owner separately")
    if remove and len(configs) <= 2:
        raise ValueError("Keep at least two peers; use single-server setup to leave the network")
    if add:
        name, sep, host = add.partition("=")
        if not sep or name in configs or len(configs) >= 65:
            raise ValueError("Add requires a new unique node=reachable-host (maximum 65 nodes)")
        Peer(node_id=name, host=host, fingerprint="0"*64)
        if not (root / "OFFLINE-CA.key").is_file():
            raise ValueError("Adding a peer requires the original offline CA key")
    executable = find_openssl()
    def run(*args):
        subprocess.run([executable, *map(str, args)], capture_output=True, check=True, timeout=60)
    ca = root / "ca.crt"
    run("verify", "-check_ss_sig", "-CAfile", ca, ca)
    fingerprints = {}
    for node in configs:
        cert = root / node / "node.crt"
        run("verify", "-purpose", "sslclient", "-CAfile", ca, cert)
        run("verify", "-purpose", "sslserver", "-CAfile", ca, cert)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_cert_chain(cert, root/node/"node.key")
        fingerprints[node] = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
    for cfg in configs.values():
        if any(peer.fingerprint != fingerprints[peer.node_id] for peer in cfg.peers):
            raise ValueError("Bundle fingerprint does not match the node certificate")
    private_directory(output)
    shutil.copyfile(ca, output/"ca.crt")
    if add:
        folder = output / name
        folder.mkdir(mode=0o700)
        cnf = folder / "openssl.cnf"
        cnf.write_text(OPENSSL_CONFIG, encoding="ascii")
        run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", folder/"node.key",
            "-out", folder/"node.csr", "-subj", f"/CN={name}", "-config", cnf)
        ext = folder / "extensions.cnf"
        ext.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth,clientAuth\n", encoding="ascii")
        run("x509", "-req", "-in", folder/"node.csr", "-CA", ca, "-CAkey", root/"OFFLINE-CA.key",
            "-set_serial", str(uuid.uuid4().int), "-days", "365", "-out", folder/"node.crt", "-extfile", ext)
        run("verify", "-purpose", "sslclient", "-CAfile", ca, folder/"node.crt")
        run("verify", "-purpose", "sslserver", "-CAfile", ca, folder/"node.crt")
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert((folder/"node.crt").read_text())).hexdigest()
        existing = {}
        for cfg in configs.values():
            for peer in cfg.peers:
                if peer.node_id in existing and (existing[peer.node_id].host, existing[peer.node_id].port) != (peer.host, peer.port):
                    raise ValueError("Existing bundles disagree about a reachable address")
                existing[peer.node_id] = peer
        new = PeerConfig(enabled=True, network_id=first.network_id, node_id=name,
                         listen_host="0.0.0.0", state_owner=first.resolved_state_owner,
                         ca_file="ca.crt", cert_file="node.crt", key_file="node.key",
                         peers=[p.model_copy(update={"allow_power": False}) for p in existing.values()])
        for cfg in configs.values():
            cfg.state_owner = owner
            cfg.peers.append(Peer(node_id=name, host=host, fingerprint=fingerprint))
        configs[name] = new
    else:
        del configs[remove]
        for cfg in configs.values():
            cfg.peers = [p for p in cfg.peers if p.node_id != remove]
    for node, cfg in configs.items():
        folder = output/node
        folder.mkdir(exist_ok=True, mode=0o700)
        if not add or node != name:
            for filename in ("node.crt", "node.key"):
                shutil.copyfile(root/node/filename, folder/filename)
        shutil.copyfile(ca, folder/"ca.crt")
        (folder/"node.key").chmod(0o600)
        (folder/"peer-network.toml").write_text(tomli_w.dumps(cfg.model_dump()), encoding="utf-8")
    (output/"README.txt").write_text(
        "Membership plan: stop peers and back up each installation.\n"
        "Existing peers: use python -m mitra_bot.peer_membership --apply <this-node-plan>/peer-network.toml --config peer-network.toml --bot-stopped.\n"
        "New peer: install only its own bundle through mitra-setup. Reuse the network Discord bot token.\n"
        "Restart every remaining peer and run mitra-doctor, then /servers doctor and /servers list.\n"
        "Removal is effective only after every remaining node receives the membership change.\n"
        "Keep this provisioning snapshot private. Store the original offline CA key separately for future additions.\n",
        encoding="utf-8")
    return sorted(configs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, default=Path("peer-bundles"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path, default=Path("peer-network.toml"))
    parser.add_argument("--bot-stopped", action="store_true")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--add", help="New node ID=reachable DNS hostname or IP")
    action.add_argument("--remove", help="Node ID to remove from every remaining trust list")
    action.add_argument("--apply", type=Path, help="Apply this node's prepared peer-network.toml; preserve local settings")
    args = parser.parse_args()
    try:
        if args.apply:
            if not args.bot_stopped:
                parser.error("Stop this node and pass --bot-stopped before applying membership")
            backup = apply_membership(args.apply, args.config)
            print(f"Membership saved; previous configuration: {backup.name}. Restart and run /servers doctor.")
            return
        if not args.output:
            parser.error("--output is required to prepare new bundles")
        members = prepare(args.bundle_root, args.output, add=args.add, remove=args.remove)
        print("Prepared membership: " + ", ".join(members))
        print("No running configuration changed. Follow README.txt in the output folder; keep all keys private.")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Membership preparation stopped ({type(exc).__name__}). Check complete bundles, certificates and the offline CA key.\n")


if __name__ == "__main__":
    main()

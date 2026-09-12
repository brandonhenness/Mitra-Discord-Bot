"""Create a repaired CA certificate using the original offline key.

Writes a new public certificate only. Existing bundles, keys, node identities,
configuration and monitoring data remain untouched.
"""
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from mitra_bot.init_peers import OPENSSL_CONFIG, find_openssl


def repair(bundle_root: Path, output: Path):
    root, output = bundle_root.resolve(), output.resolve()
    ca, key = root / "ca.crt", root / "OFFLINE-CA.key"
    if not ca.is_file() or not key.is_file():
        raise ValueError("The original provisioning folder must contain ca.crt and OFFLINE-CA.key.")
    if output.exists():
        raise ValueError("Choose a new output filename; existing files are never overwritten.")
    nodes = sorted(root.glob("*/node.crt"))
    if not nodes:
        raise ValueError("No node certificates found in the provisioning folder.")
    executable = find_openssl()
    def run(*args):
        try:
            return subprocess.check_output([executable, *map(str, args)], stderr=subprocess.PIPE, timeout=60)
        except subprocess.CalledProcessError:
            raise ValueError("Certificate repair or verification failed. Original files were not changed.") from None
    public = run("x509", "-in", ca, "-noout", "-pubkey")
    if public != run("pkey", "-in", key, "-pubout", "-passin", "pass:"):
        raise ValueError("OFFLINE-CA.key does not match this CA certificate.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mitra-ca-repair-", dir=output.parent) as temporary:
        temporary = Path(temporary)
        config = temporary / "openssl.cnf"
        config.write_text(OPENSSL_CONFIG, encoding="ascii")
        fixed = temporary / "ca.crt"
        # Keep the original CA identity, serial and validity dates. Replace all
        # extensions with one explicit set, removing duplicate constraints.
        run("x509", "-in", ca, "-clrext", "-signkey", key, "-passin", "pass:",
            "-preserve_dates", "-extfile", config, "-extensions", "ca", "-out", fixed)
        run("verify", "-check_ss_sig", "-CAfile", fixed, fixed)
        for node in nodes:
            run("verify", "-purpose", "sslserver", "-CAfile", fixed, node)
            run("verify", "-purpose", "sslclient", "-CAfile", fixed, node)
        if run("x509", "-in", fixed, "-noout", "-pubkey") != public:
            raise ValueError("CA identity changed unexpectedly.")
        for attribute in ("-subject", "-serial", "-dates"):
            if run("x509", "-in", fixed, "-noout", attribute) != run("x509", "-in", ca, "-noout", attribute):
                raise ValueError("CA identity or validity changed unexpectedly.")
        with output.open("xb") as stream:
            stream.write(fixed.read_bytes())
    return len(nodes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        count = repair(args.bundle_root, args.output)
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"Repair stopped: {exc}\n")
    print(f"Repaired CA saved to {args.output}; verified {count} existing node certificates.")
    print("Stop all peers, back up and replace ONLY their ca.crt files with this certificate, then restart.")
    print("Keep the offline key private. Node keys, peer configuration and databases do not change.")


if __name__ == "__main__":
    main()

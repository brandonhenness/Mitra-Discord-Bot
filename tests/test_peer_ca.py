import hashlib
import ssl
import subprocess

import pytest

from mitra_bot.init_peers import find_openssl, provision
from mitra_bot.repair_peer_ca import repair


@pytest.fixture
def bundles(tmp_path):
    try:
        find_openssl()
    except RuntimeError:
        pytest.skip("OpenSSL required")
    root = tmp_path / "bundles"
    provision(root, {"a": "127.0.0.1", "b": "127.0.0.1"})
    return root


def command(*args):
    return subprocess.check_output([find_openssl(), *map(str, args)], stderr=subprocess.PIPE)


def test_provision_ignores_ambient_request_extensions(tmp_path, monkeypatch):
    config = tmp_path / "ambient.cnf"
    config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=extra\n"
                      "[dn]\nCN=Ambient CA\n[extra]\nbasicConstraints=CA:FALSE\n"
                      "subjectAltName=DNS:unexpected.example\n")
    monkeypatch.setenv("OPENSSL_CONF", str(config))
    root = tmp_path / "fresh"
    provision(root, {"a": "127.0.0.1", "b": "127.0.0.1"})
    text = command("x509", "-in", root / "ca.crt", "-noout", "-text").decode()
    assert text.count("X509v3 Basic Constraints:") == 1
    assert "CA:TRUE" in text and "unexpected.example" not in text


def tlv(tag, value):
    length = len(value)
    encoded = bytes([length]) if length < 128 else length.to_bytes((length.bit_length()+7)//8, "big")
    if length >= 128:
        encoded = bytes([128+len(encoded)]) + encoded
    return bytes([tag]) + encoded + value


def fields(value):
    result = []
    while value:
        tag, size = value[0], value[1]
        offset = 2
        if size & 128:
            count = size & 127
            size = int.from_bytes(value[offset:offset+count], "big")
            offset += count
        result.append((tag, value[offset:offset+size]))
        value = value[offset+size:]
    return result


def test_repair_duplicate_constraint_preserves_existing_network(bundles, tmp_path):
    # Create a genuinely signed malformed CA with a duplicate extension, matching
    # the failure seen with older OpenSSL installations and default config files.
    ca = bundles / "ca.crt"
    outer = fields(fields(ssl.PEM_cert_to_DER_cert(ca.read_text()))[0][1])
    tbs = fields(outer[0][1])
    index = next(i for i, item in enumerate(tbs) if item[0] == 0xA3)
    extensions = fields(tbs[index][1])[0][1]
    constraint = next(tlv(tag, value) for tag, value in fields(extensions)
                      if b"\x06\x03\x55\x1d\x13" in value)
    tbs[index] = (0xA3, tlv(0x30, extensions + constraint))
    body = tlv(0x30, b"".join(tlv(tag, value) for tag, value in tbs))
    signing_input = tmp_path / "tbs.der"
    signing_input.write_bytes(body)
    signature = command("dgst", "-sha256", "-sign", bundles / "OFFLINE-CA.key", signing_input)
    malformed = tlv(0x30, body + tlv(*outer[1]) + tlv(0x03, b"\0" + signature))
    ca.write_text(ssl.DER_cert_to_PEM_cert(malformed))
    with pytest.raises(subprocess.CalledProcessError):
        command("verify", "-CAfile", ca, bundles / "a/node.crt")
    before = {p: hashlib.sha256(p.read_bytes()).digest() for p in bundles.rglob("*") if p.is_file()}
    fixed = tmp_path / "repaired-ca.crt"
    assert repair(bundles, fixed) == 2
    assert before == {p: hashlib.sha256(p.read_bytes()).digest() for p in before}
    for name in ("a", "b"):
        command("verify", "-CAfile", fixed, bundles / name / "node.crt")
    text = command("x509", "-in", fixed, "-noout", "-text").decode()
    assert text.count("X509v3 Basic Constraints:") == 1
    with pytest.raises(ValueError, match="never overwritten"):
        repair(bundles, fixed)


def test_repair_rejects_wrong_offline_key(bundles, tmp_path):
    other = tmp_path / "other"
    provision(other, {"c": "127.0.0.1", "d": "127.0.0.1"})
    (bundles / "OFFLINE-CA.key").write_bytes((other / "OFFLINE-CA.key").read_bytes())
    output = tmp_path / "repaired.crt"
    with pytest.raises(ValueError, match="does not match"):
        repair(bundles, output)
    assert not output.exists()

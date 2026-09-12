import subprocess

import pytest

from mitra_bot.release_tools import allowed_file


@pytest.mark.parametrize("filename", ["ca.crt", "node.crt", "OFFLINE-CA.key", "certificate.pem",
                                      "identity.pfx", "identity.p12", "node.csr", "ca.srl",
                                      "certificate.cer", "bot.log.1", "config.toml.before-membership-123"])
def test_private_deployment_files_are_ignored_and_excluded_from_bundle(filename):
    name = "mitra_bot/private/" + filename
    assert not allowed_file(name)
    result = subprocess.run(["git", "check-ignore", "--no-index", name], capture_output=True, text=True)
    assert result.returncode == 0


def test_documented_configuration_examples_remain_in_release():
    assert allowed_file("TERMS_OF_SERVICE.md")
    assert allowed_file("PRIVACY_POLICY.md")
    assert allowed_file(".env.example")
    assert allowed_file("config.example.toml")
    assert allowed_file("peer-network.example.toml")

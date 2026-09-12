"""Opt-in private mesh configuration, separate from Discord-editable settings."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mitra_bot.storage.config_store import tomllib


class Peer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$")
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=9843, ge=1, le=65535)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    # This peer may issue power commands to THIS machine.
    allow_power: bool = False


class PeerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    network_id: str = Field(default="", max_length=64)
    node_id: str = Field(default="local", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$")
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=9843, ge=1, le=65535)
    ca_file: str = ""
    cert_file: str = ""
    key_file: str = ""
    state_file: str = "peer-state.db"
    poll_seconds: int = Field(default=30, ge=5, le=3600)
    health_interval: int = Field(default=10, ge=5, le=300)
    health_timeout: int = Field(default=3, ge=1, le=30)
    health_failures: int = Field(default=3, ge=2, le=20)
    health_down_seconds: int = Field(default=30, ge=10, le=3600)
    health_startup_grace: int = Field(default=60, ge=30, le=3600)
    health_recovery_successes: int = Field(default=2, ge=2, le=20)
    health_cooldown: int = Field(default=300, ge=0, le=86400)
    health_raw_days: int = Field(default=7, ge=1, le=30)
    health_history_days: int = Field(default=90, ge=7, le=365)
    health_incident_days: int = Field(default=365, ge=7, le=730)
    state_owner: str = ""
    peers: list[Peer] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_mesh(self):
        if self.enabled and not all((self.network_id.strip(), self.ca_file, self.cert_file, self.key_file)):
            raise ValueError("Enabled mesh requires network_id, ca_file, cert_file and key_file")
        ids = [p.node_id for p in self.peers]
        fingerprints = [p.fingerprint for p in self.peers]
        if self.node_id in ids or len(ids) != len(set(ids)):
            raise ValueError("Peer node IDs must be unique and exclude this node")
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("Each peer requires its own certificate")
        if self.state_owner and self.state_owner not in [self.node_id, *ids]:
            raise ValueError("state_owner must name a configured node")
        return self

    @property
    def resolved_state_owner(self) -> str:
        return self.state_owner or min([self.node_id, *(p.node_id for p in self.peers)])


def load_peer_config() -> PeerConfig:
    explicit = os.getenv("MITRA_PEER_CONFIG_PATH")
    path = Path(explicit or "peer-network.toml")
    if not path.exists() and not explicit:
        return PeerConfig()
    cfg = PeerConfig.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
    # Paths belong to the configuration directory, not the process working directory.
    for name in ("ca_file", "cert_file", "key_file", "state_file"):
        value = getattr(cfg, name)
        if value:
            setattr(cfg, name, str((path.parent / value).resolve()))
    return cfg

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class CloudflareAPIEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool = False
    result: Any = None
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    messages: List[Dict[str, Any]] = Field(default_factory=list)


class CloudflareDNSRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    name: str
    content: str
    ttl: int = 1
    proxied: Optional[bool] = None


class CloudflareZone(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str

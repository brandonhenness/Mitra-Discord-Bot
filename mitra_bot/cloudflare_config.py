"""Local DNS assignments; credentials and peer configuration are never replicated."""
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CloudflareTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    node_id: str = Field(default="local", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    zone_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    record_ids: list[str] = Field(min_length=1, max_length=1000)
    token_env: str = Field(default="CLOUDFLARE_API_TOKEN", pattern=r"^[A-Z][A-Z0-9_]*$")
    oauth_profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def unique_records(self):
        if any(not value.strip() or "/" in value for value in self.record_ids):
            raise ValueError("Record IDs must be nonempty IDs")
        if len(set(self.record_ids)) != len(self.record_ids):
            raise ValueError("Duplicate DNS record assignment")
        return self


def select_targets(raw, node_id):
    targets = [CloudflareTarget.model_validate(value) for value in raw]
    names, records = set(), set()
    for target in targets:
        if target.name in names:
            raise ValueError("Cloudflare target names must be unique")
        names.add(target.name)
        for record in target.record_ids:
            key = (target.zone_id, record)
            if key in records:
                raise ValueError("A DNS record can only be assigned to one server")
            records.add(key)
    return [target for target in targets if target.node_id in {"local", node_id}]

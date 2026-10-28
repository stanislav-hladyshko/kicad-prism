"""Response models for the admin service-client routes (ADR scripting-and-mcp §3.2).

These describe the rows ``service_client_service`` already answers with:
``_public_client`` plus, on create and rotate, the one-time ``client_secret``.
``extra="allow"`` keeps a column a later migration adds on the wire.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict


class ServiceClientOut(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    client_id: str
    name: str
    role: str
    scopes: list[str]
    enabled: bool
    created_at: str
    updated_at: str
    last_used_at: Optional[str]


class ServiceClientWithSecret(ServiceClientOut):
    """A created or rotated client, whose secret is shown once."""

    client_secret: str


class ServiceClientList(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[ServiceClientOut]


class ServiceClientDeleted(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    ok: bool

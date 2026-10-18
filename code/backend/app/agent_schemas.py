"""Public Agent API schemas; internal archives and model outputs stay private."""
from typing import Literal

from pydantic import BaseModel, Field

from .inventory_schemas import Strict


Scope = Literal['inventory:read', 'history:read', 'images:read']
Presence = Literal['present', 'unseen', 'removed', 'uncertain']


class NewConnection(Strict):
    name: str = Field(min_length=1, max_length=80)
    cabinet_ids: list[str] = Field(min_length=1, max_length=100)
    scopes: list[Scope] = Field(default_factory=lambda: ['inventory:read'], min_length=1, max_length=3)
    days: int = Field(default=90, ge=1, le=365, strict=True)


class Pairing(Strict):
    code: str = Field(min_length=40, max_length=100)


class Location(BaseModel):
    id: str
    label: str


class ImageReference(BaseModel):
    id: str
    url: str


class FieldOrigin(BaseModel):
    source: str | None = None
    certainty: str | None = None


class AgentItem(BaseModel):
    id: str
    cabinet_id: str
    name: str
    name_kind: str
    category: str
    description: str | None = None
    brand: str | None = None
    variant: str | None = None
    presence_status: Presence
    identity_status: str
    action_status: str
    product_match_status: str
    stored_at: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    last_observed_at: str | None = None
    production_on: str | None = None
    expires_on: str | None = None
    quantity: int | None = None
    unit: str | None = None
    version: int
    updated_at: str
    location: Location | None
    field_meta: dict[str, FieldOrigin]
    images: list[ImageReference] | None = None


class Snapshot(BaseModel):
    cabinet_id: str
    inventory_revision: int
    generated_at: str
    inventory_known: bool
    completeness: str
    pending_operation_count: int
    recording_sessions: dict[str, int]
    source_live_status: Literal['unknown']
    items: list[AgentItem]
    next_cursor: str | None


class ItemDetail(BaseModel):
    item: AgentItem
    inventory_revision: int
    generated_at: str


class Cabinet(BaseModel):
    id: str
    name: str
    inventory_revision: int
    inventory_known: bool
    locations: list[Location]


class CabinetList(BaseModel):
    cabinets: list[Cabinet]


class HistoryEvent(BaseModel):
    kind: str
    media_window_ms: list[int] | None = None


class HistoryEntry(BaseModel):
    id: str
    action: str
    inventory_revision: int
    recorded_at: str
    source: str
    filmed_inventory_action: bool
    events: list[HistoryEvent]
    location_id: str | None


class ItemHistory(BaseModel):
    item_id: str
    inventory_revision: int
    history: list[HistoryEntry]
    next_cursor: str | None

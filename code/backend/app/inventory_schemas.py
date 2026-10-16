"""Only application-validated plans cross the inventory write boundary."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class FieldSource(Strict):
    source: Literal['vision', 'ocr', 'barcode', 'user', 'system', 'derived', 'unknown', 'speech', 'rag', 'web']
    certainty: Literal['recorded', 'estimated', 'unknown']
    evidence_refs: list[str] = Field(default_factory=list)
    extractor: str | None = None
    supporting_sources: list[str] = Field(default_factory=list)


class ItemValues(Strict):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(default='未知', min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=1000)
    brand: str | None = Field(default=None, max_length=120)
    variant: str | None = Field(default=None, max_length=180)
    quantity: int | None = Field(default=None, ge=1, strict=True)
    unit: str | None = None
    location_id: str | None = None
    identity_status: Literal['candidate', 'confirmed', 'group_uncertain'] = 'candidate'
    presence_status: Literal['present', 'unseen', 'removed', 'uncertain'] = 'present'
    action_status: Literal['confirmed', 'pending'] = 'confirmed'
    product_match_status: Literal['unknown', 'category_only', 'candidate', 'verified'] = 'unknown'
    product_id: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    stored_at: str | None = None
    production_on: date | None = None
    expires_on: date | None = None
    shelf_life_raw: str | None = None
    opened_at: str | None = None
    review_at: str | None = None
    review_reason: str | None = None
    notes: str | None = None
    field_meta: dict[str, FieldSource] = Field(default_factory=dict)

    @model_validator(mode='after')
    def valid_business_fields(self):
        from .database import timestamp
        for key in ('first_seen_at', 'last_seen_at', 'stored_at', 'opened_at', 'review_at'):
            value = getattr(self, key)
            if value is not None:
                setattr(self, key, timestamp(value))
        if self.quantity is not None and not self.unit:
            raise ValueError('Known quantity requires a unit')
        for key in ('production_on', 'expires_on'):
            if getattr(self, key) is not None:
                meta = self.field_meta.get(key)
                if not meta or meta.source not in ('ocr', 'barcode', 'user', 'derived', 'speech') or meta.certainty != 'recorded':
                    raise ValueError('Actual label dates require recorded label/user provenance')
                if meta.source == 'derived' and not meta.evidence_refs:
                    raise ValueError('Derived dates require explicit input evidence')
        if self.production_on and self.expires_on and self.production_on > self.expires_on:
            raise ValueError('Expiry precedes production date')
        return self


class Decision(Strict):
    candidate_id: str = Field(min_length=1)
    action: Literal['add', 'remove', 'return', 'update', 'keep']
    item_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)
    evidence_refs: list[str] = Field(min_length=1)
    patch: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def identity_required(self):
        if self.action == 'add' and (self.item_id or self.expected_version is not None):
            raise ValueError('A new candidate cannot already have an item identity')
        if self.action != 'add' and (not self.item_id or self.expected_version is None):
            raise ValueError('Existing items require item_id and expected_version')
        return self


class CommitPlan(Strict):
    operation_id: str
    run_id: str
    base_revision: int = Field(ge=0, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=200)
    reviewed_by: str = Field(min_length=1, max_length=120)
    coarse_only: bool = False
    decisions: list[Decision] = Field(max_length=100)

    @model_validator(mode='after')
    def unique_items(self):
        candidates = [d.candidate_id for d in self.decisions]
        items = [d.item_id for d in self.decisions if d.item_id]
        if len(set(candidates)) != len(candidates) or len(set(items)) != len(items):
            raise ValueError('A candidate/item can only have one net decision per commit')
        return self

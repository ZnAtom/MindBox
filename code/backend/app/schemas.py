import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Support = Literal['high', 'medium', 'low']


class View(BaseModel):
    model_config = ConfigDict(extra='forbid')
    frame_id: str
    bbox: list[float] = Field(min_length=4, max_length=4)

    @field_validator('bbox')
    @classmethod
    def valid_box(cls, value):
        if not all(math.isfinite(v) and 0 <= v <= 1000 for v in value):
            raise ValueError('bbox must contain finite 0..1000 coordinates')
        if value[2] <= value[0] or value[3] <= value[1]:
            raise ValueError('bbox must use xyxy order with positive area')
        return value


class Candidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate_id: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(max_length=60)
    description: str = Field(max_length=600)
    views: list[View] = Field(min_length=1, max_length=16)


class Detection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    objects: list[Candidate] = Field(max_length=16)
    uncertainties: list[str] = Field(max_length=20)


class VerifiedItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate_id: str
    target_kind: Literal['movable_item', 'background_structure', 'uncertain']
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(max_length=60)
    description: str = Field(max_length=600)
    brand: str | None
    variant: str | None
    readable_text: list[str] = Field(max_length=30)
    category_support: Support
    identity_support: Support
    support_reason: str = Field(max_length=600)
    localization_status: Literal['usable', 'partial', 'multiple_objects', 'wrong_target', 'uncertain']
    evidence_refs: list[str] = Field(min_length=1, max_length=12)


class Verification(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[VerifiedItem] = Field(max_length=16)


class NormalizedItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate_id: str
    category: Literal['饮品', '食品', '日用品', '工具', '电子用品', '其他', '未知']
    catalog_refs: list[str] = Field(max_length=5)
    rationale: str = Field(max_length=400)


class Normalization(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[NormalizedItem] = Field(max_length=16)


class Event(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['add', 'remove', 'return', 'move', 'take_and_return', 'uncertain']
    candidate_ids: list[str] = Field(min_length=1, max_length=16)
    before_frame_id: str
    after_frame_id: str
    explanation: str = Field(max_length=500)
    support: Support


class Events(BaseModel):
    model_config = ConfigDict(extra='forbid')
    events: list[Event] = Field(max_length=24)
    uncertainties: list[str] = Field(max_length=20)


def pixel_box(bbox, width, height, padding=0.035):
    view = View(frame_id='validation', bbox=bbox)
    x1, y1, x2, y2 = view.bbox
    dx, dy = (x2 - x1) * padding, (y2 - y1) * padding
    box = (max(0, math.floor((x1 - dx) * width / 1000)),
           max(0, math.floor((y1 - dy) * height / 1000)),
           min(width, math.ceil((x2 + dx) * width / 1000)),
           min(height, math.ceil((y2 + dy) * height / 1000)))
    if box[2] - box[0] < 12 or box[3] - box[1] < 12:
        raise ValueError('Crop is too small to inspect')
    return box

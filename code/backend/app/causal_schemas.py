"""Observations from frames already delivered to the causal replay worker."""
from typing import Literal

from pydantic import Field, model_validator

from .action_schemas import Strict
from .schemas import Support, View


class TrackPoint(Strict):
    frame_id: str
    region: Literal['inside', 'outside', 'frame_edge', 'out_of_frame', 'occluded', 'unknown']
    bbox: list[float] | None
    at_rest: bool
    drawer_phase: Literal['opening', 'open', 'closing', 'closed', 'unknown'] | None = None

    @model_validator(mode='after')
    def validate_bbox(self):
        if self.bbox is not None:
            View(frame_id=self.frame_id, bbox=self.bbox)
        if self.region in ('inside', 'outside', 'frame_edge') and self.bbox is None:
            raise ValueError('Visible objects need a bounding box')
        if self.region in ('out_of_frame', 'occluded', 'unknown') and self.bbox is not None:
            raise ValueError('Unobserved objects cannot have an invented box')
        if self.at_rest and self.region != 'inside':
            raise ValueError('at_rest means supported inside the drawer')
        return self


class CausalTrack(Strict):
    track_id: str = Field(min_length=1, max_length=60, pattern=r'^[A-Za-z0-9_-]+$')
    name: str = Field(max_length=120)
    category: str = Field(max_length=60)
    description: str = Field(max_length=600)
    target_kind: Literal['movable_item', 'background_structure', 'uncertain']
    object_support: Support
    association: Literal['supported', 'uncertain']
    visibility_fraction: float | None = Field(ge=0, le=1)
    occlusion_level: Literal['none', 'mild', 'moderate', 'severe', 'unknown']
    points: list[TrackPoint] = Field(min_length=1, max_length=32)


class CausalObservation(Strict):
    drawer_phase: Literal['opening', 'open', 'closing', 'closed', 'unknown']
    drawer_only: bool
    tracks: list[CausalTrack] = Field(max_length=12)
    uncertainties: list[str] = Field(max_length=12)

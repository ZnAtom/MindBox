from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import Support, View


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Phase(Strict):
    phase: Literal['opening', 'open', 'closing', 'closed', 'unknown']
    start_frame_id: str
    end_frame_id: str
    interior_visible: bool


class Window(Strict):
    window_id: str = Field(min_length=1, max_length=40)
    start_frame_id: str
    end_frame_id: str
    reason: str = Field(max_length=500)


class ScenePlan(Strict):
    phases: list[Phase] = Field(min_length=1, max_length=20)
    before_snapshot: View | None
    after_snapshot: View | None
    windows: list[Window] = Field(max_length=16)
    uncertainties: list[str] = Field(max_length=20)


class ObjectState(Strict):
    location: Literal['inside', 'outside', 'absent', 'occluded', 'unknown']
    frame_ids: list[str] = Field(max_length=4)
    interior_clear: bool
    at_rest: bool
    description: str = Field(max_length=500)


class Transition(Strict):
    candidate_id: str
    before: ObjectState
    after: ObjectState
    identity: Literal['supported', 'uncertain']
    evidence: Literal['crossing', 'comparable_states', 'occlusion_only', 'none']
    drawer_relative_motion: bool
    support: Support


class StateComparison(Strict):
    objects: list[Transition] = Field(max_length=16)
    drawer_only: bool
    uncertainties: list[str] = Field(max_length=20)

"""The standard event in the middle.

Step 1 turns the orchestrator's events into these. Step 2 turns these into what
the frontend receives. The orchestrator's format is not ours and will change;
this one is ours and should not. When Flowise renames something, only step 1
moves.

Adding a field here is cheap. Reaching into an orchestrator-shaped dict from
step 2 is what we are avoiding.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CanonicalEvent:
    """Base for everything the middle layer speaks."""


@dataclass
class RunStarted(CanonicalEvent):
    pass


@dataclass
class StepStarted(CanonicalEvent):
    step_id: str
    label: str


@dataclass
class StepFinished(CanonicalEvent):
    step_id: str
    label: str


@dataclass
class Token(CanonicalEvent):
    text: str


@dataclass
class ToolStarted(CanonicalEvent):
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolFinished(CanonicalEvent):
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    output: str | None = None
    # True when the stream ended before this call's output ever arrived.
    incomplete: bool = False


@dataclass
class Usage(CanonicalEvent):
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass
class FinalResult(CanonicalEvent):
    """The orchestrator's finished answer object, before our final-payload hook."""

    payload: dict[str, Any]


@dataclass
class Conversation(CanonicalEvent):
    """Ids the frontend needs to continue a multi-turn conversation."""

    chat_id: str | None
    message_id: str | None
    session_id: str | None


@dataclass
class Dropped(CanonicalEvent):
    """A rule looked at an upstream event and decided it carries nothing for us.

    Explicit, so that "we chose to drop this" is distinguishable from "no rule
    matched", which is always an error.
    """

    reason: str


@dataclass
class RunFinished(CanonicalEvent):
    """Always sent, on every exit path.

    reason is one of: "completed", "upstream_error", "client_disconnected".
    """

    reason: str
    detail: str | None = None

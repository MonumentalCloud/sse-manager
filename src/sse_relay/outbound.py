"""Step 2: our standard events become what the frontend receives.

This is the format we control and the one that should stay stable. Adding a
frontend event is one line here.

A rule returns `WireEvent`s. The endpoint is what writes them to the socket.
"""

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from . import events as ev
from .rules import Rule


@dataclass
class WireEvent:
    """One SSE event as the frontend will see it."""

    name: str
    data: dict[str, Any]


class Emit(Rule):
    """The one-line case: one standard event becomes one frontend event.

        Emit("token", text=lambda e: e.text)
    """

    # `name` is positional-only: several events have a field called `name` too.
    def __init__(self, name: str, /, **fields: Callable[[Any], Any]):
        self.name = name
        self.fields = fields

    def feed(self, value: Any) -> Iterable[Any]:
        return (WireEvent(self.name, {k: f(value) for k, f in self.fields.items()}),)


class Silent(Rule):
    """Standard events the frontend has no use for."""

    def feed(self, value: Any) -> Iterable[Any]:
        return ()


class FinalAnswerRule(Rule):
    """The final answer, after the payload hook has shaped it.

    The hook is imported rather than inlined so that the shaping stays one
    function in one file — a different job from the streaming rules.
    """

    def feed(self, value: Any) -> Iterable[Any]:
        from .final_payload import shape_final_payload

        return (WireEvent("final", shape_final_payload(value.payload)),)


def build_rules() -> dict[type, Rule]:
    """One fresh set per request, same reason as step 1."""
    return {
        ev.RunStarted: Emit("run_started"),
        ev.StepStarted: Emit("step_started", id=lambda e: e.step_id, label=lambda e: e.label),
        ev.StepFinished: Emit("step_finished", id=lambda e: e.step_id, label=lambda e: e.label),
        ev.Token: Emit("token", text=lambda e: e.text),
        ev.ToolStarted: Emit("tool_started", name=lambda e: e.name, arguments=lambda e: e.arguments),
        ev.ToolFinished: Emit(
            "tool_finished",
            name=lambda e: e.name,
            arguments=lambda e: e.arguments,
            output=lambda e: e.output,
            incomplete=lambda e: e.incomplete,
        ),
        ev.Conversation: Emit(
            "conversation",
            chat_id=lambda e: e.chat_id,
            message_id=lambda e: e.message_id,
            session_id=lambda e: e.session_id,
        ),
        ev.FinalResult: FinalAnswerRule(),
        ev.RunFinished: Emit("done", reason=lambda e: e.reason, detail=lambda e: e.detail),
        # Counted in the summary log line; not the frontend's business.
        ev.Usage: Silent(),
        ev.Dropped: Silent(),
    }

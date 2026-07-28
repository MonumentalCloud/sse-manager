"""Step 2: our standard events become what the frontend receives.

The target today is the monimo SSE protocol (agent architecture document, 4.1).
It will change, so the mapping is written as data: the table at the bottom of
this file says, for each of our standard events, which frontend events come out
and what goes in each field. Re-pointing a field is an edit to one line there.

Only the envelope is not here — trace_id, seq and ts are stamped in protocol.py,
so a rule never has to think about them.

Field values are written with four small readers:

    F("text")            a field on our standard event
    R("message_id")      a value the run is carrying (see RunContext)
    S("channel")         a value from config.toml
    Fn(lambda e, run: …) anything else

Three protocol events have no entry: `plan.created`, `interaction.required` and
`interaction.resolved`. The orchestrator emits nothing corresponding to a plan
or to a human-in-the-loop pause, and inventing them would be fake data. Each
becomes one line here once it does.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import events as ev
from .config import Settings
from .rules import Rule


@dataclass
class WireEvent:
    """One event's name and data, before the envelope goes on."""

    name: str
    data: dict[str, Any]


@dataclass
class RunContext:
    """What the whole run knows about itself.

    Several protocol fields are not per-event facts: `message_id` repeats on
    every `message.delta` and again on `run.end`, and `elapsed_ms` is the gap
    between two events. That state lives here rather than in the rules.
    """

    settings: Settings
    session_id: str | None
    user_turn_id: str | None
    started: float = field(default_factory=time.monotonic)

    # The protocol's own message id. Deliberately local to the run: the
    # orchestrator's chatMessageId only arrives at the very end, and
    # `message.delta` needs an id from the first token onward.
    message_id: str = "msg:1"

    chat_id: str | None = None
    orchestrator_message_id: str | None = None

    agent_calls: int = 0

    # Calls announced but not yet finished, oldest first, keyed by name. The
    # orchestrator sends every output at the end, so several calls are open at
    # once and a name can repeat — `describe_tools` is called twice in a normal
    # run. Without the queue every agent.end reports the last call's step_seq.
    pending: dict[str, deque[tuple[int, float]]] = field(default_factory=dict)

    # Set by the bookkeeping below, read by the shape being built right now.
    step_seq: int = 0
    last_elapsed_ms: int | None = None

    final_payload: dict[str, Any] | None = None

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


# ---------------------------------------------------------------------------
# The four field readers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class F:
    """A field on our standard event."""

    name: str

    def __call__(self, event: Any, run: RunContext) -> Any:
        return getattr(event, self.name)


@dataclass(frozen=True)
class R:
    """A value the run is carrying."""

    name: str

    def __call__(self, event: Any, run: RunContext) -> Any:
        return getattr(run, self.name)


@dataclass(frozen=True)
class S:
    """A value from config.toml."""

    name: str

    def __call__(self, event: Any, run: RunContext) -> Any:
        return getattr(run.settings, self.name)


@dataclass(frozen=True)
class Fn:
    """Anything the three above cannot say."""

    call: Callable[[Any, RunContext], Any]

    def __call__(self, event: Any, run: RunContext) -> Any:
        return self.call(event, run)


Reader = F | R | S | Fn


@dataclass(frozen=True)
class Shape:
    """One frontend event: its name, and where each of its fields comes from.

    A plain value is used as-is, so constants need no wrapper.
    """

    name: str
    fields: dict[str, Any]

    def __init__(self, name: str, /, **fields: Any):
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "fields", fields)

    def build(self, event: Any, run: RunContext) -> WireEvent:
        return WireEvent(
            self.name,
            {key: value(event, run) if callable(value) else value for key, value in self.fields.items()},
        )


@dataclass(frozen=True)
class Mapping:
    """What one standard event turns into.

    `before` is bookkeeping that has to happen first — bumping a counter,
    stopping a timer, remembering a value for later. It runs once, before the
    shapes are built, and returning nothing is normal.
    """

    shapes: tuple[Shape, ...] = ()
    before: Callable[[Any, RunContext], None] | None = None


class TableRule(Rule):
    """Runs one table entry. The only rule class step 2 needs."""

    def __init__(self, mapping: Mapping, run: RunContext):
        self.mapping = mapping
        self.run = run

    def feed(self, value: Any) -> Iterable[Any]:
        if self.mapping.before is not None:
            self.mapping.before(value, self.run)
        return [shape.build(value, self.run) for shape in self.mapping.shapes]


# ---------------------------------------------------------------------------
# Bookkeeping used by the table
# ---------------------------------------------------------------------------


def _start_tool(event: Any, run: RunContext) -> None:
    run.agent_calls += 1
    run.step_seq = run.agent_calls
    run.pending.setdefault(event.name, deque()).append((run.agent_calls, time.monotonic()))


def _finish_tool(event: Any, run: RunContext) -> None:
    """Match this finish to the oldest unfinished call of the same name."""
    queue = run.pending.get(event.name)
    if queue:
        step_seq, started = queue.popleft()
        run.step_seq = step_seq
        run.last_elapsed_ms = int((time.monotonic() - started) * 1000)
    else:
        # An output for a call we never saw announced. Report it rather than
        # attributing it to whichever call happens to be current.
        run.step_seq = 0
        run.last_elapsed_ms = None


def _remember_conversation(event: Any, run: RunContext) -> None:
    run.chat_id = event.chat_id
    run.orchestrator_message_id = event.message_id
    if event.session_id:
        run.session_id = event.session_id


def _hold_final_payload(event: Any, run: RunContext) -> None:
    """The final answer arrives before the run ends but belongs on `run.end`.

    The hook runs now, while the value is in hand; its output waits.
    """
    from .final_payload import shape_final_payload

    run.final_payload = shape_final_payload(event.payload)


_RUN_STATUS = {"completed": "completed", "upstream_error": "error", "client_disconnected": "cancelled"}


def _usage(event: Any, run: RunContext) -> dict[str, Any]:
    # `retries` is null because the orchestrator does not report retries.
    # Inventing a number would be fake data.
    return {"elapsed_ms": run.elapsed_ms(), "agent_calls": run.agent_calls, "retries": None}


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

TABLE: dict[type, Mapping] = {
    ev.RunStarted: Mapping((
        Shape("run.start", session_id=R("session_id"), user_turn_id=R("user_turn_id"), channel=S("channel")),
    )),

    ev.StepStarted: Mapping((
        Shape("node.status", node=F("step_id"), phase="node", state="running", label=F("label")),
    )),

    ev.StepFinished: Mapping((
        Shape("node.status", node=F("step_id"), phase="node", state="completed", label=F("label")),
    )),

    ev.Token: Mapping((
        Shape("message.delta", message_id=R("message_id"), text=F("text")),
    )),

    # A sub-agent sits behind one MCP node, so the node is the `agent` and the
    # sub-agent is the `skill`. Which is which is config, not a hardcoded name.
    ev.ToolStarted: Mapping(
        (
            Shape("agent.start", step_seq=R("step_seq"), agent=S("mcp_agent_name"), skill=F("name")),
            # The frontend needs something to render while the sub-agent is opaque.
            Shape("node.status", node=S("mcp_agent_name"), phase="tool_call", state="running", label=F("name")),
        ),
        before=_start_tool,
    ),

    # `trajectory_ref` is null until Langfuse lands; it is their id to create.
    ev.ToolFinished: Mapping(
        (
            Shape(
                "agent.end",
                step_seq=R("step_seq"),
                agent=S("mcp_agent_name"),
                skill=F("name"),
                status=Fn(lambda e, run: "incomplete" if e.incomplete else "ok"),
                elapsed_ms=R("last_elapsed_ms"),
                retries=None,
                trajectory_ref=None,
                output=F("output"),
            ),
            Shape(
                "node.status",
                node=S("mcp_agent_name"),
                phase="tool_call",
                state=Fn(lambda e, run: "incomplete" if e.incomplete else "completed"),
                label=F("name"),
            ),
        ),
        before=_finish_tool,
    ),

    # Held for run.end rather than sent on their own.
    ev.Conversation: Mapping(before=_remember_conversation),
    ev.FinalResult: Mapping(before=_hold_final_payload),

    ev.RunFinished: Mapping((
        Shape(
            "run.end",
            status=Fn(lambda e, run: _RUN_STATUS.get(e.reason, e.reason)),
            message_id=R("message_id"),
            usage=Fn(_usage),
            chat_id=R("chat_id"),
            orchestrator_message_id=R("orchestrator_message_id"),
            # Our one extension to the documented shape: the final-payload hook's
            # output has to reach the frontend, and the document has no event for it.
            result=R("final_payload"),
        ),
    )),

    # Counted in the summary log line; not the frontend's business.
    ev.Usage: Mapping(),
    ev.Dropped: Mapping(),
}


def error_event(settings: Settings, detail: str | None) -> WireEvent:
    """Sent just before `run.end` when the run stopped because of a failure."""
    return WireEvent(
        "error",
        {
            "code": "UPSTREAM_ERROR",
            "stage": "agent_call",
            "agent": settings.mcp_agent_name,
            "retriable": True,
            "message": detail,
            "trace_ref": None,
        },
    )


def build_rules(run: RunContext) -> dict[type, Rule]:
    """One fresh set per request; state never crosses requests."""
    return {kind: TableRule(mapping, run) for kind, mapping in TABLE.items()}

"""Step 1: the orchestrator's events become our standard events.

This is the file that moves when Flowise changes. Everything downstream is
insulated from it.

The table maps an upstream event name to a factory that produces a fresh rule
per request, because stateful rules must not share memory between requests.
Adding an event type is one line. If an event arrives with no entry here, the
request stops with an error — see engine.py.

Every name in this table was observed on the wire; see docs/orchestrator-events.md.
"""

from typing import Any, Callable, Iterable

from .events import (
    Conversation,
    Dropped,
    FinalResult,
    RunStarted,
    StepFinished,
    StepStarted,
    Token,
    ToolFinished,
    ToolStarted,
    Usage,
)
from .rules import Ignore, Map, Rule

# ---------------------------------------------------------------------------
# Rules that need memory
# ---------------------------------------------------------------------------


class FlowStatusRule(Rule):
    """`agentFlowEvent` is "INPROGRESS" then "FINISHED" for the run as a whole.

    The trailing FINISHED is dropped: the run's real ending is decided by the
    endpoint, which must send exactly one closing event on all three exit paths.
    A second "the run finished" from here would race it.
    """

    def feed(self, value: Any) -> Iterable[Any]:
        if value == "INPROGRESS":
            return (RunStarted(),)
        if value == "FINISHED":
            return (Dropped(reason="run end is decided by the endpoint, not upstream"),)
        raise ValueError(f"unknown agentFlowEvent status {value!r}")


class NodeStatusRule(Rule):
    """`nextAgentFlow` carries its direction in a status field rather than the name."""

    def feed(self, value: Any) -> Iterable[Any]:
        status = value["status"]
        node_id, label = value["nodeId"], value["nodeLabel"]
        if status == "INPROGRESS":
            return (StepStarted(step_id=node_id, label=label),)
        if status == "FINISHED":
            return (StepFinished(step_id=node_id, label=label),)
        raise ValueError(f"unknown nextAgentFlow status {status!r}")


class AnswerTextRule(Rule):
    """Tokens, with two problems fixed.

    First, 73% of them are empty strings — forwarding those costs a frontend
    event each and says nothing.

    Second, the answer begins with a literal `\\n\\nRESULT: ` marker, and it arrives
    split across the first several tokens. Removing it means holding the opening
    text back until enough has accumulated to tell whether the marker is there —
    a later piece deciding how an earlier one is rewritten. This is the case that
    the feed/flush shape exists for.
    """

    MARKER = "\n\nRESULT: "

    def __init__(self) -> None:
        self.held = ""
        self.opening_done = False

    def feed(self, value: Any) -> Iterable[Any]:
        if not isinstance(value, str):
            raise ValueError(f"token payload should be a string, got {type(value).__name__}")
        if value == "":
            return ()
        if self.opening_done:
            return (Token(text=value),)

        self.held += value
        if self.held.startswith(self.MARKER):
            self.opening_done = True
            rest = self.held[len(self.MARKER) :]
            self.held = ""
            return (Token(text=rest),) if rest else ()

        # Still short enough that the marker could yet complete: keep waiting.
        if self.MARKER.startswith(self.held):
            return ()

        # It is definitely not the marker, so release everything held.
        self.opening_done = True
        text, self.held = self.held, ""
        return (Token(text=text),)

    def flush(self) -> Iterable[Any]:
        if not self.held:
            return ()
        text, self.held = self.held, ""
        self.opening_done = True
        return (Token(text=text),)


class ToolCallRule(Rule):
    """Pair each tool call with its output.

    The orchestrator announces calls one at a time as `calledTools`, always with
    an empty `toolOutput`, and then sends every output at once in a single
    `usedTools` array at the very end. So a call's result is known long after the
    call itself, and the rule has to remember which calls it has already seen.

    The two events disagree about what their array means, which is not documented
    anywhere: each `calledTools` carries only the call just made (and a trailing
    empty one), while the single `usedTools` carries every call made so far. So
    `calledTools` entries are appended and `usedTools` entries are matched against
    what has already been announced.

    The real sub-agent name is nested inside `toolInput.tool_name` when the
    orchestrator goes through the MCP `invoke_tool` wrapper; `tool` alone would
    report every sub-agent as "invoke_tool".
    """

    def __init__(self) -> None:
        self.announced: list[tuple[str, dict[str, Any]]] = []
        self.finished = 0

    @staticmethod
    def _identify(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        name = entry["tool"]
        arguments = entry.get("toolInput") or {}
        if name == "invoke_tool" and isinstance(arguments, dict) and "tool_name" in arguments:
            return arguments["tool_name"], arguments.get("arguments") or {}
        return name, arguments if isinstance(arguments, dict) else {"value": arguments}

    def called(self, value: Any) -> Iterable[Any]:
        out = []
        for entry in value:
            name, arguments = self._identify(entry)
            self.announced.append((name, arguments))
            out.append(ToolStarted(name=name, arguments=arguments))
        return out

    def used(self, value: Any) -> Iterable[Any]:
        out = []
        for entry in value[self.finished :]:
            name, arguments = self._identify(entry)
            out.append(ToolFinished(name=name, arguments=arguments, output=entry.get("toolOutput") or None))
        self.finished = len(value)
        return out

    def feed(self, value: Any) -> Iterable[Any]:  # pragma: no cover - see below
        raise AssertionError("ToolCallRule is bound to two upstream names; use called/used")

    def flush(self) -> Iterable[Any]:
        """Any call whose output never arrived still has to be closed out.

        Otherwise a dropped connection leaves the frontend showing a tool as
        running forever.
        """
        return [
            ToolFinished(name=name, arguments=arguments, output=None, incomplete=True)
            for name, arguments in self.announced[self.finished :]
        ]


class _Bound(Rule):
    """Points one upstream event name at one method of a shared rule object."""

    def __init__(self, owner: Rule, method: Callable[[Any], Iterable[Any]]):
        self.owner = owner
        self.method = method

    def feed(self, value: Any) -> Iterable[Any]:
        return self.method(value)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def build_rules() -> dict[str, Rule]:
    """One fresh set of rules per request. State never crosses requests."""
    tools = ToolCallRule()

    return {
        "agentFlowEvent": FlowStatusRule(),
        "nextAgentFlow": NodeStatusRule(),
        "token": AnswerTextRule(),
        "calledTools": _Bound(tools, tools.called),
        "usedTools": _Bound(tools, tools.used),
        "usageMetadata": Map(
            Usage,
            input_tokens=".input_tokens",
            output_tokens=".output_tokens",
            total_tokens=".total_tokens",
        ),
        "result": Map(FinalResult, payload="."),
        "metadata": Map(
            Conversation,
            chat_id=".chatId",
            message_id=".chatMessageId",
            session_id=".sessionId",
        ),
        "end": Ignore("upstream [DONE]; the endpoint sends the real closing event"),
        # Large duplicate node dump — result already carries it, and the frontend
        # has no use for the full prompt text of every node.
        "agentFlowExecutedData": Ignore("duplicated by the result payload"),
    }

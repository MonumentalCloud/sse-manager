"""The one hook that shapes the final answer object before it goes out.

Its own file on purpose. This is a different job from the per-event streaming
rules, and mixing the two makes both harder to follow.

It is also its own function on purpose: when Langfuse lands, this is one of the
things we will want traced, and tracing it should be a line above the `def`
rather than a refactor.
"""

from typing import Any

from .config import RelayError
from .inbound import identify_tool

# The orchestrator's answer text opens with this literal marker.
RESULT_MARKER = "\n\nRESULT: "


def _flatten_used_tools(raw: Any) -> list[dict[str, Any]]:
    """`result.usedTools` is a list of lists, unlike the `usedTools` event.

    Undocumented, and the two are otherwise identical in shape. Flatten one
    level; anything else is a shape we have not seen and should stop on.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RelayError(f"result.usedTools should be a list, got {type(raw).__name__}")

    flat: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, list):
            flat.extend(entry)
        elif isinstance(entry, dict):
            flat.append(entry)
        else:
            raise RelayError(f"unexpected result.usedTools entry: {type(entry).__name__}")
    return flat


def shape_final_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn the orchestrator's result object into the frontend's final event.

    Deliberately narrow: the orchestrator's result also carries
    `agentFlowExecutedData`, which is a full dump of every node including entire
    system prompts. That is hundreds of kilobytes the frontend has no use for,
    and it has already been streamed as step and tool events.
    """
    text = payload.get("text", "")
    if text.startswith(RESULT_MARKER):
        text = text[len(RESULT_MARKER) :]

    return {
        "text": text.strip(),
        "question": payload.get("question"),
        "chat_id": payload.get("chatId"),
        "message_id": payload.get("chatMessageId"),
        "session_id": payload.get("sessionId"),
        # Named through the same helper the streamed events use, so the final
        # object and the stream agree on what each sub-agent was called.
        "tools_used": [
            {"name": name, "arguments": arguments}
            for name, arguments in (identify_tool(e) for e in _flatten_used_tools(payload.get("usedTools")))
        ],
    }

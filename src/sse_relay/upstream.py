"""The call to the orchestrator, and reading what it sends back.

Its own function for the same reason as everything else: this is the first thing
we will want a Langfuse decorator on.

Two things here exist because of what job zero found, not because of theory:

- Chunks split mid-line *and* mid-UTF-8-character, so bytes accumulate in a
  buffer and are only decoded once a complete block is in hand. Decoding each
  chunk on its own corrupts Korean text.
- There is no read timeout. A sub-agent thinking for ninety seconds sends
  nothing during that time and looks exactly like a dead connection; a timeout
  short enough to be useful would kill healthy streams.
"""

import json
from typing import Any, AsyncIterator

import httpx

from .config import RelayError, Settings
from .telemetry import RequestLog

# One `data:` block carrying the full node dump ran past 400 KB in job zero, so
# the buffer has to tolerate blocks far larger than any default line limit.
MAX_BLOCK_BYTES = 8 * 1024 * 1024


def build_request(
    settings: Settings,
    question: str,
    request_id: str,
    chat_id: str | None,
    user_id: str | None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
        # Survives Langfuse: it is what lines their logs up with ours.
        "x-genos-trace-id": request_id,
    }
    body: dict[str, Any] = {"question": question, "stream": True}
    if chat_id:
        body["chatId"] = chat_id
        headers["x-genos-session-id"] = chat_id
    if user_id:
        headers["x-genos-user-id"] = user_id
    return settings.run_url, headers, body


async def stream_orchestrator(
    settings: Settings,
    request_log: RequestLog,
    question: str,
    request_id: str,
    chat_id: str | None = None,
    user_id: str | None = None,
) -> AsyncIterator[tuple[str, Any]]:
    """Yield `(event_name, payload)` as the orchestrator produces them."""
    url, headers, body = build_request(settings, question, request_id, chat_id, user_id)

    timeout = httpx.Timeout(
        connect=settings.connect_timeout,
        read=None,  # see the module docstring
        write=settings.write_timeout,
        pool=settings.connect_timeout,
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=body, headers=headers) as response:
            if response.status_code != 200:
                detail = (await response.aread()).decode("utf-8", errors="replace")
                raise RelayError(f"orchestrator returned {response.status_code}: {detail[:500]}")

            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > MAX_BLOCK_BYTES:
                    raise RelayError(f"upstream block exceeded {MAX_BLOCK_BYTES} bytes without a separator")
                while b"\n\n" in buffer:
                    head, _, rest = buffer.partition(b"\n\n")
                    buffer = bytearray(rest)
                    parsed = _parse_block(head.decode("utf-8"), request_log)
                    if parsed is not None:
                        yield parsed

            if buffer.strip():
                parsed = _parse_block(buffer.decode("utf-8"), request_log)
                if parsed is not None:
                    yield parsed


def _parse_block(block: str, request_log: RequestLog) -> tuple[str, Any] | None:
    """One `data: {...}` block becomes `(event, data)`.

    A block that cannot be parsed raises. Skipping it quietly is exactly the bug
    that would be found weeks later.
    """
    block = block.strip()
    if not block:
        return None
    request_log.raw_line(block)

    if block.startswith("data:"):
        payload = block[len("data:") :].strip()
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RelayError(f"could not parse upstream data block: {exc}: {payload[:300]!r}") from exc
        if not isinstance(decoded, dict) or "event" not in decoded:
            raise RelayError(f"upstream data block has no event field: {payload[:300]!r}")
        return decoded["event"], decoded.get("data")

    if block.startswith("result:"):
        # The GenOS gateway appends one bare `result:` line after `[DONE]`, a
        # duplicate of the `result` event we have already handled.
        return None

    raise RelayError(f"unrecognised upstream line prefix: {block[:200]!r}")

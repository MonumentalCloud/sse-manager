"""Interim logging. One file, deliberately thin, easy to delete.

Real telemetry is coming as Langfuse in decorator style. Nothing here invents a
trace or span id, because Langfuse creates and nests its own and anything we
made up now would be dead code later.

The one thing that survives Langfuse is the request id: it goes to the
orchestrator as `x-genos-trace-id`, so their logs and ours can be lined up.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("sse_relay")


def new_request_id() -> str:
    return str(uuid.uuid4())


@dataclass
class RequestLog:
    request_id: str
    log_raw: bool
    started: float = field(default_factory=time.monotonic)
    events_in: int = 0
    events_out: int = 0

    def raw_line(self, block: str) -> None:
        """Every incoming line exactly as received, when the switch is on.

        When a stream misbehaves you need the raw text, not a summary.
        """
        if self.log_raw:
            log.info("[%s] raw %r", self.request_id, block)

    def transformed(self, upstream_name: str, rule: Any, produced: list[Any]) -> None:
        self.events_in += 1
        log.info(
            "[%s] +%.3fs in=%s rule=%s out=[%s]",
            self.request_id,
            time.monotonic() - self.started,
            upstream_name,
            type(rule).__name__,
            ", ".join(type(p).__name__ for p in produced) or "-",
        )

    def emitted(self, count: int = 1) -> None:
        self.events_out += count

    def finished(self, reason: str, detail: str | None = None) -> None:
        log.info(
            "[%s] done in %.3fs — %d events in, %d out — %s%s",
            self.request_id,
            time.monotonic() - self.started,
            self.events_in,
            self.events_out,
            reason,
            f" ({detail})" if detail else "",
        )

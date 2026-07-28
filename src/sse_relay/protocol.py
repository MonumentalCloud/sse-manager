"""The envelope every event we send to the frontend is wrapped in.

Shape comes from the monimo agent architecture document, 4.1 SSE Protocol:

    {"event": "...", "trace_id": "mnm-4f7a-e21", "seq": 1,
     "ts": "2026-07-21T15:30:00.010+09:00", "data": {...}}

`trace_id`, `seq` and `ts` are stamped here rather than in the rules, so that a
rule only has to decide the event name and its data.

`seq` counts up across the run. `ping` is always seq 0 and never advances the
counter, matching the document's example of a ping arriving mid-run at seq 0.
"""

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .outbound import WireEvent

PING = "ping"


@dataclass
class TraceIds:
    """Two ids for one run, kept together so logs on both sides line up.

    `genos` is a bare uuid4 because that is what `x-genos-trace-id` accepts.
    `public` is the same uuid under the protocol's prefix, so a frontend report
    and a GenOS usage-log entry can always be matched back to each other.
    """

    genos: str
    public: str

    @classmethod
    def new(cls, prefix: str) -> "TraceIds":
        run = str(uuid.uuid4())
        return cls(genos=run, public=f"{prefix}-{run}")


@dataclass
class Sequencer:
    """Stamps the envelope. One per request."""

    trace_id: str
    timezone: str
    seq: int = 0
    _tz: dt.timezone = field(init=False)

    def __post_init__(self) -> None:
        self._tz = _parse_offset(self.timezone)

    def now(self) -> str:
        # Milliseconds and a +09:00-style offset, as in the protocol document.
        return dt.datetime.now(self._tz).isoformat(timespec="milliseconds")

    def envelope(self, event: WireEvent) -> dict[str, Any]:
        if event.name == PING:
            seq = 0
        else:
            self.seq += 1
            seq = self.seq
        return {
            "event": event.name,
            "trace_id": self.trace_id,
            "seq": seq,
            "ts": self.now(),
            "data": event.data,
        }

    def encode(self, event: WireEvent) -> bytes:
        body = json.dumps(self.envelope(event), ensure_ascii=False)
        return f"event: {event.name}\ndata: {body}\n\n".encode("utf-8")


def _parse_offset(text: str) -> dt.timezone:
    """"+09:00" -> a fixed-offset timezone."""
    sign = 1 if text[0] == "+" else -1
    hours, _, minutes = text[1:].partition(":")
    return dt.timezone(sign * dt.timedelta(hours=int(hours), minutes=int(minutes or 0)))

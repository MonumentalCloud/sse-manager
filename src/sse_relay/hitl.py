"""Human-in-the-loop: the bridge between an MCP tool call and the open stream.

The flow, end to end:

    agent calls ask_user (MCP tool)
      └► MCP server POSTs /hitl {session_id, prompt, options}
           └► we emit interaction.required on that session's live stream
                └► frontend renders buttons, user picks
                     └► frontend POSTs /interactions/{id}/resolve
                          └► /hitl returns the answer → the tool returns → agent continues

Registry lifecycle — the part that keeps memory bounded at scale:

- An entry exists only while its run's stream is open. It is registered when
  /ask starts and removed in the same `finally` that closes the stream, on all
  three exit paths. There is no sweeper because there is nothing to sweep:
  steady-state size equals concurrent open streams, never total users.
- A pending interaction is bounded twice: `hitl.expires_seconds` fails it with
  a timeout answer to the agent, and the run ending cancels it outright.
- `hitl.max_active_runs` is the backstop cap, and `hitl.max_pending_per_run`
  stops one confused agent from queueing questions forever.

All four knobs live in config.toml under [hitl].

This registry is in-process on purpose: the deploy harness runs one process, so
one dict is correct. The day the serving runs several replicas or workers, the
/hitl callback can land on a pod that does not hold the stream — that is the
Redis/pub-sub work item, and no amount of tuning here substitutes for it.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .outbound import WireEvent
from .telemetry import log


class HitlError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class ActiveRun:
    """One open /ask stream that can receive interaction events."""

    session_id: str
    trace_id: str
    queue: asyncio.Queue
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


class Registry:
    """session_id → live run, and interaction_id → its waiting /hitl call."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.runs: dict[str, ActiveRun] = {}
        self.interactions: dict[str, tuple[ActiveRun, asyncio.Future]] = {}
        self.counter = 0

    # -- run lifecycle, called by the /ask stream -----------------------------

    def register(self, session_id: str, trace_id: str, queue: asyncio.Queue) -> ActiveRun:
        if len(self.runs) >= self.settings.hitl_max_active_runs:
            raise HitlError(503, f"active run cap reached ({self.settings.hitl_max_active_runs})")
        run = ActiveRun(session_id=session_id, trace_id=trace_id, queue=queue)
        # Two tabs on one session: the newest run owns HITL for that session id.
        self.runs[session_id] = run
        return run

    def unregister(self, run: ActiveRun) -> None:
        # Identity check: a newer run may have taken this session id over.
        if self.runs.get(run.session_id) is run:
            del self.runs[run.session_id]
        for interaction_id, future in list(run.pending.items()):
            if not future.done():
                future.cancel()
            run.pending.pop(interaction_id, None)
            self.interactions.pop(interaction_id, None)

    # -- the two ends of one interaction --------------------------------------

    async def request(
        self,
        session_id: str,
        prompt: str,
        interaction_type: str,
        options: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Called by /hitl. Emits interaction.required and waits for the answer."""
        run = self.runs.get(session_id)
        if run is None:
            raise HitlError(404, f"no live run for session {session_id!r} — the stream has ended or never existed")
        if len(run.pending) >= self.settings.hitl_max_pending_per_run:
            raise HitlError(429, f"run already has {len(run.pending)} pending interaction(s)")

        self.counter += 1
        interaction_id = f"int:{self.counter}-{uuid.uuid4().hex[:8]}"
        resume_token = f"{session_id}#{interaction_id}"
        expires = self.settings.hitl_expires_seconds

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        run.pending[interaction_id] = future
        self.interactions[interaction_id] = (run, future)

        await run.queue.put(
            WireEvent(
                "interaction.required",
                {
                    "interaction_id": interaction_id,
                    "type": interaction_type,
                    "prompt": prompt,
                    "options": options or [],
                    "resume_token": resume_token,
                    "expires_in_s": int(expires),
                },
            )
        )
        log.info("[%s] interaction.required %s (%.0fs window)", run.trace_id, interaction_id, expires)

        try:
            answer = await asyncio.wait_for(future, timeout=expires)
            return {"status": "answered", "interaction_id": interaction_id, "answer": answer}
        except asyncio.TimeoutError:
            await run.queue.put(
                WireEvent(
                    "interaction.resolved",
                    {"interaction_id": interaction_id, "resolution": "expired", "resume_token": resume_token},
                )
            )
            log.info("[%s] interaction %s expired unanswered", run.trace_id, interaction_id)
            return {"status": "expired", "interaction_id": interaction_id, "answer": None}
        except asyncio.CancelledError:
            # The run ended (user closed the page, stream died) while waiting.
            raise HitlError(410, f"the run for session {session_id!r} ended before the user answered") from None
        finally:
            run.pending.pop(interaction_id, None)
            self.interactions.pop(interaction_id, None)

    async def resolve(self, interaction_id: str, answer: Any) -> dict[str, Any]:
        """Called by the frontend. Emits interaction.resolved and releases /hitl."""
        entry = self.interactions.get(interaction_id)
        if entry is None:
            raise HitlError(404, f"unknown or already-finished interaction {interaction_id!r}")
        run, future = entry
        if future.done():
            raise HitlError(410, f"interaction {interaction_id!r} already resolved or expired")

        await run.queue.put(
            WireEvent(
                "interaction.resolved",
                {
                    "interaction_id": interaction_id,
                    "resolution": "answered",
                    "resume_token": f"{run.session_id}#{interaction_id}",
                },
            )
        )
        future.set_result(answer)
        log.info("[%s] interaction %s answered", run.trace_id, interaction_id)
        return {"status": "ok", "interaction_id": interaction_id}

    def stats(self) -> dict[str, int]:
        return {
            "active_runs": len(self.runs),
            "pending_interactions": len(self.interactions),
        }

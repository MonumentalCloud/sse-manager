"""The service.

One request from the frontend opens one response stream, which opens one
connection to the orchestrator, which has its own transform state. All of it
lives and dies with that one request, so there is no session manager, no
connection registry and no broadcast list here — two concurrent users are just
two of these running side by side.
"""

import asyncio
import logging
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .config import Settings, load_settings
from .engine import Transformer
from .events import RunFinished
from .outbound import RunContext, WireEvent, error_event
from .protocol import PING, Sequencer, TraceIds
from .telemetry import RequestLog

log = logging.getLogger("sse_relay")

app = FastAPI(title="SSE Relay")


class ClientGone(Exception):
    """The user closed the page."""


class AskRequest(BaseModel):
    question: str
    session_id: str | None = None
    user_turn_id: str | None = None
    user_id: str | None = None


def get_settings() -> Settings:
    if not hasattr(app.state, "settings"):
        app.state.settings = load_settings()
    return app.state.settings


@app.on_event("startup")
async def _startup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    # Build the rule tables once at boot. A malformed table is a startup failure,
    # not something a user discovers halfway through a stream.
    Transformer(
        settings,
        RequestLog(request_id="startup-check", log_raw=False),
        RunContext(settings=settings, session_id=None, user_turn_id=None),
    )
    log.info(
        "sse-relay up — orchestrator=%s strict=%s log_raw=%s heartbeat=%ss read_timeout=%s",
        settings.run_url,
        settings.strict,
        settings.log_raw,
        settings.heartbeat_seconds,
        settings.read_timeout,
    )


@app.get("/healthz")
@app.get("/health")  # the GenOS serving harness probes this exact path
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz/upstream")
async def healthz_upstream() -> JSONResponse:
    """Whether the orchestrator itself is reachable."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.connect_timeout) as client:
        response = await client.get(
            settings.healthcheck_url,
            headers={"Authorization": f"Bearer {settings.api_key}"},
        )
    return JSONResponse({"status_code": response.status_code, "body": response.text[:200]})


@app.post("/ask")
async def ask(body: AskRequest, request: Request) -> StreamingResponse:
    settings = get_settings()
    trace = TraceIds.new(settings.trace_id_prefix)

    return StreamingResponse(
        _relay(settings, request, body, trace),
        media_type="text/event-stream",
        headers={
            # Some proxies hold a response and deliver it all at once when it
            # finishes, which would silently destroy the point of this service.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Trace-Id": trace.public,
        },
    )


async def _pump(
    settings: Settings,
    request_log: RequestLog,
    transformer: Transformer,
    body: AskRequest,
    trace: TraceIds,
    queue: asyncio.Queue,
) -> None:
    """Read the orchestrator, transform, and post results for the writer.

    Cancelling this task cancels the httpx stream, which is what actually stops
    the orchestrator working on an answer nobody will read.
    """
    from .upstream import stream_orchestrator

    try:
        async for name, payload in stream_orchestrator(
            settings,
            request_log,
            question=body.question,
            request_id=trace.genos,
            chat_id=body.session_id,
            user_id=body.user_id,
        ):
            for wire in transformer.handle(name, payload):
                await queue.put(wire)
        await queue.put(None)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 - handed to the writer, not swallowed
        await queue.put(exc)


async def _relay(
    settings: Settings,
    request: Request,
    body: AskRequest,
    trace: TraceIds,
) -> AsyncIterator[bytes]:
    request_log = RequestLog(request_id=trace.public, log_raw=settings.log_raw)
    run = RunContext(settings=settings, session_id=body.session_id, user_turn_id=body.user_turn_id)
    transformer = Transformer(settings, request_log, run)
    sequencer = Sequencer(trace_id=trace.public, timezone=settings.timezone)
    queue: asyncio.Queue = asyncio.Queue()

    log.info(
        "[%s] ask question=%r session_id=%s genos_trace_id=%s",
        trace.public,
        body.question,
        body.session_id,
        trace.genos,
    )

    pump = asyncio.create_task(_pump(settings, request_log, transformer, body, trace, queue))
    reason, detail = "completed", None

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=settings.heartbeat_seconds)
            except asyncio.TimeoutError:
                # Nothing for a while. A sub-agent thinking looks exactly like a
                # dead connection to anything in between, so say we are still here.
                if await request.is_disconnected():
                    raise ClientGone from None
                request_log.emitted()
                yield sequencer.encode(WireEvent(PING, {}))
                continue

            if item is None:
                break
            if isinstance(item, BaseException):
                raise item

            request_log.emitted()
            yield sequencer.encode(item)

    except ClientGone:
        reason = "client_disconnected"
        log.info("[%s] client disconnected — cancelling the orchestrator request", trace.public)
    except asyncio.CancelledError:
        # uvicorn cancels the response task when the socket goes away.
        log.info("[%s] response cancelled — cancelling the orchestrator request", trace.public)
        await _cancel(pump)
        raise
    except BaseException as exc:  # noqa: BLE001
        reason, detail = "upstream_error", f"{type(exc).__name__}: {exc}"
        # Loudly, with the whole traceback. Nothing here turns a crash into a
        # polite message; the frontend gets a closing event and the log gets the
        # real failure.
        log.exception("[%s] stream failed", trace.public)
        if settings.strict:
            async for chunk in _closing(settings, transformer, sequencer, request_log, reason, detail):
                yield chunk
            request_log.finished(reason, detail)
            await _cancel(pump)
            raise
    finally:
        await _cancel(pump)

    # Normal finish and client disconnect both land here. Whatever the rules are
    # still holding is released first, then exactly one closing event — otherwise
    # the frontend cannot tell "still thinking" from "crashed" and spins forever.
    async for chunk in _closing(settings, transformer, sequencer, request_log, reason, detail):
        yield chunk
    request_log.finished(reason, detail)


async def _closing(
    settings: Settings,
    transformer: Transformer,
    sequencer: Sequencer,
    request_log: RequestLog,
    reason: str,
    detail: str | None,
) -> AsyncIterator[bytes]:
    """Flush what the rules are holding, then `error` if any, then `run.end`."""
    for wire in transformer.flush():
        request_log.emitted()
        yield sequencer.encode(wire)

    if reason == "upstream_error":
        request_log.emitted()
        yield sequencer.encode(error_event(settings, detail))

    for wire in transformer.emit(RunFinished(reason=reason, detail=detail)):
        request_log.emitted()
        yield sequencer.encode(wire)


async def _cancel(task: asyncio.Task) -> None:
    if task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

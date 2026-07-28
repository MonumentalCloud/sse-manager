"""The service.

One request from the frontend opens one response stream, which opens one
connection to the orchestrator, which has its own transform state. All of it
lives and dies with that one request, so there is no session manager, no
connection registry and no broadcast list here — two concurrent users are just
two of these running side by side.
"""

import asyncio
import json
import logging
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .config import Settings, load_settings
from .engine import Transformer
from .events import RunFinished
from .outbound import WireEvent
from .telemetry import RequestLog, new_request_id

log = logging.getLogger("sse_relay")

app = FastAPI(title="SSE Relay")


class ClientGone(Exception):
    """The user closed the page."""


class AskRequest(BaseModel):
    question: str
    chat_id: str | None = None
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
    Transformer(settings, RequestLog(request_id="startup-check", log_raw=False))
    log.info(
        "sse-relay up — orchestrator=%s strict=%s log_raw=%s heartbeat=%ss",
        settings.run_url,
        settings.strict,
        settings.log_raw,
        settings.heartbeat_seconds,
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz/upstream")
async def healthz_upstream() -> JSONResponse:
    """Whether the orchestrator itself is reachable."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            settings.healthcheck_url,
            headers={"Authorization": f"Bearer {settings.api_key}"},
        )
    return JSONResponse({"status_code": response.status_code, "body": response.text[:200]})


@app.post("/ask")
async def ask(body: AskRequest, request: Request) -> StreamingResponse:
    settings = get_settings()
    request_id = new_request_id()

    return StreamingResponse(
        _relay(settings, request, body, request_id),
        media_type="text/event-stream",
        headers={
            # Some proxies hold a response and deliver it all at once when it
            # finishes, which would silently destroy the point of this service.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Request-Id": request_id,
        },
    )


def encode(event: WireEvent) -> bytes:
    return f"event: {event.name}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n".encode("utf-8")


async def _pump(
    settings: Settings,
    request_log: RequestLog,
    transformer: Transformer,
    body: AskRequest,
    request_id: str,
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
            request_id=request_id,
            chat_id=body.chat_id,
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
    request_id: str,
) -> AsyncIterator[bytes]:
    request_log = RequestLog(request_id=request_id, log_raw=settings.log_raw)
    transformer = Transformer(settings, request_log)
    queue: asyncio.Queue = asyncio.Queue()

    log.info("[%s] ask question=%r chat_id=%s", request_id, body.question, body.chat_id)

    pump = asyncio.create_task(_pump(settings, request_log, transformer, body, request_id, queue))
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
                yield b": ping\n\n"
                continue

            if item is None:
                break
            if isinstance(item, BaseException):
                raise item

            request_log.emitted()
            yield encode(item)

    except ClientGone:
        reason, detail = "client_disconnected", None
        log.info("[%s] client disconnected — cancelling the orchestrator request", request_id)
    except asyncio.CancelledError:
        # uvicorn cancels the response task when the socket goes away.
        reason, detail = "client_disconnected", None
        log.info("[%s] response cancelled — cancelling the orchestrator request", request_id)
        raise
    except BaseException as exc:  # noqa: BLE001
        reason, detail = "upstream_error", f"{type(exc).__name__}: {exc}"
        # Loudly, with the whole traceback. Nothing here turns a crash into a
        # polite message; the frontend gets a closing event and the log gets the
        # real failure.
        log.exception("[%s] stream failed", request_id)
        if settings.strict:
            async for chunk in _closing(transformer, request_log, reason, detail):
                yield chunk
            request_log.finished(reason, detail)
            await _cancel(pump)
            raise
    finally:
        await _cancel(pump)

    # Normal finish and client disconnect both land here. Whatever the rules are
    # still holding is released first, then exactly one closing event — otherwise
    # the frontend cannot tell "still thinking" from "crashed" and spins forever.
    async for chunk in _closing(transformer, request_log, reason, detail):
        yield chunk
    request_log.finished(reason, detail)


async def _closing(  # noqa: D401
    transformer: Transformer,
    request_log: RequestLog,
    reason: str,
    detail: str | None,
) -> AsyncIterator[bytes]:
    for wire in transformer.flush():
        request_log.emitted()
        yield encode(wire)
    for wire in transformer.emit(RunFinished(reason=reason, detail=detail)):
        request_log.emitted()
        yield encode(wire)


async def _cancel(task: asyncio.Task) -> None:
    if task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

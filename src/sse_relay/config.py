"""Settings and the two development switches.

Both switches default on. Turning strict mode off is a production decision to
make later, deliberately — not a thing to reach for when something breaks.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    base_url: str
    workflow_id: str
    api_key: str

    # Switch 1: stop loudly on anything unexpected instead of carrying on.
    strict: bool

    # Switch 2: log every incoming line exactly as received.
    log_raw: bool

    # A sub-agent can think for a long time while sending nothing. That is not a
    # dead connection, so there is no read timeout at all — the disconnect watcher
    # and the client going away are what end a stream early.
    connect_timeout: float
    write_timeout: float

    # Nginx/Envoy in front of the browser will drop an idle connection. A comment
    # line every few seconds keeps it open and is ignored by EventSource.
    heartbeat_seconds: float

    @property
    def run_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/gateway/workflow/{self.workflow_id}/run/v2"

    @property
    def healthcheck_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/gateway/workflow/{self.workflow_id}/healthcheck"


def load_settings() -> Settings:
    """Read configuration. Missing required values raise here, at startup."""
    return Settings(
        base_url=os.environ["ORCHESTRATOR_BASE_URL"],
        workflow_id=os.environ["ORCHESTRATOR_WORKFLOW_ID"],
        api_key=os.environ["orchestrator_key"],
        strict=_flag("SSE_RELAY_STRICT", True),
        log_raw=_flag("SSE_RELAY_LOG_RAW", True),
        connect_timeout=float(os.environ.get("SSE_RELAY_CONNECT_TIMEOUT", "10")),
        write_timeout=float(os.environ.get("SSE_RELAY_WRITE_TIMEOUT", "30")),
        heartbeat_seconds=float(os.environ.get("SSE_RELAY_HEARTBEAT_SECONDS", "10")),
    )


class RelayError(RuntimeError):
    """Something arrived that we do not understand. In strict mode this is fatal."""

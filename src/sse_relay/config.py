"""Load config.toml, let the environment override any of it.

One file holds every link and every knob so that moving this service somewhere
new is an edit to config.toml, or a handful of environment variables, and never
a code change. The API key is the one exception: it is named here but read from
the environment, so config.toml stays safe to commit.
"""

import os
import pathlib
import tomllib
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from dotenv import load_dotenv

load_dotenv()

T = TypeVar("T")

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / "config.toml"


class RelayError(RuntimeError):
    """Something arrived that we do not understand. In strict mode this is fatal."""


def _env(name: str, current: T, cast: Callable[[str], T]) -> T:
    raw = os.environ.get(name)
    return current if raw is None else cast(raw)


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    base_url: str
    workflow_id: str
    api_key: str
    run_path: str
    healthcheck_path: str

    connect_timeout: float
    write_timeout: float
    read_timeout: float | None
    max_block_bytes: int

    heartbeat_seconds: float

    trace_id_prefix: str
    channel: str
    timezone: str
    mcp_agent_name: str

    strict: bool
    log_raw: bool

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path.format(workflow_id=self.workflow_id)

    @property
    def run_url(self) -> str:
        return self._url(self.run_path)

    @property
    def healthcheck_url(self) -> str:
        return self._url(self.healthcheck_path)


def load_settings(path: pathlib.Path | None = None) -> Settings:
    """Read config.toml, then apply environment overrides. Missing key raises."""
    config_path = path or pathlib.Path(os.environ.get("SSE_RELAY_CONFIG", DEFAULT_CONFIG_PATH))
    if not config_path.exists():
        raise RelayError(f"no config file at {config_path}; set SSE_RELAY_CONFIG to point at one")

    with config_path.open("rb") as handle:
        raw: dict[str, Any] = tomllib.load(handle)

    orchestrator = raw["orchestrator"]
    timeouts = raw["timeouts"]
    stream = raw["stream"]
    protocol = raw["protocol"]
    development = raw["development"]

    api_key_env = _env("SSE_RELAY_API_KEY_ENV", orchestrator["api_key_env"], str)
    try:
        api_key = os.environ[api_key_env]
    except KeyError:
        raise RelayError(
            f"the orchestrator key is expected in the {api_key_env!r} environment variable "
            f"(named by orchestrator.api_key_env in {config_path})"
        ) from None

    read_seconds = _env("SSE_RELAY_READ_TIMEOUT", float(timeouts["read_seconds"]), float)

    return Settings(
        base_url=_env("SSE_RELAY_BASE_URL", orchestrator["base_url"], str),
        workflow_id=_env("SSE_RELAY_WORKFLOW_ID", str(orchestrator["workflow_id"]), str),
        api_key=api_key,
        run_path=orchestrator["run_path"],
        healthcheck_path=orchestrator["healthcheck_path"],
        connect_timeout=_env("SSE_RELAY_CONNECT_TIMEOUT", float(timeouts["connect_seconds"]), float),
        write_timeout=_env("SSE_RELAY_WRITE_TIMEOUT", float(timeouts["write_seconds"]), float),
        # 0 means "never give up waiting", which is what a thinking sub-agent needs.
        read_timeout=None if read_seconds <= 0 else read_seconds,
        max_block_bytes=_env("SSE_RELAY_MAX_BLOCK_BYTES", int(timeouts["max_block_bytes"]), int),
        heartbeat_seconds=_env("SSE_RELAY_HEARTBEAT_SECONDS", float(stream["heartbeat_seconds"]), float),
        trace_id_prefix=_env("SSE_RELAY_TRACE_ID_PREFIX", protocol["trace_id_prefix"], str),
        channel=_env("SSE_RELAY_CHANNEL", protocol["channel"], str),
        timezone=_env("SSE_RELAY_TIMEZONE", protocol["timezone"], str),
        mcp_agent_name=_env("SSE_RELAY_MCP_AGENT_NAME", protocol["mcp_agent_name"], str),
        strict=_env("SSE_RELAY_STRICT", bool(development["strict"]), _as_bool),
        log_raw=_env("SSE_RELAY_LOG_RAW", bool(development["log_raw"]), _as_bool),
    )

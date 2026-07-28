"""Runs the two transform steps.

    orchestrator's event  ->  our standard event  ->  what the frontend receives
            step 1                                          step 2

Each step is its own function, so that adding `@observe()` above it later is a
one-line change rather than pulling a long block apart.
"""

from typing import Any, Iterable

from . import inbound, outbound
from .config import RelayError, Settings
from .events import CanonicalEvent
from .outbound import WireEvent
from .rules import Rule
from .telemetry import RequestLog


class Transformer:
    """One per request. Owns the rule objects and therefore their memory."""

    def __init__(self, settings: Settings, request_log: RequestLog):
        self.settings = settings
        self.log = request_log
        self.inbound_rules = inbound.build_rules()
        self.outbound_rules = outbound.build_rules()

    # -- step 1 ------------------------------------------------------------

    def to_canonical(self, name: str, payload: Any) -> list[CanonicalEvent]:
        """The orchestrator's event becomes zero or more of our standard events."""
        rule = self.inbound_rules.get(name)
        if rule is None:
            raise RelayError(
                f"no step 1 rule for orchestrator event {name!r}. "
                f"Add one to inbound.py; known: {sorted(self.inbound_rules)}"
            )
        produced = list(rule.feed(payload))
        self.log.transformed(name, rule, produced)
        return produced

    # -- step 2 ------------------------------------------------------------

    def to_wire(self, event: CanonicalEvent) -> list[WireEvent]:
        """Our standard event becomes zero or more frontend events."""
        rule = self.outbound_rules.get(type(event))
        if rule is None:
            raise RelayError(
                f"no step 2 rule for standard event {type(event).__name__}. "
                f"Add one to outbound.py; known: {sorted(t.__name__ for t in self.outbound_rules)}"
            )
        return list(rule.feed(event))

    # -- both --------------------------------------------------------------

    def handle(self, name: str, payload: Any) -> list[WireEvent]:
        out: list[WireEvent] = []
        for canonical in self.to_canonical(name, payload):
            out.extend(self.to_wire(canonical))
        return out

    def flush(self) -> list[WireEvent]:
        """Release everything the rules are still holding, because the stream ended.

        Runs on every exit path, including a client disconnect — a rule holding
        a half-finished tool call still has to close it out.
        """
        out: list[WireEvent] = []
        for rule in _unique(self.inbound_rules.values()):
            for canonical in rule.flush():
                out.extend(self.to_wire(canonical))
        return out

    def emit(self, event: CanonicalEvent) -> list[WireEvent]:
        """Push a standard event we produced ourselves through step 2."""
        return self.to_wire(event)


def _unique(rules: Iterable[Rule]) -> list[Rule]:
    """Two upstream names can share one rule object; flush it once, not twice."""
    seen: dict[int, Rule] = {}
    for rule in rules:
        owner = getattr(rule, "owner", rule)
        seen.setdefault(id(owner), owner)
    return list(seen.values())

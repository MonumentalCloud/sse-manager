"""What a rule is.

A rule is an object, not a function, because not every transform is
one-in-one-out. Some need to hold pieces back and release them later, and some
need a later piece before they know how to rewrite an earlier one. Writing the
simple ones as plain functions would mean rewriting all of them the first time
one of those shows up — and per docs/orchestrator-events.md, two already have.

    feed(value)  -> zero or more outputs, right now
    flush()      -> whatever is still being held, because the stream ended
"""

from typing import Any, Callable, Iterable

from .events import CanonicalEvent
from .extract import extract


class Rule:
    """Base class. Stateless rules only need `feed`."""

    def feed(self, value: Any) -> Iterable[Any]:
        raise NotImplementedError

    def flush(self) -> Iterable[Any]:
        """Called once when the stream ends, however it ends."""
        return ()


class Map(Rule):
    """The one-line case: build one output from fields at fixed paths.

        Map(Token, text=".data")

    Each keyword is a constructor argument; each value is a path into the
    upstream payload. `const` supplies arguments that are not read from the
    payload at all.
    """

    def __init__(self, build: Callable[..., CanonicalEvent], /, _const: dict[str, Any] | None = None, **paths: str):
        self.build = build
        self.paths = paths
        self.const = _const or {}

    def feed(self, value: Any) -> Iterable[Any]:
        kwargs = {name: extract(value, path) for name, path in self.paths.items()}
        return (self.build(**kwargs, **self.const),)


class Ignore(Rule):
    """Explicitly carry nothing onward, with a reason on the record.

    Different from having no rule: no rule is always an error.
    """

    def __init__(self, reason: str):
        self.reason = reason

    def feed(self, value: Any) -> Iterable[Any]:
        from .events import Dropped

        return (Dropped(reason=self.reason),)

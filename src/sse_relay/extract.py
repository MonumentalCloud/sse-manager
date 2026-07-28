"""Reach into a decoded JSON value at a written path and pull out what is there.

`.data`, `.tool.name`, `.data[0].toolInput` — that is the whole syntax. Rules in
the mapping tables are written in terms of these strings so that adding an event
type stays a one-line change.

A path that does not resolve raises. A rule that names a field the orchestrator
did not send is a bug in the rule, and silence about it is how you find out
weeks later.
"""

import re
from typing import Any

from .config import RelayError

_SEGMENT = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def extract(value: Any, path: str) -> Any:
    """Follow `path` into `value`. `"."` means the value itself."""
    if path in ("", "."):
        return value

    position = 0
    current = value
    for match in _SEGMENT.finditer(path):
        if match.start() != position:
            raise RelayError(f"malformed path {path!r} at offset {position}")
        position = match.end()

        key, index = match.group(1), match.group(2)
        if key is not None:
            if not isinstance(current, dict):
                raise RelayError(f"path {path!r}: expected an object at {key!r}, got {type(current).__name__}")
            if key not in current:
                raise RelayError(f"path {path!r}: no key {key!r} (have {sorted(current)})")
            current = current[key]
        else:
            if not isinstance(current, list):
                raise RelayError(f"path {path!r}: expected a list at [{index}], got {type(current).__name__}")
            i = int(index)
            if i >= len(current):
                raise RelayError(f"path {path!r}: index {i} out of range (length {len(current)})")
            current = current[i]

    if position != len(path):
        raise RelayError(f"malformed path {path!r} at offset {position}")
    return current

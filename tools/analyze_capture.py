"""Read a capture produced by probe_raw.py and report what was actually in it.

Reassembles the byte chunks (they split mid-line and mid-UTF-8-character), then
reports the line prefixes, the event vocabulary, and the shape of each event's
payload. This is what turns a capture into the event list the transform rules
are written against.

Usage:
    uv run tools/analyze_capture.py captures/simple2.jsonl
"""

import argparse
import collections
import json
import pathlib


def reassemble(jsonl_path: pathlib.Path) -> str:
    """Chunks split mid-character, so decode the concatenated bytes, not each chunk."""
    raw = bytearray()
    for line in jsonl_path.open(encoding="utf-8"):
        rec = json.loads(line)
        raw.extend(rec["text"].encode("utf-8", errors="surrogatepass"))
    return raw.decode("utf-8", errors="replace")


def shape(value, depth: int = 0):
    """Describe a JSON value's structure without printing megabytes of prompt text."""
    if depth > 2:
        return "..."
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0], depth + 1), f"...x{len(value)}"] if value else []
    if isinstance(value, str):
        return f"str({len(value)})" if len(value) > 40 else repr(value)
    return type(value).__name__


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture", type=pathlib.Path)
    args = ap.parse_args()

    text = reassemble(args.capture)
    blocks = [b for b in text.split("\n\n") if b.strip()]

    prefixes = collections.Counter(b.split(":", 1)[0] for b in blocks)
    print(f"=== {args.capture.name}: {len(blocks)} blocks ===")
    print("\nline prefixes:")
    for k, v in prefixes.most_common():
        print(f"  {v:6d}  {k!r}")

    events = collections.Counter()
    first_of: dict[str, object] = {}
    order: list[str] = []
    unparsed = 0

    for b in blocks:
        if not b.startswith("data: "):
            continue
        try:
            obj = json.loads(b[6:])
        except json.JSONDecodeError:
            unparsed += 1
            continue
        ev = obj.get("event")
        events[ev] += 1
        if ev not in first_of:
            first_of[ev] = obj.get("data")
            order.append(ev)

    print(f"\nevent types inside 'data: ' blocks ({unparsed} unparseable):")
    for k, v in events.most_common():
        print(f"  {v:6d}  {k}")

    print("\nfirst appearance order:")
    print("  " + " -> ".join(order))

    print("\npayload shape per event type:")
    for ev in order:
        print(f"\n  {ev}:")
        print("    " + json.dumps(shape(first_of[ev]), ensure_ascii=False)[:1200])

    # The tail matters: how the stream signals it is finished.
    print("\nlast 5 blocks (truncated):")
    for b in blocks[-5:]:
        print(f"  {b[:160]!r}")


if __name__ == "__main__":
    main()

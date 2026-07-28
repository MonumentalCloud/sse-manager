"""Job zero: connect to the real orchestrator and record exactly what it sends.

Writes two files per run into captures/:
  <name>.raw    every chunk of bytes as it arrived, with arrival offsets
  <name>.jsonl  one record per chunk: {t, size, text}

No parsing, no interpretation. The point is to find out what the event
vocabulary actually is, so read the output rather than guessing from it.

Usage:
    uv run tools/probe_raw.py --name simple --question "1 + 1은?"
"""

import argparse
import asyncio
import json
import pathlib
import time
import uuid

import httpx

from sse_relay.config import load_settings

CAPTURES = pathlib.Path(__file__).resolve().parent.parent / "captures"


async def capture(name: str, question: str, stream: bool, chat_id: str | None) -> None:
    settings = load_settings()

    url = settings.run_url
    trace_id = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
        "x-genos-trace-id": trace_id,
    }
    body: dict = {"question": question}
    if stream:
        body["stream"] = True
    if chat_id:
        body["chatId"] = chat_id
        headers["x-genos-session-id"] = chat_id

    CAPTURES.mkdir(exist_ok=True)
    raw_path = CAPTURES / f"{name}.raw"
    jsonl_path = CAPTURES / f"{name}.jsonl"

    print(f"POST {url}")
    print(f"trace_id={trace_id} stream={stream}")
    print(f"question={question!r}\n")

    started = time.monotonic()
    chunks = 0
    total_bytes = 0

    # No read timeout by default: a sub-agent can think for a long time without
    # sending anything, and that is not a dead connection.
    timeout = httpx.Timeout(
        connect=settings.connect_timeout,
        read=settings.read_timeout,
        write=settings.write_timeout,
        pool=settings.connect_timeout,
    )

    with raw_path.open("wb") as raw_f, jsonl_path.open("w", encoding="utf-8") as jsonl_f:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=body, headers=headers) as res:
                print(f"HTTP {res.status_code}")
                for k, v in res.headers.items():
                    print(f"  {k}: {v}")
                print()
                res.raise_for_status()

                async for chunk in res.aiter_bytes():
                    t = time.monotonic() - started
                    chunks += 1
                    total_bytes += len(chunk)
                    raw_f.write(b"\n===== +%.3fs  %d bytes =====\n" % (t, len(chunk)))
                    raw_f.write(chunk)
                    raw_f.flush()
                    jsonl_f.write(
                        json.dumps(
                            {
                                "t": round(t, 4),
                                "size": len(chunk),
                                "text": chunk.decode("utf-8", errors="replace"),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    jsonl_f.flush()
                    preview = chunk.decode("utf-8", errors="replace")[:120].replace("\n", "\\n")
                    print(f"+{t:7.3f}s  {len(chunk):6d}B  {preview}")

    elapsed = time.monotonic() - started
    print(f"\ndone in {elapsed:.2f}s — {chunks} chunks, {total_bytes} bytes")
    print(f"wrote {raw_path} and {jsonl_path}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--question", required=True)
    p.add_argument("--no-stream", action="store_true")
    p.add_argument("--chat-id")
    a = p.parse_args()
    asyncio.run(capture(a.name, a.question, not a.no_stream, a.chat_id))


if __name__ == "__main__":
    main()

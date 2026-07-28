"""Step 3: confirm that a client going away actually stops the orchestrator.

Checking our own logs only proves we noticed. The question is whether the
orchestrator stopped working, so the check has to be on their side.

The evidence used here is the orchestrator's own conversation memory. A turn the
orchestrator runs to completion is written into the history for that `chatId`
and it can recall it on the next turn. A turn that was aborted part-way is not.
So:

  baseline   run a turn to completion, then ask about it on the same chatId
             -> the orchestrator remembers, which proves the check works at all
  cancelled  disconnect part-way, then ask about it on the same chatId
             -> the orchestrator has nothing, which means the run was abandoned

Usage:
    uv run tools/disconnect_test.py --relay http://127.0.0.1:8080
"""

import argparse
import asyncio
import json
import uuid

import httpx

from sse_relay.config import load_settings

# A question inside the agent's own role, so a completed turn is definitely
# written to history. Asking it to memorise something personal is not reliable:
# it sometimes declines, which makes the baseline prove nothing.
MEMORABLE = "이번 달 예산 설정 알려줘"
RECALL = "직전 턴에서 내가 했던 질문을 그대로 한 문장으로만 다시 말해줘. 이전 대화가 없으면 '이전 대화 없음'이라고만 답해."


async def ask_relay(relay: str, question: str, chat_id: str, cut_after: float | None) -> dict:
    """Ask through the relay. If cut_after is set, drop the connection at that point."""
    events: list[str] = []
    body = {"question": question, "session_id": chat_id}
    timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)

    async def read() -> None:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{relay}/ask", json=body) as response:
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        events.append(line[len("event: ") :])

    if cut_after is None:
        await read()
        return {"events": events, "cut": False}

    task = asyncio.create_task(read())
    await asyncio.sleep(cut_after)
    if task.done():
        raise RuntimeError(f"stream finished in under {cut_after}s — nothing to cut")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return {"events": events, "cut": True}


async def ask_orchestrator_directly(question: str, chat_id: str) -> str:
    """Ask the orchestrator without the relay, so the answer is its own memory."""
    settings = load_settings()
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
        "x-genos-session-id": chat_id,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=None, write=30, pool=10)) as client:
        response = await client.post(
            settings.run_url, json={"question": question, "chatId": chat_id}, headers=headers
        )
        response.raise_for_status()
        return response.json()["data"]["text"]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", default="http://127.0.0.1:8080")
    ap.add_argument("--cut-after", type=float, default=8.0)
    args = ap.parse_args()

    print("=" * 70)
    print("BASELINE — run a turn to completion, then ask the orchestrator about it")
    print("=" * 70)
    baseline_chat = str(uuid.uuid4())
    result = await ask_relay(args.relay, MEMORABLE, baseline_chat, cut_after=None)
    print(f"chatId={baseline_chat}")
    print(f"relay sent: {json.dumps(_counts(result['events']), ensure_ascii=False)}")
    recalled = await ask_orchestrator_directly(RECALL, baseline_chat)
    print(f"orchestrator recalls: {recalled.strip()[:200]!r}\n")

    print("=" * 70)
    print(f"CANCELLED — disconnect after {args.cut_after}s, then ask the same thing")
    print("=" * 70)
    cut_chat = str(uuid.uuid4())
    result = await ask_relay(args.relay, MEMORABLE, cut_chat, cut_after=args.cut_after)
    print(f"chatId={cut_chat}")
    print(f"relay sent before the cut: {json.dumps(_counts(result['events']), ensure_ascii=False)}")
    forgotten = await ask_orchestrator_directly(RECALL, cut_chat)
    print(f"orchestrator recalls: {forgotten.strip()[:200]!r}\n")

    print("=" * 70)
    print("Read the two 'orchestrator recalls' lines. The baseline should echo the")
    print("question back; the cancelled one should report no previous conversation.")
    print("That difference is the proof, and it comes from the orchestrator's own")
    print("memory rather than from our logs.")
    print("=" * 70)


def _counts(events: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in events:
        out[e] = out.get(e, 0) + 1
    return out


if __name__ == "__main__":
    asyncio.run(main())

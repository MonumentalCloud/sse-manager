# What the orchestrator actually sends

Job zero. Not guessed — recorded from workflow 4848 on `https://genos.genon.ai`
with `tools/probe_raw.py`, then read with `tools/analyze_capture.py`.

Captures behind this document:

| capture     | question              | outcome                                  |
| ----------- | --------------------- | ---------------------------------------- |
| `simple2`   | `1 + 1은?`            | 194 blocks, 24.4s, no tool calls          |
| `subagent2` | `이번 달 예산 설정 알려줘` | 298 blocks, 39.0s, 3 tool calls           |
| `subagent`  | same as above         | orchestrator dropped the connection at 154 blocks |

## The endpoint

```
POST {base}/api/gateway/workflow/{workflow_id}/run/v2
Authorization: Bearer {orchestrator_key}
Content-Type: application/json
```

`{"question": "...", "stream": true}`. Optional `chatId` in the body plus
`x-genos-session-id` for multi-turn, `x-genos-trace-id` to line their logs up
with ours, `x-genos-user-id` for the GenOS usage log.

Healthcheck is `GET {base}/api/gateway/workflow/{workflow_id}/healthcheck` →
`{"status":"ok"}`.

### The GitBook is wrong about the wire format

The GenOS docs describe token lines starting with `token: ` and a final line
starting with `result: `. That is not what workflow 4848 sends. What actually
arrives is Flowise-native SSE:

```
data: {"event": "<name>", "data": <payload>}\n\n
```

There is **one** bare `result: {...}` line, appended after
`data: {"event":"end","data":"[DONE]"}` — a duplicate of the `result` event.
No bare `token: ` lines exist at all. Parsing per the documented format would
find zero tokens and one result, i.e. it would look like a non-streaming API.

Two more things the docs do not mention, both of which break naive parsers:

- **`Content-Type` is `text/plain; charset=utf-8`**, not `text/event-stream`.
- **Chunks split mid-line and mid-UTF-8-character.** Large payloads arrive in
  4096-byte pieces that cut Korean characters in half. Decode the accumulated
  buffer, never each chunk on its own, and split blocks on `\n\n` only after
  reassembly.

## Event vocabulary

Every event type observed across both complete captures. This list is the input
to the transform rules; anything not on it hard-fails in strict mode by design.

| event                  | payload                                                    | notes |
| ---------------------- | ---------------------------------------------------------- | ----- |
| `agentFlowEvent`       | `"INPROGRESS"` \| `"FINISHED"`                              | whole-run status, once each |
| `nextAgentFlow`        | `{nodeId, nodeLabel, status}` where status is `INPROGRESS`/`FINISHED` | one pair per flow node |
| `agentFlowExecutedData`| array of `{nodeId, nodeLabel, data, previousNodeIds, status}` | large; full node dump, sent twice |
| `token`                | string                                                      | the answer, one piece at a time |
| `calledTools`          | `[{tool, toolInput, toolOutput}]`                           | `toolOutput` is always `""` here — this is the *start* signal |
| `usedTools`            | `[{tool, toolInput, toolOutput}]`                           | same shape, `toolOutput` filled — sent once, at the end |
| `usageMetadata`        | `{input_tokens, output_tokens, total_tokens, ...}`          | once |
| `result`               | `{text, chatId, chatMessageId, question, sessionId, agentFlowExecutedData, usedTools?}` | the final answer object |
| `metadata`             | `{chatId, chatMessageId, question, sessionId}`              | once, after `result` |
| `end`                  | `"[DONE]"`                                                  | last `data:` block |

Observed order:

```
agentFlowEvent(INPROGRESS)
  → nextAgentFlow(INPROGRESS/FINISHED per node)
  → agentFlowExecutedData
  → token × N
  → calledTools × N
  → usedTools
  → usageMetadata
  → agentFlowEvent(FINISHED)
  → result → metadata → end
  → bare `result: {...}` line
```

## How much sub-agent detail we get

This was the open question. The answer is **medium, and asymmetric**.

The orchestrator is a Flowise AgentFlow whose sub-agents are MCP tools behind a
`Genos MCP` node. What crosses the wire:

- **We do see each tool call start**, live, as a `calledTools` event carrying the
  tool name and its input — e.g.
  `{"tool": "invoke_tool", "toolInput": {"tool_name": "budget_inquiry", "arguments": {}}}`.
  The real sub-agent name is nested inside `toolInput.tool_name`, not in `tool`.
- **We do not see anything from inside a sub-agent** — no progress, no partial
  output, no token stream of its own. A sub-agent is opaque between start and finish.
- **Outputs arrive only at the very end**, in one `usedTools` event containing every
  call with its `toolOutput` filled in. So the pairing of a call to its result is
  not one-to-one in time: N `calledTools` events spread over the run, then one
  `usedTools` array at the end.

That last point is the concrete reason a rule needs memory: to report a
finished tool call the moment its output is known, a rule has to hold the
`calledTools` announcements and match them against the single late `usedTools`
array.

Tool names seen in one run: `describe_tools` (twice — the agent enumerating the
81 available tools), then `invoke_tool` with `tool_name: budget_inquiry`.

## Other things worth knowing before writing rules

- **73% of `token` events are empty strings** (205 of 280 in `subagent2`). They are
  keepalive-ish noise and carry no text. Forwarding them one-for-one wastes a
  frontend event each. Coalescing them is the other natural job for a memory rule.
- **The answer text is prefixed with `\n\nRESULT: `.** The assembled tokens and
  `result.text` both begin that way. Stripping it is the final-payload hook's job,
  not a streaming rule's.
- **`result.text` equals the concatenation of all `token` payloads.** So the final
  object is a duplicate of what already streamed, not new information.
- **The orchestrator drops connections.** One of three runs of the same question
  died with `peer closed connection without sending complete message body`. This
  is not theoretical — it must produce a closing event to the frontend.
- **First byte takes ~1.5s; total runs 24–39s.** Delivery is genuinely incremental
  (chunk arrival timestamps are spread across the whole run), so the premise of
  this service holds — nothing is buffered to the end upstream.
- **No `error` event has been observed yet.** It is not in the list above because
  this document only records what was seen. Strict mode will stop loudly if one
  appears, which is how we will learn its shape.

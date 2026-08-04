# GenOS Gateway API — measured shapes

The reference for calling GenOS as-is from a client application. GenOS publishes
no OpenAPI spec (every docs-ish path returns the SPA shell, verified on two
instances), so everything below was measured on real wires:

- `genos.genon.ai` (Flowise 3.0.0 workflows) — workflow 4848, serving 270
- internal instance `192.168.73.181:40908` (Flowise 3.1.3) — workflows 2791, 2797

Auth for everything: `Authorization: Bearer <key>`. Keys are scoped; a key for
one resource can be rejected (`RBAC: access denied`) or invalid
(`Invalid token`, 401) elsewhere. Missing header → 401 `Bearer token required`.

---

## 1. Workflow gateway — `/api/gateway/workflow/{workflow_id}`

### GET `/healthcheck`

→ `200 {"status":"ok"}`. Fast (<3s). Auth required.

### POST `/run/v2` — non-streaming

Body: `{"question": "..."}`

→ `200`:
```json
{"code": 0, "errMsg": "success", "data": {
  "question": "...", "text": "<final answer>",
  "chatId": "...", "chatMessageId": "...", "sessionId": "...",
  "agentFlowExecutedData": [ per-node inputs/outputs/status — LARGE, includes
                             full system prompts and model configs ],
  "action": { only present when the run STOPPED at a Human Input node }
}}
```

Beware: `agentFlowExecutedData` leaks the flow's internals (system prompts,
internal service URLs, `x-genos-access-token` values). Do not forward it to
end users.

### POST `/run/v2` — streaming

Body: `{"question": "...", "stream": true}`

Response is `Content-Type: text/plain` (NOT text/event-stream), chunked, and
chunks split mid-line and mid-UTF-8-character — buffer bytes, split on `\n\n`,
decode per block. Blocks:

```
data: {"event": "<name>", "data": <payload>}
```

Event vocabulary (all observed; token-by-token text arrives only in the final
seconds — progress events are what stream live):

| event | payload | notes |
|---|---|---|
| `agentFlowEvent` | `"INPROGRESS"` \| `"FINISHED"` \| `"STOPPED"` \| `"ERROR"` | run status |
| `nextAgentFlow` | `{nodeId, nodeLabel, status}` | per node, INPROGRESS/FINISHED/STOPPED/ERROR |
| `agentFlowExecutedData` | array of node dumps | large, repeated |
| `token` | string | ~70% are empty strings |
| `calledTools` | `[{tool, toolInput, toolOutput:""}]` | tool call started (only the current call) |
| `usedTools` | `[{tool, toolInput, toolOutput}]` | all calls with outputs, once, at end |
| `usageMetadata` | token counts | |
| `action` | see HITL below | only on STOPPED |
| `result` | same object as non-streaming `data` | |
| `metadata` | `{chatId, chatMessageId, question, sessionId}` | |
| `end` | `"[DONE]"` | |

After `end`, one **bare** line `result: {...}` (not `data:`-wrapped) duplicates
the result. The documented `token: ` line format does not exist.

MCP-wrapped tools report `tool: "invoke_tool"` with the real sub-agent name in
`toolInput.tool_name`. `calledTools`/`usedTools` disagree about array meaning
(current-call vs all-calls); `result.usedTools` is nested one level deeper
(list of lists) than the `usedTools` event.

### Multi-turn

Body `chatId: "<uuid or stable id>"` (+ header `x-genos-session-id: <same>`).
Conversation memory is keyed on it server-side. A run cancelled mid-flight
leaves no trace in memory.

### Useful headers

- `x-genos-trace-id: <uuid4>` — groups requests in GenOS 이용 로그
- `x-genos-user-id: <id>` — attributes usage
- `x-genos-session-id` — see multi-turn

### HITL: stop & resume (Human Input node) — full contract in docs/hitl-contract.md

Stop: stream ends with `nextAgentFlow(STOPPED)` → `agentFlowEvent("STOPPED")` →
`action {id, mapping{approve,reject}, elements[2 buttons], data{nodeId,…}}` →
`metadata` → `end`. `result.text` = the question to display.

Resume (gateway forwards `humanInput` — verified):
```json
POST /run/v2
{"question": "", "stream": true, "chatId": "<same>",
 "humanInput": {"type": "proceed"|"reject", "startNodeId": "<action.data.nodeId>",
                "feedback": "<optional; becomes a user chat message>"}}
```
Resumes at the checkpointed node; binary only. Resume against a non-STOPPED
session is refused by the engine.

### Error shapes seen

- 401 `Bearer token required` / `Invalid token` (plain text)
- 403 `RBAC: access denied` (plain text)
- 405 nginx HTML (wrong path shape)
- In-stream: `agentFlowEvent: "ERROR"` + failing node's `error` string inside
  `agentFlowExecutedData`; stream still terminates normally
- Streams can also drop mid-run (`incomplete chunked read`) — observed ~1 in 3
  on some runs; clients must treat an unterminated stream as a failed run

---

## 2. Code serving gateway — `/api/gateway/code_serving/{serving_id}/{path}`

Routes to whatever HTTP API the deployed code serving implements, path
pass-through, bearer auth at the gateway. SSE streams pass through unbuffered
(verified: live event timing preserved). 503
`"사용가능한 코드서빙 리비전이 없습니다."` when no revision has traffic
assigned — a running pod is not sufficient; the revision needs weight/active.

## 3. LLM serving gateway — `/api/gateway/rep/serving/{serving_id}`

OpenAI-compatible (from GenOS's own embedded docs; not independently verified):
`GET /v1/models`, `POST /v1/chat/completions` with standard OpenAI bodies.

## 4. Adjacent surfaces (seen in configs, unmeasured)

- MCP gateway: `http://llmops-gateway-api-service:8080/mcp/{id}/mcp` (internal
  URL seen in flow configs) — external gateway path unknown
- A2A: sub-agents invoked with taskId/contextId/artifacts;
  `x-genos-a2a-extensions: https://genos.genon.ai/a2a/extensions/hitl-ui/v1`
  (extension URI is an identifier; not resolvable)
- `request_user_input` tool (confirm / single-select / multi-select with
  options[{value,label,desc}]) — exists in internal flows; its stop/resume wire
  shape is the top unmeasured item

---

## Open items

1. **`request_user_input` tool lane** — attach the tool to a test agent (no
   Human Input node) and capture stop + resume shapes.
2. Upload/file fields on `run/v2` (Flowise supports `uploads` — unverified
   through the gateway).
3. MCP gateway external path + auth.
4. A2A agent endpoints (agent card, task get/cancel) if the frontend ever talks
   to sub-agents directly.

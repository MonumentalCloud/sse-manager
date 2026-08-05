# HITL through the GenOS gateway — measured contract

Measured 2026-08-04 against an internal GenOS instance (Flowise 3.1.3), workflow
2797: `Start → Agent 0 → Human Input 0 → (proceed → Loop 0 / reject → Loop 1)`.
Nothing here is inferred from source alone; every shape below crossed a real wire.

## The stop (what arrives when a Human Input node is reached)

The run executes normally until the Human Input node, then the stream ends with
this tail (after the last `token`):

```
data: {"event":"nextAgentFlow","data":{"nodeId":"humanInputAgentflow_0","nodeLabel":"Human Input 0","status":"STOPPED"}}
data: {"event":"agentFlowExecutedData","data":[ ... last node has "status":"STOPPED" ... ]}
data: {"event":"agentFlowEvent","data":"STOPPED"}
data: {"event":"action","data":{
  "id":"fe077027-f4ac-43a2-898c-0e237daea966",
  "mapping":{"approve":"Proceed","reject":"Reject"},
  "elements":[{"type":"agentflowv2-approve-button","label":"Proceed"},
              {"type":"agentflowv2-reject-button","label":"Reject"}],
  "data":{"nodeId":"humanInputAgentflow_0","nodeLabel":"Human Input 0","input":{...}}}}
data: {"event":"metadata","data":{...}}
data: {"event":"end","data":"[DONE]"}
result: {...}    ← bare line; result.text is the question to display,
                    result.action duplicates the action event
```

The question text shown to the user is `result.text` (LLM-generated when the
node's description mode is "Use LLM", fixed otherwise — use fixed for
production-predictable wording).

## The resume (what the frontend sends back)

```
POST {base}/api/gateway/workflow/{id}/run/v2
{
  "question": "",
  "stream": true,
  "chatId": "<same chatId as the stopped run>",
  "humanInput": {
    "type": "proceed" | "reject",
    "startNodeId": "<action.data.nodeId>",
    "feedback": "<optional free text>"
  }
}
```

Verified behavior:

- **The GenOS gateway forwards `humanInput` to Flowise.** This was the single
  biggest unknown; it works.
- Execution deserializes the checkpoint and restarts **at the Human Input
  node**: observed `humanInput:INPROGRESS → FINISHED → (proceed edge) → Loop 0 →
  Agent 0 → …`.
- `type:"reject"` follows the reject edge (verified; the test flow's Loop 1 was
  misconfigured and errored, but routing was correct).
- `feedback` becomes a **user chat message** in the agent's history on both
  proceed and reject — free text rides along with the button.
- If the graph loops back, the flow can stop again; each stop mints a fresh
  `action.id`. The frontend must treat every stop independently.
- Errors surface as `agentFlowEvent: "ERROR"` plus the failing node's error in
  `agentFlowExecutedData` — the stream still terminates cleanly.

## Edge cases (measured, 2026-08-04, workflow 2797)

| scenario | what actually happens |
|---|---|
| resume with a wrong `startNodeId` | HTTP **200** with `{"code":1,"error_code":"00020003","errMsg":"No error message provided"}` |
| resume a session whose last run ERRORED | identical generic error envelope |
| resume a second time (replay) | works if the flow stopped again (each stop is fresh); against a finished run, the generic error |
| plain question (no `humanInput`) while STOPPED | **no error** — the old checkpoint is abandoned and a brand-new run starts from Start, chat history intact |
| resume after such an interleaved question | targets the **newest** STOPPED execution for the session |
| transient 502 (nginx HTML) | seen once mid-battery; retry succeeded — clients need one retry on 5xx |

Consequences for a frontend:

- **Check `code` in the body, not the HTTP status** — engine failures arrive as
  HTTP 200 with `code: 1`, and the gateway strips the engine's descriptive
  message, so failure causes are indistinguishable. Treat any `code != 0` on a
  resume as "this question is no longer answerable; re-ask."
- **Buttons must not assume exclusivity** — a typed message silently invalidates
  the pending question. After any user turn, the only trustworthy pending
  question is the one from the *latest* response.
- One retry on 5xx is warranted; the platform hiccups.

## Constraints (unchanged from the source reading)

- The node lane is **binary**: `mapping` is hardcoded approve/reject. Custom
  multiple choice does not fit through it.
- Resume requires the previous execution for (sessionId, workflow) to be in
  state `STOPPED`; anything else is refused by the engine.

## The tool lane (A2A HITL) — spec-defined, NOT implemented on this instance

Documented by the platform (see `docs/vendor/a2a-agent-manual.md` and
`docs/vendor/a2a-hitl-ui-extension-v1.md`), but **measured absent**
(2026-08-05, internal instance, master 2810 `Start → A2A Agent 노드` →
sub card 145, HITL 전달 ON, everything freshly deployed). Findings, each
one from a real wire capture:

1. **Protocol mismatch, instance-wide.** Every `/a2a/{id}` endpoint — including
   a registration created the same day (card 144) — implements only the legacy
   dialect: `tasks/send` works, `message/send` returns JSON-RPC `-32601`
   byte-identical to a garbage method, while the agent cards falsely advertise
   `protocolVersion: "0.3.0"`. The dynamic **Agent 노드**'s client speaks
   `message/send` (per manual §8 internal targets run "v0.3 호환 모드"), so
   every new master fails with `a2aAgentUsed … "Unknown method: message/send"`.
   Old masters keep working because their clients predate the rename
   (`tasks/send` era). **Workaround measured:** the deterministic
   **A2A Agent 노드** (manual §5) reaches the sub fine.
2. **`request_user_input` is never injected.** With the master's HITL 전달
   toggle ON, the sub's Agent node input carries `messages` only — no tools
   array. Prompted to call the tool, the sub's model can only emit the call as
   plain text. No `input-required`, no `interactionId`, no `component`
   anywhere in the stream.
3. **A Human Input 노드 inside the sub does not propagate.** The sub's flow
   stops internally, but the A2A server wraps its accumulated text (the
   question itself) in a **`completed`** task; the master finishes normally
   and the frontend receives the question as the final answer. Nothing is
   resumable: `contextId` is always empty, each master call mints a fresh
   `taskId`, and a follow-up message simply abandons the sub's checkpoint
   (same abandon semantics as the node lane).

Conclusion: the vendor manual documents a newer platform build than what is
deployed. Until the A2A server component is upgraded (owner: GenOS platform
team), the only HITL that works end-to-end through the gateway is the node
lane above — a Human Input node in the **master** workflow. The extension-v1
shapes (`{interactionId, action, values}` and the run/v2 resume body carrying
them) remain unmeasurable until then.

## What this means for the relay

New inbound vocabulary to support (one line each in `inbound.py`, plus the
outbound mapping):

- `nextAgentFlow` with `status:"STOPPED"` → step event with a stopped state
- `agentFlowEvent: "STOPPED"` → run outcome `awaiting_input` (instead of the
  endpoint's `completed`)
- `action` → `interaction.required` (id, options from `mapping`/`elements`,
  `resume_token = session_id # action.data.nodeId`)

Plus a `/resume` endpoint that translates
`{resume_token, decision, feedback?}` into the `humanInput` body above and
streams the continuation like `/ask`, opening with `interaction.resolved`.

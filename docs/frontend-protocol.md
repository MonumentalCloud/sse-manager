# What we send the frontend

The target is the monimo SSE protocol, agent architecture document section 4.1.
Every event goes out as:

```
event: <name>
data: {"event": "<name>", "trace_id": "mnm-…", "seq": 1,
       "ts": "2026-07-28T14:55:48.178+09:00", "data": { … }}
```

`trace_id`, `seq` and `ts` are stamped in `protocol.py`, never by a rule. `seq`
counts up across the run; `ping` is always `seq: 0` and does not advance it,
matching the document's example of a mid-run ping at seq 0.

`trace_id` is `{protocol.trace_id_prefix}-{uuid4}`, and the bare uuid4 goes to
the orchestrator as `x-genos-trace-id`. So one id ties a frontend report, our
logs and a GenOS usage-log entry together.

## Events we send

| event           | when                                    | data |
| --------------- | --------------------------------------- | ---- |
| `run.start`     | the orchestrator accepted the question  | `session_id`, `user_turn_id`, `channel` |
| `node.status`   | a flow node or a tool call changes state | `node`, `phase` (`node`/`tool_call`), `state` (`running`/`completed`/`incomplete`), `label` |
| `agent.start`   | a sub-agent call begins                 | `step_seq`, `agent`, `skill` |
| `agent.end`     | a sub-agent call's output arrives       | `step_seq`, `agent`, `skill`, `status`, `elapsed_ms`, `retries`, `trajectory_ref`, `output` |
| `message.delta` | a piece of the answer                   | `message_id`, `text` |
| `ping`          | every `stream.heartbeat_seconds`        | `{}` |
| `error`         | the run stopped because of a failure    | `code`, `stage`, `agent`, `retriable`, `message`, `trace_ref` |
| `run.end`       | always, exactly once, last              | `status`, `message_id`, `usage`, `chat_id`, `orchestrator_message_id`, `result` |

`run.end.status` is `completed`, `error`, or `cancelled`.

### Where we differ from the document, and why

- **`run.end.result`** is ours. The final-payload hook has to reach the frontend
  and the document defines no event for it, so its output rides on `run.end`.
  It holds the cleaned answer: `text`, `question`, `chat_id`, `message_id`,
  `session_id`, `tools_used`.
- **`agent.end.output`** is ours. The sub-agent's actual return value is the only
  place the frontend can see what a tool produced.
- **`retries` and `trajectory_ref` are always null.** The orchestrator does not
  report retries, and trajectory storage is the Langfuse work that has not
  landed. A number there would be invented.
- **`agent` and `skill`.** The document's example splits a company
  (`samsung_securities`) from a capability (`stock_index_inquiry`). This
  orchestrator reaches all 81 of its sub-agents through a single MCP node, so
  `agent` is that node — `protocol.mcp_agent_name`, `genos_mcp` by default — and
  `skill` is the sub-agent. Change the config value when the topology changes.
- **`message_id` is `msg:1`, ours, not the orchestrator's.** `message.delta`
  needs an id from the first token, and the orchestrator's `chatMessageId` only
  arrives at the very end. That real id is on `run.end` as
  `orchestrator_message_id`.

### Human-in-the-loop events

| event | when | data |
| ----- | ---- | ---- |
| `interaction.required` | an ask_user MCP tool call reached `/hitl` | `interaction_id`, `type`, `prompt`, `options`, `resume_token`, `expires_in_s` |
| `interaction.resolved` | the user answered, or the window expired | `interaction_id`, `resolution` (`answered`/`expired`), `resume_token` |

These do not come from the orchestrator's stream. The agent calls an `ask_user`
MCP tool; that tool POSTs `/hitl {session_id, prompt, type, options}` to us and
blocks; we emit `interaction.required` on the live stream for that session; the
frontend answers with `POST /interactions/{interaction_id}/resolve {answer}`;
`/hitl` returns the answer as the tool's result and the run continues on the
same stream. If nobody answers within `hitl.expires_seconds`, the tool gets
`{status: "expired"}` and the stream gets `interaction.resolved` with
`resolution: "expired"`.

`/ask` requests without a `session_id` cannot receive interactions — the MCP
tool has no name for the run.

The registry behind this holds one entry per open stream, removed in the same
`finally` that closes the stream — its size tracks concurrent runs, never total
users. Bounds live in `config.toml` under `[hitl]`; occupancy is visible at
`GET /healthz/hitl`. It is in-process state: correct on today's single-process
deploy, and the flagged Redis work item the day the serving scales out.

### Events in the document that we do not send

`plan.created` — the orchestrator emits nothing that corresponds to a plan; see
`docs/orchestrator-events.md` for what it does emit. Producing it would mean
inventing it. It becomes one line in `inbound.py` and one in `outbound.py` when
the orchestrator starts sending something real behind it.

## Changing this protocol

`outbound.py` holds the whole mapping as a table. One entry per standard event,
saying which frontend events come out and where each field's value comes from:

```python
ev.Token: Mapping((
    Shape("message.delta", message_id=R("message_id"), text=F("text")),
)),
```

Four readers cover the sources: `F("…")` a field on our standard event,
`R("…")` something the run is carrying, `S("…")` a value from `config.toml`,
`Fn(lambda e, run: …)` anything else. A bare value is a constant.

So renaming an event, moving a field, or adding one is an edit inside that
table. `before=` handles the cases that need bookkeeping first — a counter, a
timer, a value stashed for `run.end`.

Nothing in `inbound.py` changes when the frontend protocol changes. That is the
entire reason our own event sits in the middle.

## Request

```
POST /ask
{"question": "…", "session_id": "sess:abc", "user_turn_id": "turn:12", "user_id": "653"}
```

Only `question` is required. `session_id` turns on the orchestrator's multi-turn
memory; without it every turn starts fresh.

Health: `GET /healthz` for us, `GET /healthz/upstream` for the orchestrator.

## The three exit paths

Every run ends with `run.end`, and the error path emits `error` immediately
before it. One caveat worth stating plainly: when the user closes the page, the
closing event is still produced and logged, but there is no socket left to write
it to. The reason it exists at all is the other two paths, where the frontend is
still listening and needs to tell "crashed" apart from "still thinking".

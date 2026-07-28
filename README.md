# SSE Relay

Sits between our frontend and the GenOS orchestrator, turning their stream into
ours while they are still working.

```
frontend  ──POST /ask──►  THIS SERVICE  ──run/v2──►  orchestrator (GenOS workflow)
          ◄──our SSE────                ◄──their SSE──
```

## Run it

```bash
uv sync
echo 'orchestrator_key=…' > .env
uv run uvicorn sse_relay.app:app --host 0.0.0.0 --port 8080
```

```bash
curl -N -X POST localhost:8080/ask -H 'Content-Type: application/json' \
  -d '{"question":"이번 달 예산 설정 알려줘","session_id":"sess:abc","user_turn_id":"turn:12"}'
```

## Deploying as a GenOS code serving

A code serving revision is four fields: language, build command, start command,
and environment variables. There is no Dockerfile — the platform builds the repo
with the build command in a Python base image and runs the start command.

| field | value |
| ----- | ----- |
| language | `python` |
| build command | `pip install -r requirements.txt` |
| start command | `python main.py` |
| envs | `orchestrator_key` (required); `PORT` if the platform expects a specific one |

`main.py` exists exactly for this: it puts `src/` on the path so nothing needs
`pip install .`, reads `$PORT` (default 8080), and binds 0.0.0.0. The
`uvicorn main:app --host 0.0.0.0 --port 8080` style works too.

If `orchestrator_key` is missing from envs the app fails at startup on purpose,
with a message saying exactly which variable to set — look in the container
logs. After deploying, the request sits in an approval queue before the
container starts; then the log panel walks through scheduling → initializing →
running, and build problems surface there as `CrashLoopBackOff` or similar.

Once running, the endpoint is
`{genos_url}/api/gateway/code_serving/{serving_id}/ask` with the serving's own
bearer key. Check that streaming survives the gateway: the first `message.delta`
must arrive seconds before `run.end`, not together with it.

## Configuration

Everything lives in [`config.toml`](config.toml) — orchestrator URL, workflow id,
paths, timeouts, heartbeat, protocol names, and the two development switches.
Each value names the environment variable that overrides it, so a deployment
never needs a code change or even a file edit.

The API key is the deliberate exception: `config.toml` names the environment
variable to read it from (`orchestrator_key`), so the file stays safe to commit.

Two switches, both on by default:

- `development.strict` — stop loudly on anything unexpected. An event with no
  rule, a line that will not parse, a payload of the wrong shape: all of them
  end the request with the full traceback in the log. Leave it on. Turning it
  off is a production decision to make deliberately, later.
- `development.log_raw` — log every incoming line exactly as received. When a
  stream misbehaves you need the raw text, not a summary.

## Layout

| file | what it is |
| ---- | ---------- |
| `config.py` | reads `config.toml`, applies environment overrides |
| `upstream.py` | the call to the orchestrator and the reading of its stream |
| `events.py` | our standard event — the format in the middle |
| `inbound.py` | **step 1**: their events become ours |
| `outbound.py` | **step 2**: ours become the frontend's |
| `extract.py` | the `.tool.name` path helper the tables are written in |
| `rules.py` | what a rule is: `feed()` and `flush()` |
| `engine.py` | runs both steps; no rule match is an error |
| `protocol.py` | the envelope: `trace_id`, `seq`, `ts` |
| `final_payload.py` | the one hook that shapes the final answer |
| `telemetry.py` | interim logging, deliberately thin, easy to delete |
| `app.py` | the endpoint, heartbeat, disconnect, closing event |

Two steps rather than one because the orchestrator's format is not ours and will
change, while the frontend's is ours and should not. The standard event in the
middle absorbs the change: when Flowise renames something, only `inbound.py`
moves; when the frontend protocol changes, only `outbound.py` does.

Both steps are tables. Adding an event type is a line.

## Documentation

- [`docs/orchestrator-events.md`](docs/orchestrator-events.md) — what the
  orchestrator actually sends, recorded rather than guessed, including three
  places where the GenOS docs are wrong.
- [`docs/frontend-protocol.md`](docs/frontend-protocol.md) — what we send, how it
  maps to the monimo SSE protocol, and how to change it.

## Tools

```bash
uv run tools/probe_raw.py --name mycapture --question "…"   # record a raw stream
uv run tools/analyze_capture.py captures/mycapture.jsonl    # read the event list out of it
uv run tools/disconnect_test.py                             # prove cancellation reaches the orchestrator
```

`probe_raw.py` writes to `captures/`, which is gitignored. Re-run it whenever the
orchestrator changes; `analyze_capture.py` will show any event type the rules do
not know about yet.

## Telemetry

Real telemetry is coming as Langfuse, in decorator style. Nothing here invents a
trace or span id, because Langfuse creates and nests its own.

What that shaped: the things worth tracing are already their own functions —
`stream_orchestrator`, `Transformer.to_canonical`, `Transformer.to_wire`,
`shape_final_payload`. Adding `@observe()` is a line above each `def`, not a
refactor.

Until then, `telemetry.py` logs one line per event (which came in, which rule
matched, what went out, when) and one summary per request (duration, events in
and out, and why it ended). One id per request goes upstream as
`x-genos-trace-id`; that one stays useful after Langfuse lands.

## Notes for whoever picks this up

- Sessions are not a thing here. One request, one upstream connection, one
  transform state, all dying together. Two users are two of these side by side.
- There is no read timeout on the orchestrator connection. A sub-agent thinking
  for ninety seconds sends nothing and looks exactly like a dead socket.
  `timeouts.read_seconds = 0` in `config.toml` says so on purpose.
- `X-Accel-Buffering: no` is set on the response. Without it a proxy can hold the
  whole stream and deliver it at the end, which breaks the point of the service
  while looking like it works.
- Closing the page cancels the upstream request. This is verified against the
  orchestrator's own memory, not our logs — see `tools/disconnect_test.py`.

# 14 — Streaming

> Streaming is not a UI nicety. For agents whose runs take 10–120 s, streaming *is* the difference
> between a usable product and a spinner. It is also the main source of coupling between your agent
> and your frontend.

## 1. Concepts

### Stream modes

| Mode | Payload type | Content |
|---|---|---|
| `values` | `ValuesStreamPart` | Full state after each super-step |
| `updates` | `UpdatesStreamPart` | Only the changed keys per node; parallel updates stream separately |
| `messages` | `MessagesStreamPart` | `(message_chunk, metadata)` — LLM tokens |
| `custom` | `CustomStreamPart` | Whatever you emit via `get_stream_writer()` |
| `checkpoints` | `CheckpointStreamPart` | Checkpoint events (same shape as `get_state()`); needs a checkpointer |
| `tasks` | `TasksStreamPart` | Task start/finish with results and errors; needs a checkpointer |
| `debug` | `DebugStreamPart` | `checkpoints` + `tasks` + extra metadata |

Multiple modes can be combined: `stream_mode=["updates", "messages", "custom"]`.

### The v2 protocol (LangGraph ≥ 1.1) — use it

`version="v2"` gives every chunk the same shape regardless of mode count or subgraph settings:

```python
{"type": "values" | "updates" | "messages" | "custom" | "checkpoints" | "tasks" | "debug",
 "ns": (),          # namespace tuple; populated for subgraph events
 "data": ...}       # payload, type varies by mode
```

v1 changes the output shape based on your options (raw dict / `(mode, data)` /
`(namespace, data)` / `(namespace, mode, data)`), which makes client code fragile. v2 also:

- enables type narrowing on `chunk["type"]`
- returns `GraphOutput` from `invoke()` with `.value` and `.interrupts` (instead of an
  `__interrupt__` key inside the state dict — deprecated)
- coerces Pydantic/dataclass state properly in `values` mode

## 2. How to implement

### Consuming a multi-mode stream

```python
for part in graph.stream(inputs, config,
                         stream_mode=["updates", "messages", "custom"],
                         version="v2"):
    if part["type"] == "messages":
        msg, meta = part["data"]
        if meta.get("langgraph_node") == "final_answer":
            emit_token(msg.content)
    elif part["type"] == "updates":
        for node, update in part["data"].items():
            emit_step(node, update)
    elif part["type"] == "custom":
        emit_progress(part["data"])
```

### Filtering token streams

Only stream tokens from the node/model that the user should see:

```python
# by node
if meta["langgraph_node"] == "final_answer": ...

# by tag on the model
model = init_chat_model("claude-sonnet-4-6", tags=["user_facing"])
if "user_facing" in meta.get("tags", []): ...
```

Disable streaming for a model that shouldn't emit tokens (a classifier, a summariser):
`init_chat_model(..., disable_streaming=True)`.

### Custom progress events

```python
from langgraph.config import get_stream_writer

@tool
def index_corpus(path: str) -> str:
    """Index a corpus of documents."""
    writer = get_stream_writer()
    for i, doc in enumerate(docs):
        index(doc)
        if i % 50 == 0:
            writer({"type": "progress", "done": i, "total": len(docs)})
    return "indexed"
```

This is the correct channel for progress bars, intermediate reasoning summaries, citations, and
"what the agent is doing right now" UI. It also works for models that don't support token streaming.

> **Python < 3.11 async**: `get_stream_writer()` does not work. Add a `writer` parameter to the node
> or tool signature, and pass `RunnableConfig` explicitly into async LLM calls.

### Subgraph streaming

```python
for part in graph.stream(inputs, stream_mode="updates", subgraphs=True, version="v2"):
    depth = len(part["ns"])           # ns identifies which subgraph emitted it
```

Without `subgraphs=True` the parent stream shows a subgraph node as one opaque update.

### Over the network (Agent Server)

```python
from langgraph_sdk import get_client
client = get_client(url=DEPLOYMENT_URL)

async for chunk in client.runs.stream(thread_id, "agent",
                                      input={"messages": [...]},
                                      stream_mode=["messages", "custom"]):
    ...
```

Server-side, events are published to Redis by the queue worker and forwarded by the API server as
SSE. Two important endpoints:

- `/join` — wait for a run's final state without polling (use this instead of a poll loop).
- `/stream` (join-stream) — **reconnect** to an in-progress run's stream after a dropped connection.

## 3. Scenarios

| Scenario | Streaming design |
|---|---|
| Chat UI | `messages` filtered to the user-facing node + `custom` for tool-progress chips |
| Long background job with a dashboard | `custom` progress events + `updates` for stage transitions; client reconnects via join-stream |
| Mobile client on a flaky network | Background run + `/join` for the result; resume the stream on reconnect rather than restarting |
| Debugging a production incident | `debug` / `tasks` modes in a staging replay, plus LangSmith traces |
| Multi-agent UI showing per-agent activity | `subgraphs=True`, key UI panes off `part["ns"]` |
| Approval UX | `values` parts carry `interrupts`; render the interrupt payload as a form ([15](15-human-in-the-loop-and-interrupts.md)) |

## 4. Staff-level considerations

- **The stream is a public API.** Whatever your frontend reads becomes a contract. Define an
  application-level event schema (`{"type": "progress"|"citation"|"status", ...}`) emitted via
  `custom`, and translate LangGraph internals into it. Never let the UI depend on node names.
- **Pin `version="v2"` now.** v1 shape-shifting is a long-term maintenance tax, and the
  `__interrupt__`-in-dict access path is deprecated.
- **Streaming multiplies Redis load.** Every token is an event published from worker → Redis →
  API server → client. High-volume token streaming is a real capacity item; size Redis and consider
  streaming only the final answer's tokens ([19](19-scaling-and-performance.md)).
- **Never poll.** Polling run status at 1 Hz across 10k concurrent runs is a self-inflicted DDoS on
  Postgres. Use `/join` or the stream.
- **Reconnection must be designed, not hoped for.** Long runs will outlive TCP connections. Use
  background runs + join-stream, and make the client idempotent about duplicate/missing chunks.
- **Token streaming leaks intermediate reasoning.** Filter by node/tag so users don't see the
  planner's or the guardrail's output. This is a privacy and product-quality issue, not just noise.
- **Backpressure**: a slow consumer can't slow the graph. Buffer or drop on the client, and treat
  the stream as best-effort while the checkpoint remains the source of truth.

## 5. Anti-patterns

- Streaming raw `values` to the browser (ships the entire state, including internal fields and
  potentially PII, on every step).
- Coupling the UI to node names or LangGraph payload shapes.
- Polling `GET /runs/{id}` in a loop.
- Emitting a `custom` event per item in a 10,000-item loop.
- Streaming tokens from every model call, including summarisers and classifiers.
- Assuming the stream is durable — it isn't; the checkpoint is.

## 6. Design-review questions

1. What is our application-level event schema, and is it decoupled from LangGraph internals?
2. Which model calls stream to the user, and how is that filtered?
3. What happens when the connection drops at t=45 s of a 90 s run?
4. What is the event rate per run at p95, and what does that do to Redis?
5. Does anything sensitive appear in the streamed payloads?
6. Are we on `version="v2"` everywhere, including the SDK client?

## References

- `/oss/python/langgraph/streaming`, `/oss/python/langgraph/event-streaming`
- `/oss/python/langchain/streaming`
- `/langsmith/background-run`, `/langsmith/agent-server-api/streaming/protocol-v2-event-stream-sse`
- `/oss/python/langchain/frontend/overview` (generative UI, join/rejoin, message queues)

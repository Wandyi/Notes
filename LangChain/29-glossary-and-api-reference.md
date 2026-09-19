# 29 — Glossary & API Quick Reference

## Glossary

| Term | Meaning |
|---|---|
| **Actor** | A `PregelNode`; reads from and writes to channels |
| **Agent Server** | The LangSmith Deployment runtime: API servers + queue workers + Postgres + Redis |
| **Assistant** | A versioned configuration over a deployed graph (prompts, model, tools). Deployment-only concept |
| **Channel** | A state key with a value type, update type and update function (reducer) |
| **Checkpoint** | A `StateSnapshot` of a thread at a super-step boundary |
| **Checkpointer** | Thread-scoped persistence (`BaseCheckpointSaver`) |
| **`checkpoint_ns`** | Namespace identifying which graph/subgraph a checkpoint belongs to |
| **Deep Agents** | An agent harness with planning, filesystem, subagents, skills, permissions |
| **Double texting** | A second run arriving while one is active; strategies: enqueue / reject / interrupt / rollback |
| **Durability mode** | `exit` / `async` / `sync` — how often state is persisted |
| **Entrypoint** | Functional API workflow (`@entrypoint`), compiles to a `Pregel` |
| **Harness** | An opinionated agent implementation over a framework (e.g. Deep Agents) |
| **Interrupt** | A durable pause (`interrupt()`), resumed with `Command(resume=...)` |
| **Middleware** | Hooks around the agent loop (`before_*`, `after_*`, `wrap_*`) |
| **`N_JOBS_PER_WORKER`** | Concurrent runs per queue worker (default 10) |
| **Pending writes** | Per-task writes persisted within an in-progress super-step |
| **Pregel** | The LangGraph runtime; BSP over actors and channels |
| **Reducer** | Function combining writes to a channel |
| **Run** | One execution of a graph, possibly on a thread |
| **Skill** | A packaged prompt + knowledge bundle, progressively disclosed |
| **Store** | Cross-thread persistence (`BaseStore`), namespaced key/value with optional vector search |
| **Subagent** | An agent invoked as a tool, with an isolated context window |
| **Super-step** | One BSP tick: plan → execute (parallel) → update; produces a checkpoint |
| **Task** | `@task` in the Functional API; a checkpointed unit of work |
| **Thread** | A conversation/workflow instance identified by `thread_id`; accumulates checkpoints |
| **Time travel** | Replaying or forking from a historical checkpoint |

---

## Graph API

```python
from langgraph.graph import StateGraph, START, END

builder = StateGraph(State, input_schema=In, output_schema=Out, context_schema=Ctx)
builder.add_node("name", fn, retry_policy=..., timeout=..., error_handler=...,
                 cache_policy=..., defer=False)
builder.add_edge("a", "b")
builder.add_conditional_edges("a", route_fn, ["b", "c"])
builder.set_node_defaults(retry_policy=..., timeout=..., error_handler=...)   # 1.2+
graph = builder.compile(checkpointer=..., store=..., cache=..., name="my_graph")
```

Execution:

```python
graph.invoke(inputs, config, context=..., durability="async", version="v2")
graph.stream(inputs, config, stream_mode=[...], subgraphs=True, version="v2")
await graph.ainvoke(...); graph.astream(...); graph.batch([...])
graph.get_state(config, subgraphs=False)
graph.get_state_history(config)
graph.update_state(config, values, as_node="node_name")
graph.get_graph().draw_mermaid()
```

Config keys: `{"configurable": {"thread_id", "checkpoint_id", "checkpoint_ns", ...},
"recursion_limit": 1000, "tags": [...], "metadata": {...}, "callbacks": [...]}`.

## Types

```python
from langgraph.types import (
    Send, Command, Overwrite, RetryPolicy, TimeoutPolicy, CachePolicy,
    default_retry_on, interrupt, Interrupt, GraphOutput, StreamPart,
)
from langgraph.errors import NodeError, NodeTimeoutError, GraphRecursionError, GraphDrained
from langgraph.runtime import Runtime, RunControl, get_runtime
from langgraph.managed import RemainingSteps
from langgraph.config import get_stream_writer
from langgraph.channels import LastValue, Topic, BinaryOperatorAggregate, EphemeralValue, DeltaChannel
```

| Type | Signature / key fields |
|---|---|
| `Send` | `Send(node, state, timeout=None)` |
| `Command` | `Command(update=?, goto=?, graph=?, resume=?)`; `Command.PARENT` |
| `RetryPolicy` | `max_attempts=3, initial_interval=0.5, backoff_factor=2.0, max_interval=128.0, jitter=True, retry_on=default_retry_on` |
| `TimeoutPolicy` | `run_timeout=?, idle_timeout=?, refresh_on="auto"\|"heartbeat"` |
| `CachePolicy` | `key_func=?, ttl=?` |
| `NodeError` | `.node`, `.error` |
| `NodeTimeoutError` | `.node`, `.elapsed`, `.kind`, `.idle_timeout`, `.run_timeout` |
| `StateSnapshot` | `values, next, config, metadata, created_at, parent_config, tasks` |
| `GraphOutput` | `.value`, `.interrupts` (v2 invoke) |
| `Runtime` | `.context`, `.store`, `.stream_writer`, `.execution_info`, `.heartbeat()`, `.drain_requested` |
| `execution_info` | `node_attempt, node_first_attempt_time, thread_id, run_id, checkpoint_id, task_id` |

## Functional API

```python
from langgraph.func import entrypoint, task

@task(retry_policy=..., timeout=...)
def work(x): ...

@entrypoint(checkpointer=..., store=..., timeout=60)
def wf(inputs: dict, previous=None, store=None, writer=None): 
    return entrypoint.final(value=..., save=...)
```

## Persistence

```python
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer

from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import PostgresStore
```

Checkpointer interface: `put`, `put_writes`, `get_tuple`, `list`, `delete_thread` (+ `a*` async).
Store interface: `get`, `put`, `delete`, `search`, `list_namespaces` (+ `a*` async).

## Agents & middleware

```python
from langchain.agents import create_agent, AgentState
from langchain.agents.middleware import (
    AgentMiddleware, hook_config,
    before_agent, before_model, after_model, after_agent,
    wrap_model_call, wrap_tool_call, ModelRequest, ModelResponse,
    SummarizationMiddleware, HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware, ToolCallLimitMiddleware,
    ModelFallbackMiddleware, ModelRetryMiddleware,
    ToolRetryMiddleware, ToolErrorMiddleware,
    PIIMiddleware, TodoListMiddleware, LLMToolSelectorMiddleware,
    ContextEditingMiddleware, ProviderToolSearchMiddleware,
    ShellToolMiddleware, FilesystemMiddleware, FilesystemFileSearchMiddleware,
    SubAgentMiddleware, LLMToolEmulatorMiddleware, RubricGradingMiddleware,
)
```

Hook order: `before_*` first→last; `wrap_*` nested (first is outermost); `after_*` last→first.
Jump targets: `"end"`, `"tools"`, `"model"` (declare with `can_jump_to`).

## Tools

```python
from langchain.tools import tool, InjectedToolCallId
from langgraph.prebuilt import InjectedState, InjectedStore
from langgraph.runtime import get_runtime
```

## Deep Agents

```python
from deepagents import create_deep_agent
create_deep_agent(model=..., tools=[...], system_prompt=..., memory=[...],
                  skills=[...], subagents=[...], backend=..., middleware=[...],
                  checkpointer=..., store=...)
```

## Deployment (`langgraph.json`)

```json
{
  "dependencies": ["."],
  "graphs": {"agent": "./src/agent.py:graph"},
  "env": "./.env",
  "auth": {"path": "./src/auth.py:auth"},
  "checkpointer": {"ttl": {"strategy": "delete|keep_latest",
                           "sweep_interval_minutes": 60, "default_ttl": 43200}},
  "store": {"ttl": {"refresh_on_read": true,
                    "sweep_interval_minutes": 120, "default_ttl": 10080}},
  "dockerfile_lines": []
}
```

CLI: `langgraph dev` · `langgraph build` · `langgraph up`

## SDK

```python
from langgraph_sdk import get_client, Auth
client = get_client(url=..., api_key=...)
client.threads.create/get/search/delete/copy(...)
client.runs.create/stream/join/get/cancel(...)
client.assistants.create/update/search/get_versions/set_latest(...)
client.crons.create/search/delete(...)
client.store.put_item/get_item/search_items/list_namespaces(...)
```

## Environment variables

| Variable | Purpose |
|---|---|
| `LANGSMITH_TRACING` | Enable tracing |
| `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | Tracing auth and project routing |
| `LANGGRAPH_AES_KEY` | Checkpoint encryption key |
| `N_JOBS_PER_WORKER` | Concurrent runs per queue worker |
| `POSTGRES_URI`, `REDIS_URI` | Self-hosted data stores |

## Key defaults worth memorising

| Setting | Default |
|---|---|
| `recursion_limit` | 1000 (since 1.0.6) |
| `durability` | `"async"` |
| `N_JOBS_PER_WORKER` | 10 |
| `RetryPolicy.max_attempts` | 3 |
| Subgraph `checkpointer` | `None` (per-invocation) |
| Double texting | `enqueue` |
| Store `search` limit | 10 |
| Store TTL `refresh_on_read` | `true` |
| Checkpoint TTL `strategy` | `"delete"` |
| Autoscaling | disabled |

## References

- Python API reference: <https://reference.langchain.com/python/>
- Full docs index: <https://docs.langchain.com/llms.txt>
- Common errors: `/oss/python/common-errors`
- Release policy / versioning: `/release-policy`, `/versioning`

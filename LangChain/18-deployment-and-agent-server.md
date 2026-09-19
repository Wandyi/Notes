# 18 — Deployment & Agent Server Architecture

## 1. Concepts

### What a deployment consists of

| Component | Role |
|---|---|
| **Graphs** | The "blueprints". Each graph in `langgraph.json` gets a default **assistant** |
| **PostgreSQL** | Core resources (assistants, threads, runs, crons) + checkpoints + store. Always the source of truth |
| **Redis** | Ephemeral only: run signalling, cancellation, and stream pub/sub between workers and API servers. **No user/run data persists here** |
| **API servers** | Serve client requests (create run, read thread, stream). Do **not** execute agent code |
| **Queue workers** | The execution engine. Claim runs from the durable queue, execute graphs, write checkpoints, publish events |

### Runtime architecture

```
User ──request──► API Server ──create run──► Postgres
                     │             └──notify──► Redis
                     │                            │ wake
                     │                    Queue Worker (Queue Loop → N workers)
                     │                            │
                     │              checkpoints/status ──► Postgres
                     │              events ──► Redis ──► API Server ──SSE──► User
```

### Run execution lifecycle

1. Client → API server → **pending run persisted in the durable task queue**.
2. A queue worker claims the run, **acquires a lease**, loads the graph, executes.
   **At most one run executes per thread at a time.**
3. The worker writes checkpoints (frequency = durability mode) and publishes stream events to Redis.
4. If a client holds a `/stream` connection, the API server subscribes and forwards SSE.
5. On completion the worker updates run status and frees its slot.

Each worker runs up to `N_JOBS_PER_WORKER` (default 10) runs concurrently.

### Three deployment modes

| Mode | Shape | Use |
|---|---|---|
| **Single host** | API server manages the queue itself; no separate workers (default self-hosted) | Dev, low traffic |
| **Split API and queue** | Dedicated queue workers (`queue.enabled: true`); each tier scales independently | Standard production |
| **Distributed runtime** | Separate orchestration and execution processes | Large scale, high concurrency |

Containers are **stateless but persistent**, built from one image. **At least one queue worker must
always be listening** or runs are orphaned.

### Graph loading

- **Compiled graph (recommended)**: export a compiled instance; loaded once at container startup, no
  per-request compile cost.
- **Factory function**: invoked on **every** run — use only for genuine per-run customisation, and
  keep it cheap.

In both cases **the server injects the checkpointer and store**. Do not configure them in graph code.

### Assistants

An assistant is a **versioned configuration over a deployed graph** — prompts, model, tools,
`context` values — with no code change and no redeploy.

- Every graph gets a default assistant; invoke by graph id (`"agent"`) or assistant UUID.
- Editing an assistant creates a **new version**; you can promote or roll back.
- Use cases: per-customer configuration, per-user personalisation, environment variants,
  A/B testing, specialised task variants.

This is the cleanest multi-tenant configuration primitive in the stack — one graph, N tenant
assistants, independent rollback.

### Other platform features

| Feature | What it gives you |
|---|---|
| **Background runs** | Create a run and return immediately; `/join` for the result, `/stream` (join-stream) to reconnect |
| **Cron jobs** | Scheduled runs, optionally bound to a thread |
| **Double texting** | Policy when a second run arrives on a busy thread: `enqueue` (default), `reject`, `interrupt`, `rollback` |
| **TTLs** | Automatic checkpoint/thread and store-item expiry ([07](07-persistence-and-checkpointers.md), [08](08-stores-and-long-term-memory.md)) |
| **Custom auth** | `@auth.authenticate` + `@auth.on` handlers for identity and resource-level authorization ([22](22-security-guardrails-multitenancy.md)) |
| **MCP endpoint / A2A** | Expose your deployment as tools to other agents |
| **Webhooks** | Notify external systems on run completion |

### Double texting — pick deliberately

| Strategy | Behaviour | Use when |
|---|---|---|
| `enqueue` (default) | Second run waits for the first | Ordered task processing |
| `reject` | Second run refused | Idempotent submit buttons; avoid accidental duplicates |
| `interrupt` | Stop the first, keep its progress, insert new input, continue | Chat where the user corrects themselves — **must handle partial tool calls** |
| `rollback` | Discard the first run entirely, start fresh | Chat where the new message supersedes the old |

`interrupt` is the trap: a tool call may be initiated but unfinished. Your graph must tolerate
orphaned/partial tool calls (strip unmatched `ToolMessage`s / dangling tool_calls before the next
model call).

## 2. How to implement

### `langgraph.json`

```json
{
  "dependencies": ["langchain_openai", "./my_package"],
  "graphs": {
    "support_agent": "./my_package/agent.py:graph",
    "batch_enricher": "./my_package/batch.py:workflow"
  },
  "env": "./.env",
  "checkpointer": {
    "ttl": { "strategy": "keep_latest", "sweep_interval_minutes": 60, "default_ttl": 43200 }
  },
  "store": {
    "ttl": { "refresh_on_read": true, "sweep_interval_minutes": 120, "default_ttl": 10080 }
  },
  "dockerfile_lines": ["RUN apt-get update && apt-get install -y libpq-dev"]
}
```

Project layout:

```
my-app/
├── my_package/
│   ├── agent.py          # compiled graph exported here
│   ├── nodes.py
│   ├── state.py
│   └── tools.py
├── langgraph.json
├── pyproject.toml
└── .env
```

### Local development

```bash
langgraph dev            # local server + Studio, hot reload
langgraph build          # build the deployment image
langgraph up             # run the full stack (Postgres + Redis) locally
```

### Client SDK

```python
from langgraph_sdk import get_client

client = get_client(url=DEPLOYMENT_URL, api_key=KEY)

thread = await client.threads.create(ttl={"strategy": "delete", "ttl": 43200})

# background run
run = await client.runs.create(thread["thread_id"], "support_agent",
                               input={"messages": [...]},
                               config={"configurable": {"tenant_id": "acme"}},
                               durability="async",
                               multitask_strategy="rollback")

final = await client.runs.join(thread["thread_id"], run["run_id"])   # no polling
```

### Assistants via API

```python
asst = await client.assistants.create(
    graph_id="support_agent",
    config={"configurable": {"model": "claude-sonnet-4-6", "tone": "formal"}},
    name="acme-prod",
)
await client.assistants.update(asst["assistant_id"], config={...})   # creates a new version
await client.assistants.set_latest(asst["assistant_id"], version=3)  # promote/rollback
```

### Cron

```python
await client.crons.create(assistant_id="digest_agent",
                          schedule="0 9 * * MON-FRI",
                          input={"scope": "weekly"})
```

## 3. Scenarios

| Scenario | Deployment shape |
|---|---|
| Internal tool, <5 rps | Single host, default config |
| Customer-facing chat, bursty | Split API/queue, autoscaling on both tiers, Redis sized for stream fan-out |
| Batch pipeline, 100k runs/night | Split API/queue; many workers, high `N_JOBS_PER_WORKER`, `durability="exit"`, `reject` double-texting |
| Multi-tenant SaaS | One graph, one assistant per tenant, custom auth filtering threads by tenant, per-tenant TTLs |
| Regulated / air-gapped | Self-hosted control+data plane, own Postgres/Redis, `LANGGRAPH_AES_KEY`, no external egress |
| Agents built on other frameworks | Wrap via the Functional API / `deployments-wrap-sdk` and deploy on the same platform |

## 4. Staff-level considerations

- **Two independent scaling axes.** Request concurrency scales with API replicas; run concurrency is
  `queue_workers × N_JOBS_PER_WORKER`. Confusing them is the most common capacity mistake
  ([19](19-scaling-and-performance.md)).
- **One run per thread at a time is a design constraint.** A "thread" is a serialisation point. If a
  tenant funnels all traffic into one thread, you have a per-tenant bottleneck no amount of workers
  fixes. Model your thread granularity around concurrency, not just conversation.
- **Never compile your own checkpointer/store into a deployed graph.** It shadows managed
  persistence, breaks TTLs, Studio, thread APIs and time travel.
- **Factory functions are a per-request tax.** Prefer a compiled graph + assistant config for
  variation.
- **Redis is ephemeral but not optional.** Losing it drops in-flight streams and signalling; runs
  survive (they're in Postgres) but clients see broken connections. Plan the failure mode.
- **Assistants are your config-deployment separation.** Prompt changes become a config version
  promotion with instant rollback, not a code deploy — a huge operational win. Wire assistant
  version promotion into your change-management process.
- **Graph ids are permanent.** Renaming a graph in `langgraph.json` orphans its assistants and
  threads. Treat as breaking.
- **Deploy safety**: without graceful drain ([17](17-durability-fault-tolerance-idempotency.md)),
  every rolling deploy interrupts running work. Set `terminationGracePeriodSeconds` above your
  longest node timeout.

## 5. Anti-patterns

- Running a single queue worker with no replicas (a restart orphans every in-flight run).
- Polling run status instead of `/join` or `/stream`.
- Long-lived HTTP requests waiting for a 5-minute run instead of background runs + join.
- One thread per tenant (serialisation bottleneck) or one thread for everything (unbounded state).
- Secrets in `langgraph.json`/`.env` committed to the repo instead of the platform's secret store.
- `interrupt` double-texting without handling partial tool calls.
- No TTLs → Postgres grows until it becomes the incident.

## 6. Design-review questions

1. What are the expected read rps and write rps, and how were API replicas and queue workers sized?
2. What is our thread granularity, and does it create a per-tenant serialisation point?
3. Which double-texting strategy, and have we tested partial tool calls under `interrupt`?
4. How do prompt changes ship — code deploy or assistant version? What's the rollback?
5. What happens on a rolling deploy to runs currently executing?
6. Where do secrets live, and who can read the deployment's env?
7. What is the retention policy for threads, runs and store items?

## References

- `/langsmith/agent-server`, `/langsmith/agent-server-overview`
- `/langsmith/application-structure`, `/oss/python/langgraph/application-structure`
- `/langsmith/assistants`, `/langsmith/configuration-cloud`
- `/langsmith/double-texting`, `/langsmith/background-run`, `/langsmith/cron-jobs`
- `/langsmith/self-hosted`, `/langsmith/control-plane`, `/langsmith/data-plane`
- `/langsmith/cli` (configuration file reference)

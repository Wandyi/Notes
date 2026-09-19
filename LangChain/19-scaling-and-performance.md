# 19 — Scaling & Performance

## 1. Concepts

### The two concurrencies (get this right first)

| | Request concurrency | Run concurrency |
|---|---|---|
| What | API requests served at once (create run, read thread, stream) | Runs executing at once |
| Scales with | **API server replicas** | **queue workers × `N_JOBS_PER_WORKER`** |
| Bottleneck symptom | HTTP latency/timeouts, 503s | Runs sit in `pending`, queue depth grows |

Creating a run is a **fast write**: the API server persists a pending run and returns; it does not
wait for execution. Raising `N_JOBS_PER_WORKER` increases run throughput but does **not** increase
request-serving capacity.

### Capacity math

```
available_jobs        = number_of_queue_workers × N_JOBS_PER_WORKER
throughput_per_second = available_jobs / average_run_execution_time_seconds

number_of_queue_workers = target_throughput × avg_run_seconds / N_JOBS_PER_WORKER
```

Example: 50 runs/sec, 8 s average run, `N_JOBS_PER_WORKER=25` → `50 × 8 / 25 = 16` workers steady
state. Size autoscaling max from **peak** throughput, not average.

### Tuning `N_JOBS_PER_WORKER` (default 10)

| Workload | Guidance |
|---|---|
| CPU-bound assistant | 10 is usually right; lower it if CPU saturates or runs are delayed |
| Memory-bound | Lower — workers approaching memory limits will OOM |
| **I/O-bound** (the common case: LLM + API calls) | **Raise it** (25–50) |

No hard upper limit, but workers are **greedy**: they claim as many runs as they have free slots.
Setting it too high with bursty traffic → uneven utilisation, longer run times, memory spikes.

### Write load drivers

New runs, checkpoint writes during execution, long-term memory writes, new threads, new assistants,
and deletions. Handled by API servers, queue workers, Redis and Postgres.

### Read load drivers

Getting run results, thread state, searching runs/threads/crons/assistants, retrieving checkpoints
and long-term memory. Handled by API servers, Postgres and Redis.

## 2. How to implement

### Reference Helm configurations (from the docs)

Load levels: low ≈ 5 rps, medium ≈ 50 rps, high ≈ 500 rps; assumes ~1 s average run, moderate CPU/memory.

| | Low/Low | Low reads / High writes | High reads / Low writes | Medium/Medium | High/High |
|---|---|---|---|---|---|
| Write rps | 5 | 500 | 5 | 50 | 500 |
| Read rps | 5 | 5 | 500 | 50 | 500 |
| **API servers** (1 CPU / 2Gi) | 1 | 6 | 10 | 3 | 15 |
| **Queue workers** (1 CPU / 2Gi) | 1 | 10 | 1 | 5 | 10 |
| **`N_JOBS_PER_WORKER`** | 10 | 50 | 10 | 10 | 50 |
| **Redis** | 2Gi | 2Gi | 2Gi | 2Gi | 2Gi |
| **Postgres** | 2 CPU / 8Gi | 4 CPU / 16Gi | 4 CPU / 16Gi (+2 read replicas) | 4 CPU / 16Gi | 8 CPU / 32Gi |

```yaml
# high reads / high writes
api:
  replicas: 15
  resources: {requests: {cpu: "1", memory: "2Gi"}, limits: {cpu: "2", memory: "4Gi"}}
queue:
  enabled: true
  replicas: 10
  resources: {requests: {cpu: "1", memory: "2Gi"}, limits: {cpu: "2", memory: "4Gi"}}
config:
  numberOfJobsPerWorker: 50
postgres:
  resources: {requests: {cpu: "8", memory: "32Gi"}, limits: {cpu: "16", memory: "64Gi"}}
```

Autoscaling (off by default — enable it for bursty traffic):

```yaml
api:
  autoscaling: {enabled: true, minReplicas: 15, maxReplicas: 25}
queue:
  autoscaling: {enabled: true, minReplicas: 10, maxReplicas: 20}
```

On LangSmith **Cloud** the platform autoscales; these Helm values don't apply.

### Enable queue workers

```yaml
queue:
  enabled: true      # offloads queue management from the API server
```

Without this the API server manages the queue itself (single-host mode) — fine for dev, not for load.

### The write-reduction checklist

1. **Minimise redundant checkpointing.** Default durability is `async`. If a run only needs its final
   state, use `exit`:
   ```python
   run = await client.runs.create(thread_id, "agent", durability="exit")
   ```
2. **Fewer super-steps.** Merge trivial nodes; a checkpoint is written per super-step.
3. **Smaller channels.** `DeltaChannel` for append-heavy channels; URIs instead of payloads.
4. **TTLs** so Postgres doesn't grow without bound.

### The read-reduction checklist

1. **Filter and paginate** every search API call.
2. **Never poll** — use `/join` for the final state, `/stream` for live output.
3. **Read replicas** for high-read deployments (`postgres.readReplicas: 2`).
4. **Cache** derived reads (thread lists, assistant configs) at your API layer.

### Application-level performance

| Lever | Effect |
|---|---|
| Async everywhere; `asyncio.to_thread` for unavoidable blocking | Prevents event-loop starvation across co-tenant runs |
| Node caching (`CachePolicy`) | Skips deterministic expensive nodes |
| Prompt caching (provider middleware) | Big TTFT and cost win on stable prefixes |
| Model routing (cheap model for easy turns) | Cuts both latency and spend |
| Parallel tool calls / `Send` fan-out | Wall-clock latency |
| Context trimming | Fewer tokens → lower TTFT and cost |
| Connection pooling for Postgres and HTTP clients | Avoids connection storms at high `N_JOBS_PER_WORKER` |

## 3. Scenarios

| Scenario | Diagnosis and fix |
|---|---|
| Runs queue up; API latency fine | Run-concurrency bound. Add queue workers or raise `N_JOBS_PER_WORKER` (if I/O-bound) |
| API 503s; queue is empty | Request-concurrency bound. Add API replicas |
| Worker OOM | Lower `N_JOBS_PER_WORKER`; shrink state; check for per-run caches |
| Postgres CPU pegged | Checkpoint write amplification. Reduce super-steps, use `exit`/`DeltaChannel`, add TTLs, scale Postgres |
| p99 latency spikes for unrelated users | Blocking I/O in a node starving the event loop |
| Redis memory climbing | High-volume token streaming; reduce streamed events, size Redis up |
| Costs rising faster than traffic | Context growth per turn; add summarisation and measure tokens/run ([23](23-cost-and-token-economics.md)) |

## 4. Staff-level considerations

- **Define an SLO per run class.** Interactive chat: TTFT < 1.5 s, p95 completion < 20 s. Batch:
  throughput, not latency. Approval flows: nothing (they're paused). Different classes deserve
  different deployments or at least different assistants and durability settings.
- **Separate deployments by workload shape.** Batch and interactive traffic on the same workers means
  a nightly job starves your chat users. Two deployments (or two queues) is usually cheaper than the
  incident.
- **Queue depth is your leading indicator.** Alert on pending-run age, not just count. Scale on it.
- **Model provider rate limits are a real capacity constraint**, often before your infra is. Track
  429s per provider, implement token-bucket limiting, and have a fallback model configured.
- **Checkpoint write rate is the number to model early.**
  `runs/sec × super_steps_per_run × avg_checkpoint_bytes` gives you both IOPS and storage growth.
  Do this on a whiteboard before you pick instance sizes.
- **Load-test with realistic runs.** A synthetic 200 ms run tells you nothing about a system whose
  real runs are 12 s with 40 KB checkpoints. Replay real traces.
- **Capacity headroom for HITL**: paused runs consume storage and thread rows indefinitely. Model
  them separately from active runs.

## 5. Anti-patterns

- Scaling API replicas to fix queue backlog (or vice versa).
- Raising `N_JOBS_PER_WORKER` on a CPU/memory-bound assistant.
- One shared deployment for batch and interactive traffic.
- Polling loops (`GET /runs/{id}` at 1 Hz × 10k runs).
- `durability="sync"` by default "to be safe".
- No TTL, then an emergency Postgres migration at 2 TB.
- Load testing with mocked models (hides token-count and rate-limit effects).
- Autoscaling disabled on bursty traffic (it's off by default).

## 6. Design-review questions

1. What are the target read rps and write rps, and what's the average run duration?
2. Show the capacity calculation for queue workers. What's the autoscaling max?
3. Is the assistant CPU-, memory-, or I/O-bound? What is `N_JOBS_PER_WORKER` and why?
4. What is the checkpoint write rate and daily storage growth?
5. Are batch and interactive workloads isolated?
6. What are the provider rate limits, and what happens when we hit them?
7. What do we alert on: queue depth, pending age, p95 run time, 429 rate, checkpoint size?

## References

- `/langsmith/agent-server-scale`
- `/langsmith/agent-server#runtime-architecture`
- `/langsmith/data-plane#autoscaling`, `/langsmith/cloud-platform-features#scaling`
- `/oss/python/langgraph/checkpointers#durability-modes`

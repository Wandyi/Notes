# 28 — Staff-Level Design Review Checklist

One page to bring to (or run) an architecture review for a LangGraph system. Each item links to the
file with the detail.

---

## 1. Problem framing

- [ ] What fraction of this flow is genuinely model-decided? Everything else should be static edges. → [01](01-ecosystem-and-mental-model.md)
- [ ] Which layer are we building on (`StateGraph` / `create_agent` / Deep Agents) and why not the one above? → [01](01-ecosystem-and-mental-model.md)
- [ ] If multi-agent: is the driver context, org boundaries, or parallelism? Can one agent do it? → [12](12-multi-agent-architecture.md)
- [ ] What is the definition of a "successful task", and how will we measure the rate? → [20](20-observability-and-evaluation.md)

## 2. State & data model

- [ ] Every channel classified: state / store / context / external blob. → [03](03-state-channels-and-reducers.md)
- [ ] Every reducer commutative and associative where parallel writes are possible.
- [ ] p50/p99 checkpoint size, and its growth curve with thread length. `DeltaChannel` considered?
- [ ] Nothing in state that we would not want in a database dump (PII, secrets, credentials).
- [ ] Input/output schemas separate from the internal state schema. → [02](02-graph-api-core.md)
- [ ] No rich domain objects in state (serialization/upgrade hazard). → [07](07-persistence-and-checkpointers.md)

## 3. Execution model

- [ ] Super-steps per run, and the resulting checkpoint write rate at target load. → [04](04-pregel-runtime-and-execution-model.md)
- [ ] Maximum fan-out width and the downstream QPS it implies. → [05](05-control-flow-command-send-routing.md)
- [ ] No blocking I/O in nodes; enforced by lint in CI.
- [ ] Every loop bounded by a domain counter, not just `recursion_limit`.
- [ ] `Command` and static edges not mixed on the same node.

## 4. Context engineering

- [ ] Written token budget per model call, by section, with enforcement and monitoring. → [09](09-context-engineering.md)
- [ ] Max size of any tool return value, and what stops it entering the window. → [11](11-tools-and-tool-execution.md)
- [ ] Stable prefix first for prompt caching; volatile content last. → [23](23-cost-and-token-economics.md)
- [ ] Compaction strategy defined, and the pre-compaction transcript preserved somewhere.
- [ ] Tool count visible at p95, and a plan if it grows.

## 5. Reliability

- [ ] Timeout ladder drawn end-to-end and monotonic (client < node < worker < queue lease < gateway). → [17](17-durability-fault-tolerance-idempotency.md)
- [ ] One layer owns retries per dependency; the others are disabled.
- [ ] Every external side effect enumerated with its idempotency key.
- [ ] `durability` chosen per run class, deliberately. → [07](07-persistence-and-checkpointers.md)
- [ ] SIGTERM → `request_drain()` wired; grace period > longest node timeout.
- [ ] Compensation paths for partial failures (saga), themselves idempotent.
- [ ] Dead-letter path for deterministically-failing runs.

## 6. Human-in-the-loop

- [ ] No side effect before an `interrupt()` that isn't idempotent. → [15](15-human-in-the-loop-and-interrupts.md)
- [ ] No conditional/looping/reordered `interrupt()` calls within a node.
- [ ] Interrupt payload schema versioned, JSON-serializable, free of secrets.
- [ ] Resume authorization enforced server-side against the IdP.
- [ ] Approval SLA, escalation and timeout behaviour defined.
- [ ] Thread TTL exceeds the approval SLA.

## 7. Platform & scale

- [ ] Read rps and write rps targets; API replicas and queue workers sized from the capacity formula. → [19](19-scaling-and-performance.md)
- [ ] `N_JOBS_PER_WORKER` matched to the workload's bound (CPU / memory / I/O).
- [ ] Autoscaling enabled if traffic is bursty (it's off by default).
- [ ] Batch and interactive workloads isolated.
- [ ] Thread granularity does not create a per-tenant serialisation point. → [18](18-deployment-and-agent-server.md)
- [ ] Double-texting strategy chosen; partial tool calls handled if `interrupt`.
- [ ] TTLs configured for threads and store items.
- [ ] Checkpointer/store injected by the platform, not compiled into the graph.

## 8. Security & tenancy

- [ ] Worst-case action if the model is fully adversarial — is it acceptable? → [22](22-security-guardrails-multitenancy.md)
- [ ] `tenant_id` never originates from model output; injected at every layer.
- [ ] Capability matrix: tool × identity × worst-case effect × approval requirement.
- [ ] Delegated (user-scoped) credentials preferred over a broad service account.
- [ ] Retrieval filtered at query time, not post-filtered. → [25](25-rag-and-knowledge.md)
- [ ] Code/shell execution sandboxed, egress allowlisted, no prod credentials.
- [ ] Encryption at rest; trace anonymizers; PII middleware on input **and** output.
- [ ] Deletion runbook exists and has been executed end-to-end at least once.
- [ ] Self-hosted: authentication explicitly implemented (there is no default).

## 9. Cost

- [ ] Measured cost per run at p50/p95, and the target. → [23](23-cost-and-token-economics.md)
- [ ] Prompt cache-hit rate tracked; known cache-busters documented.
- [ ] Cheap models used for classification/summarisation/judging.
- [ ] Hard caps on model calls and tool calls; user-visible behaviour when they trip.
- [ ] Cost attributable per tenant / assistant version / subagent.
- [ ] Unit economics modelled at 10× volume.

## 10. Observability & evaluation

- [ ] Consistent trace metadata (tenant, user, release, assistant version, run class). → [20](20-observability-and-evaluation.md)
- [ ] Golden dataset exists, is growing from production failures, and gates merges.
- [ ] Trajectory evals (not just final answers), with a scope-containment (`subset`) check.
- [ ] LLM judges calibrated against human labels.
- [ ] Sampling policy at target volume; 100% of errors traced.
- [ ] Five alerts that would catch a bad deploy within 10 minutes.
- [ ] Trace retention and access control defined.

## 11. Testing

- [ ] Suite runs without network access; models scripted. → [21](21-testing-strategy.md)
- [ ] Interrupt/resume idempotency tested.
- [ ] Real-Postgres checkpointer round-trip with the real state schema.
- [ ] Reducer property tests (associativity) if using `DeltaChannel`.
- [ ] Middleware order asserted.
- [ ] Every past incident has a test or eval case.

## 12. Change management

- [ ] State changes additive; no renames of nodes or keys with live threads. → [26](26-migration-and-versioning.md)
- [ ] Inventory of `interrupted` / `busy` threads before any risky change.
- [ ] `flow_version` pinning where business semantics change.
- [ ] Config (prompts/models) ships as assistant versions with instant rollback; code ships separately.
- [ ] Archived checkpoints as fixtures prove deserialization after dependency bumps.
- [ ] Maximum supported thread staleness defined and TTL-enforced.

---

## The 12 failure modes that cause most production incidents

| # | Failure mode | Detection | Prevention |
|---|---|---|---|
| 1 | `InMemorySaver` in production | State vanishes on deploy | Startup assertion + lint |
| 2 | Non-idempotent side effect before `interrupt()` | Duplicate charges/emails | Enumerate and key every effect |
| 3 | Blocking I/O in a node | p99 spikes for unrelated runs | Async lint rule, load test |
| 4 | Unbounded context growth | Cost/latency creep, quality drop | Token budget + summarisation + alerts |
| 5 | Unbounded agent loop | Cost spike, timeouts | Call limits + `RemainingSteps` |
| 6 | Missing reducer on a fan-in channel | `InvalidUpdateError` under load | Test parallel writes |
| 7 | Checkpoint write amplification | Postgres CPU saturation | Fewer super-steps, `exit`, `DeltaChannel` |
| 8 | Node renamed with paused threads | Resume failures | Add-then-remove + thread inventory |
| 9 | `tenant_id` from model output | Cross-tenant leak | Injection-only; isolation test |
| 10 | Polling instead of `/join` | Postgres read storm | Ban polling in the client SDK wrapper |
| 11 | No graceful drain | Every deploy kills in-flight runs | SIGTERM → `request_drain()` |
| 12 | No eval gate | Silent quality regressions | Block merges on the golden set |

## Interview / review probing questions

1. Walk me through what happens when a worker pod is evicted mid-run. What does the user see?
2. Two nodes write `findings` in the same super-step. What happens, and why?
3. A node calls `charge_card()` then `interrupt()`. What's wrong, and what are three fixes?
4. Queue depth is growing but API latency is flat. What do you scale?
5. Cost per run doubled after a deploy with no traffic change. Where do you look first?
6. How do you ship a new mandatory compliance step without applying it to in-flight threads?
7. Why must a `DeltaChannel` reducer be associative and pure?
8. When would you choose the Functional API over the Graph API, and what do you give up?
9. Design tenant isolation for threads, store, retrieval and traces.
10. Your agent has 60 tools and picks the wrong one 20% of the time. Give three fixes, ordered by cost.

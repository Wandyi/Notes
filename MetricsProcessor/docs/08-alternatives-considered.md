# 08 · Alternatives considered

Design records for the decisions that had a real competing option. Each notes
what would change the answer, because most of these are conditional on scale or
workload rather than universally right.

---

## 1 · Sharded single-writer vs. shared map with a lock

**Chosen:** one goroutine per shard owning its state exclusively.

**Rejected:** `sync.Map`, or a striped `map[string]*seriesState` under an
`RWMutex`, folded by a worker pool.

Locking has three costs here beyond throughput. It puts an atomic operation on a
path that runs millions of times a second; it makes ordering *impossible* to
guarantee, because two workers holding the lock in turn have no defined order
between them; and it makes correctness a property of every future call site rather
than of one goroutine.

Sharding gives ordering for free as a consequence of the structure, not as an
additional mechanism. That is the real argument — the performance win is
incidental.

**What would change it:** a workload where series arrive so unevenly that one
shard is permanently hot. The fix there is a better key (add a label), not a lock.

---

## 2 · Per-series ordering vs. global total ordering

**Chosen:** ordering guaranteed within `(source, series)`.

**Rejected:** a global sequencer giving total order across all producers.

Global ordering needs either a global clock (does not exist) or a consensus
sequencer (serializes the entire pipeline through one point, and takes the whole
system down with it). And nobody asks a metrics system a question whose answer
depends on whether checkout's request preceded billing's.

Per-series ordering is exactly as much as the folds actually need — counter reset
detection and gauge last-write-wins — and no more.

**What would change it:** distributed tracing, where cross-service causality *is*
the product. That is a different system with a different data model.

---

## 3 · Event time vs. processing time

**Chosen:** windows keyed on producer event time, closed by watermark.

**Rejected:** windowing on arrival time at the aggregator.

Processing time is much simpler — no watermarks, no lateness, no skew handling —
and it is wrong in a way that is very hard to debug: the same input produces
different output depending on how busy the system was. A GC pause silently moves
events into the next window. Every historical comparison becomes a comparison of
system load as much as of application behaviour.

Event time costs the watermark machinery and the `AllowedLateness` delay. It buys
results that are reproducible and comparable across time.

---

## 4 · Sequence numbers vs. timestamp sorting

**Chosen:** per-`(source, series)` sequence numbers with a bounded reorder buffer.

**Rejected:** sorting a buffer by event-time timestamp.

Timestamps come from producer clocks. Two points from the same producer can share
a millisecond, and NTP steps can move a clock backwards, producing a *stable* wrong
order that no amount of buffering repairs. Sequence numbers are monotonic by
construction and detect loss (a gap) and replay (a repeat) — neither of which a
timestamp can express at all.

The cost is a real contract on producers, which is why the SDK implements it
rather than leaving it to each service to reinvent. Producers that opt out
(`Seq == 0`) fall back to arrival order, so the contract is opt-in rather than
mandatory.

---

## 5 · Errors as values vs. errors as control flow

**Chosen:** `Result{Aggregates, Err}` — both delivered together.

**Rejected:** returning `(nil, err)` on the first failure.

In an aggregation pipeline, unwinding to report one bad point throws away the fold
of every good point that shared the window. The Go idiom of early-return on error
is right for a request path and actively harmful here.

**Cost:** callers must remember to inspect `Err` on a successful return. Mitigated
by making it a struct field they can see rather than a second return value they
can `_`, and by `ErrorOrNil()` at every `error`-typed boundary — a non-nil
`*MultiError` in a nil-valued `error` slot is the classic Go typed-nil trap.

---

## 6 · `DropNewest` vs. `Block` under overload

**Chosen:** shed by default; block is configurable.

**Rejected:** always applying backpressure to producers.

Backpressure from a metrics system travels up the SDK into the request-handling
goroutines of the services being observed. The monitoring system becomes the
outage. Monitoring should degrade before the thing it monitors.

**What would change it:** billing or SLO-burn pipelines where the metric *is* the
product. `PolicyBlock` exists for exactly those, with a bounded `SubmitTimeout` so
it is bounded blocking rather than unbounded.

---

## 7 · Mergeable sketch vs. exact quantiles

**Chosen:** sparse log-bucketed sketch (DDSketch-shaped), ~1.2% relative error.

**Rejected:** retaining all values and sorting; fixed linear buckets.

Exact quantiles need every value: unbounded memory, and — worse — a fold that
cannot be merged, which would sink both scatter-gather reads and independent
folding across replicas.

Linear buckets bound *absolute* error, which is the wrong guarantee for latency
data spanning microseconds to seconds: buckets are simultaneously too coarse at
the bottom and too fine at the top. Log buckets bound *relative* error uniformly
across magnitudes.

Mergeability is the load-bearing property, not the memory saving. It is what makes
"two replicas folded the same window" a solvable situation.

---

## 8 · Three services vs. a monolith

**Chosen:** ingest / aggregate / query as separate deployables.

**Rejected:** one binary, scaled uniformly.

The tiers scale on genuinely different axes — producer count, series cardinality,
query rate — and only one of them is memory-bound and stateful. Fused, you scale
the expensive tier for the cheap tier's reasons, and you cannot roll the stateless
part without disturbing aggregation state.

**Cost, stated plainly:** two extra network hops, a transport dependency, and a
distributed failure mode (partition ownership) that a monolith does not have.

**What would change it:** below roughly 100k series and 50k points/sec, the
monolith is genuinely better and the whole `pkg/pipeline` core runs standalone in
one process — that is what `cmd/demo` does. The split earns its cost at scale, and
not before.

---

## 9 · StatefulSet vs. Deployment for the aggregator

**Chosen:** StatefulSet with stable per-pod DNS.

**Rejected:** Deployment behind a load-balanced Service.

Partition ownership means the gateway addresses a *specific* replica, not "any
healthy one". A Deployment's random pod names and unordered rollouts would
reshuffle ownership on every deploy, abandoning every open window and cold-starting
every reorder buffer.

**What would change it:** a broker with real consumer groups (Kafka, JetStream).
Then the broker owns partition assignment and rebalancing, the aggregator becomes
addressable-by-anyone, and a Deployment is correct. That is the recommended
production shape; the StatefulSet is what makes the brokerless path work.

---

## 10 · What is deliberately not built

Named so that their absence reads as a decision rather than an oversight.

| Not built | Why | What it would take |
| --- | --- | --- |
| Kafka adapter | keeps the module dependency-free and runnable | ~200 lines on `franz-go`; the real work is committing offsets **after** window close, not after submit |
| Durable WAL for in-flight windows | at most `WindowSize` of loss on a hard kill, and the graceful path already flushes | write-ahead log per shard, replayed on start |
| Sliding / session windows | tumbling covers dashboards, alerting, and billing | more open windows per series; the memory model changes |
| Prometheus exposition | core stays dependency-free; counters are already the right shape | a small adapter over `/v1/stats` |
| Auth on ingest | environment-specific; belongs at the mesh or gateway | mTLS or a token middleware in `httpx` |
| Query cache | fan-out amplification only bites past ~10 replicas | short-TTL cache keyed on the query, in the read tier |

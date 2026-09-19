# Q1 — High-throughput Kafka consumer with a bounded worker pool

**Question.** Design a high-throughput Go service that consumes from Kafka and processes
messages with concurrent workers. It must bound concurrency, apply backpressure when
downstream dependencies are slow, support context-based cancellation, guarantee
at-least-once processing, and expose worker-utilisation and queue-depth metrics.

The answer is below. A compiling, race-tested implementation of the concurrency core lives
in [`reference_impl/`](reference_impl) — `go test -race ./...` passes.

---

## 1. The one invariant everything else serves

Almost every bug in a concurrent Kafka consumer is a violation of a single rule:

> **Never commit an offset until every record at or below it has reached a terminal
> outcome.**

Sequential consumers get this for free. The moment you process concurrently, records
complete out of order — offset 104 finishes while 100 is still waiting on a database — and
the naive "commit the highest offset I've seen" turns at-least-once into silent data loss
the first time the pod is evicted mid-batch.

So the design question is not "how do I run N goroutines". It is **"what is the smallest
piece of shared state that lets me answer *what is safe to commit right now*, and who owns
it?"** Everything else — pool shape, backpressure, shutdown order — falls out of that
answer.

The state is one **commit watermark per partition**, and the owner is a single tracker
guarded by one mutex.

---

## 2. Architecture

```
                    ┌──────────────────────────────────────────────┐
   Kafka broker ───▶│  fetch loop  (exactly 1 goroutine)           │
        ▲           │  Poll → Track(offset) → Submit(record)       │
        │           └───────────────────┬──────────────────────────┘
        │                               │ blocks when queues are full
        │                               │  ◀── BACKPRESSURE LEVEL 1
        │           ┌───────────────────▼──────────────────────────┐
        │           │  bounded channel(s)   cap = QueueDepth       │
        │           └───────────────────┬──────────────────────────┘
        │                               │
        │           ┌───────────────────▼──────────────────────────┐
        │           │  worker pool   (N goroutines, N = hard cap)  │
        │           │    ├─ AdaptiveLimiter.Acquire()  ◀── LEVEL 2 │
        │           │    ├─ handler under WithTimeout()            │
        │           │    ├─ retry w/ jittered backoff → DLQ        │
        │           │    └─ Tracker.Ack(record)                    │
        │           └───────────────────┬──────────────────────────┘
        │                               │            │
        │           ┌───────────────────▼────────┐   │  AIMD feedback
        │           │  OffsetTracker             │   ▼
        │           │  per-partition watermark   │  ┌─────────────────┐
        │           └───────────────────┬────────┘  │ DB / HTTP / gRPC│
        │                               │           └─────────────────┘
        │           ┌───────────────────▼──────────────────────────┐
        └───────────│  commit loop  (1 goroutine, ticker-driven)   │
       lag = the    └──────────────────────────────────────────────┘
    backpressure
      signal
```

**Goroutine inventory** — a design you can't enumerate, you can't debug:

| Goroutine | Count | Owner | Terminates when |
|---|---|---|---|
| fetch loop | 1 | `Consumer.Run` | `ctx` cancelled or fatal poll error |
| workers | `Workers` | `Pool.Start` | their queue is closed and drained |
| commit loop | 1 | `Consumer.Run` | drain completes or `workCtx` cancelled |
| Kafka client internals | client-owned | client | `client.Close()` |

That is `N+2` goroutines plus the client's. There is no `go func()` per message anywhere in
the design — see §9.

**Why the fetch loop is single-threaded.** It is the cheapest correctness win in the whole
system. Because one goroutine calls `Track` for every record, offsets are registered in
ascending order per partition *by construction*, with no lock held across dispatch and no
ordering assertion to get wrong. Parallelism belongs after the queue, not before it.

---

## 3. Bounded concurrency

`PoolConfig.Workers` is the hard ceiling on concurrent handler invocations. Two pool shapes,
one flag:

| `Ordered` | Layout | Guarantee | Cost |
|---|---|---|---|
| `false` | 1 shared channel, N workers | none beyond per-partition offsets | fastest; free work-stealing — a slow record can't idle other workers |
| `true` | N channels, 1 worker each, `fnv32(key) % N` | per-key FIFO | head-of-line blocking within a key; uneven keys skew load |

Ordered mode exists because Kafka's per-partition ordering is the thing people accidentally
throw away when they add a worker pool. If the handler is `UPDATE ... WHERE id = key`,
processing two updates for the same key concurrently reorders them and the last writer wins
arbitrarily. Sharding by key restores order for keys while keeping N-way parallelism across
them — strictly better than the usual "fix" of dropping back to one worker per partition.

Records with no key have no ordering contract, so they round-robin rather than all hashing
to shard 0.

**Sizing N.** Little's Law, not vibes: `N = target_throughput × mean_handler_latency`.
10k msg/s at 8ms of mostly-I/O latency needs ~80 workers, and those are cheap — 80 blocked
goroutines cost ~8KB each of stack and no CPU. For CPU-bound handlers the answer is instead
`GOMAXPROCS`, and more workers only add scheduler churn. Measure with the utilisation metric
in §7, then set N; don't guess and leave it.

---

## 4. Backpressure — three levels, each covering the previous one's blind spot

**Level 1 — the bounded channel.** `Submit` blocks on a full queue. That block propagates
backwards: the fetch loop stops polling, the client's fetch buffer fills, it stops issuing
fetch requests, and the backlog stays **on the broker**, where it is durable, replicated,
and visible as consumer lag on a dashboard you already have. Backlog in a Go channel is none
of those things.

This is why `QueueDepth` should be *small* (single-digit multiples of the worker count). A
deep queue doesn't add throughput — steady-state throughput is set by the workers, not the
buffer — it only adds latency and enlarges the set of records that must be reprocessed after
a crash.

```go
func (p *Pool) Submit(ctx context.Context, r Record) error {
	q := p.queues[p.shardFor(r)]
	select {
	case q <- r:
		p.cfg.Metrics.SetQueueDepth(int(p.queued.Add(1)))
		return nil
	case <-ctx.Done():
		return ctx.Err()   // never block forever, even on the happy path
	}
}
```

**Level 2 — the adaptive limiter.** Level 1 bounds *memory*; it does nothing about *pressure
on the dependency*. When the database degrades from 5ms to 500ms, a fixed pool of 80 workers
cheerfully puts 80 concurrent slow queries on it — the classic retry-storm amplification
where the consumer converts a slow dependency into a dead one.

[`AdaptiveLimiter`](reference_impl/limiter.go) closes that loop with AIMD, the same control
law as TCP congestion control:

- any error, **or a success slower than `LatencyBudget`**, halves the permit ceiling;
- every `limit` consecutive clean calls raises it by one.

Counting slow successes as congestion is the part people skip, and it's the part that
matters: a saturated database usually stays *correct* long before it starts erroring.
Latency is the early signal; errors are the late one.

The floor (`Min`) is never zero, so a fully degraded dependency still gets trickle traffic to
probe recovery — a limiter that closes completely can never discover that the outage ended.
When permits run out, workers block, queues fill, and Level 1 engages: the whole system
degrades to the speed of its slowest dependency instead of collapsing.

**Level 3 — horizontal.** Consumer lag is the scaling signal; partitions are the ceiling.
A consumer group can't usefully run more pods than partitions, so partition count is a
capacity decision made at topic-creation time. Say so in the design review, because it's
expensive to change later.

---

## 5. Context and cancellation

Three distinct context lifetimes, deliberately not one:

```go
ctx        // process lifetime; cancelled by SIGTERM
 └─ workCtx = context.WithoutCancel(ctx)   // workers; cancelled only to ABORT
     └─ hctx = context.WithTimeout(workCtx, HandlerTimeout)   // per record
```

Detaching `workCtx` from `ctx` is what makes graceful shutdown possible at all. If workers
inherited `ctx` directly, SIGTERM would cancel every in-flight handler instantly and "drain"
would be a lie.

**`HandlerTimeout` is mandatory, not optional.** Without it one hung HTTP call pins a worker
forever, which permanently reduces effective concurrency and — because that record never
acks — freezes the partition's watermark. Keep it comfortably below the group's liveness
budget: with librdkafka-based clients that's `max.poll.interval.ms`; Go-native clients
(sarama, franz-go) heartbeat from a background goroutine so a slow handler won't instantly
cost the assignment, but it will still stall rebalance participation.

**Shutdown order is load-bearing.** From [`consumer.go`](reference_impl/consumer.go):

1. **Stop fetching.** `ctx` is cancelled; the fetch loop returns. No further `Submit`, which
   is precisely the precondition that makes closing the queues safe (a send on a closed
   channel panics).
2. **Drain, with a deadline.** `Pool.Close()` closes the queues; workers finish the buffered
   records and exit their `range` loops. Racing it against `DrainTimeout` bounds the wait —
   an unbounded drain lets one stuck handler hold the process past Kubernetes'
   `terminationGracePeriodSeconds` and turn a clean stop into a SIGKILL, which is the exact
   outcome the drain was supposed to avoid.
3. **Abort the stragglers.** On timeout, cancel `workCtx`. Records still running are
   abandoned un-acked — the watermark never passed them, so they're redelivered.
4. **Commit last, on a detached context:**

```go
finalCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), c.cfg.CommitTimeout)
```

`context.WithoutCancel` matters here. `ctx` is *already cancelled* by this point, so a
commit derived from it fails instantly with `context.Canceled` — the single most valuable
commit of the process's life, guaranteed to fail. The observable symptom is "we reprocess
thousands of messages on every deploy", and the cause is one missing line.

Backoff sleeps use `sleepCtx`, not `time.Sleep`, for the same reason: a plain sleep makes
shutdown wait out the longest retry delay.

---

## 6. At-least-once

### 6.1 The watermark

Rules, in [`offsets.go`](reference_impl/offsets.go):

```
commit(P) = lowest offset dispatched from P that is not yet acked
          = highest dispatched offset + 1, when all are acked
```

`Ack` marks the offset done, then retires the contiguous completed prefix. Out-of-order
completion is normal and costs nothing; the only thing that pins the watermark is the
*oldest incomplete* record.

Two details that look like over-engineering and aren't:

- **Offsets are not assumed contiguous.** Log compaction, aborted transactions and control
  records leave holes. A tracker that counts `next++` waits forever for an offset that will
  never be delivered, and the partition silently stops committing. The tracker therefore
  remembers the offsets it actually dispatched (`TestWatermarkToleratesOffsetGaps`).
- **Ack means *terminal*, not *successful*.** A record still being retried must not be
  acked — the frozen watermark is exactly what preserves at-least-once across a crash
  mid-retry.

### 6.2 Poison pills: the deliberate stall

A record that exhausts its retries has only bad options, and the design makes the choice
explicit rather than accidental:

| Config | Behaviour | When |
|---|---|---|
| `DeadLetter` set | publish to DLQ, then ack | default for production |
| `DropOnExhausted: true` | ack and count `dropped` | availability > completeness (telemetry, clickstream) |
| neither (default) | **don't ack** — watermark freezes, lag climbs, someone gets paged | financial/ledger data |

The default is the stall. A stuck partition is loud and recoverable; silent data loss is
neither. `TestPoisonPillStallsWatermarkByDefault` pins that behaviour so a future refactor
can't quietly "fix" it.

Note also that a DLQ publish failure falls through to the same decision — a DLQ you can't
write to must not be treated as a successful dead-letter.

### 6.3 Rebalances

A rebalance is the other way records get processed twice. `OnPartitionsRevoked` must block
and do three things **in order**: wait for in-flight records on the revoked partitions
(`WaitDrained`), commit their final watermark, then forget them. Committing before the drain
acknowledges work the new owner will never redo — the same bug as §1, wearing a different
hat.

Whatever doesn't drain inside the callback's deadline is simply reprocessed by the new
owner. Prefer `cooperative-sticky` assignment so only the moving partitions pay this cost
instead of the whole assignment stopping.

### 6.4 Idempotency is the handler's job

At-least-once means duplicates are *guaranteed*, not merely possible: on every crash, every
rebalance, and every commit whose response was lost. The consumer cannot make the handler
idempotent — it can only make the requirement explicit, which `Handler`'s doc comment does.
In practice: `INSERT ... ON CONFLICT DO NOTHING` keyed on `(topic, partition, offset)`, an
upsert keyed on a business ID, or a dedup table with a TTL longer than the retention you'd
ever replay.

If the write and the offset commit must be atomic, at-least-once is the wrong tool —
that's the transactional-outbox pattern or Kafka EOS with `read_committed`, and it costs
throughput. Worth naming the alternative; usually worth not choosing it.

---

## 7. Observability

Cheap, non-blocking calls behind the [`Metrics`](reference_impl/metrics.go) interface.

```go
kafka_consumer_queue_depth              gauge     // records buffered in the pool
kafka_consumer_inflight_records         gauge     // dispatched, not yet acked
kafka_worker_pool_size                  gauge     // configured N
kafka_worker_busy_seconds_total         counter   // time inside handlers
kafka_downstream_blocked_seconds_total  counter   // time waiting for a limiter permit
kafka_downstream_concurrency_limit      gauge     // AIMD's current ceiling
kafka_record_process_seconds            histogram // by result: ok|retry_exhausted|dropped
kafka_record_retries_total              counter
kafka_offset_commit_seconds             histogram
kafka_offset_commit_failures_total      counter
kafka_consumer_lag                       gauge    // from the client, per partition
```

**Worker utilisation is a counter of busy-seconds, not a percentage gauge.** A gauge sampled
every 15s averages away exactly the bursts you're hunting. A counter reconstructs
utilisation at any resolution, after the fact, and survives restarts:

```promql
sum(rate(kafka_worker_busy_seconds_total[1m])) / avg(kafka_worker_pool_size)
```

`1.0` means every worker was busy every second. The same trick applies to
`blocked_seconds_total`, which answers "how much of our capacity is spent *waiting* on the
database" — usually the most actionable number on the dashboard.

**The diagnostic matrix.** Four metrics, read together, localise the bottleneck without
attaching a profiler:

| Utilisation | Queue depth | Blocked time | Lag | Diagnosis | Action |
|---|---|---|---|---|---|
| high | high | low | rising | handler is the bottleneck | more workers, or make the handler faster |
| low | high | **high** | rising | downstream saturated; the limiter is doing its job | fix the DB, don't add workers |
| low | **0** | low | rising | fetch-bound: not enough data arriving | bigger fetch sizes, more partitions/pods |
| low | 0 | low | flat, but **watermark not advancing** | poison pill | check `inflight == 1` and the stalled offset |

That last row is why `inflight` and the watermark are exported separately from lag. "Lag is
rising" alone can't distinguish a slow consumer from a stuck one, and the responses are
opposite.

**Alerts worth having:** utilisation `> 0.85` for 10m (scale before you're behind);
`concurrency_limit` below `Max` for 10m (a dependency is degraded — often the earliest
warning anywhere in the fleet); commit failures non-zero (duplicates are accumulating);
watermark flat while lag `> 0` (stall).

Tracing: start the span at dispatch and propagate through `hctx`, with the Kafka headers as
the parent context so the producer's trace stitches to the consumer's. Log at record level
only for terminal failures — one log line per message at 10k msg/s is its own outage.

---

## 8. Failure modes

| Failure | Detection | Behaviour | Guarantee |
|---|---|---|---|
| Handler returns transient error | `retries_total` | jittered exponential backoff, up to `MaxRetries` | retried in place |
| Handler returns `ErrFatal` | `process{result="retry_exhausted"}` | skips retries, straight to DLQ | no wasted attempts |
| Handler hangs | `process_seconds` p99 | `HandlerTimeout` fires, counts as an error | worker is never lost |
| Downstream slow | `blocked_seconds`, `concurrency_limit` | AIMD halves concurrency; backpressure to broker | no amplification |
| Downstream down | `concurrency_limit == Min` | trickle probes; lag grows on the broker | recovers automatically |
| Broker unavailable | poll errors | client retries; workers drain | no loss |
| Commit RPC fails | `commit_failures_total` | next tick resends an absolute offset | idempotent, self-healing |
| Pod evicted (SIGTERM) | — | drain → final commit | duplicates ≤ in-flight |
| Pod killed (SIGKILL/OOM) | restart | resume from last commit | duplicates ≤ queue + in-flight |
| Rebalance | rebalance events | drain revoked partitions, commit, release | duplicates on the moving partitions only |
| Poison pill | watermark flat, `inflight == 1` | DLQ, drop, or stall — per config | explicit, never silent |

Full-jitter backoff (`ExponentialBackoff`) is deliberate: fixed backoff re-synchronises every
worker onto the same retry schedule and hits a recovering dependency with a thundering herd
at exactly the wrong moment.

---

## 9. Rejected alternatives

**`go handleMessage(msg)` per message.** Unbounded concurrency: one traffic spike and you
have 200k goroutines, an exhausted connection pool, and an OOM. It also has no join point,
so shutdown can't drain and offsets can't be tracked. This is the single most common
version of this code in the wild.

**Unbounded or very deep channel as the queue.** Converts backpressure into latency and then
into an OOM. The broker is a better buffer than your heap in every dimension: it's durable,
replicated, observable, and its buffer doesn't die with the pod.

**A semaphore instead of a worker pool.** `sem <- struct{}{}` before `go func()` does bound
concurrency, but you still pay goroutine setup per message and still need a `WaitGroup` for
shutdown. A fixed pool over a channel is fewer moving parts and reuses stacks.

**Committing from the worker.** Committing the offset you just finished is the §1 bug
verbatim: worker A finishes 104 while worker B is still on 100. It also multiplies commit
RPCs by the message rate. Commit from one goroutine, on a ticker, from the watermark.

**Per-message `sync.Mutex` around a `map[int64]bool`.** Works, but grows without bound
unless something retires entries, and every worker contends on it. The tracker retires the
contiguous prefix on each ack and reclaims the slice, so its memory is bounded by in-flight
records — which is bounded by `QueueDepth + Workers`.

**Fixed concurrency with no adaptive limiter.** Correct but fragile: it holds pressure
constant precisely when the dependency needs it reduced.

---

## 10. Wiring it up

```go
func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	tracker := kafkaworker.NewOffsetTracker()
	limiter := kafkaworker.NewAdaptiveLimiter(kafkaworker.LimiterConfig{
		Start:         32,
		Min:           4,
		Max:           64,               // == Workers
		LatencyBudget: 250 * time.Millisecond,
		OnChange:      func(n int) { concurrencyLimitGauge.Set(float64(n)) },
	})

	pool := kafkaworker.NewPool(kafkaworker.PoolConfig{
		Workers:        64,              // N = throughput × latency (Little's Law)
		QueueDepth:     128,             // small on purpose: the broker is the buffer
		Ordered:        true,            // per-key ordering; handler is not commutative
		HandlerTimeout: 5 * time.Second, // << group liveness budget
		MaxRetries:     4,
		Handler:        orders.NewHandler(db),
		DeadLetter:     dlq.New(client, "orders.dlq"),
		Limiter:        limiter,
		Tracker:        tracker,
		Metrics:        promMetrics,
	})

	consumer, err := kafkaworker.NewConsumer(kafkaworker.ConsumerConfig{
		Fetcher:        adapter, // franz-go / sarama adapter
		Committer:      adapter,
		Pool:           pool,
		Tracker:        tracker,
		Metrics:        promMetrics,
		CommitInterval: 5 * time.Second,
		DrainTimeout:   25 * time.Second, // < terminationGracePeriodSeconds (30s)
		OnError:        func(err error) { slog.Error("consumer", "err", err) },
	})
	if err != nil {
		log.Fatal(err)
	}
	if err := consumer.Run(ctx); err != nil {
		log.Fatal(err)
	}
}
```

**Client settings that matter** (franz-go names; equivalents exist elsewhere):

| Setting | Value | Why |
|---|---|---|
| `DisableAutoCommit` | **on** | auto-commit acknowledges records that are still in flight — §1 again, enabled by default |
| `Balancers` | `CooperativeSticky` | only moving partitions stop during a rebalance |
| `FetchMaxBytes` / `FetchMaxPartitionBytes` | tuned | the client's own fetch buffer is another backpressure stage; it stops fetching once full |
| `terminationGracePeriodSeconds` | `> DrainTimeout + CommitTimeout` | otherwise the drain is theatre and SIGKILL wins |

---

## 11. What the reference implementation proves

`cd reference_impl && go test -race -count=12 ./...`

| Test | Property |
|---|---|
| `TestWatermarkHoldsForOldestInFlight` | out-of-order completion never advances past an in-flight offset |
| `TestWatermarkToleratesOffsetGaps` | compaction/transaction holes don't wedge the watermark |
| `TestAtLeastOnceUnderConcurrentCompletion` | end-to-end, 4 partitions × 250 records, jittered latency, 5% error rate, 2ms commit ticker — **every commit is checked against the completion set**, and all 1000 records complete |
| `TestQueueDepthIsBounded` | buffering never exceeds `QueueDepth` |
| `TestOrderedModePreservesPerKeyOrder` | per-key FIFO under 8-way parallelism |
| `TestPoisonPillStallsWatermarkByDefault` | no silent loss without a DLQ |
| `TestDeadLetterUnblocksWatermark` | DLQ keeps the partition moving |
| `TestBackoffIsCancellable` | shutdown doesn't wait out a 1-hour backoff |
| `TestLimiterBoundsConcurrency` / `…HalvesOnFailure` / `…TreatsSlowSuccessAsCongestion` | AIMD control law |
| `TestLimiterAcquireHonoursContext` / `…ShrinkWithWaiters` | no permit leaks on cancellation or shrink |

The end-to-end test is the one worth keeping. It asserts the §1 invariant *on every commit*
rather than checking a final total, so an ordering bug fails the test on the commit that
introduced it instead of hiding behind an eventually-correct end state.

---

## 12. Summary

| Requirement | Mechanism |
|---|---|
| Bounded concurrency | fixed pool of `N` workers over bounded channels; `Ordered` shards by key hash for per-key FIFO |
| Backpressure | blocking `Submit` → fetch loop stalls → backlog on the broker; AIMD limiter on the downstream, driven by latency *and* errors |
| Context cancellation | detached `workCtx` for drain-vs-abort, `HandlerTimeout` per record, cancellable backoff, `WithoutCancel` for the final commit |
| At-least-once | per-partition watermark = lowest un-acked offset; commit after drain; explicit poison-pill policy; idempotent handlers |
| Observability | busy-seconds and blocked-seconds counters (not sampled gauges), queue depth, in-flight, adaptive limit, plus a four-metric matrix that localises the bottleneck |

The through-line: **concurrency is easy, coordination is the design.** One goroutine owns
dispatch, one mutex owns the watermark, one ticker owns commits, and every channel is
bounded so that when something downstream slows down, the pressure lands on the broker —
where it's durable and visible — instead of on the heap.

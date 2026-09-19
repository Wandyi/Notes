# 06 · API reference

## ingest-gateway (`:8081`)

### `POST /v1/metrics`

```json
{
  "source": "checkout-7d9f-x2k",
  "points": [
    {
      "service": "checkout",
      "name": "http_requests_total",
      "kind": "counter",
      "value": 1487,
      "labels": {"route": "/pay", "pod": "checkout-7d9f-x2k"},
      "event_time": "2026-08-05T14:37:52.023Z",
      "source": "checkout-7d9f-x2k",
      "seq": 1487
    }
  ]
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `service`, `name` | yes | together with `labels`, form the series key |
| `kind` | yes | `counter` \| `gauge` \| `histogram` |
| `value` | yes | must be finite; counters are **cumulative** |
| `event_time` | yes | RFC3339; windowing uses this, never arrival time |
| `source` | yes | producer instance; defaults to the batch `source` |
| `labels` | no | max 32; sorted server-side for a canonical key |
| `seq` | no | per `(source, series)`, monotonic. Omit to opt out of reordering |

> Two contract details that are easy to get wrong and expensive to discover later:
> **cumulative counters need an instance label** (`pod`), or several replicas'
> independently rising totals collapse into one series where every interleaving
> looks like a reset; and **`seq` is per `(source, series)`**, not per producer.

**Responses**

| Status | Meaning |
| --- | --- |
| `202 Accepted` | all points accepted |
| `207 Multi-Status` | partial success — see `errors[]` |
| `400 Bad Request` | malformed body, or every point invalid |
| `413` | batch exceeds `MAX_BATCH_POINTS` |
| `503` | transport unavailable — **retry this batch** |

```json
{
  "accepted": 997,
  "rejected": 3,
  "errors": [
    {"code": "validation", "source": "checkout-7d9f-x2k",
     "message": "", "at": "2026-08-05T14:37:52Z"}
  ]
}
```

### `GET /v1/stats`, `GET /healthz`, `GET /readyz`

---

## aggregator (`:8082`)

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/ingest` | receive an envelope (push transport). `503` ⇒ retry |
| `GET /v1/query` | this replica's slice of the aggregates |
| `GET /v1/stats` | pipeline, store, and transport counters |
| `GET /v1/errors` | bounded sample of recent partial failures |
| `GET /v1/quarantine` | currently shed producers and cooldown expiry |
| `GET /healthz`, `/readyz` | liveness and readiness |

`GET /v1/stats` shape:

```json
{
  "pipeline": {
    "accepted": 36276, "received": 36276, "folded": 35976, "emitted": 138,
    "windows": 20, "rejected": 285, "backpressure": 0, "late": 1,
    "duplicates": 1, "gaps": 0, "panics": 0, "quarantines": 1, "shed": 15,
    "dropped_results": 0, "queue_depth": 0, "queue_capacity": 16384,
    "open_windows": 0
  },
  "store": {"series": 30, "aggregates": 138, "evicted": 0,
            "rejected_cardinality": 0},
  "windows_consumed": 20, "aggregates_stored": 138, "store_failures": 0
}
```

---

## query-api (`:8083`)

### `GET /v1/query`

| Parameter | Example | |
| --- | --- | --- |
| `service` | `checkout` | exact match |
| `name` | `http_latency_ms` | exact match |
| `label` | `label=route:/pay` | repeatable; all must match |
| `from`, `to` | RFC3339 or unix seconds | window overlap |
| `limit` | `500` | |

```bash
curl 'localhost:8083/v1/query?service=checkout&name=http_latency_ms&label=route:/pay&limit=100'
```

```json
{
  "aggregates": [{
    "series_key": "checkouthttp_latency_msroute=/pay",
    "service": "checkout", "name": "http_latency_ms", "kind": "histogram",
    "window_start": "2026-08-05T14:37:20Z", "window_end": "2026-08-05T14:37:30Z",
    "count": 3994, "sum": 199700.5, "min": 5.01, "max": 99.98,
    "last": 48.9, "last_event_time": "2026-08-05T14:37:29.998Z",
    "p50": 49.6, "p90": 89.9, "p99": 99.1
  }],
  "count": 1,
  "replicas": {"asked": 3, "answered": 3},
  "degraded": false
}
```

| Status | Meaning |
| --- | --- |
| `200` | all replicas answered |
| `206 Partial Content` | fewer than `MIN_REPLICAS` answered; data is incomplete |
| `503` | no replica answered |

`degraded` and `replicas` let a caller distinguish **"no data"** from **"we could
not see all of it"** — a distinction that silently disappears in most fan-out
APIs, and one that turns into a false all-clear on a dashboard.

### Aggregate fields

| Field | Kinds | |
| --- | --- | --- |
| `count`, `sum`, `min`, `max` | all | |
| `last`, `last_event_time` | all | last by **event time** |
| `delta`, `rate`, `resets` | counter | reset-corrected increase, per second |
| `p50`, `p90`, `p99` | histogram | log-bucketed sketch, ~1.2% relative error |
| `out_of_order`, `late`, `gaps` | all | per-series data quality |
| `partial` | all | closed while input was known to be missing |

---

## Go APIs

### Streaming

```go
p, err := pipeline.New(cfg)
defer p.Close(ctx)

go func() {                         // required: a stalled consumer is backpressure
    for res := range p.Results() {
        store.Put(ctx, res.Aggregates)
        if res.Err != nil {
            log.Warn("partial", "failures", res.Err.Total(), "summary", res.Err)
        }
    }
}()

if err := p.Submit(ctx, point); err != nil {
    if errors.Is(err, merr.ErrBackpressure) { /* shed; retry later */ }
}
```

### Synchronous

```go
snap, err := pipeline.Collect(ctx, cfg, points)  // err: config failure only
for _, a := range snap.Aggregates { ... }
if snap.Err != nil {
    fmt.Println(snap.Err)                        // "3 partial failures (validation=2 panic=1); first: ..."
    errors.Is(snap.Err, model.ErrNoService)      // specific causes survive
}
```

### Producer SDK

```go
c, _ := client.New(client.Options{
    Endpoint: "http://ingest-gateway", Service: "checkout", Source: os.Getenv("POD_NAME"),
    MaxBatch: 500, FlushInterval: 100 * time.Millisecond, MaxRetries: 2,
})
defer c.Close(ctx)

c.Count("http_requests_total", total, labels)   // cumulative
c.Gauge("inflight_requests", n, labels)
c.Observe("http_latency_ms", ms, labels)        // → p50/p90/p99
```

The SDK assigns sequence numbers, batches, and preserves order across retries.
Export `c.Stats()` — a client dropping points is invisible server-side.

## Configuration

**gateway:** `LISTEN_ADDR`, `AGGREGATOR_ENDPOINTS`, `BUS_PARTITIONS`,
`MAX_BATCH_POINTS`, `PUBLISH_TIMEOUT`, `PUBLISH_RETRIES`, `SHUTDOWN_GRACE`

**aggregator:** `LISTEN_ADDR`, `SHARDS`, `WINDOW_SIZE`, `ALLOWED_LATENESS`,
`REORDER_DEPTH`, `MAX_REORDER_DELAY`, `SHARD_QUEUE_SIZE`, `RESULT_QUEUE_SIZE`,
`MAX_FUTURE_SKEW`, `OVERFLOW_POLICY`, `QUARANTINE_ERROR_RATE`, `RETENTION`,
`MAX_SERIES`, `MAX_WINDOWS_PER_SERIES`, `SHUTDOWN_GRACE`

**query-api:** `LISTEN_ADDR`, `AGGREGATOR_ENDPOINTS`, `FANOUT_TIMEOUT`,
`MIN_REPLICAS`

**all:** `LOG_LEVEL`, `LOG_FORMAT` (`json` | `text`)

# Observability and SLOs for Kafka

Nine docs have referenced metrics. This one collects them, explains which ones mislead, and
builds the alert set that actually catches the failures in docs 01–09 — mapped scenario by
scenario, so you can check your own alerting against it.

It assumes the methods from [`Observability/`](../Observability/README.md) in this repository:
**RED** (Rate, Errors, Duration) for things that serve requests, **USE** (Utilization,
Saturation, Errors) for finite resources. If those are unfamiliar, read
[`Observability/03-red-vs-use-and-golden-signals.md`](../Observability/03-red-vs-use-and-golden-signals.md)
first — it is short and it is the framework this doc applies.

## Kafka needs both methods, for different things

A Kafka deployment is two things wearing one name, and instrumenting it as one thing is why most
Kafka dashboards are unhelpful.

**The cluster is a resource.** It has finite disk, finite page cache, finite network, finite
request-handler threads, and finite partition capacity. USE applies: utilisation (how much of the
disk is used), saturation (how deep is the request queue), errors (failed produce requests).
Nobody has a "Kafka SLO" in the sense of a request success rate, because the cluster is not what
users interact with.

**The pipeline is a request-serving system**, with the unusual property that the request and the
response are separated in time. A record enters at `checkout-api` and "completes" when
`order-processor` has written it to `orders-db`. RED applies to that journey — rate of records,
errors in processing, and **duration measured end to end**, which is the signal users actually
feel.

⚠️ The mistake almost every team makes is instrumenting only the first of these. Broker
dashboards are easy — the JMX beans exist, the exporters are off-the-shelf — and they cannot tell
you that orders are arriving in the database forty minutes late. Every broker metric can be green
during `L-05` (a poison message wedging one partition), `D-04` (a hanging transaction), and
`C-01` (a rebalance storm), because in all three the cluster is working perfectly and the
pipeline is not.

**Instrument the pipeline first.** It is more work, because it requires application code rather
than an exporter, and it is where the incidents are.

---

## The pipeline signal: end-to-end freshness

The one metric worth building before any other. In every consumer, after processing each record:

```java
long ageMillis = System.currentTimeMillis() - record.timestamp();
pipelineFreshness
    .labels(record.topic(), consumerGroupId)
    .observe(ageMillis / 1000.0);
```

This is a histogram of **how old a record was when it finished being processed**, which answers
the question everyone actually asks during an incident — "how far behind are we?" — in units a
non-Kafka person understands.

What makes it better than consumer lag, which is the metric most teams use instead:

| | Consumer lag (records) | End-to-end freshness (seconds) |
|---|---|---|
| Comparable across topics with different rates | No | Yes |
| Meaningful when traffic is zero | No — reads zero whether healthy or dead | Yes — keeps climbing when stuck |
| Includes producer-side and broker-side delay | No | Yes |
| Directly expressible as an SLO | No | Yes |
| Available without application changes | Yes | No |

The last row is why lag is popular. Build freshness anyway; lag remains useful as the capacity
signal (doc 06) and freshness becomes the correctness-and-experience signal.

⚠️ Freshness depends on record timestamps, so everything in doc 07 (`T-03`) about producer clocks
applies. On a topic where producers set timestamps from event time rather than send time,
freshness measures something different from what you expect. Use `LogAppendTime` on topics where
freshness is the primary signal, or carry a separate `produced_at` header.

---

## An SLO for a streaming pipeline

Pipelines need **two** SLIs, because they have two distinct failure modes and one metric cannot
cover both.

**SLI 1 — freshness.** *The proportion of order-lifecycle events written to `orders-db` within
30 seconds of being produced.*

Where 30 seconds comes from, rather than from a round number: `order-processor` feeds
`invoice-rollup`, which runs hourly at ten past the hour (from the CronJobs collection). An event
delayed more than a few minutes misses its invoicing window and lands in the next hour's rollup.
Thirty seconds gives two orders of magnitude of margin against that deadline, which is the
correct shape for a target — tight enough to detect problems early, loose enough that ordinary
variation does not consume budget.

**SLI 2 — completeness.** *The proportion of events produced that are eventually processed.* This
exists because freshness cannot see a dropped record. A record lost to `R-01` or skipped by
`C-10` is never processed, so it never contributes a freshness observation, and a pipeline that
silently drops 2% of records can show perfect freshness.

Completeness has to be measured by comparison, not by instrumentation:

```promql
# Events processed vs events produced, over a window long enough to absorb normal lag
  sum(increase(pipeline_records_processed_total{topic="orders.created"}[1h]))
/ sum(increase(kafka_server_brokertopicmetrics_messagesinpersec_total{topic="orders.created"}[1h]))
```

**The target, and its budget.**

```
SLO: 99.5% of events fresh within 30 s, measured over 30 days

error budget:  0.5% × 43,200 minutes = 216 minutes = 3.6 hours per month
```

Three and a half hours a month of a pipeline more than 30 seconds behind. Check that against doc
06's derivation: a single twenty-minute flash sale produces a **forty-nine-minute** recovery, and
during most of that the pipeline exceeds 30 seconds of lag. So **two flash sales a month consume
half the budget** — which tells you either the target is wrong, or `order-processor` needs more
capacity, and that is exactly the conversation an SLO is supposed to force.

That is the test of whether an SLO is set correctly: it should be achievable with the system
working as designed, and it should be *threatened* by the things you want to spend engineering
effort on. A budget that is never touched is set too loosely to influence any decision.

---

## The broker metric inventory

JMX MBean names, with the Prometheus JMX-exporter form beneath. Grouped by the USE dimension
they serve.

### Utilisation — how much of the resource is in use

| Metric | Prometheus | Watch for |
|---|---|---|
| `kafka.log:type=Log,name=Size` | `kafka_log_log_size` | Disk growth; feeds the projection alert in `B-02` |
| `kafka.server:type=BrokerTopicMetrics,name=BytesInPerSec` | `kafka_server_brokertopicmetrics_bytesinpersec_total` | Ingress against network capacity |
| `kafka.server:type=BrokerTopicMetrics,name=BytesOutPerSec` | `..._bytesoutpersec_total` | Egress; the input to the cross-zone cost in `S-09` |
| `kafka.controller:type=KafkaController,name=GlobalPartitionCount` | `kafka_controller_kafkacontroller_globalpartitioncount` | Partition growth against the `S-01` limits |

### Saturation — how much work is queued or delayed

This is where the real signal is, and it is the dimension most dashboards omit.

| Metric | Prometheus | Watch for |
|---|---|---|
| `RequestMetrics,name=RequestQueueTimeMs,request=Produce` | `kafka_network_requestmetrics_requestqueuetimems` | Handler-thread starvation (`S-10`) |
| `RequestMetrics,name=LocalTimeMs,request=Produce` | `..._localtimems` | **Local disk or page-cache pressure** (`B-06`) |
| `RequestMetrics,name=RemoteTimeMs,request=Produce` | `..._remotetimems` | **Replication is the bottleneck** (`B-06`) |
| `ReplicaFetcherManager,name=MaxLag,clientId=Replica` | `kafka_server_replicafetchermanager_maxlag` | Furthest-behind follower (`R-08`) |
| `KafkaRequestHandlerPool,name=RequestHandlerAvgIdlePercent` | `kafka_server_kafkarequesthandlerpool_requesthandleravgidlepercent` | Below 0.3 means handlers are saturated |
| `SocketServer,name=NetworkProcessorAvgIdlePercent` | `kafka_network_socketserver_networkprocessoravgidlepercent` | Below 0.3 means `num.network.threads` is too low |
| Node exporter: disk read throughput | `node_disk_read_bytes_total` | **Near zero when healthy.** Non-zero means the page-cache cliff (`L-09`) |

The four-way produce-latency breakdown — queue, local, remote, response — is the highest-value
panel on a Kafka dashboard, because it distinguishes five different incidents at a glance. Doc 01
(`B-06`) has the interpretation table.

The disk-read row deserves emphasis: a healthy Kafka cluster serves reads from page cache and
performs almost no physical reads. That makes `node_disk_read_bytes_total` an unusually clean
signal — there is no tuning question about the threshold, because the healthy value is zero.

### Errors and correctness

| Metric | Prometheus | Watch for |
|---|---|---|
| `ReplicaManager,name=UnderReplicatedPartitions` | `kafka_server_replicamanager_underreplicatedpartitions` | Any non-zero (`B-05`) |
| `ReplicaManager,name=UnderMinIsrPartitionCount` | `..._underminisrpartitioncount` | Any non-zero — **writes are failing** (`R-05`) |
| `KafkaController,name=OfflinePartitionsCount` | `kafka_controller_kafkacontroller_offlinepartitionscount` | Any non-zero — total outage for those partitions |
| `KafkaController,name=ActiveControllerCount` | `kafka_controller_kafkacontroller_activecontrollercount` | Cluster-wide sum must be exactly 1 (`B-10`) |
| `ControllerStats,name=UncleanLeaderElectionsPerSec` | `kafka_controller_controllerstats_uncleanleaderelectionspersec_total` | Any increase — **data was discarded** (`R-07`) |
| `LogManager,name=OfflineLogDirectoryCount` | `kafka_log_logmanager_offlinelogdirectorycount` | Any non-zero (`B-03`) |
| `LogCleanerManager,name=time-since-last-run-ms` | `kafka_log_logcleanermanager_time_since_last_run_ms` | Rising — **the cleaner died** (`T-09`) |
| `LogCleanerManager,name=uncleanable-partitions-count` | `..._uncleanable_partitions_count` | Any non-zero (`T-09`) |

⚠️ The last two are missing from almost every Kafka dashboard and they catch the slowest, most
expensive failure in doc 07. Add them.

### Client metrics that brokers cannot give you

| Metric | Side | Catches |
|---|---|---|
| `kafka_producer_buffer_available_bytes` | Producer | Buffer exhaustion before it blocks threads (`P-02`) |
| `kafka_producer_record_error_total` | Producer | Sends failing while the application ignores callbacks (`P-01`) |
| `kafka_consumer_coordinator_rebalance_total` | Consumer | Rebalance storms, ten minutes before lag shows it (`C-01`) |
| `kafka_consumer_coordinator_last_poll_seconds_ago` | Consumer | Approaching `max.poll.interval.ms` (`C-01`) |
| `kafka_consumer_fetch_manager_records_lag_max` | Consumer | Per-partition lag from the consumer's own view |
| `pipeline_freshness_seconds` | Your code | Everything the above cannot see |

---

## The alert set, mapped to what it catches

Nine alerts. Each row names the scenarios it detects, so you can audit coverage rather than
accumulate alerts.

| # | Alert | Expression | Severity | Catches |
|---|---|---|---|---|
| 1 | Partitions offline | `sum(kafka_controller_kafkacontroller_offlinepartitionscount) > 0` | Page | `B-02`, `B-03`, `R-07` |
| 2 | Below minimum ISR | `sum(kafka_server_replicamanager_underminisrpartitioncount) > 0` for 2m | Page | `R-05`, `B-12` |
| 3 | No active controller | `sum(kafka_controller_kafkacontroller_activecontrollercount) != 1` for 2m | Page | `B-10` |
| 4 | Pipeline freshness breach | `histogram_quantile(0.99, sum(rate(pipeline_freshness_seconds_bucket[5m])) by (le, topic)) > 30` for 10m | Page | `C-01`, `L-04`, `L-05`, `D-04` |
| 5 | Unclean leader election | `increase(kafka_controller_controllerstats_uncleanleaderelectionspersec_total[1h]) > 0` | Page | `R-07` — data was discarded |
| 6 | Under-replicated, sustained | `sum(kafka_server_replicamanager_underreplicatedpartitions) > 0` for 15m | Ticket | `B-01`, `B-05`, `B-06`, `R-08` |
| 7 | Disk exhaustion projected | `predict_linear(kafka_log_log_size[6h], 48*3600) > capacity * 0.85` | Ticket | `B-02`, `T-07`, `T-09` |
| 8 | Log cleaner stalled | `kafka_log_logcleanermanager_time_since_last_run_ms > 600000` | Ticket | `T-09` |
| 9 | Rebalance rate elevated | `rate(kafka_consumer_coordinator_rebalance_total[15m]) * 3600 > 4` | Ticket | `C-01`, `C-04` |

And the three that are specific enough to be worth adding once you have been bitten:

| # | Alert | Expression | Catches |
|---|---|---|---|
| 10 | Page-cache cliff crossed | `rate(node_disk_read_bytes_total{instance=~"kafka-.*"}[10m]) > 50e6` for 15m | `L-09` |
| 11 | Dead-letter queue non-empty | `sum(kafka_log_log_size{topic=~".*\\.dlq"}) > 0` | `L-05`, `L-08` |
| 12 | Consumer position near expiry | `(retention_seconds - max(kafka_consumer_lag_seconds) by (consumergroup)) < 12*3600` | `T-01` |

### What deliberately is not on the list

Just as important, because each of these is an alert people add and then learn to ignore:

- **Broker CPU.** Kafka is rarely CPU-bound, and when it is, the request-latency breakdown says
  so more precisely.
- **Absolute consumer lag thresholds.** Doc 06 (`L-01`, `L-02`) — the same number means different
  things at different hours, and structurally-lagged consumers make the threshold meaningless.
  Alert on freshness and on lag *derivative* instead.
- **Broker heap usage.** A healthy Kafka broker's heap sawtooths constantly. Alert on garbage
  collection pause time (`B-07`), which is the thing that actually causes harm.
- **Partition count.** It only matters relative to the `S-01` limits, and it changes slowly
  enough to be a review item rather than an alert.
- **`BytesInPerSec` thresholds.** Traffic going up is not an incident. Traffic going to *zero* is
  (`L-03`), and that is a different alert.

---

## Dashboard layout

Four rows, in the order you would read them during an incident.

**Row 1 — is the cluster intact?** Offline partitions, under-min-ISR count, under-replicated
count, active controller count, offline log directories. Five single-stat panels, all of which
should read zero or one. If any is wrong, stop here and go to doc 01.

**Row 2 — is the pipeline delivering?** End-to-end freshness p50/p99/p999 per topic, records
processed versus records produced, error rate per consumer group, dead-letter queue depth. This
is the row that reflects what users experience, and it is the row most Kafka dashboards do not
have.

**Row 3 — where is the bottleneck?** The produce-latency breakdown (queue, local, remote,
response) as a stacked graph, plus request-handler and network-processor idle percentage, plus
broker disk read throughput. Doc 01's table turns this row into a diagnosis.

**Row 4 — capacity and trend.** Disk used and projected per broker, partition-replicas per
broker, consumer lag per group, rebalance rate, page-cache residency window. This row is read in
planning meetings, not incidents, and it is where the doc 08 breakpoints become visible before
they arrive.

⚠️ Per-partition panels belong on a separate drill-down dashboard, not the main one. At
Riverbend's year-3 scale, a per-partition graph is 11,800 series, which is both unreadable and a
cardinality problem in its own right — see
[`Observability/05-instrumentation-and-cardinality.md`](../Observability/05-instrumentation-and-cardinality.md).
Use `topk(10, ...)` on the main dashboard and keep the full breakdown behind a topic selector.

---

## What to take away

1. **Kafka needs both USE and RED, applied to different things.** The cluster is a resource; the
   pipeline is a request-serving system whose request and response are separated in time.
2. **Instrument the pipeline first.** Every broker metric can be green during a rebalance storm,
   a poison message, or a hanging transaction — and those are the incidents.
3. **End-to-end freshness in seconds is the single best Kafka metric,** and it requires
   application code rather than an exporter. Build it anyway.
4. **A pipeline needs two SLIs.** Freshness cannot see a dropped record, because a dropped record
   never produces an observation.
5. **Derive the SLO threshold from a downstream deadline,** not from a round number, and check
   the budget against known events — two Riverbend flash sales consume half of a 99.5% monthly
   budget, which is information worth having before you commit to the target.
6. **The four-way produce-latency breakdown distinguishes five incidents** and belongs on every
   Kafka dashboard.
7. **Broker disk read throughput is near zero when healthy,** which makes it one of the few Kafka
   alerts that needs no threshold tuning.
8. **Add the log-cleaner metrics.** They are absent from nearly every dashboard and they catch the
   slowest and most expensive failure in the collection.
9. **Do not alert on absolute consumer lag, broker CPU, or heap usage.** Alert on freshness, on
   lag derivative, and on garbage-collection pause time.
10. **Twelve alerts cover the whole catalogue.** If you have forty Kafka alerts, most of them are
    training people to ignore the other twelve.

Next: [11-case-studies.md](11-case-studies.md), where six of these failures happen for real.

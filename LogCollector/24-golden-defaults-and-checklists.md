# Golden Defaults, Checklists, and Triage

The reference doc. Everything here is derived elsewhere in the collection; this is the version
to have open during an incident, a design review, or a Friday afternoon of configuration.

---

## Triage under pressure

Work down. Stop at the first thing that is wrong.

```
0.  ⚠  Do not open the log search to debug the log platform.
       Use the platform-health dashboard and the break-glass path. (doc 18)

1.  time_until_unrecoverable = kafka_retention − archiver_lag_time
    < 2h   → THIS IS THE INCIDENT. Runbook 2. Everything else waits.

2.  Is data arriving?     gateway.records_received_total, vs the 7-day same-hour baseline
    ≈ 0      → total ingest stop. Check certificates FIRST, then DNS, config, Kafka.
    spiking  → volume incident. Find the tenant, then the service, then the line.

3.  Is data moving?       consumer lag, in TIME, per group
    indexer lagging       → search is stale. Recoverable. Loud.
    tail-router lagging   → live tail broken. 5-second tolerance.
    archiver lagging      → see step 1.

4.  Is data queryable?    canary completeness
    failing while ingest is fine → the query or index path, not the pipeline.

5.  What changed?         config versions · deploys · certificate expiry · agent rollout
    Anything in the last hour is the cause until disproven.

6.  Who is affected?      per-tenant breakdown of whichever signal is bad
    one tenant  → contain it there
    everyone    → consider the shedding switch
```

### The five things that make an incident worse

```
⚠ Restarting agents "to clear something"      → discards their buffers (doc 18, runbook 1)
⚠ Scaling up a badly lagging consumer          → more cold reads, worse page-cache eviction (B-04)
⚠ Raising a memory limit without reducing input → moves the ceiling, not the shape (A-07)
⚠ Deleting an agent's buffer while it runs      → frees nothing; restart first (A-06)
⚠ Enabling unclean leader election              → trades silent loss for availability you
                                                  already have via agent buffers (B-07)
```

### Emergency levers, fastest first

| Lever | Time to effect | Recovers |
|---|---|---|
| Dynamic log level → WARN, one service | ~30 s | that service's volume |
| Shedding switch level 2 | ~60 s | 41% of platform volume |
| Emergency tenant rate limit | ~60 s | that tenant's excess |
| Extend Kafka retention | next segment roll | time before permanent loss |
| Start the raw-dump consumer | ~5 min | prevents loss during an archiver outage |
| Narrow default query time range | ~60 s | query-tier load |
| Roll back a config version | ~60 s | whatever the config broke |
| Scale the gateway or processing | 3–8 min | ingest capacity |
| Scale Kafka | hours to days | nothing useful during an incident |

**Note the last row.** Kafka cannot be scaled on incident timescales, which is why doc 06 sizes
it for peak permanently.

---

## Golden configuration

Annotated. Every value is derived somewhere in the collection; the annotation says where and
what moves it.

### Agent (Fluent Bit or equivalent)

```yaml
# Reading — doc 02
file_tracking:            device_inode_and_fingerprint   # never path — A-01
fingerprint_bytes:        256                            # inode reuse — A-01
rotate_wait:              5s                             # finish the renamed file first
scan_interval:            30s                            # inotify fallback — A-14
skip_long_lines:          false                          # handle CRI P/F — A-05

multiline:
  start_pattern:          '^\d{4}-\d{2}-\d{2}T'
  flush_timeout:          2s        # ← LARGEST term in the tail budget. 500ms if tailing
                                    #   matters more than never splitting a trace (doc 12)
  max_lines:              500       # unbounded = OOM
  max_bytes:              262144

# Enrichment — doc 02
k8s_metadata:             watch                          # never poll — 12,000 pollers
k8s_cache_ttl:            300s
k8s_deleted_grace:        300s                           # A-08
k8s_block_on_failure:     false                          # NEVER block on the API server

# Buffering — doc 02
memory_buffer:            64MB                           # ~15 s on a p99 host
disk_buffer:              min(max(2GB, 4% of volume), 20GB)
disk_buffer_path:         /var/lib/lantern-buffer        # SEPARATE VOLUME — A-06, A-13
disk_buffer_stop_at:      85%
when_full:                drop_oldest                    # not newest — you want current data
drop_order:               [TRACE, DEBUG, low_value_INFO, INFO, WARN]   # ERROR last
checkpoint_on:            acknowledgement                # not on send — A-03
checkpoint_interval:      5s

# Per-container limits — doc 16
per_container_rate_limit: 2MB/s                          # A-09, raise by exception
rate_limit_action:        drop_and_count

# Transport — doc 05
protocol:                 otlp_grpc
compression:              lz4                            # zstd-3 if the hop crosses a zone
batch_max_records:        500
batch_max_bytes:          1MB
batch_timeout:            200ms                          # 50ms for actively tailed services
recovery_rate_limit:      1.5x                           # ← T-03. Almost nobody sets this.
reconnect_backoff:        100ms * 2^n, max 30s, jitter 0.5–1.5x
keepalive_time:           20s                            # below the LB idle timeout
keepalive_timeout:        10s

# Resources — doc 02
cpu_request:              0.1
cpu_limit:                none                           # throttling during a spike is worse
memory_limit:             256MB                          # ~2x the internal cap, a backstop
priority_class:           system-node-critical
```

### Gateway

```yaml
# doc 05
replicas:                 90                             # 0.062 cores/MB/s × 1.7 peak
resources:                4 cores, 8GB
tenant_id_source:         mtls_client_certificate        # NEVER from the payload — M-08
strip_client_tenant_id:   true
count_client_tenant_id:   true                           # non-zero = misconfig or probing

http2_initial_stream_window:      1MB                    # 64KB default blocks large batches
http2_initial_connection_window:  4MB
max_connection_age:       30m
max_connection_age_jitter: 5m                            # ← without this you scheduled T-02
max_connection_age_grace: 5m
min_ping_interval:        10s                            # BELOW the client's keepalive

acknowledge_after:        kafka_acks_all                 # ← the correctness anchor
kafka_compression:        zstd-1                         # zstd-3 if network cost dominates
kafka_acks:               all

per_tenant_inflight_limit: 10%_of_pool                   # bulkhead, not just a rate limit
quota_levels:             [100%, 150%, 300%, 500%_absolute_ceiling]
quota_ceiling_guarantee:  1_record_per_distinct_error_per_second

always_on_redaction:      [credentials, api_keys, private_keys,
                           card_with_luhn, national_id_with_checksum]
```

### Kafka

```yaml
# doc 06
brokers:                  24 × i3en.6xlarge, 3 AZs, broker.rack SET
mode:                     KRaft, 3 dedicated controllers
replication.factor:       3
min.insync.replicas:      2
unclean.leader.election:  false
compression.type:         producer                       # ← anything else = B-09
log.retention.hours:      6                              # 168 for logs.audit
log.segment.bytes:        1GB
message.max.bytes:        4MB
num.replica.fetchers:     8

topics:                   logs.app(48p) · logs.access(120p) · logs.client(48p)
                          logs.infra(24p) · logs.batch(36p) · logs.audit(12p) · logs.dlq(12p)
partition_key:            hash(k8s.pod.uid)              # finest ordered unit — B-02
follower_fetching:        enabled, client.rack SET       # ← $200k/month if omitted (doc 20)

consumer_defaults:
  enable.auto.commit:     false                          # ← D-01, the most common violation
  auto.offset.reset:      none                           # for the archiver; earliest elsewhere
  partition.assignment:   cooperative-sticky
  group.instance.id:      set                            # static membership — B-06
  max.partition.fetch.bytes: 8MB                         # byte-based, not record-based — B-06
```

### Processing

```yaml
# doc 07
replicas:                 72, 8 cores, 16GB
stateless:                true                           # stateful work runs as separate jobs
order:                    dedup → parse → normalise → enrich → redact → derive → sample → route
                          # ← derive BEFORE sample, or every metric is wrong (V-05)
dedup_window:             60s exact                      # + idempotent storage writes
dedup_key:                xxhash64(host_id, inode, byte_offset)
enrichment:               in-memory tables from an S3 snapshot, 60s refresh, never blocking
enrichment_stamp:         owner.resolved_at
derived_metric_labels:    reviewed allow-list, hard cardinality cap, collapse to "other"
commit_offset_after:      storage write confirmed        # ← D-01
rules_source:             versioned config, CI fixtures, 1% → 10% → 100% rollout
```

### Storage

```yaml
# doc 09
partition:                dt / hr (INGEST time) / tenant / class
sort_within_file:         service.name, level, ts        # ← what makes min/max useful (W-02)
format:                   parquet
row_group:                16MB
target_file:              128MB                          # 512MB above ~30 TB/hr
flush_interval:           15min → level 0 (~15MB)
compaction:               at hour close → level 1 (128MB), atomic manifest swap
compression:              tier1 zstd-3 · tier2 zstd-9 · tier3 zstd-19
raw_column:               tier 1 only + all parse failures + 2% sample   # 3.4× if universal
catalogue:                transactional (Postgres → Iceberg above ~200M objects)
                          NEVER discover objects by LIST — W-11
tiers:                    0: 0–15min in memory · 1: 15min–48h NVMe · 2: 2–30d S3
                          3: 30d–13mo Glacier IR
```

### Indexing

```yaml
# doc 10
partition_pruning:        always — free, and the largest factor
row_group_min_max:        always — 0.02%, useless without the sort order
bloom_trace_id:           tier1 p=0.001 · tier2 p=0.01 · tier3 none
bloom_identifiers:        tier1 p=0.01 · tier2 p=0.05 · tier3 none
ngram_body:               tier1 only, n=3, p=0.08        # 6.8% overhead
min_substring_length:     4                              # = n+1 — X-04
inverted_index:           none
indexing_policy:          OPT-IN — every indexed field justified by a query pattern
```

### Query

```yaml
# doc 11
regex_engine:             RE2 or equivalent linear-time  # ← Q-04, not negotiable
max_program_size:         10MB
max_pattern_length:       4KB
default_time_range:       15 minutes                     # ← highest-leverage UI decision
max_bytes_scanned:        500GB hot/warm · 50GB cold
max_execution_time:       120s
max_workers_per_query:    50% of pool
max_result_records:       100,000                        # beyond this, export
distribution:             by object, PULL-based          # straggler = one object
cost_estimate:            shown before execution above a threshold
completeness_metadata:    on EVERY response
truncation:               newest-first
caches:                   footers (all) · catalogue (all) · decompressed blocks (LRU)
                          NOT results
cache_partitioning:       by tenant, proportional to committed volume — M-06
queues:                   interactive > scheduled > export (export on a SEPARATE pool)
under_load:               REJECT, do not queue
```

---

## Design-review checklist

For a new log platform, or a significant change. Twenty questions; a "no" is a finding.

**Requirements**
- [ ] Is the volume figure average or peak, raw or compressed, and is that written down?
- [ ] Is "real time" a number?
- [ ] Is the loss budget stated, allocated across stages, and *measured*?
- [ ] Is retention justified per class by what becomes impossible if it is shorter?

**Collection and transport**
- [ ] Does the agent track files by inode and fingerprint, not by path?
- [ ] What happens when the agent cannot keep up — which of block/drop-new/drop-old/spill?
- [ ] Is the agent's disk buffer on a separate volume from the application's?
- [ ] Is the recovery send rate limited, not just the steady-state rate?
- [ ] Does `tenant.id` come from the authenticated identity, never the payload?

**Buffer and processing**
- [ ] Is there a durable buffer between ingest and storage?
- [ ] Is broker count derived from page-cache residency, not disk?
- [ ] Does every handoff acknowledge *after* durability?
- [ ] Are derived metrics computed *before* sampling?
- [ ] Are irreversible transformations downstream of the buffer?

**Storage and query**
- [ ] Is data partitioned by ingest time and sorted by the common filter columns?
- [ ] Is every index justified by a query pattern, or is indexing on by default?
- [ ] Does the query planner estimate cost *before* execution?
- [ ] Does every result carry completeness metadata?

**Operations**
- [ ] Do the platform's own logs go somewhere the platform does not control?
- [ ] Is `time_until_unrecoverable` on the dashboard and alerted at two hours?

---

## New-service onboarding checklist

What a team should do before their first log reaches production.

- [ ] **Structured output to stdout.** JSON, one record per event, no files inside the
      container.
- [ ] **The mandatory core**: `ts` (RFC 3339 UTC), `level` (the six-value enum), `body`. The
      agent supplies the rest.
- [ ] **Constant message, variation in fields.** `"charge failed"`, not
      `"charge failed for user 98712"`.
- [ ] **Stack traces in `error.stack` as a string field**, never as multiline output. This one
      convention removes `A-04`, the largest term in the tail latency budget, and improves
      compression.
- [ ] **Semantic-convention names** for anything that has one.
- [ ] **Values unbounded, keys bounded.** Never an identifier as an object key.
- [ ] **Levels mean what the platform says they mean.** The test for `ERROR`: would you page on
      a sustained rate of this?
- [ ] **No request or response bodies in production.** Use the sanctioned capture tool.
- [ ] **No credentials, tokens, or headers logged wholesale.**
- [ ] **Non-blocking appender** with `neverBlock` or the equivalent. Losing a log must never
      cost a request.
- [ ] **Dynamic log level endpoint** wired up, with expiry.
- [ ] **A committed volume figure**, and awareness of what happens at 150% and 300%.
- [ ] **Trace context propagated**, so `trace.id` is present.
- [ ] **Read your own cost report** once, before it becomes a surprise.

---

## Three tiers: what to build at your scale

Most readers do not have a 10 TB/hr platform. Build the tier you are in and not the one above.

### Tier A — under 100 GB/day

```
Agents (whatever your backend ships) → a hosted service. Stop.

Do NOT build: a gateway, a buffer, tiering, quotas, a control plane, cells.
DO have:      structured logging, a retention policy, and somebody's credit card.
Cost:         $300–3,000/month. Engineering: ~0.
```

The entire rest of this collection describes costs you have not incurred.

### Tier B — 100 GB/day to 5 TB/day

```
Agents → Kafka → one consumer → ClickHouse or Loki or Elasticsearch → a UI.

DO build:  a Kafka buffer (the highest-value component per dollar at this scale)
           structured logging and a schema convention
           volume control — the percentages are the same as at 10 TB/hr
           basic tiering: hot local, cold in object storage
           loss measurement: canaries at minimum
Maybe:     a gateway, once you have a second tenant or a redaction requirement
Do NOT:    cells, multiple Kafka clusters, a custom query engine, a full quota system
Cost:      $3,000–40,000/month. Engineering: 0.5–2.
```

**The single most valuable thing in this tier is the Kafka buffer**, because it converts every
downstream failure from data loss into lag — which is doc 21's `CS-1` learned cheaply.

### Tier C — above 5 TB/day

Everything in this collection, in this order:

```
1. Volume control        — before anything else, it changes every other decision
2. The durable buffer    — if you somehow do not have one
3. Loss measurement      — you cannot improve what you cannot see
4. Tiering and columnar storage
5. The gateway, quotas, and multi-tenancy
6. Query cost estimation and admission control
7. Cost attribution and showback
8. Cells, when one system stops being operable
```

**Step 1 first, always.** Choosing a stack, a vendor, or a cluster size before reducing volume
means choosing with inputs that are about to change by a factor of five.

---

## Numbers worth remembering

The ones that transfer to other systems.

| | |
|---|---|
| 10 TB/hr | = 2.78 GB/s = 5.56M records/s at 500 B |
| Log line, average | ~500 bytes — the figure everyone guesses and everything depends on |
| Peak to average, diurnal, global | 1.7× |
| Worst single-service incident spike | **31×** |
| Machine-generated share of volume | **65%**, and nobody reads it individually |
| Successful access logs | 44% of ingest; aggregating them is the biggest single lever |
| Queries against data under 1 hour old | **72%**, and that data is 0.4% of what you store |
| Fresh-versus-old query density | **10,000,000 : 1** per stored terabyte |
| Columnar + dictionary + zstd | **11:1**, versus 4.6:1 for the same codec on rows |
| Kafka page-cache residency | the number that sizes the cluster — 26 min here |
| Brute-force regex, 48 h | 480 TB → **192,000 cores** for a 10-second answer |
| Achievable prune factor | 10⁴ to 10⁷ |
| Scan rate, decompress + RE2 | **250 MB/s of raw bytes per core** |
| Target Parquet file | 128 MB |
| Cross-zone transfer | **35% of the bill** — the line nobody budgets |
| Cost per GB ingested, self-hosted | **$0.042**, all in |
| Cost per TB/hr per month | **~$29,400** |
| Achievable loss rate | 0.01%, which is 48 million records a day |
| Build-vs-buy break-even | 2–5 TB/day |

---

## The collection in twelve lines

1. **A log collector is seven stages**, and naming the stage is most of diagnosing the problem.
2. **Log loss is silent**, so a platform that does not measure it has an unknown loss rate, not
   a zero one.
3. **The platform is most loaded exactly when it is most needed**, and both spikes — ingest and
   query — land in the same minute.
4. **Volume control beats every other optimisation combined**, and it changes the answer to
   every other decision, so do it first.
5. **Partition by ingest time, search by event time, and make the gap visible.**
6. **Keep irreversible operations downstream of the durable buffer**, so every mistake is
   temporary rather than permanent.
7. **Values may be unbounded; keys must be bounded.**
8. **Sorting is what makes column statistics useful** — 87.5% pruning or 0%, same index, same
   cost, and nothing reports the difference.
9. **The query tier is a pruning problem, not a scanning problem**, and every pruning step is a
   decision made earlier in the pipeline.
10. **Prefer a boundary to a convention.** Separate paths over priority fields, path-scoping
    over filters, price signals over policies.
11. **Make degradation visible.** A silent gap, a partial result, a dropped field, a dead
    redaction rule — each makes someone draw a confident wrong conclusion, and **zero is almost
    always a lie.**
12. **When you must guess, record that you guessed.**

---

Back to the [README](README.md).

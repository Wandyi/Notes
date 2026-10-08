# Flow Two: Interactive Search

*"The team can get exact data by issuing a regex query to the logs in real time."*

Doc 11 built the engine. This doc is about the thing wrapped around it: the query language, the
interaction loop, and the design decisions that determine whether engineers use the platform or
go back to `kubectl logs`.

That framing is not soft. **A search system that is technically excellent and awkward to use has
failed**, because the alternative — SSH to the box, `grep` the file — is always available and
people will take it. Every hour an engineer spends not using your platform is an hour of
incident data nobody can correlate, and it is also the most reliable early signal that something
about the design is wrong.

Scenario IDs continue the `Q-nn` series from doc 11.

---

## What an engineer actually does

Watch someone debug and the pattern is always the same. It is not one query.

```
14:22  "something is wrong with checkout"
       → service:checkout-api level:ERROR, last 15 minutes
       → 4,100 results. Too many.

14:23  → adds | count by error.code
       → card_declined 3,980 · timeout 87 · validation 33
       → "card_declined, then"

14:24  → service:checkout-api error.code:card_declined, last 15 minutes
       → looks at one record. It has payment.provider:stripe.

14:25  → adds | count by payment.provider
       → stripe 3,980 · adyen 0
       → "only stripe"

14:26  → clicks one record → "show surrounding 50 lines from this pod"
       → sees a connection-pool warning 200 ms before each failure

14:28  → service:payment-api "pool" last 1 hour
       → finds the pool exhaustion starting at 14:04

14:31  → changes the time range to 13:30–14:10 to find the trigger
       → correlates with a deploy marker
```

**Seven queries in nine minutes.** Each one is a small mutation of the last, and the value came
from the *sequence*, not from any single query.

That observation drives the design. Optimising the single-query path matters much less than
optimising the loop:

- **Latency compounds.** Seven queries at 8 seconds is a minute of waiting inside a nine-minute
  investigation. At 1.5 seconds it is ten seconds and the engineer stays in flow.
- **Refinement must be cheap to express.** Adding a filter should be a few keystrokes, not
  rewriting the query.
- **The results must suggest the next query.** Field value distributions on the side of a result
  set are how the engineer got from "4,100 errors" to "only stripe" — the system offered the
  next question.
- **Iteration hits the same blocks**, which is why doc 11's decompressed-block cache matters far
  more than a result cache.

---

## The query language

### What the candidates look like

The same question — errors from `checkout-api` in the last 15 minutes containing
`card_declined`, counted by provider — in five languages:

```
Lucene / Elasticsearch KQL
  service.name:"checkout-api" and level:ERROR and body:*card_declined*
  (then a separate aggregation, expressed in JSON)

LogQL (Loki)
  sum by (payment_provider) (
    count_over_time({service="checkout-api", level="ERROR"} |= "card_declined" [15m])
  )

SQL (ClickHouse)
  SELECT payment_provider, count()
  FROM logs
  WHERE service = 'checkout-api' AND level = 'ERROR'
    AND ts >= now() - INTERVAL 15 MINUTE
    AND position(body, 'card_declined') > 0
  GROUP BY payment_provider

Splunk SPL
  index=main service=checkout-api level=ERROR "card_declined"
  | stats count by payment_provider

A pipe language (what Lantern uses)
  service:checkout-api level:ERROR "card_declined"
  | count by payment.provider
```

### What makes one good for logs

Six properties, and they are not the ones a language designer would list first.

**1. The common case is short.** The most frequent query in any log platform is "errors from one
service, recently". If that takes more than about twenty characters, people will not use the
platform casually — and casual use is what builds the habit that makes it available during an
incident.

```
service:checkout-api level:ERROR              ← 30 characters, no aggregation ceremony
```

Compare the SQL version, which needs `SELECT`, `FROM`, a time predicate and a `WHERE`, most of
which is the same every time. SQL is a fine query language and a poor *search* language, and the
difference is that search is interactive and repetitive.

**2. Refinement is append-only.** Adding a constraint should mean typing at the end, not editing
the middle. Pipe languages have this property naturally; SQL does not, because narrowing means
going back into the `WHERE` clause.

**3. Free text works without ceremony.** A bare string should search the message:

```
"card_declined"          ← searches the body
error.code:card_declined ← searches a field
```

This is what people try first, and if it does not work they conclude the platform is hard.

**4. Aggregation is in the same language.** The transition from "show me records" to "count them
by something" is the most valuable step in the loop above, and it must not require switching
tools or syntaxes. Elasticsearch's split between a query DSL and an aggregation DSL is the
clearest example of this going wrong.

**5. It is honest about cost.** The language should make expensive things look expensive. A
leading wildcard, an unbounded time range, a cross-tenant scope — these should be visibly
different from cheap operations, not quietly identical.

**6. It is composable with the outside world.** Output as JSON to `jq`, a CLI that reads the same
syntax, and a permalink that reproduces the exact query and time range.

### Lantern's language

```
# Selection: field:value pairs and bare strings, implicitly ANDed
service:payment-api level:>=WARN "card_declined"

# Regex, explicitly delimited so the cost is visible in the syntax
service:payment-api body:~/pool.*exhaust/

# Boolean structure when needed
service:payment-api (error.code:timeout or error.code:refused) not host:ip-10-0-3-47

# Time, as a first-class part of the query rather than a separate control
service:payment-api last:15m
service:payment-api from:14:00 to:14:15

# Pipes for everything after selection
service:payment-api level:ERROR
  | count by error.code, payment.provider
  | sort desc
  | limit 20

# Context expansion, which is a verb rather than a new query
service:payment-api "pool exhausted" | context 50
```

Three deliberate choices worth defending:

**`~/regex/` rather than bare regex.** Doc 11 established that a regex without an indexable
literal is a full scan. Making regex visually distinct means the user sees they are doing
something different, and it lets the parser treat `"literal"` as the cheap path without
ambiguity.

**`level:>=WARN` works because level is an ordered enum** (doc 04). A small thing that removes a
great deal of `level:WARN or level:ERROR or level:FATAL`.

**Time is in the query text**, not only in a separate picker. It makes a query shareable as one
string, which matters enormously during an incident — pasting a link into a channel is how
investigation state propagates between people.

---

## The interaction loop

### Progressive results

Doc 11's engine streams. The interface must too:

```
t=0.0s   query submitted, plan shown: "scanning ~1.3 GB across 81 row groups"
t=0.3s   first results appear (from tier 0 — in memory, no index needed)
t=0.8s   30% complete, 1,200 results, histogram filling in
t=1.4s   100% complete, 4,100 results
```

**Results from tier 0 arrive in 300 ms** because it is in memory and unindexed (doc 09). For the
48% of queries that only touch the last 15 minutes, the search *is* that fast. For wider queries,
the recent data appears immediately and the rest fills in — which keeps the user engaged and,
more practically, often answers the question before the query finishes.

Order matters: **return newest first.** During an incident the most recent matching record is
almost always the most relevant, and it means an aborted query still gave the useful part.

### The histogram is not decoration

Every result set gets a count-over-time histogram above it. It is the single most useful element
on the page, for three reasons:

- **It answers "when did this start?"** without another query, which is usually the real
  question.
- **It is a time-range selector.** Drag across the spike, and the query re-runs scoped to it.
  This is the fastest refinement in the whole interface.
- **It shows whether the result set is what you think.** A flat line at the same rate for a week
  means you found background noise, not the incident.

Compute it as part of the scan, not as a separate query — the scan already visits every matching
record, so bucketing by time is nearly free and a second query would double the cost.

### Field value distributions

Down the side, for each field present in the results, the top values and their share:

```
error.code          payment.provider       host.name
  card_declined 97%   stripe        100%     ip-10-0-3-47  4%
  timeout        2%   adyen           0%     ip-10-0-7-12  4%
  validation     1%                          (81 others)
```

**This is how an engineer discovers the shape of a problem.** `payment.provider: stripe 100%` is
the finding in the walkthrough above, and nobody had to think to ask for it.

Computing it requires care: the naive version computes distributions over the full result set,
which for 4 million results is expensive. Compute over a **sample of up to 10,000 results** and
label it as an estimate. The shape is what matters, not the exact percentages, and a labelled
estimate that arrives in 200 ms beats an exact figure that costs another scan.

### Context expansion

`| context 50` — show the 50 records before and after this one, from the same stream,
**ignoring the filter**.

This is the operation that most closely reproduces `tail -f` on a box, and it is the one people
miss most when it is absent. It is also the reason doc 06 keyed Kafka by `pod.uid` and doc 09
sorts by service: the surrounding records from one pod are physically adjacent, so context
expansion is a small ranged read rather than a new query.

```
implementation: given record_id, find its object and row group from the catalogue,
                read that row group, filter to the same k8s.pod.uid,
                return the 50 rows either side by ts
cost:           one 16 MB row-group read, ~60 ms
```

### The "no results" problem

An empty result set is the least helpful screen in any search system, and for logs it is
genuinely ambiguous between five causes:

1. It genuinely did not happen.
2. It happened outside your time range.
3. The field name is wrong (`status` versus `http.response.status_code`).
4. The records were sampled away (doc 08).
5. The pipeline lost them (doc 15).

**The interface must help distinguish these**, because the default assumption — number 1 — is
wrong more than half the time and leads people down wrong paths.

```
No results for:
  service:checkout-api error_code:card_declined last:15m

  ⓘ  Field "error_code" does not exist for this service.
     Did you mean "error.code"?  (present in 94% of this service's records)

  ⓘ  Dropping the error.code filter gives 4,100 results in this window.

  ⓘ  This service is sampled at 1:8 for level:INFO. level:ERROR is not sampled.

  ⓘ  Pipeline health for checkout-api in this window: normal (0.002% loss).
```

Each of those four lines is cheap to compute and each eliminates one of the five causes. Together
they turn a dead end into a next step. **This screen is worth more engineering effort than most
performance work**, because it is where trust is won or lost.

### Permalinks and sharing

Every query state — text, time range, selected record, expanded context — is a URL. During an
incident this is how investigation propagates:

> "I'm seeing pool exhaustion from 14:04 — https://lantern/s/8f2a91"

**Make the time range absolute in the shared link**, never relative. A link saying `last:15m`
means something different to the person who opens it an hour later, and they will not notice.
Resolve `last:15m` to `from:14:15 to:14:30` at share time, and show that it was resolved.

This is a small thing that causes real confusion when it is wrong, and it is a special case of
the collection's recurring rule: **do not silently reinterpret something depending on when it is
evaluated.**

---

## Access control on search

Doc 17 covers the policy. The query-tier mechanics:

**Tenant isolation is structural.** Doc 09 put `tenant` in the storage path, so a query is
scoped by prefix, not by a `WHERE` clause. A bug in the query engine cannot leak across tenants
because the engine is never given the other tenant's objects. **Filtering is a policy; pathing
is a boundary**, and boundaries survive bugs.

**Field-level restriction.** Some fields are restricted even within a tenant — records marked
with PII classes, audit records, records from regulated services. The planner drops restricted
columns from the projection based on the caller's role, and — importantly — **tells the user
that it did**:

```
3 fields hidden by policy (user.email, user.ip, payment.card_last4).
Request access: [link]
```

Silently omitting fields produces wrong conclusions, again.

**Every search is itself audited.** Who searched for what, when, how many records were returned.
This is a compliance requirement in most regulated contexts and it is also genuinely useful —
doc 08's "how many times has anyone searched for this log statement" report is built from it,
and so is the query-cost attribution in doc 11.

---

## The search failure catalogue

Continuing `Q-nn` from doc 11.

### Q-10 · Field name mismatch produces a confident empty result

**What you see.** No results. The engineer concludes the event did not occur.

**Mechanism.** `status` versus `http.response.status_code` versus `statusCode`. Doc 04's schema
governance reduces this; it does not eliminate it, because 40 tenants and thousands of services
will always have variation.

**Prevent.** Field-name suggestions on empty results, computed from the fields actually present
for that service in that window. Field autocomplete while typing, scoped to the selected
service. And publish per-service field inventories so the answer is discoverable before the
query.

### Q-11 · The user does not know their data was sampled

**What you see.** A count that is 8× lower than the user expects. Or a search for a specific
request that finds nothing because that request was sampled out.

**Mechanism.** Doc 08's sampling, invisible at query time.

**Prevent.** Show the effective sampling rate for every service and level in the result set
header. Use `sum(sample_weight)` automatically for counts (doc 08's `V-01`). And on an empty
result for a specific identifier, say explicitly: "this service samples INFO at 1:8; a specific
record may not have been retained."

### Q-12 · An expensive query pattern spreads by copy-paste

**What you see.** A slow, wide, unindexable query appearing repeatedly from different users
over weeks.

**Mechanism.** Someone shared a permalink in a channel during an incident. It worked. Now it is
the team's standard starting point, and it scans 40 TB every time.

**Confirm it.** Group query logs by normalised query shape and rank by total cost. The expensive
shape will be near the top with many distinct users.

**Recover.** Contact the users with a cheaper equivalent. This is a conversation, not a block.

**Prevent.** Cost feedback at query time so the original author sees it. Saved searches that the
platform team can review and optimise centrally. And a "this query is expensive — here is a
cheaper equivalent" hint that appears on the recognised shape.

### Q-13 · Saved searches and alerts silently break

**What you see.** An alert built on a saved search stops firing. Nobody notices, because a
non-firing alert looks exactly like a healthy system.

**Mechanism.** Doc 04's `S-10` — a field renamed, a service renamed, a schema change.

**Prevent.** Alert on **absence of results from a search that normally returns some**. Any saved
search or alert whose result count goes from a steady rate to exactly zero should notify its
owner. Zero is a value, and in a log platform it is usually a lie.

Run saved searches against the schema registry on change and flag ones whose fields no longer
exist — a cheap static check that catches most of it before it matters.

### Q-14 · Time-zone confusion

**What you see.** Two engineers looking at "the same" window and seeing different data. An
incident timeline that does not line up with a graph from another system.

**Mechanism.** One user's interface is in local time, the other's in UTC. Or a shared permalink
carried a relative range.

**Prevent.** **Default to UTC and label every timestamp with its zone, always.** Offer local
time as an explicit, visibly marked toggle. Absolute ranges in shared links. And show both zones
on hover, which removes most of the arithmetic people do in their heads during incidents.

---

## What to take away

1. **Engineers run a sequence of queries, not one.** Seven in nine minutes, each a small mutation
   of the last. Optimise the loop, not the single query.
2. **A technically excellent, awkward search system has failed**, because `kubectl logs` is
   always available and people will use it instead — and then the incident data is nowhere.
3. **The common case must be short.** "Errors from one service, recently" in about thirty
   characters, or casual use never becomes a habit and the platform is not reached for during an
   incident.
4. **Refinement should be append-only**, which is why pipe languages suit search and SQL does
   not, despite SQL being the better query language in every other respect.
5. **Selection and aggregation must be the same language.** The step from "show me records" to
   "count by something" is where investigations turn, and a tool switch there breaks the loop.
6. **Return newest first and stream progressively.** Tier 0 gives first results in 300 ms, which
   often answers the question before the full query finishes.
7. **The histogram is the most useful element on the page** — it answers "when did this start",
   it is the fastest refinement control, and it reveals when you have found background noise.
8. **Field value distributions offer the next question.** `payment.provider: stripe 100%` is the
   finding, and nobody had to think to ask for it.
9. **"No results" is ambiguous between five causes**, and the default assumption is wrong more
   than half the time. Four cheap hints — field suggestions, a broader-query count, the sampling
   rate, pipeline health — turn a dead end into a next step.
10. **Resolve relative time ranges to absolute at share time.** A link saying "last 15 minutes"
    means something different to whoever opens it later, and they will not notice.
11. **Tenant isolation must be structural, not a filter.** Path-based scoping survives a bug in
    the query engine; a `WHERE` clause does not.
12. **Alert when a saved search goes to exactly zero.** A detection rule that stopped matching
    looks identical to a system with no problems.

Next: [14-flow-three-analytics-export.md](14-flow-three-analytics-export.md), the third flow and
the one whose stated requirement most deserves to be argued with.

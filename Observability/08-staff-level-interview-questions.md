# Staff-Level Interview Questions: Observability Design

Twelve questions in the style you would actually get in a staff engineering loop: a realistic
scenario, not a definition to recite. Each one has a full model answer that shows the reasoning, not
just the conclusion, followed by a short note on what separates a good answer from a staff-level
one — usually a follow-up concern the question did not explicitly ask for, but that a candidate who
has actually operated one of these systems would raise unprompted.

Use these to prepare for an interview, to run one, or to sanity-check your own service's
instrumentation by answering them against it instead of against Riverbend.

---

### Q1. You inherit a service with no metrics at all and are asked to define an SLO for it within a week. Where do you start?

**Model answer.** Not with a target number — with what the service *is*, because that decides
whether you are about to write RED or USE metrics, and the SLO has to be built on top of whichever
one applies (doc 03). If it serves requests, start with an access log or a reverse-proxy metric you
almost certainly already have for free — most load balancers and ingress controllers export request
count and duration without any application changes — and use that to establish a rough baseline
before writing a single line of application instrumentation.

Concretely, for a hypothetical service resembling `checkout-api`: pull a week of ingress-level
request logs, compute p50/p99/p99.9 latency and the error rate by status code, and look at how those
numbers vary by hour and day of week. That baseline tells you two things before you set any target:
what "normal" costs in tail latency (you cannot set an SLO tighter than your current p99.9 without a
plan to actually improve it), and where the natural traffic troughs are (a target based on a 5-minute
window needs to make sense at the trough volume too — see Q9 below on why a percentage alone is not
enough).

Only then propose a target, and ground it in a business consequence, not a round number: "99.9%
success with p99 under 500ms" is defensible if you can say what breaks at 99.9% versus 99.5% —
for Riverbend's `checkout-api`, 99.9% monthly corresponds to a 43.2-minute error budget (0.1% of
43,200 minutes in 30 days), which the team can compare against how often past incidents actually
lasted, and decide whether that budget feels survivable or is already routinely blown.

**A strong candidate would also raise** that the SLO should ship with instrumentation for the
underlying SLI in the same pull request, not as a follow-up — an SLO with no corresponding metric is
a policy with no enforcement — and would ask whether any downstream consumer already depends on an
implicit, undocumented expectation of this service's latency, since that expectation is often
tighter than whatever number the team is about to formalize.

---

### Q2. A platform team proposes a blanket policy: alert on CPU > 80% for every service in the cluster. What's wrong with this, and what would you propose instead?

**Model answer.** The policy conflates two different questions doc 02 is built around: is the
resource *utilized*, and is work actually *backed up* behind it. CPU utilization at 80% tells you
the box is busy; it says nothing about whether requests are queueing, failing, or slowing down as a
result — for a request-driven service like `checkout-api`, plenty of workloads run comfortably at
80% CPU with flat p99 latency, because the work is embarrassingly parallel across 24+ replicas and
none of it queues internally. A blanket 80% CPU alert on that service pages people for a
non-event on a normal Tuesday afternoon.

The inverse failure is worse and is CS-3 in doc 07: `order-processor`'s pods sat at 18% CPU for six
hours while consumer lag climbed by 300,000 messages, because the bottleneck was I/O wait, not CPU —
a blanket CPU alert would never have fired for the incident that was actually happening.

What to propose instead: alert on the symptom that matters for each component's kind, not on a
resource number that happens to be easy to collect everywhere. For request-driven services, that's
RED-derived — error rate and p99 latency, ideally burn-rate-based (doc 04). For queue-consuming or
resource-bound components, that's the saturation signal specific to the resource — consumer lag,
queue depth, connections-in-use as a fraction of the ceiling — not CPU. CPU and memory utilization
remain useful as *diagnostic* dashboard panels for root-causing an incident already flagged by a
better signal; they are the wrong primary alert.

**A strong candidate would also raise** that a truly universal policy is possible, just not this one
— every service should alert on *something* by default, and the right universal default is a
staleness/liveness check (no successful health check or no traffic in N minutes when traffic is
expected), which is the request-driven analogue of `K8s/cronJobs` doc 08's staleness alert for
scheduled work, and catches "silently dead" without assuming CPU behaves the same way for every
workload shape.

---

### Q3. Design the metrics you would export for a brand-new Kafka consumer service, from scratch.

**Model answer.** Start from what USE requires for a resource-bound component, then add the RED-ish
per-message view, because a consumer is really both: it consumes from a queue (a resource with a
depth) and it processes discrete units of work (which have a rate and can fail).

Minimum set, modeled on `order-processor`:
```
kafka_consumergroup_lag{topic, partition}      gauge      # saturation: work waiting to be pulled
kafka_consumer_records_processed_total{result} counter    # rate + errors, split by outcome
kafka_consumer_process_duration_seconds        histogram  # duration, per-record
kafka_consumer_worker_busy_seconds_total       counter    # utilization, as a counter not a gauge —
                                                           # sampled gauges average away bursts
kafka_consumer_worker_pool_size                gauge      # denominator for turning busy-seconds
                                                           # into a utilization ratio at any window
```

The lag metric is the one to get right first, because it is the earliest, cheapest signal that
something downstream is slower than the arrival rate — CS-3 in doc 07 is exactly the incident that a
lag alert would have caught in 12 minutes instead of 6 hours. The worker-busy-seconds-as-a-counter
pattern matters because a utilization gauge sampled every 15 seconds hides short bursts of full
saturation between samples; a counter lets you compute
`rate(kafka_consumer_worker_busy_seconds_total[1m]) / kafka_consumer_worker_pool_size` at whatever
resolution you need, after the fact, from data you already collected.

**A strong candidate would also raise** that lag alone cannot distinguish "the consumer is slow" from
"the consumer is stuck" — a poison-pill record that a handler retries forever produces flat,
non-advancing lag alongside zero throughput, which looks different from lag that is merely growing.
Exporting the age of the oldest unacknowledged record (or the offset commit's own timestamp) alongside
lag closes that gap, matching a design also called out in this repository's Kafka consumer notes
under `goQuestions/q1`.

---

### Q4. On-call was paged by five different alerts overnight for what turned out to be one root cause. How do you redesign the alerting?

**Model answer.** Five pages for one root cause almost always means the alerts are defined at the
level of individual *resources* rather than at the level of *customer-visible symptoms* — each
resource downstream of the same failure crossed its own threshold and fired independently.
Concretely, if `orders-db`'s connection exhaustion (CS-1) happened today, a resource-level alerting
setup might separately page for `checkout-api` p99 latency, `checkout-api` error rate,
`orders-db` connection count, `invoice-rollup`'s next run failing because it also couldn't get a
connection, and a generic "pod restarting" alert from `checkout-api`'s liveness probe finally tripping
— five pages, one cause, and no indication from the pages themselves that they are related.

The fix is not fewer resources monitored; it is fewer things that page. Reserve paging alerts for
the smallest number of customer-facing symptom signals — for a service like `checkout-api`, that is
essentially the RED-based burn-rate alert from doc 04 and nothing else at the paging severity.
Everything else — the five resource-level signals above — becomes a **diagnostic**, visible on a
dashboard the page links to, not a second (or fifth) page. The db-connections-at-ceiling signal is
still valuable; it belongs directly on the dashboard the `checkout-api` burn-rate page links to, so
the on-call engineer sees it within the same context as the page that woke them, rather than
receiving it as an independent, uncorrelated interruption.

**A strong candidate would also raise** that this same problem shows up between services, not just
within one: if `invoice-rollup` and `checkout-api` share `orders-db`, an incident on the shared
resource should page whichever team owns the customer-facing symptom, with the dependency graph
available to explain why, rather than paging every team that happens to touch the resource.

---

### Q5. When, if ever, would you choose a summary over a histogram for a latency metric?

**Model answer.** Rarely, and it is worth being precise about why, because the two look similar on
the surface and fail differently. A Prometheus summary computes quantiles client-side, at scrape
time, over a sliding window — which means those quantiles **cannot be aggregated across instances**.
If `checkout-api` runs 24 pods and each exports its own client-computed p99, there is no correct way
to combine 24 per-pod p99 values into a fleet-wide p99; averaging them is not the same number
mathematically, and doing so quietly understates the real tail, because a percentile from one pod
handling a small, unlucky slice of traffic gets averaged in as if it were representative.

A histogram instead exports raw bucket counts, and `histogram_quantile()` computes the quantile
server-side, after summing buckets across every instance — which is the only way to get a
mathematically valid fleet-wide p99 for a horizontally-scaled service like `checkout-api`'s 24-60
replicas.

The case for a summary: client-side quantile computation costs less at extremely high per-instance
request volume where bucket cardinality (doc 05) would otherwise be a real expense, or for a
single-instance component where cross-instance aggregation was never a concern in the first place —
a local batch tool run by one process, not a fleet.

**A strong candidate would also raise** that choosing histogram buckets is its own design problem —
buckets need to bracket the SLO threshold tightly enough that `histogram_quantile`'s
linear-interpolation-within-a-bucket approximation stays accurate near the number that actually
matters (doc 05), and a badly-chosen bucket set can make a histogram less useful than a summary would
have been for that one metric, even though histograms are the right default.

---

### Q6. How do you decide an SLO target is right for the service, rather than copied from a template because "everyone uses 99.9%"?

**Model answer.** Start from what the number costs downstream, not from a convention. Two concrete
questions ground it: what does violating the target actually mean in customer or business terms, and
what does *hitting* a tighter target cost in engineering effort? For `payout-settlement` (a daily,
not request-driven, job outside this collection's RED/USE frame but instructive by contrast), a
missed run is a business incident regardless of percentage — the SLO is really "never miss a day,"
which doc 04's error-budget framing would express as an extremely tight budget, because the
consequence of one miss is disproportionate to what a smooth 99.9%-style target implies.

For `checkout-api`, work backward from what 99.9% actually buys: a 43.2-minute monthly error budget
means Riverbend can absorb about one incident of that CS-1's exact duration (17 minutes to diagnose,
plus recovery time) roughly twice a month before breaching the target — which is either comfortably
achievable, uncomfortably tight, or already routinely violated, and you only know which by comparing
the number against the last six months of actual incident data, not by asserting it in the abstract.
If the target is already being blown every month, the right move is not to loosen it quietly; it is
to either invest in reliability work funded by that data, or have an explicit conversation with
stakeholders about renegotiating the number.

**A strong candidate would also raise** that a target should be re-derived, not just re-approved,
whenever the service's architecture changes materially — adding a new synchronous dependency (like
the fraud-check call in CS-1) changes the achievable ceiling, and an SLO left unchanged after such a
change is measuring against a number that was never re-validated against the new reality.

---

### Q7. A service's RED metrics look perfect — low error rate, comfortable p99 — but customers are complaining. What do you check next?

**Model answer.** First, question whether the RED metrics are actually measuring what the customer
experiences, rather than assuming they are lying. Three concrete gaps to check, in order of how
often they explain this pattern in practice:

1. **The metric is measuring server-side duration, not client-perceived duration.** If
   `checkout-api` records its histogram from request-received to response-sent, it never sees
   time spent in a client-side retry loop, DNS resolution, TLS handshake, or a slow network hop —
   exactly the failure mode in CS-2, where the server-side view of average latency looked fine while
   real customers on a specific mobile client version experienced 12-second stalls the server-side
   average never surfaced even in its own p99 (only p99.9 did, because the affected population was a
   small fraction of the total).
2. **The percentile being watched is too low for the size of the affected population.** If 0.05% of
   customers are affected, p99 will not move; only p99.9 or a metric segmented by client
   version/region will. Confirm what percentile the dashboard is actually alerting on, and whether the
   affected fraction is smaller than that percentile's resolution.
3. **The RED metrics are correctly reporting success, but "success" is defined too loosely** — a 200
   status code with a wrong or incomplete body still counts as a success in a naive request counter.
   This is the RED-method analogue of `K8s/cronJobs` F-13 (a batch job exiting 0 while doing nothing):
   the signal is trustworthy about what it measures, and what it measures is not "did the customer get
   the right answer."

**A strong candidate would also raise** that this gap is exactly why traces and real-user-monitoring
(client-side timing, actually captured from the browser or mobile app) exist as complements to
server-side RED metrics — server-side RED tells you about the server's experience of the request, and
a growing gap between that and the customer's actual experience is itself a signal worth measuring
directly, not inferring after complaints arrive.

---

### Q8. Design the USE metrics for a thread pool inside a service — say, a fixed pool that handles slow, blocking calls to a legacy downstream system.

**Model answer.** A thread pool is a resource with the same three questions as any other: how much of
it is in use, how much work is queued waiting for a free thread, and is it producing errors. Concrete
metrics:

```
threadpool_active_threads          gauge      # utilization numerator
threadpool_max_threads             gauge      # utilization denominator (or a static config value)
threadpool_queue_depth             gauge      # saturation: work waiting for a thread
threadpool_task_wait_seconds       histogram  # time spent queued before execution started —
                                              # the clearest saturation signal available, because
                                              # it is a duration, not a count that needs interpreting
threadpool_task_rejected_total     counter    # errors: the pool's queue itself has a bound, and
                                              # rejection is what happens when that bound is hit
```

`threadpool_task_wait_seconds` deserves emphasis: a queue-depth gauge tells you *how many* tasks are
waiting, but not whether that number is meaningful without knowing the pool's throughput. A
wait-time histogram answers the actual operational question directly — "how long is a caller
waiting for a thread" — without requiring anyone to first work out what a given queue depth implies
for latency. This is the same principle as CS-1's connections-in-use fraction: express saturation as
a number whose meaning does not require translation.

**A strong candidate would also raise** that a thread pool serving a single slow downstream
dependency is exactly the situation where an adaptive concurrency limiter (as described in this
repo's `goQuestions/q1` Kafka consumer notes) is often the better fix rather than just monitoring the
pool better — sizing the pool to the dependency's actual capacity, and shedding load before the queue
grows, rather than only alerting once the queue is already long.

---

### Q9. Why can't you just set every alert threshold as a fixed percentage, like "page if error rate exceeds 1%"?

**Model answer.** Because a percentage on its own has no information about the sample size it was
computed from, and the same percentage means wildly different things depending on it. CS-5 in doc 07
is the concrete version of this: five errors out of eighty requests during Riverbend's overnight
traffic trough is 6.25% — comfortably over a naive 1% threshold — while the same five errors against
`checkout-api`'s steady 640 req/s daytime volume is 0.0026%, nowhere close. The rule was not wrong
about arithmetic; it was applied identically regardless of how much data backed the ratio, and a
ratio computed from eighty samples is far noisier than the same ratio computed from hundreds of
thousands.

The fix is a burn-rate alert (doc 04): express the observed error rate as a multiple of the *error
budget's* rate, and require that multiple to hold across two windows of different length — a short
window (catches fast, severe problems quickly) and a longer window (filters out the small-sample
noise that dominates the short window during quiet periods). Doc 04 derives the standard thresholds
(a burn rate of roughly 14.4× sustained in both a 5-minute and a 1-hour window, for a 99.9% SLO)
rather than asserting them.

**A strong candidate would also raise** that even a burn-rate alert can misbehave at extremely low
traffic — if the service gets ten requests an hour, no alerting scheme extracts a statistically
meaningful signal from that sample size, and the honest answer at that volume is to alert on absolute
counts or absence of traffic entirely, not a rate-based rule of any kind.

---

### Q10. Reconcile RED, USE, and Google's "four golden signals" for someone who has only heard of the third one.

**Model answer.** The four golden signals — latency, traffic, errors, saturation — are best
understood as RED with one signal borrowed from USE, not a third independent framework. Latency,
traffic, and errors map directly onto RED's Duration, Rate, and Errors; saturation is USE's
contribution, added because a request-driven service is very often *also* something with an internal
capacity ceiling (a thread pool, a connection pool, a queue) whose exhaustion explains failures the
first three signals cannot predict on their own — doc 03 works through exactly why `checkout-api`
needs its own RED for the requests it serves *and* a USE view of the resources (its own connection
pool, `orders-db`'s connection ceiling) it depends on, because a perfect RED picture can precede a
resource exhaustion incident by only a few minutes.

**A strong candidate would also raise** that this reconciliation is also why the four-golden-signals
framing is sometimes criticized as incomplete on its own for a component that is purely a resource
with no request-serving behavior of its own — `orders-db` has no "traffic" in the request-per-second
sense a golden-signals dashboard usually assumes; it needs the fuller USE treatment (utilization,
saturation, *and* the specific resource dimensions — connections, disk I/O, buffer cache — that a
single "saturation" panel collapses into one number).

---

### Q11. What's the actual cost of a high-cardinality label, in terms a skeptical engineer who thinks you're being paranoid would accept?

**Model answer.** Ground it in the CS-4 arithmetic rather than an abstract warning: a bounded label
set (endpoint × method × status code × pod instance, roughly 6,720 combinations for `checkout-api`)
stays flat regardless of traffic volume, because the same label *values* repeat on every request. A
label like `customer_email` is nearly unique per request, so instead of a bounded number of series,
the metric mints a new series on almost every request — at `checkout-api`'s 640 req/s, that's
roughly 32 million new series over a 14-hour window before anyone notices, each one consuming 2-4KB
of Prometheus head-block memory, which is the arithmetic behind why one careless label caused a
cluster-wide metrics outage rather than a `checkout-api`-scoped one: one Prometheus server scrapes
every service, so degrading its memory degrades everyone's dashboards and alerts at once.

The practical guardrail: treat any label whose value comes from user input, a request ID, an email,
a raw IP, or free text as presumptively unbounded, and require an explicit justification (and ideally
an enforced label-name denylist) before it ships on a metric rather than a log line.

**A strong candidate would also raise** that cardinality cost is multiplicative across dimensions,
not additive — adding one 10-value label to a metric that already has three other labels multiplies
total series by up to 10×, not by 10 additional series, which is why even "small," bounded-looking
labels deserve a moment's arithmetic before merging, not just unbounded ones.

---

### Q12. Should a scheduled batch job (a CronJob, say) be instrumented with RED, USE, both, or neither?

**Model answer.** Neither framework applies cleanly on its own, and that gap is exactly why
`K8s/cronJobs` doc 08 in this repository treats scheduled work as its own case rather than forcing
it into RED. A CronJob does not serve a continuous stream of requests (so Rate, in the RED sense, is
degenerate — it "fires" on a schedule, not on demand), but each firing does have a duration and a
success/failure outcome, which is RED-shaped in miniature. Meanwhile the job's dependencies (a
database it writes to, a queue it drains) are exactly the resources USE already covers.

The synthesis: treat each **firing** with a RED-like triplet — did it run, how long did it take, did
it succeed — but replace "Rate" with **staleness**, because the operationally important question for
something that runs on a schedule is not "how many per second" but "how long since the last
success," which is what `K8s/cronJobs` doc 08 builds its entire alerting strategy around. Then apply
USE normally to whatever resources the job's work touches — exactly as this collection does for
`invoice-rollup` and `payout-settlement`'s shared use of `orders-db`.

**A strong candidate would also raise** that this is precisely why a "job succeeded" signal is
insufficient on its own (the CS-1-style trap, and `K8s/cronJobs` F-13's exit-0-while-failing case) —
a scheduled job needs an *outcome* metric describing the effect of the run (rows written, records
processed), not just the process exit code, for the same reason a request-driven service needs error
rate to mean "the customer got a correct answer," not merely "the server returned a response."

# Change — Deploys, Configuration, and Schema, the Largest Single Cause of Outages

If you keep one habit from this collection, keep this one:

> **When something breaks, the first question is "what changed in the last sixty minutes?" — not
> "what is broken?"**

Not because change is the only cause, but because it is the most common one by a wide margin, it
is the fastest to check, and it is the fastest to undo. A team that spends forty minutes
debugging a cascade before discovering a config push at the start of it has spent forty minutes
that a five-second check would have saved.

The statistics are consistent across every published postmortem corpus: **a large majority of
production incidents in mature systems are triggered by a change the operators made
themselves.** Hardware fails rarely and you designed for it. Traffic grows predictably. What
surprises a system is a new version of itself.

And there is an asymmetry that makes this the highest-leverage doc in the collection: unlike a
disk failure or a dependency's outage, **you control when changes happen, how fast they
propagate, how much they affect, and whether they can be undone.** Every one of those four is a
dial, and most organisations leave several of them at the wrong setting.

## Why change is different from every other failure source

Three properties, and they compound.

**1. It is correlated by construction.** Doc 00 established that redundancy only helps against
independent failures. A deploy is the most perfectly correlated event in your system: every
instance receives the *same* new code. Your three availability zones, your N+2 headroom, your
multi-region architecture — none of it helps, because the failure is identical everywhere.

**2. Configuration propagates faster than code and is reviewed less.** A code deploy goes through
CI, review, staging, and a rolling rollout that takes ten minutes. A config change goes live in
thirty seconds, often from a UI, often with no review, often by someone who is not the service
owner. **The blast radius is the same and the safety mechanisms are not.** This single asymmetry
accounts for a large share of the worst outages in the industry.

**3. The failure is often not where the change is.** A change to service A breaks service D,
because D depended on a behaviour of A that was never in a contract. The team that made the
change sees nothing wrong. The team that is paged has no idea a change occurred.

## The dials you control

| Dial | Bad setting | Good setting |
|---|---|---|
| **Blast radius** | All instances at once | 1% → 5% → 25% → 100%, with gates |
| **Propagation speed** | Instant and global | Deliberately staged, with a minimum time per stage |
| **Detection time** | A human noticing | An automated health gate per stage, in tens of seconds |
| **Undo time** | Re-run the pipeline, 20 minutes | One command or automatic, under 2 minutes |
| **Coupling** | Services must deploy together | Every change independently deployable and independently revertible |

The last one is the hardest and the most valuable. If service A's new version requires service
B's new version, then you cannot roll back A without rolling back B, and you now have a
coordinated rollback under pressure — which is how a ten-minute incident becomes a two-hour one.

## Deployment strategies compared

| Strategy | How | Blast radius during rollout | Rollback speed | Cost | Catches |
|---|---|---|---|---|---|
| **Recreate** | Stop all, start all | 100%, with downtime | Redeploy | Lowest | Nothing |
| **Rolling** | Replace N at a time | Grows from `N/total` to 100% | Roll forward or back, minutes | Low | Crashes and gross errors, if you watch |
| **Blue/green** | Full parallel fleet, switch traffic | 0% then 100%, instantly | **Instant** (switch back) | 2× infrastructure during the switch | Little — the switch is all-or-nothing |
| **Canary** | Small % to the new version, compare | Bounded at the canary % | Fast (remove the canary) | Moderate | Error rate, latency, and business-metric regressions |
| **Shadow / mirror** | Copy traffic to the new version, discard responses | **0%** — responses are not used | N/A | Duplicate load on downstreams | Crashes, latency, and correctness (by comparing outputs) — with zero user risk |
| **Feature flag** | Deploy dark, enable per cohort | Exactly what you choose | **Instant** (flip the flag) | Code complexity (`G-05`) | Everything, at any granularity |

The combination that actually works in practice, and which each of the running systems uses in
some form:

1. **Deploy dark behind a flag**, so deployment and release are separate events. The code ships
   with the flag off; nothing changes for users; if the deploy itself is broken you find out with
   no feature risk.
2. **Progressive rollout of the binary** (rolling or canary), with automated health gating.
3. **Progressive enablement of the flag** by cohort, with the same gating.
4. **Automated rollback** at both stages.

Separating deploy from release is the key move, and it is worth being explicit about why: it
turns one risky event with two possible causes of failure into two events each with one, and it
gives you an instant undo (the flag) for the half that is most likely to be wrong (the
behaviour).

## The failure catalogue

### G-01 · The deploy that goes everywhere at once

**What you see.** 100% error rate, starting within seconds of a deploy completing.

**Mechanism.** No progressive rollout, or a rollout so fast it is effectively simultaneous. A
`maxSurge: 100%` / `maxUnavailable: 50%` rolling update on a 40-pod deployment replaces
everything in under a minute — which is a rolling update in configuration and a recreate in
practice.

**Confirm it.** Deploy timestamp versus error onset. If the error rate rises with the same shape
as the rollout percentage, it is the deploy.

**Recover.** Roll back. And this is where the *undo time* dial matters: measure how long it
takes, today, from "decide to roll back" to "old version serving 100% of traffic." Most teams
have never measured it and are surprised — a Helm rollback that re-pulls images and waits for
readiness on 40 pods can be eight minutes, which is eight minutes of full outage.

**Prevent.**

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxSurge: 25%
    maxUnavailable: 0          # never go below the current replica count
minReadySeconds: 30            # a pod must stay ready for 30s before the next batch
progressDeadlineSeconds: 600   # fail the rollout rather than grinding forever
```

`maxUnavailable: 0` is the important one and it is not the default: it means capacity never drops
during a deploy, at the cost of needing headroom for the surge.

`minReadySeconds` is the second most important: it forces a pause between batches during which a
crash-on-startup or a fast-failing version is caught. Without it, a rolling update can replace
every pod before the first bad one has had time to fail.

And the thing that makes all of it work: **an automated gate that watches error rate and latency
during the rollout and aborts.** Argo Rollouts, Flagger, or Spinnaker automated canary analysis
all do this. The rule of thumb: the gate should be able to detect and abort within two minutes,
which means the canary stage must carry enough traffic to be statistically meaningful within two
minutes — see `G-15`.

### G-02 · The rollback that does not roll back

**What you see.** You roll back and the problem persists, or a new one appears.

**Mechanism.** Code is reversible. Its effects are not. The specific cases:

- **A schema migration ran.** The new code added a column and backfilled it. The old code does
  not know about the column, which is fine — but if the migration *dropped* or *renamed*
  something, the old code is now broken. This is why expand–contract exists (`G-07`).
- **Data was written in a new format.** New code writes JSON with a new field, or a new
  serialisation version, into a shared store. Old code cannot deserialise it. Rolling back the
  code does not roll back the rows.
- **Messages were published in a new format.** They are sitting in a topic. The rolled-back
  consumer cannot read them.
- **External state changed.** Records created in a third-party system, emails sent, webhooks
  fired.
- **A cache was populated in the new format**, so rolled-back code reads garbage. (Fixable by
  including a version in the cache key — `C-11`.)
- **A stateful migration is one-way**: an index was rebuilt, a partition was split.

**Prevent.** The discipline is: **every change must be independently revertible, which means every
change must be backward compatible with the version before it.** Concretely:

- New code must read both old and new data formats.
- New code must write a format old code can read — **until the old code is gone**, at which point
  a later change can stop writing the old format.
- Migrations are additive first; destructive steps come in a *separate, later* change.
- Never roll back across a destructive migration; roll *forward* with a fix.

And know, per service, which changes are revertible and which are not. A deploy that crosses a
one-way boundary should say so in the pull request, so the person deciding at 3 a.m. knows.

### G-03 · The configuration push with deploy blast radius and no deploy process

**What you see.** A total outage with no deploy in the deploy log.

**Mechanism.** Property 2 from the top of this doc. Configuration includes far more than most
teams' change process covers:

- Service configuration (timeouts, pool sizes, retry policies, thresholds)
- Mesh and gateway configuration (routes, policies, mTLS mode) — `D-08`
- Feature flags — `G-04`
- Load-balancer and DNS configuration — `E-01`, `E-11`
- WAF and rate-limit rules
- IAM policies and security groups
- Autoscaling parameters
- Database parameter groups
- Content and pricing data that drives behaviour

Every one of those can cause a total outage, and in most organisations several of them are
changeable by one person through a UI in under a minute, with no review, no staging, and no
automated rollback.

Riverbend's `RB-1` incident (doc 16) is exactly this: a valid promotions configuration that
collapsed a cache hit rate and took checkout down for 39 minutes.

**Prevent.** **Configuration is code, and must go through the code process.** Specifically:

1. **Store it in version control**, so there is a diff, an author, a review, and a history. This
   alone changes the character of config incidents, because "what changed" becomes answerable.
2. **Validate it in CI** against a schema *and* against semantic invariants (doc 05's route-
   shadowing check is an example).
3. **Roll it out progressively with health gating**, exactly like code. If your config system
   cannot target a subset of instances, that is the gap to close — it is the single most valuable
   feature a config system can have.
4. **Automated rollback** on regression.
5. **A tested freeze/pause** so a bad push stops spreading.
6. **An audit log that is reviewed**, showing who changed what, when, and with what scope.

The organisational version of this: **treat "who can change production configuration" with the
same seriousness as "who can deploy code."** In most places the config list is much longer, and
nobody has audited it.

### G-04 · Feature flags as untested code paths

**What you see.** Enabling a flag breaks something the flag was not supposed to affect. Or
disabling one, months later, breaks something because the off-path has rotted.

**Mechanism.** A flag creates two code paths and your tests exercise one. The *other* path is
what runs in production when the flag is flipped, and it may never have run under load, with real
data, at concurrency.

The rotting-off-path version is the one that catches people during an incident: a flag has been
on for eighteen months. During an incident someone turns it off as a mitigation. The off-path has
not been executed since, calls a service that has since been decommissioned, and the mitigation
makes things worse.

**Prevent.**

- **Test both paths in CI.** A flag without tests for both states is an untested change waiting
  for a flip.
- **Exercise the off-path in production**, at a small percentage, permanently — the same argument
  as doc 03's untested fallback (`P-13`). If a flag is a kill switch you intend to use during an
  incident, it must be exercised when there is no incident.
- **Flags have an owner and an expiry.** A flag is a temporary state between two permanent ones.
  Any flag older than 90 days is either a permanent configuration option (in which case remove
  the flag and make it configuration) or debt. Track flag age and make it visible.
- **Cap the number of live flags per service.** This is a blunt instrument and it works, because
  the alternative is `G-05`.

### G-05 · Flag interaction: the combinatorial explosion

**What you see.** A bug that only occurs for a specific combination of flags, affecting a small
and seemingly random set of users, and which nobody can reproduce.

**Mechanism.** `n` boolean flags produce `2^n` configurations. Twenty flags is 1,048,576 possible
states. You test a handful. Production runs many of them simultaneously, distributed across
users by percentage rollouts and targeting rules, so different users are literally running
different programs.

The debugging experience is: user X has a bug, user Y with identical inputs does not, and the
difference is a flag combination nobody can see without querying the flag service for that
specific user.

**Prevent.**

- **Log the full flag evaluation context with every request** — which flags were evaluated and
  what they returned — and attach it to the trace. Without this, per-user behaviour differences
  are undebuggable. With it, "what was different about user X" is one query.
- **Prefer mutually exclusive variants to independent booleans.** One flag with four variants is
  four states; two booleans is four states too, but three booleans is eight and four is sixteen.
  Multi-variant flags keep the space linear.
- **Delete flags aggressively**, which is the only thing that actually reduces `2^n`.

### G-06 · The flag service as a hard dependency

**What you see.** The feature-flag service has an incident and every service that reads flags
fails, or reverts every feature simultaneously.

**Mechanism.** Flag evaluation happens on the request path. If the SDK makes a network call per
evaluation, the flag service's availability is in your availability product (doc 00). If the SDK
fails closed, an outage turns off every feature at once — including ones whose off-path has
rotted (`G-04`).

**Prevent.** The posture from doc 00's table: **fail-static, with a compiled-in default.**

- The SDK maintains a **local cache of all flag values**, refreshed by streaming or polling. Every
  evaluation is in-process, with no network call, in microseconds.
- On a service outage, the cache continues to be used — **never expired because the service is
  unreachable** (`D-05`'s inversion again).
- The cache is persisted to local disk so a pod starting during an outage has values.
- Every flag has a hardcoded default in the calling code for the case where there is no cached
  value at all, and that default is the *current production value*, not `false`.
- Alert on flag-cache age, so you know you are running on stale values.

### G-07 · The schema migration that locks the table

**What you see.** A migration that should take milliseconds causes a multi-minute outage.

**Mechanism.** `S-18`, expanded. Two distinct problems:

*The operation itself is slow.* `ALTER TABLE ... ADD COLUMN ... DEFAULT <value>` historically
rewrote the whole table (PostgreSQL before 11, MySQL depending on version and engine). On a
500-million-row table that is tens of minutes with an exclusive lock.

*The lock queue.* This is the worse one and it is not obvious. PostgreSQL's lock queue is
**ordered**: if a long-running `SELECT` holds a shared lock, and your `ALTER TABLE` requests an
exclusive lock, the `ALTER` waits — and **every subsequent query, including short reads, queues
behind the `ALTER`.** A 50-millisecond schema change blocked behind a 10-minute analytics query
stops all traffic to that table for 10 minutes.

```
t=0:00  Analytics query starts (10 min runtime), holds ACCESS SHARE
t=0:30  ALTER TABLE requests ACCESS EXCLUSIVE → waits
t=0:31  Normal SELECT requests ACCESS SHARE → waits behind the ALTER
        ... every query from here waits ...
t=10:00 Analytics finishes, ALTER runs in 50ms, everything unblocks
```

Nine and a half minutes of total outage from a fast migration.

**Prevent.**

```sql
-- Always, for DDL. Fail fast instead of queueing behind a long read.
SET lock_timeout = '2s';
ALTER TABLE orders ADD COLUMN gift_message text;
-- If it times out, retry. Do NOT remove the timeout.
```

Plus: use online-migration tooling (`gh-ost`, `pt-online-schema-change`, `pg_repack`) for
anything that rewrites a table; know which operations are instant on your version (adding a
nullable column with no default is instant on modern PostgreSQL; adding an index requires
`CREATE INDEX CONCURRENTLY`); and run migrations during low traffic even when you believe they
are instant, because the lock queue makes "instant" conditional on what else is running.

### G-08 · Migration and deploy in the wrong order — the expand/contract discipline

**What you see.** Errors during a deploy window, in whichever direction you did not think about:
old code hitting a changed schema, or new code hitting an unchanged one.

**Mechanism.** During any rolling deploy, **old and new code run simultaneously against the same
database.** That is not an edge case; it is guaranteed, for the duration of every deploy. So the
schema must be compatible with both versions at the same time, which a single "rename a column"
migration never is.

**Prevent.** The **expand–contract** (or parallel-change) pattern. Renaming `orders.total` to
`orders.total_amount` takes four deploys, not one:

```
Deploy 1 — EXPAND
  Migration: ADD COLUMN total_amount (nullable, no default → instant)
  Code:      writes BOTH columns, reads `total`
  Compatible with: previous version (which ignores the new column) ✓

Deploy 2 — BACKFILL
  Job:       copy total → total_amount in batches (see G-09)
  Code:      unchanged
  Compatible with: everything ✓

Deploy 3 — SWITCH READS
  Code:      writes both, reads `total_amount`
  Compatible with: deploy 2's version (both columns are current) ✓
  Revertible: yes — deploy 2's code still works ✓

Deploy 4 — CONTRACT
  Code:      writes and reads `total_amount` only
  Migration: DROP COLUMN total  (a separate, later change — after a soak period)
  ⚠️ This step is NOT revertible past deploy 3
```

Four steps over days instead of one step in an afternoon. Every intermediate state is valid,
every step is independently revertible except the last, and there is no deploy window during
which anything is broken.

The same pattern applies to every kind of contract change — API fields, event schemas, queue
message formats — and it is the general answer to "how do two versions coexist."

The discipline that makes it stick: **destructive operations are never in the same change as the
thing that makes them possible.** Add the column now, drop the old one next month, after you have
confirmed nothing reads it (which you can confirm by monitoring, if you instrument the read).

### G-09 · The backfill that overwhelms

**What you see.** A migration job takes the database down, or saturates replication, or fills the
disk.

**Mechanism.** `UPDATE orders SET total_amount = total` on 500 million rows:

- One transaction holds locks on every row it touches, blocking concurrent writes (`S-10`).
- It generates one WAL record per row — potentially hundreds of gigabytes — which must be
  replicated. Replicas fall hours behind (`S-05`), and any read-from-replica path serves ancient
  data.
- It generates 500 million dead tuples, which `VACUUM` must reclaim (`S-11`).
- The disk may not have room for the WAL plus the table bloat.

**Prevent.** Batch, throttle, and make it resumable:

```sql
-- Batched, resumable, low-impact. Run in a loop from a job, not as one statement.
WITH batch AS (
  SELECT order_id FROM orders
  WHERE total_amount IS NULL
  ORDER BY order_id
  LIMIT 5000
  FOR UPDATE SKIP LOCKED
)
UPDATE orders o SET total_amount = o.total
FROM batch b WHERE o.order_id = b.order_id;
-- commit; sleep 100ms; repeat until zero rows affected
```

Then: **throttle the loop against replication lag** — pause when lag exceeds a threshold and
resume when it recovers. That single control turns a backfill from a risk into a background
process, and it is about ten lines in the job.

Size the batch so each transaction is well under a second, make the job idempotent and resumable
(the `WHERE total_amount IS NULL` predicate does both), and run it as a job you can stop — not
as a migration that must complete before the deploy proceeds.

### G-10 · The contract change nobody knew was a contract

**What you see.** Service A deploys; service D breaks. A's team sees a green rollout.

**Mechanism.** A's behaviour changed in a way that was not in the API schema but which D depended
on. The catalogue of accidental contracts:

- **Field ordering** in a JSON response, which a client parsed positionally.
- **A field that was always present** becoming optional, or becoming `null`.
- **A default value** changing.
- **Error semantics**: returning 404 instead of an empty list; returning 500 instead of 400,
  which changes whether callers retry (`R-07`).
- **Latency**: A got 3× slower but still correct, so D's timeout now fires (`R-14`).
- **Pagination size** changing from 100 to 50, so D's loop makes twice as many calls.
- **Ordering** of results: a query whose `ORDER BY` was implicit now returns a different order
  because the plan changed (`S-13`).
- **Enum values**: adding a new value to an enum that clients switch on exhaustively.

**Prevent.**

- **Consumer-driven contract tests** (Pact or equivalent): each consumer declares what it
  depends on, and the provider's CI verifies it still holds. This turns implicit contracts into
  explicit failing tests, and it is the only mechanism that catches most of the list above.
- **Schema validation in CI** for API and event schemas, with compatibility enforcement.
- **A latency budget as part of the contract**, monitored: "p99 under 100 ms" is a contract just
  as much as the response shape, and it is the one that gets broken silently.
- **Never remove or repurpose an enum value**; add new ones and require clients to handle unknown
  values (a default branch that degrades rather than throws).

### G-11 · The dependency upgrade

**What you see.** A routine library bump changes behaviour in a way nobody anticipated.

**Mechanism.** A direct dependency's minor version bump pulls transitive changes. Real examples
of the shape: an HTTP client library changing its default timeout from unlimited to 30 s (or the
reverse); a JSON library changing how it handles unknown fields; a database driver changing its
default isolation level or its prepared-statement caching; a logging library changing its default
level; a TLS library dropping support for an older cipher that one internal service still uses.

The supply-chain version is worse: a transitive dependency is compromised or yanked.

**Prevent.** Lockfiles committed and enforced. Renovate/Dependabot with **automated tests that
actually cover behaviour**, not just compilation. Staged rollout of dependency bumps exactly like
any other change — a dependency upgrade is a code change with an unusually large and poorly
understood diff. Pin transitive dependencies for anything in the critical path. And read the
changelog for defaults, because defaults are where the surprises are.

### G-12 · Infrastructure-as-code drift and the apply that deletes

**What you see.** A `terraform apply` proposes to destroy and recreate a production resource. Or
it does it before anyone reads the plan.

**Mechanism.** Two shapes.

*Drift*: someone made a manual change in the console. The next `apply` reverts it — including, if
the manual change was an emergency mitigation, reverting the mitigation. This is `F-12`'s second
outage with a specific mechanism.

*Forced replacement*: a change to an immutable attribute (an availability zone, an engine
version, a name) causes the provider to plan `destroy` then `create`. For a database, that is
data loss. The plan says so, in the middle of 400 lines of output.

**Prevent.** `prevent_destroy` lifecycle rules on every stateful resource — databases, buckets,
volumes. Plan output reviewed by a human with destroy operations highlighted (most CI
integrations can fail the build on any `destroy` in the plan for tagged resources). Drift
detection running continuously so manual changes are found in hours, not at the next apply. And
a policy that emergency manual changes are immediately followed by a code change that
reconciles them — the same `F-12` close-out discipline.

### G-13 · Secret and certificate rotation

**What you see.** A scheduled rotation causes an outage, or a rotation silently fails and the
outage comes later when the old credential expires.

**Mechanism.** Rotation is a coordinated change across every consumer of the credential, and it
has a specific shape: there is a moment when some consumers have the new secret and some have the
old. If the *verifier* only accepts one of them, half your fleet fails.

**Prevent.** The two-phase pattern, which is expand–contract for credentials:

```
Phase 1: the verifier accepts BOTH old and new (two valid keys, two valid passwords)
Phase 2: roll out the new credential to all consumers
Phase 3: confirm zero usage of the old one (requires per-credential usage metrics)
Phase 4: revoke the old one
```

Phase 3 is the step that gets skipped, and it is the one that makes phase 4 safe. It requires
the verifier to report which credential was used, which is a small amount of instrumentation
with large value.

Also: `E-07`'s certificate monitoring from outside; a short rotation period so the process runs
often enough to be reliable; and never a "big bang" rotation of everything at once.

### G-14 · The deploy that restarts everything at once

**What you see.** A deploy where every instance was replaced successfully, no errors in the
rollout, and a latency and error spike lasting two minutes.

**Mechanism.** `F-10`. The new instances are cold — empty caches, unwarmed JIT, no connection
pools — and collectively serve a fraction of the old fleet's capacity for the first minute. Pod
count never dropped and capacity did.

Plus the connection storm: every client's connections to the old pods break and re-establish
(`E-15`, `F-06`).

**Prevent.** `minReadySeconds` long enough to cover warm-up; readiness probes that require warmth
rather than liveness; load-balancer slow start; `maxUnavailable: 0`; and a pause between batches.
For services with a long warm-up (a JVM with a large working set, a model server), consider
blue/green with a warm-up period before the switch instead of rolling.

### G-15 · The canary that measures nothing

**What you see.** A canary passed and the full rollout broke production.

**Mechanism.** Canary analysis has four blind spots, and most canary setups have at least two.

*Not enough traffic to be significant.* A 1% canary on a service doing 100 req/s sees 1 req/s. To
detect a change in error rate from 0.1% to 1% with confidence, you need hundreds of errors, which
at 1 req/s and 1% is **hundreds of seconds** minimum. The canary passes because it has not seen
enough requests to fail, and this is the most common blind spot by far. Either give the canary
more traffic or run it longer — and compute which, from the rate and the effect size you need to
detect, rather than picking "5% for 10 minutes" because it sounds reasonable.

*Not enough time to hit the failure.* A memory leak takes an hour. A cache-related failure takes
a full TTL. A daily batch interaction takes a day. A canary that runs for ten minutes cannot see
any of them.

*Not the right traffic.* The canary receives a random sample, and the bug affects one customer
segment, one locale, one API version, or one device type. Random sampling under-represents the
tail exactly where bugs live.

*Comparing against the wrong baseline.* Comparing the canary to the *old* version's metrics from
last week conflates the version change with time-of-day, traffic mix, and downstream state
changes. The correct comparison is a **concurrent baseline**: a control group of old-version
instances that receive the same traffic pattern at the same moment.

**Prevent.** Address each: route enough traffic (or run long enough) for statistical
significance, computed rather than guessed; a soak stage measured in hours for anything with
state; deliberate routing of a representative slice (including internal users and a sample from
each major segment); and always a concurrent control group, compared with a test that accounts
for variance rather than a threshold on a single number.

And accept the limit honestly: **a canary catches fast, common, traffic-visible regressions.** It
does not catch slow leaks, rare paths, or correctness bugs that produce valid-looking responses.
For those you need the shadow/mirror strategy (compare outputs, not just error rates) and
reconciliation (doc 07, `T-13`).

### G-16 · The coordinated multi-service release

**What you see.** A release requiring several services to deploy in a specific order, which is
either impossible to roll back or takes an hour.

**Mechanism.** Service A's new version requires service B's new version. The deploy is now a
distributed transaction across deployment pipelines, with the same properties as doc 07's
distributed transactions and none of the tooling — partial completion is the normal case, and
there is no coordinator.

**Prevent.** This is a design failure more than an operational one. The rule: **every change must
be deployable and revertible independently**, which means every change must be compatible with
the *current* version of everything else. That is expand–contract (`G-08`) applied across
services:

```
Change 1: B adds the new endpoint, keeps the old one       (B deployable alone)
Change 2: A starts using the new endpoint behind a flag     (A deployable alone, revertible)
Change 3: Enable the flag progressively                      (revertible instantly)
Change 4: A removes the old call path                        (A deployable alone)
Change 5: B removes the old endpoint                         (B deployable alone, after
                                                              confirming zero usage)
```

Five changes instead of one coordinated release, each independently safe. It is more work and it
is the difference between a release you can undo in ninety seconds and one you cannot undo at
all.

If you genuinely cannot decouple — and it happens — then at minimum: rehearse the rollback in
staging, write the rollback procedure down before starting, and have both teams present.

## Change freezes, and what they are actually for

A change freeze during a high-stakes period (Gateline before a mega-event sale, Riverbend during
peak shopping days) is a legitimate and underrated control. The reasoning is precise, not
superstitious:

- The probability of an incident is roughly proportional to the rate of change.
- The *cost* of an incident is far higher during peak.
- Therefore reduce the rate of change during peak.

The mistakes people make with freezes:

- **Freezing everything, including the fixes.** A freeze must have a defined emergency path, or
  the first real problem is made worse by the control meant to help.
- **A long freeze followed by a big-bang unfreeze.** Two weeks of accumulated changes released
  together is a much riskier event than the changes would have been individually. Ramp back up.
- **Freezing deploys but not configuration.** Config is the higher-risk channel (`G-03`), so a
  freeze that only covers code is freezing the safer half.
- **Treating the freeze as the safety mechanism.** It is a *reduction in exposure*, not a
  substitute for progressive rollout, gating, and fast rollback. A team that only has a freeze is
  a team that cannot ship safely the rest of the year either.

## What to take away

1. **"What changed in the last sixty minutes?" is the first question in every incident** — not
   because change is the only cause, but because it is the most common, the fastest to check, and
   the fastest to undo.
2. **A deploy is the most perfectly correlated failure in your system.** Every instance gets the
   same new code, so multi-AZ, N+2, and multi-region give you nothing against it.
3. **Configuration propagates faster than code, is reviewed less, and has the same blast
   radius.** That asymmetry is the largest unaddressed risk in most organisations. Config is code:
   version control, CI validation, progressive rollout, automated rollback, audit.
4. **You control four dials — blast radius, propagation speed, detection time, undo time.**
   Measure the fourth one today; most teams are surprised by how long a rollback actually takes.
5. **Separate deploy from release.** Ship dark behind a flag, then enable progressively. One risky
   event becomes two events with one cause each, and the behavioural half gets an instant undo.
6. **`maxUnavailable: 0` plus `minReadySeconds` plus an automated health gate** is the minimum
   viable safe rolling deploy, and none of the three are defaults.
7. **Code is reversible; its effects are not.** Every change must be backward compatible with the
   version before it, and destructive steps must be a separate, later change. Know which of your
   changes cross a one-way boundary and say so in the pull request.
8. **Old and new code always run simultaneously during a deploy** — that is guaranteed, not an
   edge case. Expand–contract (add, backfill, switch reads, remove) is the general answer, for
   schemas, APIs, events, and credentials alike.
9. **A fast migration blocked behind a long read blocks everything after it.** Always set
   `lock_timeout` on DDL and retry, rather than queueing.
10. **Throttle backfills against replication lag.** Ten lines of control turn a database-killing
    migration into a background process.
11. **Flags are untested code paths and they multiply as `2^n`.** Test both states, exercise the
    off-path in production, log the full evaluation context on every request, prefer multi-variant
    flags to independent booleans, and delete flags aggressively.
12. **The flag service must be fail-static with compiled-in defaults set to the current production
    value** — not `false`, which turns an outage into a simultaneous revert of every feature.
13. **Most contracts between services are accidental**: field presence, error semantics, ordering,
    latency, pagination size. Consumer-driven contract tests are the only mechanism that catches
    them, and latency belongs in the contract.
14. **Canaries have four blind spots**: insufficient traffic for significance, insufficient time
    for slow failures, unrepresentative traffic, and a non-concurrent baseline. Compute the
    required traffic and duration rather than guessing, and always use a concurrent control group.
15. **A coordinated multi-service release is a distributed transaction with no coordinator.**
    Decompose it into independently deployable and revertible steps, even though it is five
    changes instead of one.
16. **A change freeze reduces exposure; it is not a safety mechanism.** It needs an emergency
    path, a ramped unfreeze, and it must cover configuration — or it is freezing the safer half.

Next: [12-capacity-autoscaling-and-noisy-neighbours.md](12-capacity-autoscaling-and-noisy-neighbours.md),
which covers the resources underneath everything: having enough, getting more in time, and the
container-level limits that cause the strangest failures in this collection.

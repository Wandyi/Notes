# Staff Interview Questions

Eighteen questions, with the answer a strong candidate gives and the follow-ups a strong
answer invites. They are also a self-test: if you can answer all eighteen without looking, you
have the material.

At the end, the five patterns that distinguish a strong answer from a merely complete one.

---

### 1. Add a `NOT NULL` column to a 521-million-row table with no downtime. Walk me through it.

**The answer.** It is a four-phase migration, not a statement, for two independent reasons.

*Mechanically*, `ALTER TABLE ... SET NOT NULL` scans every row under `ACCESS EXCLUSIVE` — a
336 GB scan, minutes, with the whole table unavailable and a lock queue forming behind it.

*Semantically*, the moment the constraint exists, old code that does not set the column fails
its inserts. So the constraint cannot exist until every writer has been updated.

The sequence on PostgreSQL 12 or later:

1. `ADD COLUMN currency char(3)` — nullable, no default. Metadata-only, milliseconds.
2. `ADD CONSTRAINT ... CHECK (currency IS NOT NULL) NOT VALID` — `ACCESS EXCLUSIVE` but no
   scan, so milliseconds. The constraint is enforced for new rows and not checked for old ones.
3. Deploy the dual write. Soak until nothing writes a null.
4. Backfill, chunked and throttled.
5. `VALIDATE CONSTRAINT` — scans, but under `SHARE UPDATE EXCLUSIVE`, so reads and writes
   continue.
6. `SET NOT NULL`, which PostgreSQL 12+ satisfies instantly using the validated check, then
   drop the check.

**Follow-ups a strong answer invites.** What if it is PostgreSQL 11? (No fast path; keep the
`CHECK` and do not convert it — a validated `CHECK` is functionally equivalent for most
purposes.) What about MySQL? (`ALGORITHM=INSTANT` for the column; the `NOT NULL` needs a
rebuild, so gh-ost.) Why not a default? (A default makes every pre-existing row silently claim
a value, destroying your ability to distinguish backfilled rows from real ones.)

---

### 2. Why can you not simply rename a column?

**The answer.** Not because it is slow — it is metadata-only and takes three milliseconds.
Because there is no instant at which both the old code (selecting `amount`) and the new code
(selecting `amount_minor`) can succeed, and during any rolling deploy both are running.

The deeper point is that this generalises: **every schema state must be compatible with the
code version before it and after it**, which is why the safe operations are the additive ones
and why a single logical change is three to six deploys.

**Follow-ups.** What about a view that aliases both names? (Works, and you have adopted a
view-based model with its own plan hazards.) What if you have a single monolith with one
instance? (Then you have a brief outage instead, which may be acceptable — say so explicitly
rather than pretending the constraint does not exist.)

---

### 3. A three-millisecond `ALTER TABLE` caused a seventy-second outage. Explain the mechanism.

**The answer.** The `ALTER` requested `ACCESS EXCLUSIVE` and queued behind a 90-second
analytics query holding `ACCESS SHARE`. PostgreSQL's lock queue is ordered and does not let a
later request jump a conflicting earlier one — so every subsequent query, including ones that
would not have conflicted with the analytics query, queued behind the pending `ACCESS
EXCLUSIVE`.

Then amplification: each blocked query holds a connection. At 350 requests/second against a
480-connection pool, the pool is exhausted in about 1.4 seconds, and queries against unrelated
tables start failing too.

The fix is one line: `SET lock_timeout = '2s'` plus a jittered retry loop. The `ALTER` fails
fast with `55P03` instead of queueing, and the impact is two seconds of latency rather than
seventy seconds of outage.

**Follow-ups.** What if the retry loop never succeeds? (There is a permanent long-running
reader; that is information, delivered as a clean failure. Find it with
`pg_blocking_pids`.) What is the equivalent on MySQL? (Metadata locks, same transitivity, and
`lock_wait_timeout` defaults to one year.) During the incident, do you cancel the DDL or the
blocker? (**The DDL** — cancelling the blocker grants the DDL its lock and hands it a 400-deep
queue.)

---

### 4. You need to backfill 521 million rows. How do you choose the rate?

**The answer.** By finding the binding constraint, which is usually not the database.

Enumerate everything that observes writes to the table: WAL and replication, autovacuum, CDC
connectors, triggers, `updated_at`-driven incremental syncs, the outbox, cache invalidation.
Measure each one's headroom, and the slowest sets the rate.

At Riverbend the answer was a Kafka topic. The database could absorb 18,000 rows/second — WAL
at 3 MB/s against 90 MB/s capacity — but the Debezium connector emits one change event per row,
and 18,000/second is 79× the normal 228/second. The topic's 72-hour retention was sized for 65
GB and would have received 573 GB, deleting segments under lagging consumers, which is
permanent downstream data loss. Throttling to 10× normal gives 2,300 rows/second and 63 hours.

The control loop should be feedback-driven with asymmetric backoff — back off fast,
recover slowly — because load varies over the day.

**Follow-ups.** Which signal on Aurora? (Not replica lag; shared storage means it is
milliseconds regardless. CPU, CDC lag, and your own SLI.) How do you make it finish faster?
(Route the backfill around CDC — pause the connector and re-snapshot, or filter by a
transaction marker — and accept the costs of each.) What if the table grows faster than you
backfill? (Deploy the dual write first so the target set is fixed; otherwise the arithmetic
may never close.)

---

### 5. Where is the point of no return in a column migration?

**The answer.** At "stop writing the old column", not at "drop it". That surprises people
because dropping *feels* irreversible, and by then you crossed the real line weeks earlier.

The test: *if I redeploy the previous application version right now and change nothing else,
does the system work correctly?* Before 6a, yes — the old column is still current, so
reverting the read switch is a flag flip. After 6a, the old column is frozen at the deploy
timestamp, so reverting requires re-deriving every write since.

A plan should state it explicitly: the step, the measured cost of reversal, and a named
person who decides to cross it.

**Follow-ups.** For a store cutover? (At the cutover, and "softly" — reversible while the
reverse stream is healthy, hard the moment it is not and nobody noticed.) How long between 6a
and 6b? (The maximum over all consumers, not the typical. At Riverbend that is 92 days,
because of a quarterly export.)

---

### 6. Design a zero-downtime migration of a 240-million-row table from Oracle to PostgreSQL.

**The answer.** The canonical pattern, and the ordering of the first two steps is the part
that matters:

1. **Build the target schema and write a semantic mapping document** — per column, the source
   type, the target type, and the specific hazards: `NUMBER` precision, `DATE`'s time
   component, `VARCHAR2`'s byte semantics, identifier case folding, and the empty-string rule.
2. **Start capture first** — record the SCN, open the CDC stream. Then snapshot as of that
   SCN. Capture-then-snapshot gives an overlap, which is harmless if every apply is a
   conditional upsert. Snapshot-then-capture gives a gap, which is invisible.
3. **Tail** until the target is within seconds.
4. **Verify continuously while the tail runs** — chunked checksums over the whole dataset, so
   that at cutover you already know the bulk agrees.
5. **Cut over** with a write freeze: revoke privileges (immediate, catches writers you forgot),
   drain the tail to zero measured against the source's current SCN, advance sequences with
   headroom, run a fast verification over the last hour only, flip, unfreeze. About 40 seconds.
6. **Start the reverse stream immediately**, and keep it for a stated window.

**The hazard I would call out first.** Oracle stores `''` as `NULL`. The migration preserves
this faithfully and the *behaviour* changes, because PostgreSQL distinguishes them and
application code will start using the distinction. Row counts and numeric checksums cannot
catch it.

**Follow-ups.** What if a reverse stream is impossible? (Say so in the plan and raise the
go/no-go bar — the failure is cutting over while *believing* you can roll back.) What about
the PL/SQL? (Inventory it in week one with a query against `dba_source`. At Ironvale that was
18,412 lines in one schema and it changed the estimate from two quarters to six.)

---

### 7. How do you verify that a migration produced correct data?

**The answer.** The test of a method is whether you can name a realistic defect it would
catch. Four methods with different blind spots:

- **Row counts.** Cheap, necessary, and blind to every value defect.
- **Chunked checksums.** Full coverage, resumable, and they *localise* — a mismatched chunk is
  5,000 rows and a binary search finds the row in 13 more comparisons. On a 240 GB table with
  eight parallel workers this is under five minutes, so sampling is often unnecessary.
- **Sampling,** when a full comparison is genuinely too expensive.
  `n > ln(1 − confidence) / ln(1 − p)`, so 460,500 rows gives 99% confidence that fewer than 1
  in 100,000 differ — and that number is independent of table size. Sample by a deterministic
  hash, not `ORDER BY id LIMIT n`.
- **Invariants.** The highest value per line: `sum(amount)` must equal
  `sum(amount_cents)/100`; nothing null that was not null before; and reconciliation against
  Finance, who have an independent record.

**The two traps.** Never verify by re-applying the transformation under test — that proves
determinism, not correctness. And never iterate from the backfill's own cursor, or
verification inherits the backfill's blind spot; enumerate from the table.

**Follow-ups.** 1 in 100,000 on 521 million rows is 5,210 wrong rows — is that acceptable?
(For money, no; do the full comparison. The point is that the acceptable rate is a decision
you make before choosing the method.) What if you find residual divergence that will not reach
zero? (Re-check each disagreement once — most of it is read races. Then classify and document
the rest; a team that waits for a number that can never be zero stalls in phase 4 forever.)

---

### 8. How do you roll back a migration?

**The answer.** By knowing which of three categories the step is in.

**Reversible** — additive, nothing writes or reads it yet. Undo with a statement, seconds.
**Recoverable** — the data still exists but reversal needs a tested script and hours.
**Irreversible** — a drop, a narrowing, or a cutover that accepted writes with no reverse
stream.

Down migrations are not a plan. They are the only code in the system with zero production
exercise, and for data changes they are not inverses — the down migration for "add and
backfill" is "drop", which is a second destructive operation.

Point-in-time recovery is almost never a rollback. The problem is not the 70 minutes to
restore; it is that restoring to a point two hours ago discards 482,400 orders that were
confirmed to customers. PITR's real value for migrations is making a production-sized clone to
rehearse on.

Past the point of no return, **fix forward**, and what makes that safe is what you did
earlier: the old representation still exists, the backfill is idempotent and resumable, and
verification can localise the damage.

**Follow-ups.** When *is* rolling back the deploy the wrong move? (When old pods are failing —
reverting puts every pod in the failing state. Roll forward.) How do you make sure a rollback
works? (Run every down migration in CI — up, down, up, assert — and rehearse the recoverable
ones on a clone with a timing, which is where "11 minutes per 24 hours of writes" comes from.)

---

### 9. Your team runs migrations in an init container across 40 pods. What is wrong with that?

**The answer.** Three things.

**It runs on every pod start**, not every deploy — so a node drain at 02:00 runs it again.
Usually harmless, and occasionally 40 pods race to apply a pending migration simultaneously.

**It forces the wrong ordering for contract migrations.** An init container always runs
immediately before the new code. That is correct for expand and wrong for contract, and there
is no way to express the difference.

**It couples rollback.** Reverting the deploy does not revert the migration, so a coupled
artefact gives you a single button that reverts the code and leaves the schema — which is how
a 50% failure becomes a 100% failure.

The right place is a separate, explicitly triggered pipeline step with its own status,
orderable before or after the deploy as the direction requires.

**Follow-ups.** What if you must run them from the application? (An advisory lock —
session-scoped, so it is released when the process dies, unlike a lock table.) Why not a Helm
`pre-upgrade` hook? (Acceptable for expand-only; it still cannot express contract-after-deploy.)

---

### 10. How do you run one migration across 4,096 shards?

**The answer.** As a reconciler, not a script. Re-discover targets each pass, make each step
idempotent, never let one failure abort the pass, and keep state in a table so the progress
report is a query and the pause switch is an `UPDATE`.

Waves that grow geometrically while bake times shrink: one canary with a 24-hour bake, then
1%, 5%, 25%, 100%. The 24 hours is the shortest window that exercises a batch run, a backup, a
peak hour and an autovacuum cycle.

**And "complete" means zero targets not in the desired state, never a percentage.** A
dashboard reading 99.7% is twelve shards that will fail in five months.

Separately and permanently: standing drift detection, one schema fingerprint per shard
compared against the mode. The rollout's own reporting is point-in-time; only drift detection
catches the shard restored from a pre-migration backup three weeks later.

**Follow-ups.** How do you handle version skew? (Track min and max schema version across the
fleet as a metric, have the application declare its supported range, and make the deploy
pipeline refuse if the ranges do not cover.) What if the rollout takes 45 days? (It will —
1.34 billion rows per shard at 22,000 rows/second is 16.9 hours, 64 in parallel is 45 days. So
code must tolerate all intermediate states by construction, and only one structural migration
per table may be in flight.)

---

### 11. What breaks when you move from a case-insensitive database to a case-sensitive one?

**The answer.** Uniqueness and lookups, silently.

SQL Server's default collation and MySQL 8's default are case-insensitive; PostgreSQL's is not.
A unique index on `email` that enforced uniqueness case-insensitively now enforces it
case-sensitively — a strictly weaker constraint. `Bob@x.com` and `bob@x.com` become two
accounts, and the login flow that assumed they could not both exist starts behaving strangely.
Lookups with different capitalisation stop matching.

In the other direction it is at least loud: rows that were distinct become duplicates and the
load fails.

Find it before migrating:
`SELECT lower(email), count(*) FROM customers GROUP BY 1 HAVING count(*) > 1`.

Fix with `citext`, a non-deterministic ICU collation, or a normalised lowercase column with
the unique index on it. The third is ugly, explicit, works everywhere, and is what most large
systems end up with.

**Follow-ups.** What is the downside of a non-deterministic collation? (Pattern matching —
`LIKE` and regex — is not supported on that column, which breaks more queries than people
expect.) Is there a related hazard that has nothing to do with changing engines? (Yes — a glibc
upgrade changes collation ordering and silently invalidates every text index in place. That one
is worse, because both old and new code are affected.)

---

### 12. Logical replication is both the answer to zero-downtime migrations and a hazard. Explain both.

**The answer.** It is the answer because it decouples the two sides: the subscriber can be a
different major version, a different schema, even a different engine, which is what makes
near-zero-downtime upgrades and store migrations possible at all.

It is a hazard because **it does not replicate DDL**, and the ordering rules are opposite for
the two directions. Add a column on the subscriber first; drop one on the publisher first. The
underlying principle is that the subscriber must always be a superset of what the publisher
sends. Get it backwards and the subscriber stops with
`missing replicated column`, the slot retains WAL, and the primary's disk fills — at 950 GB a
day normally and 7.8 TB a day during a backfill.

And it does not replicate sequences, which is the classic cutover failure: thirty seconds after
switching traffic, every insert is a duplicate key, because `nextval` on the subscriber returns
1.

**Follow-ups.** How do you protect the primary's disk? (`max_slot_wal_keep_size`. Past it,
PostgreSQL invalidates the slot rather than filling the disk — you have chosen to lose a
subscriber instead of a primary, and the default is the other way round.) What else does it not
carry? (Large objects, `TRUNCATE` before PG 11, materialised view contents — that list is the
cutover checklist.)

---

### 13. Your company has eight change windows a year, four hours each, and 62 engineering teams. Design the process.

**The answer.** First, do the arithmetic, because it settles the argument. Eight windows × four
hours is 1,920 minutes. At four changes per team per year that is 248 changes, so **7.7 minutes
per change** including verification and rollback rehearsal. The model is not inconvenient; it is
arithmetically impossible.

The important consequence is behavioural: an impossible control produces reclassification, not
safety. Changes become "configuration", "data fixes" and "emergencies", and the actual risk
moves to the paths with the least review while the reporting shows a well-governed process.

The replacement is to classify by risk, automatically, from the statements and the table size:

- **Class 1, metadata-only and additive** — about 71%. Automated gates only: lint, size gate,
  compatibility job, schema-snapshot replay. Self-service, any time.
- **Class 2, scans or backfills, still reversible** — about 22%. Plus a named reviewer and a
  written plan with abort criteria. Business hours with monitoring.
- **Class 3, rewrites, drops, cutovers** — about 7%, which is 17 changes a year. Full review, a
  clone rehearsal with timings, and a window. **17 into 8 windows is 114 minutes each** instead
  of 7.7.

**Follow-ups.** What about separation of duties? (A pipeline satisfies it better than a DBA
typing a script: the applied artefact is the reviewed one, it is immutably auditable, it is
deterministic, and the reviewer has domain context. The structural requirement is that
developer credentials genuinely cannot alter the schema — three roles, one afternoon.) How do
you get the DBA team to agree? (Lead with what they do instead: own the policy, the lint rules,
the thresholds, class 3 review, and drift detection. It is a better job, and it is the version
of the proposal that gets accepted.)

---

### 14. How do you change a DynamoDB table's partition key?

**The answer.** You cannot. The partition key, sort key and local secondary indexes are fixed
at table creation, because the partition key determines physical placement.

So it is a full store-to-store migration with DynamoDB on both ends: new table with the new key
schema, DynamoDB Streams on the old table as the capture mechanism, start the stream consumer
*before* the scan, parallel-segment scan with conditional writes so a scan result never
overwrites a newer streamed item, verify, cut over, keep a reverse stream.

**Follow-ups.** What about adding a GSI instead? (Often the right answer, and it has its own
hazard: a throttled GSI throttles writes to the *base* table. Provision the index at 2–3× during
backfill and project only the attributes you need.) What design lesson does this teach? (The key
schema's immutability is why DynamoDB rewards designing around access patterns up front and
punishes discovering them later more harshly than any relational engine.)

---

### 15. A team says "we do not lose data, we dual-write to both stores." What is your response?

**The answer.** Two writes to two systems cannot be atomic, so every failure mode produces
divergence: the first succeeds and the second times out, the process dies between them, or a
later statement rolls back the first. And the divergence is not self-healing, because neither
store knows it happened.

Teams reach for it because it is simpler to *write* than CDC. It is substantially harder to
*operate*, because they have replaced an automatic, ordered, resumable stream with a hand-rolled
one that has no ordering guarantee and no replay.

The answer is one durable decision point: write to one store, derive the other from its change
stream. If the second write must be synchronous for read-your-writes reasons, use a
transactional outbox — the row and the outbox entry in one local transaction, with a publisher
draining it.

**Follow-ups.** When *is* dual-writing fine? (Two columns in the same table written by one
statement. That is atomic by construction and it is why same-table migrations are so much
easier than they look.) How do you detect divergence you already have? (Chunked checksums plus
a production shadow-read comparison — one covers the cold tail, the other weights by what users
actually read.)

---

### 16. A migration completed, verification passed, and six weeks later the data turned out to be wrong. How?

**The answer.** This is the shape to take most seriously, because it has no symptom. Several
distinct causes, and they are worth separating:

- **Verification shared the backfill's blind spot.** It iterated the same ranges from the same
  cursor, so any range the backfill skipped was never checked. Fix: enumerate from the table —
  `count(*) WHERE new_col IS NULL` is the cheapest check there is.
- **Verification re-applied the transformation under test.** It proved determinism, not
  correctness.
- **The normalisation function had its own bug** — `<>` instead of `IS DISTINCT FROM` skips
  every row where either side is null.
- **Nothing errored because wrong data is valid data.** A null city is not an error; a derived
  store built from it succeeds; a feature returns an empty list, which is a valid result. Every
  component behaved correctly on wrong data.

The structural defences are verification that could have failed, standing drift detection, and
treating "zero results" as a suspicious answer that must distinguish "no matches" from "no
data".

**Follow-ups.** Why six weeks? (Because the consumer was quarterly, or a human had to notice an
implausible number. Doc 05's compatibility window table — the maximum over all consumers — is
why the soak before contract should be 92 days, not 30.)

---

### 17. How do you test a migration?

**The answer.** The highest-value job is the one most teams do not have: **run the previous
release's test suite against the new schema.** Tests normally run one code version against one
schema version, and the most dangerous failure lives in the combination — which is exactly the
state your fleet is in during every rollout.

Beyond that, two tiers. Unconditional and nearly free: lint (which encodes the whole DDL cost
table as a build failure), replay from empty, replay against a nightly production *schema*
snapshot, up-down-up for the down migration, and a size gate keyed to real table sizes.

Paid for per migration, for anything that rewrites or backfills: a production-sized clone with
timings, `EXPLAIN` on production statistics, a WAL-bytes-per-row measurement, synthetic load,
a timed rollback rehearsal, and a gameday.

**On Aurora the clone is nearly free** — copy-on-write, minutes regardless of size, and you pay
only for diverged pages. If you are on Aurora and still rehearsing against a 400,000-row
staging database, that is a habit rather than a constraint.

**Follow-ups.** What does staging specifically fail to reproduce? (Scale, concurrent load,
long-running queries, mixed code versions, replicas, CDC, statistics, tuned configuration, and
nine years of accumulated data anomalies — the last being the hardest to fake and the usual
source of calendar-time surprises.) What is the single best gameday item? (Reverse a store
cutover on the clone. A reverse stream that has never carried traffic is a configuration file,
not a capability.)

---

### 18. Should your largest tables have foreign keys?

**The answer.** It is a genuine trade and the migration cost is the input usually left out.

**For:** application-level referential integrity is not enforcement. You will get orphans, and
you will find them during an incident.

**Against, at scale:** on MySQL, foreign keys and online schema change are close to
incompatible. `pt-online-schema-change` offers three bad options — rebuild every child table
(each a multi-hour migration you did not plan), `drop_swap` (a non-atomic window with no table),
or leave them broken. gh-ost simply refuses to run on tables with foreign keys, which is
arguably the better interface. And a foreign key on PostgreSQL takes `SHARE ROW EXCLUSIVE` on
*both* tables while validating, so adding one blocks writes to a table the migration was not
supposed to touch.

**My position:** keep them where the child table is small enough that a rebuild is tolerable,
drop them on the largest tables and replace them with a reconciliation job that runs
continuously and alerts on orphans. The reconciliation job is the part people skip, and without
it "enforced in the application" means "not enforced".

**Follow-ups.** How do you add one to a large table safely? (`ADD CONSTRAINT ... NOT VALID`,
then `VALIDATE CONSTRAINT`, which takes only `SHARE UPDATE EXCLUSIVE`.) What about cross-service
boundaries? (Impossible — the constraint cannot cross databases, which is one of four things you
lose when extracting a table from a shared database, along with the join, the transaction, and
the analyst's ad-hoc query.)

---

## What separates a strong answer from a complete one

Five patterns. Candidates who show them are reasoning from mechanism; candidates who do not are
reciting.

**1. They name the constraint, not the procedure.** Asked how to backfill 521 million rows, a
complete answer describes chunking, throttling and resumability. A strong answer asks what the
binding constraint is, and arrives at a Kafka topic in another team's system. The procedure is
the same in both answers; the second one gets the rate right.

**2. They distinguish the mechanical problem from the semantic one.** Almost every question
here has both. `SET NOT NULL` is a scan *and* a compatibility break. A rename is instant *and*
unsafe. Candidates who answer only the mechanical half have usually operated a database;
candidates who answer both have usually caused an incident.

**3. They say what the recommendation costs.** "Use expand/contract" is advice. "Use
expand/contract, which costs eight weeks of calendar time, a dual write path, double storage for
the window, and an `if` in the code you must remember to remove — and do not do it on a 200 MB
table" is engineering. The strongest signal in an interview is an unprompted statement of when
*not* to apply the thing being described.

**4. They treat the response as part of the incident.** Three of doc 19's ten cases were made
worse by the response. A strong answer to "a migration caused an outage" includes which
instinct is wrong — cancel the DDL not the blocker, roll forward not back, do not clear the
dirty flag first — because that is where the avoidable damage is.

**5. They look outside the database.** Six of the ten cases involved a Kafka topic, a mobile
app, a derived store, an OS library, a change policy, or an export job, and the database was
healthy in all six. A candidate whose answer stays inside the engine will get the easy half of
most of these questions right.

Next: [21-golden-defaults-and-checklists.md](21-golden-defaults-and-checklists.md).

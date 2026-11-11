# Case Studies

Ten incidents, each built from the mechanisms in the preceding documents. They are presented
as timelines because the sequence is usually the instructive part — in several of these, the
response made things worse, and that is the detail worth remembering.

Each case cites the scenario IDs it combines, so you can read the mechanism in detail where
it is derived.

---

## CS-1 · Riverbend — the three-millisecond statement that caused a 70-second outage

**Combines:** `L-01` lock queue jam · `C-01` SELECT * · `T-12` coupled deploy · `U-02`
rollback made it worse

**Setting.** Riverbend is launching in Canada. An engineer writes a migration renaming
`orders.amount_cents` to `amount_minor` and adding `currency`, and ships it in the same pull
request as the code change. The migration runs as an init container.

**Timeline.**

```
14:01:12  A Finance report starts: an aggregate over orders. Expected 90 s.
          It holds ACCESS SHARE on orders.
14:02:00  The migration Job requests ACCESS EXCLUSIVE. It queues behind Finance.
14:02:01  Every new query on orders queues behind the pending ACCESS EXCLUSIVE,
          including SELECTs that would not have conflicted with Finance.
14:02:09  PgBouncer's 480 server connections are exhausted. Requests that never
          touch orders now fail too.
14:02:40  Page: checkout error rate 100%.
14:02:42  Finance completes. The ALTER acquires its lock, runs in 3 ms, releases.
14:03:10  Queue drained. Checkout recovers. The new pods begin rolling out.
14:03:11  OLD pods start failing with 42703: column "amount_cents" does not exist.
          Roughly 50% of checkout requests fail.
14:05:30  The responder, seeing a 50% failure rate that started with a deploy,
          reverts the deploy.
14:05:50  Failure rate goes to 100%. All pods now run old code against the new schema.
14:09:10  Someone recognises the direction error. The deploy is re-applied.
14:13:40  Rollout completes. Recovered.
```

**Total: 70 seconds of full outage, 2 minutes of 50% failure, then 4 minutes of full outage
caused by the response.**

**Mechanism.** Three independent problems, each of which alone would have been survivable.
The lock queue jam (`L-01`) is caused not by the `ALTER` being slow — it was 3 milliseconds —
but by PostgreSQL's ordered lock queue converting a compatible workload into a serialised one.
The compatibility break is a rename, which has no safe intermediate state (`C-01`). And the
rollback is doc 13's top-right cell: old code, new schema (`U-02`).

**What made it worse.** The revert. The responder applied the correct instinct for a code
incident to a schema incident, and nothing in the runbook or the dashboard told them the
direction.

**What they changed.**

1. `lock_timeout = '2s'` with a jittered retry loop in the migration runner. Not optional, not
   per-migration — in the tool.
2. Migrations moved out of the init container into an explicit pipeline step with its own
   status, orderable before or after the deploy (`T-12`).
3. A lint rule banning `RENAME`.
4. The runbook for any migration-adjacent incident now states the direction check first, in
   bold: *if old pods are failing, roll forward.*
5. Deploy and migration annotations on every dashboard (doc 16). This change alone would have
   cut the incident by several minutes, because the responder's first graph would have shown
   both events.

---

## CS-2 · Riverbend — the currency migration, and the backfill that took down the search index

**Combines:** `B-04` backfill floods CDC · `W-01` the missed write path · `V-02` self-defeating
verification

**Setting.** The same change as CS-1, done properly the second time. Six phases, eight weeks
planned.

**Timeline.**

```
Week 1    Phase 1: ADD COLUMN amount, currency. Both nullable, metadata-only, 8 ms.
Week 1    Phase 2: dual write deployed. The gap trigger from W-01 is installed.
Week 2    migration_gaps has 41 rows, all from payment-service's refund path.
          Nobody had thought of it; it lives in a different repository.
          Fixed, redeployed, clock reset.
Week 4    migration_gaps clean for 14 days. Phase 3 begins.
Week 4    The backfill starts, unthrottled, estimated 8 hours.
          02:11  Backfill starts at 18,000 rows/s.
          02:26  Search indexer consumer lag passes 10 minutes.
          02:51  Lakehouse writer lag passes 40 minutes.
          03:14  orders.created topic begins deleting segments to stay inside
                 its retention bound. The search indexer's offset is deleted.
          03:20  Page: search results stale by an hour.
          03:24  Backfill paused.
          06:00  Search index re-snapshotted from the source of truth. 4 hours
                 of search degradation.
Week 5    Backfill restarted at 2,300 rows/s - 10x the normal CDC rate rather
          than 79x. Runs 63 hours over 4 days with two pauses.
Week 6    Phase 4: verification. The first attempt compares `amount` against
          `amount_cents / 100.0` and reports zero defects. Someone notices this
          proves nothing (V-02). Replaced with a comparison against the
          storefront's declared currency plus a Finance reconciliation.
          The new verification finds 4,100 Canadian orders backfilled as 'USD'.
Week 7    Repair. Phase 5 read switch, ramped on hash(order_id).
Week 8    Phase 6a: stop writing amount_cents. The point of no return.
Week 20   Phase 6b: drop amount_cents, after the quarterly export ran once.
```

**Mechanism.** The backfill incident is `B-04` exactly: 521 million change events emitted at
79× the normal rate into a Kafka topic sized for 65 GB of retention, receiving 573 GB. The
database was never the constraint — it ran at 12% of its WAL capacity throughout — and no
database metric showed a problem.

**The most instructive part** is the verification. The first version was written by the same
engineer who wrote the backfill, using the same expression, and it passed. The data was wrong
for 4,100 orders, which is 0.0008% — a rate that sampling at the 99%/1-in-100,000 level would
have had roughly even odds of catching, and that the correct invariant check found in one
query.

**What they changed.**

1. The pre-flight checklist now requires enumerating everything that observes writes to the
   table, with the slowest one setting the rate (`B-04`).
2. A rule: **the verification must not use the backfill's expression.** Reviewed as a specific
   checklist item.
3. The CDC topic got a retention alert based on *headroom in hours*, not bytes.
4. The gap trigger (`W-01`) became a standard part of every phase-2 deploy, because it found
   the refund path in two days rather than at phase 5.

---

## CS-3 · Ironvale — the empty string that silently became NULL for 2.1 million customers

**Combines:** `M-03` Oracle empty string · `M-04` collation · `V-02` verification blind spot

**Setting.** `CUSTOMER_PREFS` — 240 million rows, 180 GB — moves from Oracle 19c to
PostgreSQL 15 as part of the Oracle exit programme. AWS DMS for the full load plus CDC.
Row counts matched exactly. Checksums on numeric columns matched exactly. Cut over on a
Saturday.

**Timeline.**

```
Sat 03:40  Cutover completes. 40 seconds of write freeze. Verification green.
Mon 09:15  Support tickets: customers report their communication preferences
           have been reset.
Mon 11:00  Investigation finds 2.1 million rows where `nickname` and three other
           optional text fields are NULL in PostgreSQL. In Oracle they "were"
           empty strings.
Mon 14:00  Realisation: in Oracle they were ALSO NULL. Oracle stores '' as NULL.
           The data did not change. The APPLICATION changed, because the new
           PostgreSQL-backed code distinguishes NULL from '' and the old
           Oracle-backed code could not.
Mon 16:30  A second finding: 340 accounts now exist in duplicate pairs differing
           only by the case of the email address.
```

**Mechanism.** Two independent semantic mismatches that no row count or numeric checksum could
catch.

`M-03`: Oracle treats `''` and `NULL` as the same value for character types. The migration
preserved this faithfully — every `''` the application believed it wrote was already `NULL` in
Oracle, and arrived as `NULL` in PostgreSQL. **The data was migrated correctly and the
behaviour changed**, because a distinction that did not exist in the source exists in the
target, and application code written against PostgreSQL started using it.

`M-04`: Oracle's unique index on `LOWER(email)` was... not what they had. They had a unique
index on `email`, relying on an application-level lowercase convention that had been broken by
a bulk import in 2019. Oracle's binary collation had permitted 340 pairs differing by case. The
PostgreSQL index permitted them too — so this was not caused by the migration. The migration
*revealed* it, because the new onboarding code checked for existing accounts case-insensitively
and surfaced the conflict.

**What made it worse.** The verification plan tested the things that were easy to test —
counts, numeric sums — and nothing that tested *semantics*. There was no step that asked "what
does this value mean in each engine?"

**What they changed.**

1. A **semantic mapping document**, required for every store migration, with one row per column
   stating the source type, the target type, and the specific semantic hazards from doc 09's
   list: empty string, date-with-time, number precision, collation, length semantics, identifier
   case.
2. Verification now includes a **behavioural** check: run the application's own read path
   against both stores and compare the rendered output, not the stored values. This would have
   caught it in the tail phase, weeks early.
3. The duplicate accounts were a pre-existing defect, and the lesson recorded was that
   **a migration surfaces latent data-quality problems**, so the plan should budget time for
   fixing things it did not cause.

---

## CS-4 · Ironvale — 03:50, seventy per cent done, ten minutes of window left

**Combines:** `L-03` unknown rewrite · `T-05` half-applied · `U-01` no retained artefact

**Setting.** A quarterly compliance change requires widening a column on `TXN_LOG` — 2.4
billion rows — from `NUMBER(12,2)` to `NUMBER(16,2)`. Tested in a pre-production environment
with 8 million rows, where it took 26 seconds. The change record states "estimated 5 minutes".

**Timeline.**

```
Sat 02:00  Window opens. Three other changes run first, taking 70 minutes.
Sat 03:12  The DBA starts the ALTER. It is a rewrite - Oracle widens NUMBER
           precision in place only in specific cases, and this was not one.
Sat 03:50  70% complete by the rough progress estimate. 10 minutes of window
           remain. The change policy requires all changes complete by 04:00.
Sat 03:52  Decision: cancel. The DBA issues a cancel.
Sat 03:52  Oracle begins rolling back. The rollback replays undo.
Sat 05:40  Rollback completes. 108 minutes, during which the table was degraded
           and the rollback could not be cancelled.
Sat 05:41  The schema is back at its original state. Nothing was achieved.
Mon        The change is resubmitted. Next available window: 5 weeks away.
           The compliance deadline is in 3 weeks.
```

**Mechanism.** `L-03`: the rewrite was not identified as a rewrite, because the pre-production
test was 300 times smaller and finished before anyone thought about it. Then the undo-based
rollback (doc 06) took longer than the work, which is the property that turns "we ran out of
time" into "we ran out of time and then lost two more hours".

**What made it worse.** The window itself. The decision at 03:52 was forced by a policy
deadline, not by a technical one. Had the operation been allowed to continue for another 40
minutes, it would have completed. The control designed to limit risk caused the loss.

**What they changed.** This incident is the one that produced doc 18's argument, and the
changes were organisational rather than technical:

1. **The arithmetic was computed** — 7.7 minutes per change — and presented to the risk
   function with this incident as the illustration.
2. **Risk classification** was adopted. 71% of changes moved to a self-service path with
   automated gates, freeing the window for the 7% that need it.
3. **Every class 3 change now requires a timed rehearsal on a production-sized clone.** The
   26-second pre-production figure would have been replaced by a real one.
4. A rule with teeth: **a change may not start unless its measured duration plus 100% margin
   fits in the remaining window.** At 03:12 with 48 minutes left, a change measured at 55
   minutes would not have been allowed to start, and the loss would have been zero rather than
   two hours.

---

## CS-5 · Lumen — twelve shards out of 4,096, found five months later

**Combines:** `O-10` partial rollout · `S-06` schema drift · `F-01` the 99.7% dashboard

**Setting.** A `tinyint` column added to `post_engagement` across 4,096 MySQL shards, via
Vitess online DDL, in 64 waves over several days.

**Timeline.**

```
March      Rollout runs. Dashboard reaches "4,084 / 4,096 - 99.7%". Declared done.
           Twelve shards: three were in a maintenance state, two had their
           migration cancelled by an operator during an unrelated incident,
           and seven were restored from backups taken before the change as part
           of a storage incident the following week.
March-Aug  The application reads the column with a null-safe default, so nothing
           fails. The feature that needed it has not shipped yet.
August     The feature ships. It writes the column.
           0.29% of writes fail with Unknown column 'reaction_type'.
August     Four hours to diagnose, because the schema was "known" to be correct -
           the rollout had been declared complete five months earlier and nobody
           suspected it.
```

**Mechanism.** `F-01`. The rollout reported an aggregate, and 12 out of 4,096 is 0.29%, which
every chart rounded to complete. The seven backup restores are the genuinely interesting part:
they happened **after** the rollout finished, so no rollout-time control could have caught
them. Only standing drift detection could.

**What made it worse.** The five-month gap between the drift appearing and anything failing.
By August, the people who ran the rollout had moved on, the runbook was stale, and the first
hypothesis was a code bug.

**What they changed.**

1. **"Complete" redefined** as `count(targets not in desired state) = 0`. The dashboard's
   primary number is the count of stragglers with their names, not a percentage.
2. **Standing drift detection**, running every 30 minutes forever: one schema fingerprint hash
   per shard, compared against the mode, alerting on any difference. About 200 lines. It found
   31 further drifted shards in its first week, from causes going back two years.
3. **The restore runbook** gained a mandatory step: replay any migrations applied after the
   backup's timestamp. This was the actual root cause of seven of the twelve and it had nothing
   to do with the migration process.

---

## CS-6 · Gateline — the freeze that delayed a security fix by six weeks

**Combines:** doc 18's calendar · `P-04` migration during an incident

**Setting.** Gateline's traffic is 5,000 QPS baseline and 500,000 QPS during an on-sale.
On-sales are announced months ahead and are contractually committed. The engineering change
freeze runs from 48 hours before each on-sale to 24 hours after, and with on-sales roughly
weekly in the autumn season there are few gaps.

**Timeline.**

```
Week 1   A security review finds that a session token is stored in a column that
         is included in an analytics export. The fix requires a schema change:
         a new column, a backfill to re-encrypt, and dropping the old column.
Week 1   The change is classified as "schema change" and therefore subject to the
         freeze calendar. The next non-freeze window is 11 days away.
Week 3   The window arrives. The on-sale schedule has changed; a stadium tour
         announcement was added. Freeze extended.
Week 5   An unrelated incident consumes the window.
Week 7   The change finally runs. Six weeks after a security finding.
Week 7   Separately: during week 4, a team needing a small additive column
         reclassified it as "configuration" to avoid the freeze and shipped it.
         Nobody reviewed it.
```

**Mechanism.** The freeze is correct in intent — a schema change during a 500,000 QPS on-sale
would be reckless — and wrong in scope. It forbade *all* migration activity, including:

- A phase-1 additive column, which is metadata-only, reversible, and invisible to running code.
- Sitting in phase 2 with a dual write running, which is a steady state, not a change.
- A security fix whose risk profile was lower than the risk of not shipping it.

**What made it worse.** The reclassification in week 4. When the control is too broad, teams
find a label that escapes it, and the work that escapes is the work nobody reviews.

**What they changed.**

1. **The freeze forbids crossing a point of no return, not existing in an intermediate phase.**
   A dual write running through a freeze is a steady state and is explicitly permitted.
2. **Freeze scope is set by risk class, not by the word "schema".** Class 1 changes are
   permitted outside the on-sale window itself; class 3 changes are not.
3. **A documented expedite path for security findings**, with a named approver, so the answer
   is "approved in two hours with extra monitoring" rather than "six weeks".
4. **The migration calendar is checked in phase 0 against the full timeline**, so a migration
   whose contract phase would land in a freeze is started earlier.

---

## CS-7 · Waypoint — the field removed for ninety thousand drivers on an old app

**Combines:** `C-10` clients you cannot upgrade · `C-01` proving a field is unused

**Setting.** Waypoint's trip record carries a `surge_multiplier_legacy` field, superseded in
2024. An engineer confirms the current app does not read it, greps the server code, finds
nothing, and removes it from both the API response and the database.

**Timeline.**

```
Day 1   Migration and API change ship together.
Day 1   App versions 7.x and 8.x: fine.
Day 1   App version 6.4, released 2024, crashes on the trip-detail screen.
        Its JSON decoding is strict and the field is non-optional in its model.
Day 1   90,000 drivers - 1.8% of active drivers - cannot see trip details.
        They can still drive; they cannot see what they earned.
Day 1   Support volume spikes. The pattern is invisible in aggregate error
        monitoring because the crash is client-side.
Day 2   Diagnosed from crash reports grouped by app version.
Day 2   The field is restored to the API response as a constant. The database
        column is gone, so the value is synthesised as 1.0.
```

**Mechanism.** `C-10`. The two-versions window for a service is four minutes. For a mobile
client it is however long users take to upgrade, and Waypoint has 14 app versions in the field
with the oldest two years old. The grep covered the server; it could not cover an app binary
shipped in 2024.

**What made it worse.** The field was restored as a **constant** rather than real data,
because the column had been dropped. Drivers on 6.4 saw a plausible-looking but wrong surge
multiplier for a week until a forced upgrade was pushed. The migration's irreversibility
turned a display bug into a correctness one.

**What they changed.**

1. **Server-side field-level access instrumentation.** Every field in every API response is
   counted by app version. Removal requires a measured zero across all supported versions for
   90 days — an affirmative signal, not an absence of evidence.
2. **A published supported-version floor**, moving every six months, with a forced upgrade
   below it. This is the only mechanism that bounds the window by policy rather than by user
   behaviour, and it costs the users who cannot upgrade.
3. **The database column is dropped only after the API field has been gone for a full cycle**,
   so the recovery path — restore the field from real data — stays available.

---

## CS-8 · Corridor — the migration that succeeded and was wrong for six weeks

**Combines:** doc 00's shape 4 · `V-04` verifying only what you migrated · `B-10` sparse keys

**Setting.** Corridor maintains versioned derived stores: a member-profile index rebuilt
nightly from a source table. A migration splits `location` into `city`, `region` and `country`.

**Timeline.**

```
Week 1   Six phases executed correctly. Backfill of 1.1 billion rows.
         Verification compares the migrated rows and passes.
Week 1   The derived store's build job is updated to read the new columns.
Week 2-7 No errors. No alerts. Dashboards green.
Week 7   A product manager notices that the "members near you" feature returns
         nothing for several European markets. Investigation finds 41 million
         profiles with NULL city and region.
Week 7   Root cause: the backfill chunked on member_id ranges computed from
         min and max. A 2017 data import had created a block of 41 million
         member_ids in a range above the then-maximum, later filled in by
         normal growth. The chunk loop's max was captured at the start and the
         block above it was never visited.
         Verification iterated the same ranges from the same cursor table,
         so it never looked at them either.
```

**Mechanism.** `V-04` exactly: verification inherited the backfill's blind spot because it
shared its source of iteration. And `B-10`: the key space was not what the loop assumed.

The reason nothing alerted is doc 00's shape 4. A `NULL` city is not an error. The derived
store built successfully. The feature returned an empty list, which is a valid result. **Every
component behaved correctly on wrong data.**

**What made it worse.** Six weeks of the derived store being rebuilt nightly from wrong source
data meant the error propagated into the search index, two analytics datasets, and a quarterly
report that had already been circulated.

**What they changed.**

1. **Verification enumerates from the table, never from the backfill's cursor.** The single
   cheapest check — `SELECT count(*) FROM members WHERE city IS NULL AND location IS NOT NULL`
   — would have returned 41 million in under a second.
2. **A completeness invariant on derived stores**: the build job asserts that the count of
   non-null key fields is within a tolerance of the previous build, and fails rather than
   publishing. A 41-million-row change in a single night is not a plausible organic shift.
3. **"Zero is a suspicious answer."** Features that return empty results now emit a metric
   distinguishing "no matches" from "no data", which is the same discipline as
   [`../LogCollector`](../LogCollector/README.md)'s rule that degradation must be visible.

---

## CS-9 · Northlight — one node on a different schema, 2,500 services affected

**Combines:** `N-04` Cassandra schema disagreement

**Setting.** Northlight's service-metadata store is a Cassandra cluster backing the control
plane for 2,500 services and 100,000 mesh sidecars. A column is added to a configuration
table.

**Timeline.**

```
10:02  ALTER TABLE ... ADD applied from a cqlsh session against node 10.0.2.7.
10:02  Gossip propagates. 46 of 47 nodes converge within 4 seconds.
10:02  Node 10.0.1.18 is under GC pressure and does not apply the change.
10:05  The control plane begins writing the new column. Writes coordinated
       through 10.0.1.18 fail with Undefined column name.
10:05  Roughly 1 in 47 control-plane writes fail. Retries mostly succeed,
       because a retry usually picks a different coordinator.
10:11  Sidecar configuration updates begin failing intermittently across the
       fleet. No single service is fully broken; everything is slightly broken.
10:24  nodetool describecluster shows two schema versions.
10:26  10.0.1.18 restarted. Schema converges. Recovered.
```

**Mechanism.** `N-04`. Cassandra's schema is eventually consistent, propagated by gossip, and
a node under pressure can miss a change. The 1-in-47 failure rate with mostly-successful
retries is the signature: it is too low to trip a threshold alert and too high to ignore, and
the retry behaviour makes it look like transient flakiness rather than a schema problem.

**What made it worse.** The symptom distribution. Because every service was slightly affected
and none was fully broken, the first forty minutes were spent looking for a network problem.

**What they changed.**

1. **Schema version count as a monitored metric**, with an alert on anything other than 1.
   This is a `nodetool describecluster` parse on a timer and it converts a forty-minute
   diagnosis into an alert.
2. **A post-DDL convergence gate**: the migration tool applies the change, then polls until all
   nodes report one schema version, and fails the migration if they do not within a timeout.
   Crucially, it does this **before** the application is allowed to use the new column.
3. **Schema changes from one designated coordinator**, serialised, never concurrently.

---

## CS-10 · Riverbend — the operating-system upgrade that silently broke every text index

**Combines:** `C-11` collation change

**Setting.** A routine Aurora PostgreSQL minor-version upgrade, which moved the underlying
operating system image and with it the version of the C library providing collation.

**Timeline.**

```
Day 0   Upgrade applied in a 90-second failover window. No errors. Normal
        monitoring green.
Day 3   A support ticket: a customer cannot log in. Their email is in the
        database; the login query returns no rows.
Day 5   Three more tickets. Pattern: all the affected emails contain a
        non-ASCII character or a hyphen.
Day 6   A new customer registration succeeds, creating a SECOND account with
        an email address that already exists. The unique index did not prevent
        it, because the index lookup did not find the existing row.
Day 8   Diagnosed. A sequential scan and an index scan for the same value
        return different results.
Day 8   REINDEX INDEX CONCURRENTLY on all text indexes. 6 hours.
Day 8   Reconciliation of duplicate accounts created during the 8-day window:
        1,106 of them, each requiring a merge decision.
```

**Mechanism.** `C-11`. The OS upgrade changed the collation ordering for a subset of strings.
A B-tree index on a text column is physically sorted by that ordering, and a binary search on
a wrongly sorted index silently misses entries. No error is produced at any point: the index is
structurally valid, it is just sorted according to rules that no longer apply.

The duplicate-account case is the worst form: `INSERT` checked the unique index, did not find
the existing row, and permitted the insert. **A unique constraint silently stopped being a
constraint.**

**What made it worse.** Nobody classified an engine upgrade as a migration. It went through
the infrastructure change process, not the schema change process, and nothing in that process
mentions indexes.

**What they changed.**

1. **Engine and OS upgrades are now class 3 changes** under doc 18's classification, with a
   mandatory post-upgrade `REINDEX` of all text indexes, in the same change window.
2. **Identifier columns moved to `COLLATE "C"`** — email, external IDs, idempotency keys,
   anything compared for equality rather than sorted for humans. `C` collation is defined by
   byte order and never changes across any OS or library version. This was a migration in
   itself, and it removes the entire class permanently for those columns.
3. **`amcheck` added to the post-upgrade verification**:
   `SELECT bt_index_check(index => c.oid, heapallindexed => true) FROM pg_class c WHERE
   c.relkind = 'i'` — which would have found it on day 0 rather than day 8.
4. **On PostgreSQL 15+**, `datcollversion` mismatch is monitored, which makes the whole thing
   an alert rather than an investigation.

---

## What these ten have in common

Reading them together, five patterns repeat:

1. **The response made it worse in three of ten.** CS-1's revert, CS-4's cancellation, CS-7's
   constant-value restoration. In each case the responder applied an instinct that is correct
   in a different context. **The runbook's job is to override the instinct**, which means it
   has to say the direction explicitly.

2. **Four of ten were silent for weeks.** CS-5 (five months), CS-8 (six weeks), CS-10 (eight
   days), CS-3 (two days). None produced an error. All were found by a human noticing something
   implausible. The only systematic defence is verification that could have failed (doc 12) and
   standing drift detection (doc 11).

3. **Six of ten involved something outside the database.** A Kafka topic, a mobile app, a
   derived store, an operating-system library, a change-management policy, an export job.
   **The database was healthy in every one of them.**

4. **Three were caused by a control rather than prevented by one.** CS-4's window forced the
   cancellation; CS-6's freeze delayed a security fix and drove a reclassification. A control
   that cannot be satisfied produces the behaviour it was meant to prevent.

5. **Every one of them changed the process afterwards, and most of the changes were cheap.**
   `lock_timeout` in the runner. A count instead of a percentage. A `nodetool` parse on a
   timer. Verification that enumerates from the table. None of these are large engineering
   projects, and all of them were obvious in hindsight — which is the argument for reading
   other people's incidents rather than generating your own.

Next: [20-staff-interview-questions.md](20-staff-interview-questions.md).

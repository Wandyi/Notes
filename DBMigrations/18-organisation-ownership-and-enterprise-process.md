# Organisation, Ownership, and Enterprise Process

Everything before this document was engineering. At Riverbend, engineering is the constraint:
a migration takes as long as the backfill takes. At Ironvale it is not. A migration there takes
**63 days at the median**, of which perhaps 14 hours is work, and no amount of better tooling
moves that number.

This document is about the other 62 days. It is the part most engineering writing skips, and
at enterprise scale it is the part that determines whether migrations happen at all.

---

## The arithmetic that shows the window model cannot work

Doc 01 established Ironvale's constraints. Restated, because everything here follows from it:

- **8 usable change windows per year** — twelve monthly Saturday 02:00–06:00 slots, minus a
  year-end freeze and two quarter-end regulatory reporting freezes.
- **4 hours each.** Total annual change capacity: **32 hours.**
- **62 teams.** At a conservative four schema changes per team per year: **248 changes.**

> 8 windows × 4 hours × 60 = 1,920 minutes
> 1,920 ÷ 248 = **7.7 minutes per change**

Seven minutes and forty seconds, including execution, verification, and the rollback rehearsal
the change policy requires. For comparison, doc 02's `CREATE INDEX CONCURRENTLY` on a
521-million-row table is 41 minutes on its own.

**The window model is not inconvenient. It is arithmetically impossible**, and it has been for
years without anyone computing it, because each team experiences only their own four changes
and sees a queue rather than a contradiction.

### What an organisation does with an impossible control

It does not stop changing. The business still needs features, and the regulator's actual
requirement — that changes are controlled, reviewed and auditable — is not the same as the
window. So the organisation routes around it, and the routes are predictable:

| Route | How it is justified | What it actually means |
|---|---|---|
| **"This is configuration, not schema"** | A new row in a lookup table; a new enum value; a feature flag | Real schema changes reclassified by argument |
| **"This is a data fix"** | An `UPDATE` run by a DBA under an incident ticket | Unreviewed DML on production, which is how doc 04's `T-07` drift arrives |
| **Emergency change** | Abbreviated approval, same-day | A path with *less* review than the normal one, used for routine work |
| **Doing it in the application** | Store the new field in an existing `jsonb` or `text` column | Doc 05's `C-13`, adopted for process reasons rather than technical ones |
| **Not doing it** | The feature is descoped | The most expensive route, and invisible |

**The control produces documentation rather than safety.** Every change that matters has
travelled a path with less scrutiny than the one the control governs, and the organisation's
actual risk has moved to those paths while its reporting shows a well-governed change process.

This is the argument to make to a risk function, and it is a strong one, because it is framed
in their terms: the control is not achieving its objective, and here is the arithmetic.

---

## What to replace it with: classify by risk, not by type

The window model treats every schema change as equally risky. They are not, and the
distribution is heavily skewed.

Classify Ironvale's 248 annual changes by the properties this collection has established as
determining risk — does it rewrite, does it scan, is it compatible with running code, is it
reversible:

| Class | Definition | Share | Count |
|---|---|---|---|
| **1 — Routine** | Metadata-only, additive, reversible, compatible with running code. `ADD COLUMN` nullable, `ADD TABLE`, `ADD INDEX CONCURRENTLY`, `ADD CONSTRAINT NOT VALID` | 71% | 176 |
| **2 — Elevated** | Scans or backfills, still reversible, still compatible. `VALIDATE CONSTRAINT`, a backfill, a new index on a large table | 22% | 55 |
| **3 — High** | Rewrites, narrows, drops, crosses the point of no return, or moves a store | 7% | **17** |

Now redo the arithmetic with only class 3 in the window:

> 17 changes ÷ 8 windows = **2.1 per window**
> 240 minutes ÷ 2.1 = **114 minutes per change**

**The same window model, applied to the right 7%, gives you nearly two hours per change
instead of seven minutes.** That is a process that can actually do what it claims: a real
rollback rehearsal, a real verification, a DBA watching.

And classes 1 and 2 need somewhere to go, which is the other half of the proposal:

| Class | Gate | Who approves | When it runs |
|---|---|---|---|
| **1** | Automated: lint, size gate, compatibility job, schema-snapshot replay (doc 17) | The pipeline. No human | Any time, self-service |
| **2** | Class 1 gates plus a named reviewer plus a written plan with abort criteria | One peer reviewer plus the team's own DBA liaison | Business hours, with monitoring; not in a window |
| **3** | Class 2 gates plus a clone rehearsal with timings, a rollback rehearsal, and a go/no-go | Change advisory board | A window |

**The classification must be automatic, not self-declared.** A linter can determine class from
the statements plus the table size — that is exactly what doc 17's lint rules and size gate
compute. Self-declaration turns the classification into the next thing to argue about, which
is where the current system already is.

---

## Separation of duties without a human in the middle

Ironvale's regulator requires that the person who authors a change is not the person who
applies it to production. This is a genuine control with a genuine purpose: it prevents one
individual from unilaterally altering production data.

It is currently implemented as "a DBA types the script." That implementation has three
problems:

1. **It is the bottleneck.** 2,400 databases, one DBA team, 32 hours of window per year.
2. **It is weak.** The DBA is not reviewing the change's correctness — they lack the domain
   context to do so — they are transcribing it. The control catches typos and nothing else.
3. **It is bypassed under pressure**, which is exactly when unilateral production changes are
   most dangerous.

**A pipeline satisfies the same control better**, and the argument is worth making carefully
because it is the one that unblocks everything else:

| Requirement | DBA types it | Pipeline applies it |
|---|---|---|
| Author ≠ applier | ✓ | ✓ — the author cannot run DDL; only the pipeline's credential can (doc 04's `T-10`) |
| The applied artefact is the reviewed one | ✗ — transcription, copy-paste, last-minute edits | ✓ — the commit hash is the artefact |
| Auditable | A ticket and a terminal session | ✓ — commit, reviewer, pipeline run, timestamps, output, all immutable |
| Repeatable and deterministic | ✗ | ✓ — the same artefact applied the same way to every environment |
| Reviewable by someone with domain context | ✗ — the DBA lacks it | ✓ — the reviewer is a named engineer who understands the table |
| Enforces the standards | ✗ — relies on the DBA's memory | ✓ — lint, size gate, compatibility job, all mandatory |

**The structural requirement is that the developer's credentials genuinely cannot alter the
schema.** Doc 04's three-role setup — `owner` reachable only from the pipeline, `app` with no
`CREATE`, `ro` for everyone else — is what makes this a control rather than a convention, and
it is an afternoon of work.

**What the DBA team does instead**, and this is the part to lead with when proposing it,
because it is a better job: they own the policy the pipeline enforces, the lint rules, the
classification thresholds, the golden configuration, the review of class 3 changes, and the
standing drift detection. They stop being typists and become the people who decide what safe
means. At Ironvale this was the change that made the proposal acceptable to the DBA team, who
had been the loudest defenders of the window model and were, reasonably, defending their role
rather than the control.

---

## Ownership: who owns `ACCT_MASTER`?

Nobody. It was created in 2007, the team that built it was reorganised four times, and 61
applications read it.

That is the actual state of the most important table in a large enterprise, and it causes
specific, diagnosable problems:

- **Nobody can approve a change**, so changes are approved by whoever is loudest or by nobody.
- **Nobody can enumerate the consumers**, so doc 00's coupling 3 is unknowable and the
  quarterly export breaks (doc 05's `C-14`).
- **Nobody is accountable for the schema's quality**, so it accretes. 340 columns, of which the
  migration history accounts for 297.

### The two registries

**A table ownership registry.** One row per table: the owning team, a named individual, and a
review date. It can be a file in a repository; the mechanism does not matter, and its existence
does.

The hard part is not building it; it is filling it in, because for a long tail of tables the
honest answer is "nobody". **The useful move is to make "unowned" a visible, countable state
rather than an absence of data**, and to require that any migration touching an unowned table
first assigns an owner. That turns the registry from a documentation project — which never
finishes — into a thing that fills itself in as work flows through it.

**A consumer registry.** One row per (table, consumer) pair: what reads it, who owns that, how
often, and how it would be affected by a change. For `orders`, doc 01's list of seven.

Populate it from evidence rather than memory:

```sql
-- Who connects, and what do they touch? Sample pg_stat_activity into a table
-- every 5 seconds for two weeks, then:
SELECT application_name, usename, count(*) AS samples
FROM activity_samples WHERE query ILIKE '%orders%'
GROUP BY 1,2 ORDER BY 3 DESC;
```

Two weeks of sampling finds everything daily and weekly. It does not find the quarterly export,
and nothing automatic will, which is why the registry needs a human pass against the list of
things that run on a schedule — the cron inventory, the scheduler, the reporting tool's saved
jobs.

**The registry's value is realised at one moment**: when a migration plan's phase 0 requires
signing off each consumer. Without it, phase 0 produces a list of the services in your own
repository.

---

## The migration review: what a reviewer can actually catch

A reviewer reading four lines of SQL cannot assess risk. A reviewer reading doc 04's annotated
header can. The checklist below is what to ask, ordered so that the cheap disqualifying
questions come first:

```
MIGRATION REVIEW

  Mechanics
  [ ] Is lock_timeout set, with a retry loop?
  [ ] Is this metadata-only on OUR engine version? (not on the latest)
  [ ] If it scans, is it split into NOT VALID plus VALIDATE?
  [ ] If it creates an index, is it CONCURRENTLY, in its own file?
  [ ] One DDL statement per file? (MySQL, Oracle)

  Compatibility
  [ ] Is the resulting schema compatible with the CURRENTLY RUNNING code?
  [ ] Is it compatible with the code we would roll back to?
  [ ] Does the pipeline run this before or after the deploy, and is that right
      for the direction (expand before, contract after)?
  [ ] Any SELECT * on this table? (pg_stat_statements, not grep)

  Scale
  [ ] Table size and row count stated in the header, from production?
  [ ] Measured duration, from a clone, not an estimate?
  [ ] If it backfills: what is the throttle signal and why that one?
  [ ] What observes writes to this table - triggers, CDC, updated_at syncs,
      outbox? Which is the slowest, and does the rate respect it?

  Consumers
  [ ] Consumer registry checked. Who signed off?
  [ ] Does a logical subscriber need this DDL, and in which order?
  [ ] Does the event schema or warehouse model change too?

  Reversal
  [ ] Which category: reversible, recoverable, irreversible?
  [ ] Is the point of no return named, with a person and a measured cost?
  [ ] Has the rollback been run, on a clone, with a timing?

  Operations
  [ ] Abort criteria as numbers, in three bands?
  [ ] Can on-call pause this with one SQL statement, and is it in the runbook?
  [ ] Registered in the migration registry? Any other migration on this table?
```

**Roughly two thirds of that checklist is answerable by automation** — the mechanics block
entirely, most of scale, and the registry checks. Automate those and the human review is the
compatibility and consumer sections, which is where a human is actually useful. A checklist
where a human verifies things a linter could is a checklist that gets rubber-stamped.

---

## Compliance concerns that migrations create

### A migration makes new copies of personal data

A right-to-erasure request must reach every copy. A migration creates copies in places the
erasure process does not know about:

| Copy | Lifetime | Does erasure reach it? |
|---|---|---|
| The new column or table | Permanent | Yes, if the erasure process is updated — **which is a step in the migration plan** |
| The old column, retained for the rollback window | 92 days | Usually not. It is deleted, which satisfies the requirement eventually |
| CDC change events in Kafka | Topic retention (72 h at Riverbend) | Rarely. 573 GB of change events from the backfill (doc 06) contains every value |
| The rehearsal clone | Days to weeks | **Almost never.** This is the most commonly missed one |
| Backups and PITR | 35 days typically | Normally documented as out of scope within the retention schedule |
| The target store, mid-cutover | Permanent | Yes, once cutover completes; ambiguous during |

**Two practical requirements follow:**

1. **Updating the erasure process is a step in the migration plan**, with a checkbox, in the
   same phase as the schema change. Adding a column that holds personal data without updating
   erasure is a defect, not an omission.
2. **The rehearsal clone must be in the compliance inventory**, with a stated lifetime and an
   automatic deletion. A clone created for a migration rehearsal and forgotten is a full copy
   of production personal data with no owner, and it is the single most common finding when an
   audit looks at migration practice.

**Crypto-shredding** is the technique that makes this tractable at scale: encrypt
per-subject data with a per-subject key, and erasure becomes deleting one key, which reaches
every copy including backups. It is a significant design commitment and it is the only approach
that genuinely scales to the copy list above.

### Data residency

A migration that moves data between regions — including a clone for rehearsal, and including
a CDC topic replicated for disaster recovery — can breach residency requirements. Ironvale's EU
customer data may not leave the EU, which means the EU rehearsal clone must be in an EU
region, and the migration tooling's own logs must not carry row values.

**The practical rule:** a migration plan touching regulated data needs a line stating where
every copy will physically live, including the temporary ones. It is one line and it is the
one an auditor will ask about.

### Audit evidence

The evidence a regulated change process needs is mostly a by-product of a good pipeline:
the commit, the reviewer, the approval, the pipeline run ID, the exact statements executed, the
timestamps, the output, and the verification result.

**The gap is usually the verification result**, because it lives in a dashboard rather than in
an artefact. Write doc 12's verification report into the change record as a file. It is the
only evidence that the change did what it was approved to do, as opposed to that it ran.

---

## The migrations nobody wants to fund

Some migrations have no feature attached. They are the ones that cause the worst incidents,
and they are hard to fund because the benefit is an absence.

- **End-of-life upgrades.** PostgreSQL 11 reaching end of support; Oracle 19c's extended
  support ending; an operating-system upgrade that is also doc 05's `C-11` collation hazard.
- **The Oracle-to-PostgreSQL programme.** 1,310 databases, three years, and 41 migrated in
  year one — a rate that completes in 32 years.
- **Paying down schema debt.** 340 columns; 43 of unknown provenance; `VARCHAR2(4000)` columns
  holding JSON.

**The framings that work**, because "it is technical debt" does not:

1. **Name the forced date.** An EOL has a date. After it, there are no security patches, which
   makes it a compliance finding rather than an engineering preference. The date converts a
   preference into a deadline somebody else owns.
2. **Price the tax, per change.** Ironvale's Oracle estate costs roughly 2.4× the engineering
   time per schema change compared with its PostgreSQL estate — the licensing constraints,
   the `DBMS_REDEFINITION` ceremony, the PL/SQL coupling. Multiply by 248 changes a year and
   the migration pays for itself in engineering time alone, before licensing.
3. **Attach it to work that is already funded.** The cheapest moment to move a table out of
   Oracle is when a team is already rewriting the service that owns it. A programme that waits
   for dedicated funding gets 41 databases a year; one that attaches to in-flight work gets
   whatever is in flight, which is more.
4. **Make the cost of *not* doing it visible in the incident record.** When an incident's root
   cause is "this is running on a version with no patches" or "this schema has 340 columns and
   nobody knows what 43 of them do", say so in the postmortem, by name. Four postmortems
   saying the same thing fund the work that four architecture documents did not.

---

## The migration calendar

Freezes exist for real reasons — Ironvale's year-end close and quarter-end regulatory
reporting genuinely should not have schema changes landing in them — and they interact badly
with long migrations.

**The specific problem:** a migration in phase 3, with a 24-hour backfill and a 92-day soak,
started in November will have its contract phase land inside the year-end freeze. The team
either waits until mid-January, holding the dual write and the double storage for an extra six
weeks, or argues for an exception.

**Two rules that help:**

1. **Plan the whole timeline against the calendar, not just the start.** A migration plan
   should include every phase's date, checked against freezes. This catches the problem in
   phase 0, where the response is "start in September" rather than "request an exception in
   December".
2. **Distinguish freezing *changes* from freezing *phases*.** Holding in phase 2, with the dual
   write running and no new change landing, is not a change — it is a steady state. Freezes
   that forbid "any migration activity" force teams to either stop safe steady states or
   reclassify them, which is the reclassification problem again. **A freeze should forbid
   crossing a point of no return, not forbid existing in an intermediate phase.**

---

## What to take away

1. **Ironvale's window model allows 7.7 minutes per change**, which is arithmetically
   impossible. Nobody had computed it, because each team sees only their own four changes.
2. **An impossible control produces reclassification, not safety.** Changes become
   "configuration", "data fixes" and "emergencies", and the real risk moves to the paths with
   less scrutiny.
3. **Classify by risk and only 7% needs a window** — and those 17 changes a year get 114
   minutes each instead of 7.7.
4. **Classification must be automatic**, computed by the linter from the statements and the
   table size. Self-declaration becomes the next argument.
5. **A pipeline satisfies separation of duties better than a DBA typing a script**: the applied
   artefact is the reviewed one, it is immutably auditable, it is deterministic, and the
   reviewer has domain context.
6. **The structural requirement is that developer credentials cannot alter the schema.** Three
   roles, one afternoon.
7. **Lead with what the DBA team does instead** — owning the policy, the lint rules, the
   thresholds, class 3 review, and drift detection. It is a better job and it is what makes
   the proposal acceptable.
8. **Make "unowned" a countable state** and require a migration to assign an owner. That turns
   the registry from a documentation project into something that fills itself in.
9. **Build the consumer registry from two weeks of sampled activity, plus a human pass over
   everything scheduled.** Sampling will never find the quarterly export.
10. **Two thirds of the review checklist is automatable.** A checklist where a human verifies
    what a linter could is a checklist that gets rubber-stamped.
11. **Updating the erasure process is a step in the migration plan**, not an afterthought — a
    migration creates copies in the new column, the CDC topic, and the rehearsal clone.
12. **A forgotten rehearsal clone is a full copy of production personal data with no owner**,
    and it is the most common audit finding in this area.
13. **State where every copy will physically live, including temporary ones.** One line, and
    it is the line an auditor asks about.
14. **Write the verification report into the change record as an artefact.** It is the only
    evidence the change did what it was approved to do, rather than that it ran.
15. **Fund EOL work with a date, a per-change tax, and attachment to in-flight projects** — and
    name the cause in postmortems. Four postmortems fund what four architecture documents did
    not.
16. **Plan the whole timeline against the freeze calendar in phase 0**, and let a freeze forbid
    crossing a point of no return rather than forbid being in an intermediate phase.

Next: [19-case-studies.md](19-case-studies.md), where ten real-shaped incidents put all of
this together.

# Compliance: Reproducing the Past, Keeping What You Must, Deleting What You May

Document 00 claimed that the compliance consumer has the weakest latency requirement and the
strongest correctness requirement in the system, and that this is the opposite of what candidates
assume. This document makes that concrete.

The requirement, stated once more because everything here follows from it:

> Given any date in the past seven years, reproduce exactly what the system knew and decided on
> that date — not what we now believe to be true, but what the record said then — and demonstrate
> that the reproduction is faithful.

Four distinct capabilities are needed to satisfy it, and a design can have three and still fail.

---

## The worked example: why was this declined on 3 March 2024?

Make it concrete before making it general. An examiner, or a customer's attorney, or the bank's
own fair-lending review, asks:

> On 3 March 2024 at 14:17 ET, account 88241063's authorization for $1,340.00 at merchant
> MRC-40192 was declined. Explain the decline.

A complete answer requires four things, and they live in four different places.

**One: the decision record.** `risk.decisions.v1` for that `transaction_id`:

```
transaction_id      8f3a91c4e85b2d60
event_time          2024-03-03T19:17:04.221Z
decision            DECLINE
decline_reason      RISK_SCORE_THRESHOLD
risk_score          0.8841
model_id            fraud-cnp-v14
model_version       14.3.2
feature_def_version fd-2024-01-22
features            { velocity_card_1m: 7, velocity_card_1h: 31,
                      distinct_merchants_1h: 11, declines_card_1h: 3,
                      amount_to_30d_avg_ratio: 14.2, ... 42 more }
rule_hits           [ R-0412, R-0877 ]
threshold_applied   0.8500
```

This is the record document 04 argued for — the exact feature vector, not a recomputation. Because
it is logged rather than derived, it is what the model actually saw, and no amount of subsequent
change to feature definitions can alter it.

**Two: the model artefact.** `fraud-cnp-v14`, version `14.3.2`, as a binary, with the training
data manifest, the validation report, and the approval record from the model risk committee. Stored
in a model registry with the same immutability rules as the data. Without this you can state the
score but not demonstrate that the score was produced by an approved model operating within its
validated range.

**Three: the dimension values in force on that date.** Merchant MRC-40192 changed category code
from 5812 to 5814 on 17 February 2024, which is *before* this transaction, so the answer must use
5814 — even though a later change may mean the merchant's current code is something else again.
Working out which version applied is exactly what the type 2 dimension from document 06 is for. The
same applies to the account's credit limit, its delinquency status, and its product type: all as
they stood on that date, not as they stand today.

**Four: the facts as the record contained them on that date.** This is the one people miss. If a
reprocess in June 2024 corrected the merchant-category normalisation (document 05's scenario),
then today's `conformed.fact_authorization` contains the corrected value. The decision in March was
made using the uncorrected value. **Both are true and both must be retrievable**, which is what
`FOR TIMESTAMP AS OF` provides:

```sql
SELECT * FROM conformed.fact_authorization
FOR TIMESTAMP AS OF '2024-03-03 19:17:04'
WHERE transaction_id = '8f3a91c4e85b2d60';
```

The complete answer joins all four. The thing to notice is that **no single technology provides
it.** The decision log, the model registry, the type 2 dimension, and the table snapshot are four
separate mechanisms, and a design missing any one of them produces an answer that is incomplete in
a way an examiner will find.

---

## Lineage: mechanical, not a diagram

"We have data lineage" usually means someone drew a diagram. For a regulatory answer, lineage has
to be a *traversal* — given a number in a filed report, mechanically identify the rows that
produced it, and for each row, the rows that produced that.

Monarch's version has three parts and none of them is a diagram tool.

**Every derived row carries the version of the code that made it.** `conformed` rows carry
`transform_version`; `curated` rows carry both their own and the `conformed` version they read.
Code versions are git commit hashes recorded at job submission.

```
curated.merchant_daily
  business_date   merchant_id   auth_count   approved_amount   transform_version   source_snapshot
  2024-03-03      MRC-40192          4,122        $86,441.02   curate-md@9f41c2a   1884731204
```

`source_snapshot` is the Iceberg snapshot ID of `conformed.fact_authorization` the job read. That
single column is what makes the traversal possible: it pins not just *which table* but *which
version of which table*.

**Column-level provenance is declared, not inferred.** Each transform registers which output
columns depend on which input columns. Inferring this from SQL parsing works until a transform
contains a UDF, at which point inference silently returns nothing. Declaration is more work and it
does not quietly fail.

**The traversal is a query, and it is tested.** Quarterly, Monarch picks a figure from the
previous quarter's regulatory filing and traces it to the underlying authorization records as a
drill, timing it. The current time is 40 minutes. The first time it was attempted it took nine
days, which is how they learned that `source_snapshot` needed to exist.

> A lineage capability that has never been exercised under time pressure is a diagram. Run the
> drill.

---

## Retention versus the right to delete, which is a genuine conflict

A customer submits a deletion request under the California Consumer Privacy Act. Monarch holds
seven years of their transactions, because the Bank Secrecy Act requires it.

These obligations are in direct conflict and the resolution is legal rather than technical, so it
is worth knowing accurately — this comes up and a confident wrong answer is worse than "I would
confirm with counsel."

**The CCPA exempts personal information collected, processed, sold, or disclosed subject to the
Gramm-Leach-Bliley Act.** Transaction records held by a financial institution for regulatory
purposes fall in that exemption. Under the GDPR the analogous route is Article 17(3)(b): the right
to erasure does not apply where processing is necessary for compliance with a legal obligation.

So the answer to "delete my transaction history" at a bank is, lawfully, **no, and here is the
statute** — not a technical problem to engineer around.

That is only part of the data, though, and the distinction is where the engineering is:

| Data | Deletable on request | Why |
|---|---|---|
| `auth.events`, `ledger.postings`, `network.clearing`, `risk.decisions` | **No** | BSA and SOX recordkeeping; GLBA-exempt from CCPA |
| `app.clickstream` | **Yes** | Product analytics; no regulatory basis for retention |
| Marketing attributes, derived preference scores, device graph | **Yes** | No retention mandate |
| Name, address, contact details | **No, while the account exists**; deletable 7 years after closure | Needed to maintain the account and to identify the records BSA requires |

So the pipeline needs deletion that works for the second and third rows and a defensible refusal
for the first and fourth. Two mechanisms:

**For clickstream and derived attributes: actual deletion, via Iceberg row-level deletes.** A
deletion request writes the user's identifiers to a `deletion_requests` control table; a weekly job
applies row-level deletes to the affected tables and then **compacts to physically remove the
rows**, because an Iceberg delete file marks rows as deleted without rewriting the data files
containing them. A deletion that leaves the bytes in S3 is not a deletion, and this is the step
teams omit.

**For anything with a regulatory basis: the refusal is logged as carefully as a deletion would
be.** The request, the exemption relied on, the date, and the person who made the determination
go into the same control table. When a regulator asks whether Monarch honours deletion requests,
the answer is a query over that table showing what was deleted, what was refused, and on what
statutory basis.

### Legal hold overrides everything

When litigation is anticipated, affected records must be preserved regardless of retention
schedules. This means **the retention enforcer cannot be a cron job that deletes partitions older
than N days** — it must consult a hold registry first.

Monarch's retention runs as a **reconciler**: it computes which partitions *should* be expired,
subtracts anything under hold, and exports the difference as a metric rather than deleting
silently. The metric is `partitions_pending_expiry` and `partitions_held`. If the reconciler
cannot reach the hold registry, it deletes nothing and alerts.

This is a direct lesson from Harbinger, whose 23 cron deletion scripts had no concept of holds
and, on one occasion, deleted records that were under a preservation order. **Retention is
control-plane data enforced by a reconciler that exports drift, never a cron job that deletes.**

---

## Access control, in three independent layers

A lake containing seven years of every customer's transactions is the most attractive internal
target in the company. Access control has to survive one layer being misconfigured, which means
layers that fail independently.

**Layer one: who can reach the data at all.** IAM policies and Lake Formation permissions, granted
to roles rather than people, with role membership reviewed quarterly. Nothing is granted to an
individual; every grant is to a role with a named owner.

**Layer two: column-level masking by classification tier.** Recall document 02's four tiers. The
`restricted` tier — name, address, date of birth, device fingerprint — is masked by default, and
unmasking requires a separate grant. An analyst building merchant reports has no business reading
customer names and does not need to be trusted not to.

```
role: fraud_analyst         → sees pan_token, bin, last4, amount, merchant, mcc; name masked
role: dispute_investigator  → sees the above, plus name and address, for accounts with an open case
role: data_engineer         → sees schema and row counts; no restricted columns at all
```

The third line is the one people find surprising and it is correct: the engineers who build the
pipeline do not need to read customer data to operate it. They need schemas, counts,
distributions, and sample records with restricted fields masked. Designing for that from the start
is much easier than removing access later.

**Layer three: row-level filtering.** The dispute investigator's access is scoped to accounts with
an open case assigned to them, enforced as a row filter joining to the case management system.
Without this, "access to disputes data" means access to all 62 million accounts.

### The access log is itself a regulated artefact

Every query against restricted data is logged — who, what role, which columns, which rows,
when, and the query text. That log is retained for seven years and is itself access-controlled,
because it contains the identifiers that were queried.

Two uses, one obvious and one less so. The obvious one is investigating misuse. The less obvious
one: when a breach is suspected, the question "whose data was exposed" is answerable only if you
know what was read. Without a query log, the answer to a regulator is "we cannot determine the
scope," which converts a contained incident into a maximal-scope notification.

---

## Model governance, because the model is in the pipeline

US bank supervisory guidance on model risk management — the Federal Reserve and OCC's SR 11-7 —
treats a model as something requiring documented development, independent validation, and ongoing
monitoring. The fraud model in document 04 is squarely in scope, and so, less obviously, are the
feature definitions that feed it.

Three obligations land on the pipeline:

**The model inventory must be able to answer which model version scored which transaction.** This
is the `model_id` and `model_version` columns in the decision record. Without them a validator
cannot determine the production population a model version scored, and therefore cannot assess its
performance.

**Feature definitions are part of the model and are versioned with it.** A change to "distinct
merchants in the last hour" changes the model's behaviour as surely as retraining does. Monarch's
`feature_def_version` column exists for this, and a change to a feature definition triggers the
same review as a model change — which is a process cost people do not anticipate when they propose
"just tweak the window."

**Ongoing monitoring requires the production scored population, with outcomes.** Which means
joining decisions to outcomes that arrive 45 to 120 days later when chargebacks resolve. That join
— a decision in March to a chargeback in June — is only possible because `transaction_id` is
carried on both and because `risk.decisions` is retained for seven years.

The thing worth saying in an interview: **model governance is a data retention requirement before
it is a modelling requirement.** The 33.5 TB of decision records in document 01 exists because of
SR 11-7, not because anyone wanted to query it.

---

## The PCI boundary, restated at the lake

Document 02 established that the card number never leaves the cardholder data environment. The
consequence at this layer is worth making explicit because it is the payoff:

**The lake, the warehouse, the dashboard store, and the feature store are all out of PCI scope.**
Which means:

- They are assessed under the bank's general information security programme rather than by a
  Qualified Security Assessor against 300 PCI requirements.
- An analyst can be granted access through a normal access review rather than CDE onboarding.
- A quarterly vulnerability scan failure on a lake component is an ordinary remediation item
  rather than a compliance event.
- Backups, replicas, and disaster-recovery copies do not multiply the CDE footprint.

The cost of achieving this was one service — the tokenization service — and the discipline to put
it before the log rather than after. **That is the highest return on a single design decision
anywhere in this architecture**, and it is available only at the start; retrofitting it means
re-tokenizing seven years of history and re-scoping an existing assessment.

---

## What to take away

1. **Reproducing a past decision needs four separate mechanisms**: the logged decision with its
   actual feature vector, the versioned model artefact, the type 2 dimension values in force, and
   the table snapshot as of that date. A design with three of the four produces an incomplete
   answer.
2. **Log the feature vector rather than recomputing it.** A recomputation reflects today's
   definitions, which is not what the model saw.
3. **Lineage is a traversal, not a diagram.** It needs the code version and the source snapshot ID
   on every derived row, declared column-level provenance rather than inferred, and a quarterly
   drill that traces a filed figure back to source records under time pressure.
4. **Deletion requests for bank transaction records are lawfully refused** — the CCPA exempts
   GLBA-covered information and GDPR Article 17(3)(b) covers legal obligations. Know this
   precisely; a confident wrong answer is worse than deferring to counsel.
5. **Clickstream and derived marketing attributes are deletable, and the deletion must include
   compaction**, because an Iceberg delete file marks rows without removing the bytes.
6. **Log refusals as carefully as deletions**, with the exemption relied on, so the answer to "do
   you honour deletion requests" is a query rather than an assertion.
7. **Retention is a reconciler that consults a legal-hold registry and exports drift, never a cron
   job that deletes.** If it cannot reach the hold registry it deletes nothing and alerts.
8. **Three independent access layers**: role-based reachability, column masking by classification
   tier, and row-level filtering scoped to an assigned case. Pipeline engineers need schemas,
   counts and masked samples — not customer data.
9. **The query log over restricted data is itself a seven-year regulated artefact**, because
   without it the answer to "whose data was exposed" is "we cannot determine the scope."
10. **Model governance is a data retention requirement first.** The 33.5 TB of decision records
    exists because SR 11-7 requires knowing which model version scored which transaction and how
    it performed against outcomes that arrive four months later.
11. **Keeping the card number inside the CDE puts the entire analytical estate out of PCI scope.**
    It is the highest-return single decision in the architecture and it is only available at the
    start.

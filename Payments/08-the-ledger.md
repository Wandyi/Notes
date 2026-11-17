# The Ledger: Why You Never Update a Balance

Everything so far has been about moving messages. This document is about keeping books, and it
contains the single most important structural decision in a payment system.

The decision is this: **a balance is not a number you store and modify. It is a projection over an
immutable list of entries.** Teams that get this wrong build systems that work for two years and
then cannot answer the question "why does this merchant's balance say $14,207.33?" — which is the
question that matters when a merchant disputes a payout, an auditor asks, or a regulator writes.

---

## The naive design, and the exact day it fails

Here is what almost every team builds first:

```sql
CREATE TABLE merchant_balances (
  merchant_id   BIGINT PRIMARY KEY,
  balance_minor BIGINT NOT NULL,
  currency      CHAR(3) NOT NULL
);
```

And the code that maintains it:

```sql
-- a capture lands
UPDATE merchant_balances
   SET balance_minor = balance_minor + 4534
 WHERE merchant_id = 8812;
```

This is correct arithmetic. It is also unrecoverable, and here is the day it fails.

Harbour Athletics calls support: "My balance says $14,207.33. I calculate $14,263.08. Where is the
$55.75?"

You query the balance table. It says 1420733. That is all it says. **There is no way to answer the
question**, because the table holds the answer to the sum and not the terms. Your options are:

- Recompute from the payments table, which means re-deriving fee logic as it existed at the time
  of each payment, including the version of the fee schedule in force on each date. If the fee
  schedule has changed, you cannot reproduce it.
- Read the application logs, which are retained for 30 days and do not cover the window.
- Tell the merchant you do not know.

The $55.75 turns out to be a refund that was applied twice because of a retry that predated
document 03's idempotency work. You cannot prove that, you cannot find the other instances, and
you cannot tell whether the same bug affected other merchants.

**The failure is not that the number is wrong. The failure is that the system cannot explain
itself.** A balance that is a stored number has thrown away its own derivation.

---

## Double-entry, derived from the problem

The accounting profession solved this in the fifteenth century and the solution is better than
anything a software engineer invents from scratch. Here is why it works, built up rather than
asserted.

Return to document 00's invariant: **Northwind Pay is never holding its own money.** Every cent in
its accounts belongs to a merchant, or is owed to a card network, or is a fee it has earned. If
you write down where every cent came from and where it went, then at every instant the total of
what you hold must equal the total of what you owe plus what you have earned.

Write that as an equation and you have the accounting identity. Now make it operational: **every
financial event is recorded as a set of entries whose amounts sum to zero.** Money is never created
or destroyed by a bookkeeping operation; it only moves between accounts.

A capture of $47.00 on Harbour Athletics' payment, with $1.66 of fees:

| Account | Direction | Amount |
|---|---|---|
| Card network receivable | debit | +4700 |
| Merchant payable: Harbour Athletics | credit | −4534 |
| Fee revenue | credit | −166 |

Sum: 4700 − 4534 − 166 = **0**. 

Read what those three lines say, because this is where the structure earns its keep:

- Northwind Pay is **owed $47.00** by the card network, which will arrive at settlement.
- Northwind Pay **owes $45.34** to Harbour Athletics, which it will pay out.
- Northwind Pay has **earned $1.66**, which is its own.

Those are three genuinely different facts about the same event, and the naive single-balance
design collapsed them into one. Now ask the merchant's question again: "where is the $55.75?" You
query the entries against the merchant-payable account for Harbour Athletics, ordered by time,
and the $55.75 is sitting there as two refund entries nine seconds apart with two different
payment ids. Resolved in one query, with proof.

A note on vocabulary, because the terms are a barrier for engineers and are simpler than they
look. **Debit** and **credit** do not mean increase and decrease, and they do not mean good and
bad. They mean the two sides of an entry. By convention, a debit is a positive number and a credit
a negative one, and an account's *type* determines which direction represents more of the thing.
For a payable — money you owe — a credit increases what you owe. If the vocabulary gets in the way,
use `+` and `−` internally and map to debit/credit only at the reporting boundary; several large
processors do exactly that.

---

## The schema

```sql
CREATE TABLE journal_entries (
  id              BIGSERIAL PRIMARY KEY,
  transaction_id  UUID        NOT NULL,       -- groups the balanced set
  account_id      BIGINT      NOT NULL,
  amount_minor    BIGINT      NOT NULL,       -- signed, document 02
  currency        CHAR(3)     NOT NULL,
  effective_at    TIMESTAMPTZ NOT NULL,       -- when it happened economically
  recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_type     TEXT        NOT NULL,       -- capture | refund | fee | payout | dispute
  source_id       TEXT        NOT NULL,       -- the payment, refund, payout id
  metadata        JSONB
);

CREATE INDEX ON journal_entries (account_id, effective_at);
CREATE INDEX ON journal_entries (source_type, source_id);
```

Five properties of this table are non-negotiable, and each is a rule that must be enforced
mechanically rather than by convention.

**There is no `UPDATE` and no `DELETE`.** Entries are append-only. Northwind Pay enforces this with
a database rule that raises on update or delete against the table, plus a role that lacks those
grants. Convention is not enough; someone will write the update eventually, and in a financial
ledger that is unrecoverable.

**Entries are grouped by `transaction_id` and each group sums to zero, per currency.** This is
checked at write time, inside the transaction, before commit. A deferred constraint or a trigger
that sums the group is the cleanest mechanism. Checking it later, in a batch job, means you find
out about imbalance after it has been read.

**Two timestamps, not one.** `effective_at` is when the event happened in economic reality;
`recorded_at` is when your system learned about it. These differ constantly: a dispute received on
Thursday may be effective from the previous Monday's transaction. Reporting that uses
`recorded_at` as the economic date will restate prior periods every time a late entry arrives.
Reporting that uses only `effective_at` cannot reproduce what a report said yesterday. You need
both, and the convention is that financial reporting uses `effective_at` while operational
monitoring uses `recorded_at`.

**Every entry names its source.** `source_type` and `source_id` let you go from a ledger entry back
to the payment that caused it, which is the query support actually runs.

**Amounts are signed integers of minor units with an explicit currency**, per document 02, and a
transaction's entries must balance **within each currency separately**. A cross-currency event does
not balance by summing dollars against yen; it balances by having an FX account that absorbs the
conversion, as the next section shows.

---

## The chart of accounts

Which accounts exist determines what questions you can answer. Northwind Pay's are grouped into
four families.

**Asset accounts** — things Northwind Pay holds or is owed.

- `acquiring_bank:us_east`, `acquiring_bank:eu_west`, … — real bank accounts, one per region and
  currency
- `network_receivable:visa`, `network_receivable:mastercard` — captured but not yet settled
- `merchant_receivable` — negative merchant balances, which is money a merchant owes back

**Liability accounts** — things Northwind Pay owes.

- `merchant_payable:{merchant_id}` — the big one, 40,000 of them
- `merchant_reserve:{merchant_id}` — funds held back per document 09
- `network_payable:visa` — refunds and fees owed outward
- `unidentified_receipts` — money received that cannot yet be attributed, discussed below

**Revenue accounts** — what Northwind Pay has earned.

- `fee_revenue:processing`, `fee_revenue:dispute`, `fee_revenue:fx`

**Expense and loss accounts.**

- `interchange_expense`, `network_assessment_expense`
- `fraud_loss`, `dispute_loss` — losses absorbed rather than passed to the merchant
- `fx_gain_loss` — the fractional cents from document 02's conversions
- `rounding_adjustment` — the other place fractional cents land

That second-to-last account deserves emphasis. Document 02 showed that converting ¥7,100 at 151.06
yields $47.0012, which must be rounded to $47.00, and that the lost 0.12 cents is real money. In
this ledger it has a home: the conversion transaction debits the dollar side $47.00, credits the
yen side ¥7,100, and the difference goes to `fx_gain_loss`. **The ledger balances because there is
an account for the thing that does not balance.** A design without such an account forces engineers
to fudge a rounding somewhere, and the fudge is undetectable.

`unidentified_receipts` is the operational equivalent. Settlement sometimes delivers an amount
that does not match any expected figure — a network adjustment, a fee rebate, an error. It must be
recorded, because the bank account changed, and it cannot yet be attributed to a merchant. It
lands in `unidentified_receipts` and becomes a work item for the reconciliation team (document
10). A ledger with no such account forces the operator to either drop the discrepancy or mis-post
it.

---

## Balances as projections

With entries as the authority, a balance is a query:

```sql
SELECT COALESCE(SUM(amount_minor), 0)
  FROM journal_entries
 WHERE account_id = $1
   AND currency = $2
   AND effective_at <= $3;
```

Note the `effective_at <= $3`. This query answers "what was the balance as of a point in time",
which the naive design could not answer at all. That capability is what makes month-end reporting
and audit response possible.

The obvious objection is performance. Harbour Athletics has been a merchant for four years and has
2.3 million entries against its payable account. Summing them on every dashboard load is not
viable.

The resolution is **snapshots**, and the discipline around them is what keeps the design honest:

```sql
CREATE TABLE account_balance_snapshots (
  account_id     BIGINT      NOT NULL,
  currency       CHAR(3)     NOT NULL,
  as_of          TIMESTAMPTZ NOT NULL,
  balance_minor  BIGINT      NOT NULL,
  last_entry_id  BIGINT      NOT NULL,
  PRIMARY KEY (account_id, currency, as_of)
);
```

A snapshot is written at each daily close. A current balance is then the most recent snapshot plus
the entries after `last_entry_id`, which for a typical merchant is a few hundred rows rather than
millions.

Three rules keep snapshots from becoming the naive design in disguise:

1. **A snapshot is a cache, and the entries are the authority.** Any snapshot can be deleted and
   rebuilt from entries. Northwind Pay rebuilds a random 0.5% of snapshots nightly and alerts on
   any mismatch — the same shape of check as document 04's status sweep, and for the same reason.
2. **Snapshots are never adjusted.** If a late entry arrives with an `effective_at` before an
   existing snapshot, you do not edit the snapshot; you mark it stale and rebuild it. Editing it
   means the snapshot no longer equals the sum of its entries, and the invariant that makes the
   whole design trustworthy is gone.
3. **No business logic reads a snapshot without also reading the subsequent entries.** A payout
   decision that reads only yesterday's snapshot will pay out money that was refunded this
   morning.

---

## Reversing a mistake

You posted a transaction and it was wrong. The entries are immutable. What now?

**You post a reversing transaction.** The original entries stay, and a new transaction with the
opposite signs is appended, linked to the original by metadata. The net effect is zero and the
history shows both what was recorded and what corrected it.

This feels wasteful to engineers and is the entire point to auditors. The merchant's balance
history now shows: a $47 capture, a $47 reversal, and a note saying why. If you had edited the
original, the history would show a $47 capture that had always been $47 and no evidence that
anything went wrong — which is exactly what someone committing fraud would want it to show.

The practical rule: **the only two operations on a ledger are "post" and "post the opposite".**
There is no third.

---

## Reservations: recording intent before certainty

Document 03's request flow included an entry labelled "ledger reservation" before the issuer was
called, and it needs explaining.

An authorization moves no money (document 01), so it posts no entries to real accounts. But
Northwind Pay needs to know that an authorization exists, and the fraud and velocity systems need
to see pending exposure. So the authorization posts to a pair of **memo accounts** that are
tracked in the ledger but excluded from financial reporting:

| Account | Direction | Amount |
|---|---|---|
| `authorization_holds:contra` | debit | +4700 |
| `authorization_holds:harbour_athletics` | credit | −4700 |

When the authorization is captured, expired or reversed, the memo entries are reversed and the
real entries posted. The memo accounts net to zero over any complete lifecycle, which is itself a
useful check: a non-zero `authorization_holds` balance older than the 7-day validity window means
authorizations are being left unresolved, which is a bug in the expiry job.

---

## Volume, and what it costs to do this properly

Count the entries for one complete payment lifecycle at Northwind Pay:

| Event | Entries |
|---|---|
| Authorization memo | 2 |
| Authorization memo reversal | 2 |
| Capture with fee split | 3 |
| Settlement receipt | 2 |
| Payout | 2 |
| **Total for a clean payment** | **11** |

A refund adds 3, a dispute adds 6 across its lifecycle, an FX conversion adds 3.

At 1,047,600,000 approved payments a year, 11 entries each is 1,047,600,000 × 11 =
**11.5 billion journal entries per year**. With refunds and disputes on top, Northwind Pay posts
roughly **13 billion entries a year**, which is 13,000,000,000 ÷ 31,536,000 = **412 entries per
second on average**, and at the 237× peak ratio from document 00, close to **98,000 entries per
second at peak**.

That is a serious write workload, and it is the honest cost of this design. Three things make it
tractable:

- **Entries are append-only**, so there is no update contention and no row-level locking on hot
  accounts. This is a significant advantage over the naive design, where every capture for a
  large merchant contends on one balance row. The busiest Northwind Pay merchant generates around
  830 captures per second at peak, which is inside the range where single-row update contention
  starts to dominate and leaves no headroom above it. Document 13 works that number through.
- **The table is partitioned by `effective_at`**, monthly, so indexes stay manageable and old
  partitions can move to cheaper storage.
- **Reads are served from snapshots plus a tail**, so the 11.5 billion rows are written once and
  read rarely.

It is worth stating the trade-off plainly rather than pretending it is free: the double-entry
ledger costs roughly eleven times the write volume of a single mutable balance column. What it buys
is the ability to answer "why is this number what it is" for any number, at any past instant, with
proof. In a system handling other people's money, that is not a feature you can add later — the
information needed to reconstruct the history is destroyed the moment you choose not to record it.

---

## What to take away

1. **A stored balance throws away its own derivation.** When a merchant disputes a figure, a
   balance column can only restate the figure, not explain it.
2. **Every financial event is a set of entries summing to zero**, checked inside the write
   transaction. Money moves between accounts; bookkeeping never creates or destroys it.
3. **The ledger table is append-only, enforced by the database**, not by convention. No update, no
   delete, ever.
4. **Record two timestamps.** `effective_at` for economic reality, `recorded_at` for when you
   learned. Reporting on the wrong one either restates history or cannot reproduce it.
5. **Have an account for the thing that does not balance** — `fx_gain_loss`, `rounding_adjustment`,
   `unidentified_receipts`. Without them, engineers fudge, and the fudge is undetectable.
6. **A balance is a projection.** Snapshots are a cache of that projection, rebuildable from
   entries, verified by a nightly sample, never adjusted in place.
7. **Correct a mistake by posting the opposite, never by editing.** The two operations are "post"
   and "post the reverse". An edited history is indistinguishable from a concealed one.
8. **Authorizations post to memo accounts**, which net to zero over a complete lifecycle and whose
   residue is a direct signal of unresolved authorizations.
9. **The cost is about 11× the write volume of a mutable balance** — 13 billion entries a year at
   Northwind Pay. The compensation is that append-only writes have no contention, which a hot
   balance row does.

Next: [09-settlement-and-payouts.md](09-settlement-and-payouts.md), which uses this ledger to move
real money to merchants.

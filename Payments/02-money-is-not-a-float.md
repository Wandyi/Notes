# Money Is Not a Float: Amounts, Currencies and Rounding

This is the shortest document in the collection and the one whose lessons are violated most
often. Representing an amount of money looks like a solved problem — it is a number — and it is
not, because the number has a unit attached, the unit has a varying number of decimal places, the
arithmetic has legal constraints, and the obvious data type silently produces wrong answers.

Northwind Pay operates in 135 presentment currencies and settles in 23. Every one of the
problems below is a live operational concern there, not a theoretical worry.

---

## Start with the mistake, so you can feel it

Here is the naive version, in any language with IEEE 754 floating point, which is nearly all of
them:

```python
>>> 0.1 + 0.2
0.30000000000000004
```

The standard explanation is that 0.1 cannot be represented exactly in binary, which is true but
does not convey why it matters. So here is a payments-shaped version of the damage.

Harbour Athletics is splitting a $47.00 order across three items for its accounting export:
$19.99, $19.99 and $7.02. Sum them as floats:

```python
>>> 19.99 + 19.99 + 7.02
46.99999999999999
```

The order total stored on the payment is `47.00`. The sum of the line items is `46.99999999999999`.
A validation check that says `if sum(items) != total: reject` now rejects a correct order. A
developer under deadline pressure "fixes" this by comparing with a tolerance of one cent, and six
months later a genuine one-cent discrepancy — the kind that indicates a real bug in fee
calculation — passes the check silently.

Now scale it. Northwind Pay processes 1,047,600,000 approved payments a year. If each one carries
an average floating-point error of even a hundredth of a cent in one direction, that is
1,047,600,000 × $0.0001 = **$104,760 per year of unexplained difference** between what the ledger
says and what the bank says. Nobody can find it, because it is not in any one transaction; it is
smeared across all of them.

The failure mode is not "the number is slightly wrong." It is **the reconciliation process stops
being able to distinguish real breaks from representation noise**, which destroys the mechanism
document 10 depends on.

---

## The fix: integer minor units

Store money as **an integer count of the currency's smallest unit, plus a currency code**. Never
as a float. Never as a decimal string without the currency.

For US dollars, the smallest unit is the cent, so $47.00 is stored as:

```json
{ "amount": 4700, "currency": "USD" }
```

This is what every serious payment API does, and it is worth being precise about why it works.
Integers are exact. Addition and subtraction of integers are exact and associative. The three
line items above become 1999 + 1999 + 702 = 4700, which equals 4700 exactly, every time, in every
language, on every platform.

The cost is that you must remember what unit you are in. An `amount` field holding `4700` is
ambiguous without the currency, and a bug that interprets minor units as major units charges a
customer one hundred times too much. Northwind Pay's internal convention, which is worth copying,
is that **any field holding a minor-unit integer is named with a suffix that says so** —
`amount_minor`, not `amount` — and any field holding a human-readable decimal is named
`amount_display` and exists only at the presentation boundary.

---

## Not every currency has two decimal places

This is the part that breaks systems built by teams who only ever handled dollars and euros. The
number of decimal places, formally called the **exponent**, varies by currency, and ISO 4217
defines it.

**Zero-decimal currencies.** The Japanese yen has no subunit in practical use. ¥5,000 is stored
as `{ "amount_minor": 5000, "currency": "JPY" }` — five thousand yen, not fifty yen. A system
that assumes two decimals everywhere and divides by 100 to display will show ¥50 for a ¥5,000
charge. Other zero-decimal currencies in Northwind Pay's list include the Korean won (KRW),
Vietnamese dong (VND), Chilean peso (CLP) and Paraguayan guaraní (PYG).

**Three-decimal currencies.** The Kuwaiti dinar is divided into 1,000 fils. KD 4.500 is
`{ "amount_minor": 4500, "currency": "KWD" }`. Bahraini dinar (BHD), Omani rial (OMR), Jordanian
dinar (JOD) and Tunisian dinar (TND) are the same.

There is an extra trap here. The card networks mostly transmit amounts with an implied two
decimals, so a three-decimal currency has to be handled specially in the message format, and the
networks require that the final digit of a three-decimal amount be **zero** in card transactions.
So KD 4.567 cannot be charged; it must be KD 4.570 or KD 4.560. Your validation layer has to know
this, because the network will reject the message otherwise and the merchant will see an opaque
failure.

**The practical rule.** Keep a currency table. It is small — roughly 180 rows — and it holds the
ISO code, the exponent, and any network-specific constraints. Every place that converts between
minor units and display format reads from that table. There is no acceptable shortcut, and
`amount / 100` appearing anywhere in a payments codebase is a defect.

---

## The two currencies on every transaction

A single cross-border payment has at least two currencies and you must name them differently or
you will conflate them.

**Presentment currency** is what the cardholder sees and agrees to. A customer in Tokyo buying
from Harbour Athletics sees ¥7,100.

**Settlement currency** is what the merchant receives. Harbour Athletics is a US company and
settles in USD, so it receives dollars.

Somewhere between those two, a conversion happened, and the question of *who* performed it
determines who bears the exchange-rate risk and what rate was used.

- **The issuer converts.** The transaction is submitted in USD; Meridian Bank's Japanese
  equivalent converts to yen at its own rate and may add a foreign-transaction fee. The
  cardholder sees a yen amount they did not see at checkout.
- **The network converts.** Submitted in one currency, settled in another, using the network's
  daily rate. Predictable but not controllable.
- **The merchant converts (dynamic currency conversion).** Harbour Athletics prices in yen,
  takes the transaction in yen, and Northwind Pay converts to USD for settlement. The cardholder
  sees exactly the number they agreed to. This is the best customer experience and the one that
  requires you to actually run FX.

Northwind Pay offers the third, which means it holds currency positions. The engineering
consequences:

**A rate has a timestamp and an expiry.** A quote of ¥151.06 to the dollar is valid for a window
— Northwind Pay uses 60 minutes — and the payment records *which quote it used*, by id, not just
the rate. When reconciliation (document 10) finds a $3 difference on a cross-border settlement,
the only way to resolve it is to re-derive the conversion from the exact quote that was applied.
A rate stored as a bare number with no quote id makes that impossible.

**The conversion is a ledger event, not an arithmetic detail.** Converting ¥7,100 to $47.00 at
151.06 produces 7100 ÷ 151.06 = $47.0012... which must be rounded, and the fraction of a cent
that is lost or gained is **real money** that has to land somewhere. In double-entry terms it
lands in an FX gain/loss account. If it does not, your ledger does not balance, and document 08
explains why that is not negotiable.

---

## Rounding: pick one rule and write it down

Any division produces a remainder, and in money the remainder is someone's cent. Three rules
appear in practice and you must know which you are using.

**Round half up.** 2.5 → 3, −2.5 → −3. What most people learned in school. Biased upward, which
over a billion transactions means you systematically collect more than you should.

**Round half to even, also called banker's rounding.** 2.5 → 2, 3.5 → 4. Ties go to the nearest
even number, so the bias cancels over many operations. This is the IEEE 754 default and the
convention most financial systems use.

**Round down, or truncate.** Always toward zero. Used where a regulation says a fee may not
exceed a stated maximum.

The bias is not negligible. Suppose a fee calculation rounds up on half-cent ties, and ties occur
in 1% of Northwind Pay's transactions. That is 10,476,000 transactions a year, each
over-collecting $0.005, for **$52,380 per year** collected in excess. That is a compliance
problem, not a rounding curiosity.

**The allocation problem.** Rounding gets genuinely hard when you must split an amount into parts
that sum exactly to the original. Harbour Athletics has a $47.00 payment and owes a 30%
commission to each of three affiliates. Naively:

- 30% of $47.00 = $14.10, three times = $42.30, which leaves $4.70. Fine, that one divides.

Try a harder one. Split $47.00 three ways equally:

- 4700 ÷ 3 = 1566.67 minor units
- Round each to 1567, sum = 4701. **You have created a cent.**
- Round each down to 1566, sum = 4698. **You have destroyed two cents.**

Neither is acceptable in a ledger that must balance. The correct technique is the **largest
remainder method**: give each party the floor of its share, then distribute the leftover units one
at a time to the parties with the largest fractional remainders.

- Floors: 1566, 1566, 1566. Sum 4698. Remainder to distribute: 2 units.
- Each had the same fractional part (0.667), so tie-break deterministically — Northwind Pay
  tie-breaks by the recipient's id, ascending.
- Final: 1567, 1567, 1566. Sum = 4700. Exact.

The deterministic tie-break matters more than which rule you pick, because the same split must
produce the same answer if it is ever recomputed. A split that uses hash iteration order will
produce a different allocation on a retry and your ledger will show a one-cent difference that
nobody can explain.

---

## What type to use in each layer

A practical table, since this is one of the few places a table beats prose.

| Layer | Representation | Why |
|---|---|---|
| Database column | `BIGINT` minor units + `CHAR(3)` currency | Exact, indexable, no driver coercion surprises |
| Application domain | A `Money` value object wrapping `int64` + currency | Makes mixed-currency arithmetic a compile error |
| JSON API | integer `amount` + string `currency` | No float in JSON, ever — `47.00` may deserialize as a double |
| Internal arithmetic needing fractions | arbitrary-precision decimal | Fee percentages, FX, interest |
| Display | formatted string from the currency table | Locale-aware, never hand-rolled |

Two notes on that table.

**`BIGINT`, not `INTEGER`.** A 32-bit signed integer tops out at 2,147,483,647 minor units, which
is $21.47 million. That is not enough. A single Northwind Pay merchant settling a day's volume in
Indonesian rupiah, a currency where a modest amount runs to millions of units, overflows it. Use
64 bits.

**A `Money` type, not a bare integer.** The reason is the single most dangerous operation in
payments: adding two amounts in different currencies. As bare integers, `4700 + 7100` compiles,
runs, and produces 11800, which is neither dollars nor yen and will be silently treated as
whichever currency the surrounding code assumes. Wrapped in a type whose addition requires
matching currency codes, the same expression fails loudly. Northwind Pay's Go implementation
returns an error from `Add` on currency mismatch; its Java implementation throws. Either is fine.
A bare `long` is not.

---

## The amount fields one payment actually has

By the time a payment has been through the lifecycle in document 01, it carries more than one
amount, and collapsing them is a frequent cause of merchant-facing reporting bugs.

- `amount_requested` — what the merchant asked to authorize. $47.00.
- `amount_authorized` — what the issuer actually approved. Usually equal, but **partial
  authorization** exists: a prepaid card with $31 on it may approve $31 of a $47 request, and
  the merchant must decide whether to accept a partial. Common in fuel and retail.
- `amount_captured` — the sum of captures. May be less than authorized.
- `amount_refunded` — the sum of refunds.
- `amount_disputed` — currently held against dispute.
- `amount_net` — what the merchant will actually receive, after fees.

The merchant-facing "how much did I get" is `amount_captured − amount_refunded − amount_disputed
− fees`, and every one of those terms is a separate aggregate over child records. Storing a
single mutable `amount` field on the payment and updating it as things happen produces a number
that is wrong in a different way for each reader. Document 08 makes the stronger version of this
argument for balances.

---

## What to take away

1. **Never use a float for money.** The error is not that a number is slightly off; it is that
   reconciliation loses the ability to tell real breaks from noise.
2. **Store an integer of minor units plus a currency code, in a 64-bit column.** Name the field
   so the unit is obvious — `amount_minor`, never `amount`.
3. **Decimal places vary by currency.** Yen has none, Kuwaiti dinar has three. Keep a currency
   table and read the exponent from it; `/ 100` anywhere in the codebase is a defect.
4. **Presentment currency and settlement currency are different fields, and the conversion
   between them is a ledger event**, with a quote id recorded, not an arithmetic detail.
5. **Choose a rounding rule explicitly and document it.** Half-up applied to 1% of Northwind Pay's
   volume over-collects $52,380 a year.
6. **Splitting an amount requires the largest-remainder method with a deterministic tie-break**,
   so that the parts sum exactly to the whole and recomputing gives the same answer.
7. **Wrap money in a type that refuses cross-currency arithmetic.** `4700 + 7100` must not
   compile or must throw, because as a silent `11800` it will eventually reach a customer.
8. **One payment has six different amounts.** Each is an aggregate over child records, not a
   mutable field.

Next: [03-the-api-and-idempotency.md](03-the-api-and-idempotency.md), which designs the merchant
-facing API and confronts the primitive that makes retries safe.

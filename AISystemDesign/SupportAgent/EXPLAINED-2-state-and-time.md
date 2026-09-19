# Part 2 — State, Memory, and Surviving Time

[Part 1](EXPLAINED.md) ended with an architecture: a coordinator hands a specialist a bounded
**lease** to talk to the customer directly, a plain-Python **turn ledger** records every turn
whether or not the coordinator ran, specialists call `propose_action` instead of moving money
themselves, and handoffs carry a structured **brief** that keeps `verified_facts` separate from
`customer_claims`. That design says who may speak and who may act. It does not say what any of them
are allowed to *remember*.

This part answers that. It is the least glamorous half of the system and the half that decides
whether customers feel like they are talking to something competent. Every failure in here surfaces
in the customer's window as one of three sentences: *"Can you give me your order number again?"*,
*"I don't see any refund on your account"*, or the worst one, *"I've refunded you $49"* said twice
about the same $49. All three are memory bugs.

We will keep using Part 1's running example, with the details filled in — because from here on the
details are the argument:

- **Dana Whitfield**, `customer_id = cust_88213`, on the **Pro** plan.
- Two charges on **Tue 3 March 2026**, both **$49.00**, on invoice **INV-771**. One is the Pro
renewal (`ch_9f21`). One is a seat add-on enabled **Mar 3 at 14:12** (`ch_9f44`).
- Order **#88213**, card **Visa ending 4021**, and the refund that eventually happens is
**RF-88213**.
- The conversation is an **email thread**. It starts **Monday 9 March 2026 at 09:14** and Dana
replies **Wednesday 11 March at 11:02**. The gap in the middle is where most of this document
lives.

---

## 1. Deriving the state from the conversation, one turn at a time

Do not start from a schema. Start from the conversation and ask, at each turn, *what would break if
the system forgot this?* The channels fall out on their own. A **channel** is one named slot in the
state, with its own type and its own merge rule — Part 1's `TypedDict` where `messages` appends and
bare types replace, considered one key at a time, with an owner attached to each.

### Turn 1 — Monday 09:14, "Why was I charged twice in March?"

Before any model runs, something has to answer a question the message does not contain: **who is
"I"?** Resolving the sending address to `cust_88213`, Pro plan, EU residency, two open orders, is a
40 ms call to the identity service, and it is the same answer for the whole conversation — so you do
it **once, at intake**, and hold it.

> **Channel 1: `customer`.** The resolved profile. Written once, read by everything.

Billing then calls `get_charges(invoice="INV-771")` and gets back about 4 KB of JSON: two charge
objects, a payment-method stub, three prior invoices, a dunning state, and a pile of nulls. Four
things in there matter for the rest of the conversation — `ch_9f21` is $49.00 and is the plan
renewal, `ch_9f44` is $49.00 and is a seat add-on, the add-on was enabled Mar 3 at 14:12, and the
seat is still active. You could re-fetch them every turn, but `get_charges` has a p95 of 340 ms and
each call spends one of the conversation's 25 tool calls — and worse, **re-deriving four facts from
4 KB of JSON is a model call, and a model call can come out differently the second time.** The first
pass said 14:12; the second, reading a truncated payload, says 14:00. Which one does Dana get told?

> **Channel 2: `facts`.** Things a tool confirmed, stored once, cited forever.
>
> **Channel 3: `messages`.** The transcript Dana actually reads. The reply is the product.

### Turn 2 — "No, I clicked that by accident."

Nothing new is verified here, and yet this turn needs two kinds of memory. Resolving *"that"*
requires the previous two messages **verbatim** — the pronoun binds to "a seat add-on that was
enabled Mar 3 at 14:12", and a summary reading "discussed the March charges" loses the referent.
Hold that thought; it becomes a hard constraint in §9.

The second kind is subtler. "I clicked that by accident" is now something the system knows, but it
is not a fact in the sense `facts` means: **no tool confirmed it.** It is an assertion by a person
with an interest in the outcome. Part 1 drew this line for handoff briefs; the same line runs
through the conversation state, and §3 makes it precise.

### Turns 3 and 4 — "Remove and refund", then the confirmation

Billing wants to propose a $49.00 refund. Before the firewall will look at it, three questions have
to be answerable without asking a model anything: which specialist is proposing (returns must not be
able to propose a refund it was never granted), what that specialist may propose and up to how much,
and how many turns it has left before the coordinator takes the conversation back. All three live on
the lease from Part 1.

> **Channel 4: `lease`.** The current grant of authority — holder, scope, turn count, allowed tools,
> proposable actions with their ceilings, and two expiry times.

Dana confirms, the firewall executes, and **RF-88213 exists in the payment processor now.** The next
turn must not be able to forget that — and notice it is *not* a message. The sentence "$49.00
refunded to the Visa ending 4021, reference RF-88213" is a *rendering* of the event, so if the
sentence is the only record, anything that rewrites the transcript destroys the record. Something
also has to know that this case is about billing, that the original complaint was a suspected double
charge, and that it is now resolved. And all four turns spent turns, tool calls, tokens, seconds and
dollars — if nothing counts them, nothing can stop a conversation spending forever.

> **Channel 5: `actions`.** Executed and denied actions as structured events, append-only.
>
> **Channel 6: `case`.** Intents, domains, resolution state, and the original problem statement.
>
> **Channel 7: `budget`.** Six counters, checked *before* each step runs.

### Now the table

Only now does a schema table teach anything, because every row has a reason behind it already.

| Channel | What it holds | Reducer | Who may write it |
|---|---|---|---|
| `messages` | The customer-visible transcript | `add_messages` — appends, upserts by message id | Intake (user turns); **the one active specialist** (exactly one message per turn); the firewall (rendered confirmations) |
| `customer` | Resolved profile: id, tier, residency, contact | replace | Intake, once |
| `case` | Intents, domains, resolution state, original problem | replace | Triage (intents, domains); coordinator (resolution state) |
| `lease` | The current authority grant, or `None` | replace | **The coordinator, and nobody else** |
| `facts` | `VerifiedFact` records with provenance | `upsert_facts` — merge by key (§3) | The tool-result extractor only |
| `actions` | Action grants and denials | append | **The action firewall only** |
| `budget` | Six counters | field-wise add | Every guarded node; fan-out reservations |

Two things about that table are worth more than the table. **First, every channel has a named owner,
and most have exactly one** — `lease` the coordinator, `actions` the firewall, `facts` the
tool-result extractor. This is not tidiness. In §5 you will see that single ownership is the only
thing standing between you and a conversation holding two contradictory leases.

**Second, look at what is not on the list.** The 4 KB of invoice JSON is not there, nor the
`get_charges` retries, nor the drafts, nor the hypothesis billing pursued for one tool call before
abandoning it. All of that existed. None of it is in the state — the most important design decision
in this document, so it gets its own section.

---

## 2. What the customer sees versus what the specialist scratches on

Here is the concrete situation. Handling turn 1, billing calls `get_invoice` (1.1 KB), `get_charges`
(times out at 3 s), `get_charges` again (4.2 KB), `get_plan` (0.6 KB), `get_payment_method` (0.3
KB), and `get_dunning_state` (0.2 KB and irrelevant, but the model checked anyway). Six tool calls,
one of them a duplicate, about 6.4 KB of JSON — call it **4,000 tokens** — plus two draft replies
the model discarded before writing the third.

Now: where does that go? The tempting answer is `messages`. It is the channel that already exists,
`add_messages` already appends, and every LangGraph tutorial you have read puts `ToolMessage`
objects straight into it. Do that and two separate things break.

**It destroys the only justification you had for separate agents.** In Part 1, the reason a
supervisor beat a single agent on multi-area questions was **context isolation** — billing's 4,000
tokens of invoice JSON never entered anyone else's context. Land those tool messages in shared
`messages` and, when the coordinator re-leases to orders, orders inherits all of it. You are paying
the multi-agent tax and getting single-agent context: strictly the worst of both designs.

**It stops the transcript reading as one coherent agent.** `messages` renders in the chat window and
gets quoted into email replies. Tool JSON in there means either the seams show or you filter at
render time — and a render-time filter is a *second* definition of "the transcript" that will drift
within a quarter, when someone adds a message type, forgets the filter, and a `ToolMessage` full of
internal charge ids goes out in an email.

### The private scratchpad

So the specialist gets its own channel that the rest of the graph cannot see — not by convention, by
schema:

```python
# `scratch` is declared on the SUBGRAPH's state type. It exists here and nowhere else.
class BillingScratch(TypedDict):
    scratch: Annotated[list, add_messages]   # 6 tool calls, 1 retry, 2 dead drafts
    hypothesis: str | None

billing = StateGraph(
    BillingScratch,
    input_schema=SpecialistBrief,     # what this specialist may READ from the parent
    output_schema=SpecialistOutput,   # what it may PROMOTE back to the parent
)
```

The mechanism is that **a channel present in the subgraph's state and absent from the parent's is
never propagated.** There is no code path for `scratch` to reach `messages`, because the parent has
no `scratch` key for it to land in. Nobody can leak it by forgetting a filter; they would have to
change the parent schema, which is a reviewed diff. The output side carries the more interesting
constraint:

```python
class SpecialistOutput(TypedDict):
    final_message: AIMessage                  # SINGULAR — the type forbids two
    new_facts: list[VerifiedFact]
    proposals: list[ActionProposal]
    release: ReleaseReason | None
```

`final_message` is one message, not a list, so a specialist that decides to emit two user-visible
messages in one turn **cannot express that**. It looks like a small thing and it is the reason
parallel fan-out does not produce a transcript where "the agent" replies three times in a row (§5).

```mermaid
flowchart LR
  S1["Billing subgraph — PRIVATE<br/>scratch: 6 tool calls · 1 retry<br/>2 dead drafts · ~4,000 tokens"]
  S1 -->|"output_schema<br/>a compile-time contract"| M["messages:<br/>exactly ONE AIMessage"]
  S1 -->|"extractor, with provenance"| F["facts: 4 VerifiedFacts<br/>one of them negative"]
  S1 -->|"everything, verbatim"| L["Turn ledger + trace"]
  M --> SH["Shared session state"]
  F --> SH
```

### The promotion rule — one of three destinations for everything produced

| Produced inside the specialist | Promoted to shared state? | Where it actually lands |
|---|---|---|
| Tool call requests and raw tool JSON | No | The ledger (immutable) and the trace |
| Intermediate reasoning, drafts, the abandoned hypothesis | No | The trace only |
| The final customer-facing message | Yes | `messages` — exactly one |
| A fact a tool confirmed | Yes | `facts`, with provenance |
| A fact a tool confirmed to be *absent* | Yes | `facts`, with `polarity="absent"` — see §3 |
| A proposed action | No — the *grant* is | `actions`, after the firewall has run |
| "I already asked Dana whether the seat was intentional" | Yes, implicitly | It is already in `messages` |

The rule underneath the table: **promote conclusions, never process.** Everything the specialist
learned is promoted; nothing about how it learned it is. And the process is not thrown away — it
goes to the ledger, where Part 1's compliance question (*"who decided to refund $49, and what did
they have in front of them?"*) is answered from raw payloads rather than a model's summary of them.

**When not to bother.** With one specialist this whole section is overhead — a single agent's
scratchpad *is* its context, and there is nobody to isolate it from. The pattern earns its keep the
moment a second specialist can inherit the first one's context, which is exactly the moment Part 1
said multi-agent starts being justified.

---

## 3. Why `facts` beats passing a transcript

Part 1 said handoffs pass a brief, not a transcript, and that the brief's `verified_facts` field
makes the receiving specialist competent without making it expensive. This is the type behind that
field.

```python
@dataclass(frozen=True)
class VerifiedFact:
    key: str            # "charge.ch_9f44.amount" — the upsert key, stable across re-reads
    value: JSONValue
    polarity: Literal["present", "absent"]   # see below; this is the row people skip
    source_tool: str    # "billing.get_charges"
    source_call_id: str # → a ledger row → the untruncated payload in the archive
    observed_at: str    # ISO-8601, stamped BY THE TOOL LAYER, not by the model
    ttl_class: Literal["immutable", "slow", "volatile"]   # §4
    trust: Literal["structured", "freetext"]              # §6
```

Every field except `value` exists to answer a question somebody will eventually ask. `source_tool`
and `source_call_id` answer *"how do you know?"* with a pointer rather than a paraphrase:
`source_call_id` resolves to a ledger row holding the full 4.2 KB response. The context window
carries 30 tokens; the archive carries the evidence.

`observed_at` answers *"as of when?"*, and it is stamped by the tool wrapper, never computed later.
This matters more than it sounds: **a reducer must be a pure function** — no `datetime.now()`, no
`uuid4()`. If `observed_at` were filled in by the merge, replaying a conversation from its
checkpoints would produce different facts than the original run, and "replay the conversation to see
what the agent knew" stops being a thing you can do.

Two of the four facts from turn 1, concretely:

```python
VerifiedFact("charge.ch_9f44.amount", "4900", "present",
             "billing.get_charges", "tc_a41f", "2026-03-09T09:14:22Z", "immutable", "structured")
VerifiedFact("seat.s_2210.active",    "true", "present",
             "billing.get_plan",    "tc_a420", "2026-03-09T09:14:23Z", "volatile",  "structured")
```

Note `amount` is `"4900"` — cents, as a string. Money is never a float anywhere in this system,
because a value that will be compared against a policy ceiling must not be able to arrive as
`48.999999`. And note that those two facts carry *different* `ttl_class` values, which is §4.

### The comparison that justifies all of this

| What the handoff carries | Tokens | Does the receiver re-ask the customer? | Injection surface | Can it go stale safely? |
|---|---|---|---|---|
| The full transcript | 8,000–15,000, growing every turn | No | **High** — attacker-written ticket bodies and gift messages ride along ([Part 3](EXPLAINED-3-tools-and-safety.md)) | No. The receiver must re-derive facts from prose, and re-derivation is where hallucinated facts enter |
| Only the last message | ~150 | **Yes, constantly** — this is the amnesia complaint from Part 1 | Low | No — there is nothing to go stale |
| `facts` plus the brief | ~400 for 8 to 20 entries | No | Low — structured fields only | Yes, per-fact, via `ttl_class` (§4) |

**A fact with provenance can be cited, audited, and expired. A sentence in a transcript can only be
re-read and re-believed.**

### Negative facts, which almost nobody stores

Turn 1 also established something absent from the four facts above: billing checked for a *third*
charge — Dana said "twice", and a customer who says "twice" is sometimes wrong about the count — and
there wasn't one.

Most implementations promote only positive findings, so that check is simply lost. Two turns later
the coordinator re-leases to returns, returns wonders whether there are other duplicate charges, and
calls `get_charges` again — 340 ms and a tool call to learn something you already knew. Under
fan-out, two specialists each independently spend a lookup discovering the same nothing.

```python
VerifiedFact("charge.INV-771.duplicates_beyond_ch_9f44", "[]", "absent",
             "billing.get_charges", "tc_a41f", "2026-03-09T09:14:22Z", "slow", "structured")
```

That is **20 tokens**. It replaces a 340 ms round trip, and it is what lets the specialist say *"I
checked and there is no third charge"* instead of *"let me look into that"*. **"We looked and it
isn't there" is a finding.** Design the fact type so it can express one — that is what `polarity` is
for — because a type that can only hold positives silently trains your system to forget every
negative result it ever obtained.

---

## 4. Facts go stale, and the system has to know which ones

Look again at the four facts. Three will still be true in a year: a charge that occurred on Mar 3
for $49.00 occurred on Mar 3 for $49.00, permanently. One of them — `seat.s_2210.active` — might be
false in an hour, because Dana could remove the seat from the web console while the email thread is
parked.

Treating those identically is a bug with a customer-visible shape. Suppose the thread resumes
Wednesday and billing, reading `seat.s_2210.active = true` from Monday, tells Dana *"the seat is
still active, shall I remove it?"* — and Dana removed it Monday afternoon. The AI is now visibly
working from stale information, which is the fastest way to lose a customer's trust in it. So each
fact declares a class:

| `ttl_class` | Means | Examples from this conversation | On resume |
|---|---|---|---|
| `immutable` | A historical event. Cannot change. | The two Mar 3 charges, their amounts, the 14:12 enablement time | Kept forever |
| `slow` | Changes on a scale of days | Return window on order #88213, plan tier, RMA state | Kept; refreshed if older than the domain's absolute TTL |
| `volatile` | Changes continuously | Seat active flag, shipment status, carrier scan, inventory | **Expired and refetched** |

Three coarse buckets, not per-fact seconds. Per-fact TTLs sound more precise and are worse: every
new tool needs someone to pick a number, nobody knows the real half-life, and the numbers rot
silently. The consequence is concrete — when Wednesday's message arrives, the resume path expires
the volatile facts and refetches them before the specialist speaks, and that refetch is what
discovers the phone agent's credit (§7).

Two honest caveats. **A shipment status verified on Monday is not a fact on Wednesday, and it is
also not a fact 45 minutes later** — the class buys a refresh policy, not truth. And the buckets are
a guess until you measure how often a refetched volatile fact differs from the cached one.

---

## 5. Concurrent writes, where three specialists collide

Part 1's Conversation B is the case: *"My order #88213 hasn't shipped and I think I got
double-charged for it. Also my teammate can't log in."* Three unrelated problems, so the coordinator
fans out with `Send` — three specialists running at once, each returning updates to the same state.
Part 1 taught you that concurrent replace-writes raise `InvalidUpdateError`. Now we care about
*which* channels that applies to, and about the one channel where the *absence* of an error is the
problem.

```mermaid
flowchart LR
  ARB["Coordinator — Send x3"] --> B["billing"]
  ARB --> O["orders"]
  ARB --> A["account"]
  B --> RED["reduce node<br/>emits ONE synthesised message"]
  O --> RED
  A --> RED
  B -.->|"facts (merge) · budget (add) · actions (append)"| OK["Parallel-safe"]
  B -.->|"lease · case.resolution_state"| ERR["InvalidUpdateError<br/>— the design working"]
  B -.->|"messages"| SIL["NO error at all —<br/>three replies from 'one agent'"]
```

| Channel | Three writers at once? | What actually happens |
|---|---|---|
| `facts` | Safe | `upsert_facts` merges by key, and it is order-independent (below) |
| `budget` | Safe | Field-wise addition — but see the pre-debit note below |
| `actions` | Safe | Append-only, and the firewall is one node anyway, so the writes serialise |
| `lease` | **Bug** | Replace semantics ⇒ `InvalidUpdateError: At key 'lease': can receive only one value per step` |
| `case.resolution_state` | **Bug** | Same. Three branches each declaring the case resolved is not a merge, it is a disagreement |
| `messages` | **Worse** | `add_messages` accepts all three appends *without error*. The graph is delighted. The customer gets three replies |

### The merge that makes `facts` safe

`upsert_facts` cannot be "last write wins", because under `Send` there is no defined order in which
the three branches' updates get applied:

```python
def upsert_facts(old, new):
    """Keep the observation with the greater (observed_at, source_call_id).

    The source_call_id tiebreak is what makes the ordering TOTAL: two tool calls can share
    a timestamp to the millisecond, and then you need a deterministic loser.
    """
    by_key = {f.key: f for f in old}
    for f in new:
        cur = by_key.get(f.key)
        if cur is None or (f.observed_at, f.source_call_id) > (cur.observed_at, cur.source_call_id):
            by_key[f.key] = f
    return sorted(by_key.values(), key=lambda f: f.key)
```

Two properties, both load-bearing, neither held by "last write wins". **Commutative** means merging
A then B equals merging B then A; **associative** means the grouping does not matter. Together they
mean the result is the same however the runtime batches the three branches — which you need, because
you do not control that batching and it changes between framework versions.

### `InvalidUpdateError` on `lease` is the design working

When two branches both write `lease`, you get an exception, and the instinct of every engineer who
has ever seen a red stack trace is to make it stop. The obvious way is to add a reducer — "keep the
newest one", say — and the error goes away.

**That is the actual bug.** A reducer on `lease` does not resolve the conflict, it *hides* it, by
silently picking one of two contradictory grants of authority. You now have a conversation where
billing believes it holds a lease permitting a $200 refund and orders believes it holds one
permitting a $500 reship — and the one that lost is still running, still talking, still proposing.

Keep replace semantics. Keep the coordinator as the sole writer. Let the runtime shout. Here is that
invariant as executable code — from `reference_impl/state.py`, which you can run:

```python
def arbiter_only(current, updates, channel):
    """Single value AND single privileged writer — the control-plane channels."""
    if len(updates) > 1:                          # two branches raced
        raise InvalidUpdateError(f"parallel write to {channel!r} from "
                                 f"{[u.writer for u in updates]}")
    if updates[0].writer != ARBITER:              # ONE write, from the wrong node
        raise InvalidUpdateError(f"{updates[0].writer!r} may not write {channel!r}")
    return updates[0].value
```

Two different failures, one guard, both of them "somebody wrote a channel they do not own."

### The `messages` case has to be prevented, not detected

`lease` protects itself. `messages` does not, because appending three messages is a perfectly
sensible thing to do and `add_messages` cannot know that these three came from branches which each
believe they are the only one speaking. There is no error to catch — just a transcript where "the
agent" reports the Reno warehouse hold, then the authorization hold, then the teammate's reset link,
as three separate turns. A robot with a stutter.

So the defence has to be structural, and §2 already built it: **fan-out branches compile with an**
`output_schema` **that has no** `messages` **key at all.** Only the reduce node, which runs with all
three results in hand, emits a turn — and it emits the numbered three-part answer from Conversation
B.

There is a general shape here worth naming. `lease` is protected by a check, `messages` by a type.
**Use a check when the violation is detectable at runtime; use a type when it is not** — and nothing
at runtime can tell a legitimate append from an illegitimate one.

### One arithmetic trap in `budget`

`budget` is additive, so parallel writes accumulate correctly. That makes it *safe* without making
it *correct*, because check-then-spend against an additive counter is a race. Suppose the dollar cap
is $0.35 and the conversation has already spent $0.28. Three `Send` branches each read `usd = 0.28`,
each compare against $0.35, each pass, and each spend $0.08. The conversation lands at **$0.52** —
49% over a cap that every branch individually respected. The fix is to **reserve before dispatch**:
the fan-out node debits `3 × worst_case_branch_cost` up front, and the reduce node credits back what
was not spent. Post-hoc accounting cannot enforce a cap it learns about after the money is gone.

---

## 6. Memory that outlives the conversation

Everything so far is scoped to one conversation. When Dana writes in again in June, none of it is
there — the checkpoint holds what the *graph* needs to resume, and this conversation resolved in
March. Long-term memory is a different store with different rules. In LangGraph it is the `Store`: a
key-value namespace that spans threads, keyed here as `(tenant_id, "customer", customer_id, kind)`.

You now have three places data lives, and conflating any two of them is a distinct bug. **The
checkpointer holds what the graph needs to resume. The Store holds what the customer needs
remembered. The ledger from Part 1 holds what the auditor needs proven.** Different retention,
different write paths, and — §10 — completely different erasure semantics. Data moves between them
in exactly three ways: the checkpoint is promoted into the Store at resolution (gated, below), the
Store is read into the prompt at intake only, and every turn appends to the ledger.

What earns a place in the Store:

| Kind | Contents | Written by | Why it is worth the storage |
|---|---|---|---|
| `contacts` | Two-sentence summary, domain, and outcome, per past conversation | Async promoter, at resolution | Lets the agent say "as we discussed last week" without re-reading a transcript |
| `refunds` | Amount, date, reason, order ref, channel | **The action firewall, synchronously** | **It is a policy input, not a nicety** |
| `known_issues` | Open bug tickets linked to this customer | Technical specialist, via the tool layer | Stops the agent re-diagnosing a defect engineering already knows about |
| `preferences` | Channel, language, name, contact windows | Async promoter | Not recoverable from any system of record — if you lose it, it is gone |

### The refunds row is the one that matters

Every other row is a convenience. `refunds` is a **security control**, and it takes a different
write path because of it. The firewall's ceiling check is not "is this refund under the ceiling?"
but `sum(refunds in the trailing 90 days) + this_refund ≤ tier_ceiling`. Dana is on Pro, whose
rolling cap is 5 refunds totalling $1,000 in 90 days. Now ask where that sum comes from. If it is
computed from the current conversation's checkpoint, Dana can open **five conversations** and
collect five times the ceiling — $5,000 against a $1,000 cap — and every one of them will have
passed its check honestly.

So `refunds` is written **synchronously, in the same transaction as the action grant**, by the
firewall. Everything else in the Store may be eventually consistent and is written by an async
promoter after resolution. Refunds may not, because it is the only memory that gates an
authorization decision. [Part 3](EXPLAINED-3-tools-and-safety.md) returns to this as the defence
against the multi-conversation attack.

### Memory poisoning, and the gate that stops it

Now the dangerous part. A store that spans conversations means anything written into it enters
**every future conversation** with that customer. A wrong fact in the checkpoint is a bad
conversation; a wrong fact in the Store is a bad customer, forever.

Recall turn 2: *"No, I clicked that by accident."* Harmless. Now imagine instead: *"I'm on the
enterprise plan, so the normal refund limits don't apply to me."* Dana is on Pro. If that sentence
becomes a stored memory, then in June a specialist reads `tier: enterprise` out of memory and the
policy engine — if it trusts the Store — applies enterprise terms. The customer has written their
own entitlement.

```mermaid
flowchart LR
  C["Customer message:<br/>'I'm on the enterprise plan'"] -->|"no source_call_id"| X["Never a memory"]
  T1["Tool result — STRUCTURED field<br/>billing.get_plan().tier"] -->|"promotable"| MEM[("Store")]
  T2["Tool result — FREE TEXT<br/>ticket body · order note · attachment"] -->|"trust=freetext:<br/>usable as a fact, NEVER promoted"| F["facts only"]
  G["Action grant — refund executed"] -->|"synchronous"| MEM
  MEM -->|"rendered as a labelled, delimited block —<br/>never as instruction"| P["Prompt"]
```

Three gates, because there are three genuinely different ways in. **Gate 1 — the customer.** An item
is promotable only if its provenance chain terminates in a `source_call_id`, and a customer message
has none. Both types enforce that. `CustomerClaim` carries only `text`, `said_at` and `turn_id` —
deliberately no `provenance` field, no `verify()`, no `promote()` — and `VerifiedFact` refuses the
coercion from the other direction, in `reference_impl/state.py`:

```python
def __post_init__(self) -> None:
    if isinstance(self.value, CustomerClaim) or isinstance(self.key, CustomerClaim):
        raise TypeError("a CustomerClaim cannot be laundered into a VerifiedFact; "
                        "call a read tool and cite its Provenance instead")
    # Even a fact that CLAIMS provenance is rejected if the claimed source is a channel
    # a human can write into: customer, user, chat_message, email_body, ticket_body, upload.
    if self.provenance.tool in UNTRUSTED_SOURCES:
        raise ValueError(f"{self.provenance.tool!r} is an untrusted channel; it cannot mint facts")
```

**Gate 2 — injected text that arrives through a tool.** Gate 1 misses this entirely: an order note
or a ticket body *does* have a `source_call_id`, because a real tool really did return it. So facts
extracted from free-text fields carry `trust="freetext"` and are **never promotable to the Store** —
usable as facts within the conversation, never as memory. Only *structured* fields of a tool
response can be promoted. [Part 3](EXPLAINED-3-tools-and-safety.md) walks an attack living inside
this gap.

**Gate 3 — the read side.** Memories render into the prompt inside a delimited, labelled block never
treated as instruction, and carry `written_at` and `source_call_id` so a specialist can discount an
old one. Every item also carries `last_confirmed`, and anything not re-confirmed within 12 months is
dropped, because **memory that is never retired eventually lies.**

One discipline that is easy to miss: even for a legitimately promotable fact, ask whether you
should. Dana's plan tier has a `source_call_id` and is structurally promotable. It is also one cheap
tool call away, and it changes when Dana upgrades. **Anything you can cheaply read live should be
read live, not remembered.**

---

## 7. Surviving the two-day pause

Now the part that only email threads teach you, because chat sessions are too short to expose it.
Part 1 explained that the checkpointer plus a `thread_id` lets a conversation pause and resume
across a redeploy. That is the mechanism. Here is the policy question the mechanism does not answer:
**when Dana's reply arrives on Wednesday, is Monday's lease still valid?**

### Two expiry times, because there are two different questions

A lease carries both. The **idle TTL** measures time since the last turn and answers *"are the facts
this lease was granted against still likely to hold?"* The **absolute TTL** measures time since the
lease was granted and answers *"has this authority grant simply been alive too long?"* You need
both, because either alone has a hole: idle TTL alone lets a chatty customer hold one lease for a
week, since it never goes idle long enough, and absolute TTL alone lets a conversation sit untouched
for 23 hours and then resume on day-old facts.

And here is the design choice worth arguing about: **the idle TTL is not a measure of the customer's
attention span. It is a measure of the half-life of that domain's facts.**

| Lease holder | Idle TTL | Absolute TTL | Why that number |
|---|---|---|---|
| Billing | 4 h | 24 h | Invoice and charge state moves slowly — nightly batches, not live events |
| Orders & Shipping | 45 min | 12 h | Carrier scans and stock exceptions move continuously |
| Technical | 12 h | 48 h | Advisory work; the "facts" are docs, which do not change under you |
| Account & Identity | 30 min | 8 h | A stale identity lease is a security posture, not an inconvenience |
| Returns | 4 h | 24 h | RMA windows move by days |

Notice what is *not* in that table: the channel. Chat and email get the same TTLs, because an
order's shipping status does not care how the customer is contacting you. So **an email conversation
about a shipment almost always re-triages on resume, and that is correct behaviour rather than a
defect** — a 45-minute idle TTL against round trips measured in hours means nearly every resume is a
fresh lease. Track re-triage rate per channel as an expected distribution you watch for drift, never
as an alert.

### What re-triage actually does

When a message lands on a parked thread, the freshness gate asks one question with three clauses: is
the lease absent, or is `now - last_turn` past the idle TTL, or is `now - granted_at` past the
absolute TTL? If none fire, you continue on the existing lease — Part 1's two-call hot path,
unchanged. If any fires, five things happen in order: invalidate the lease, expire every volatile
fact, sweep for actions still in flight, re-triage, and only then mint a new lease. Two of those
steps are easy to get wrong.

**Re-triage reads the whole conversation, not the new message.** Dana's Wednesday reply might be
*"any update?"*, which is unroutable in isolation. Triage on that string alone is a coin flip.

**Re-leasing the same domain must be free in hops.** Part 1's lease carries a hop budget to stop
ping-pong — billing releasing to orders releasing back to billing. But returning to the specialist
you were already talking to, after a pause, is not ping-pong. Charge a hop for it and long email
threads escalate for the crime of being email: at four hops and a 45-minute idle TTL, a shipping
conversation over email escalates on the fourth customer reply, every time.

### The worked timeline

```mermaid
sequenceDiagram
  autonumber
  participant U as Dana (email)
  participant IN as Intake
  participant LM as Lease manager
  participant CO as Coordinator
  participant BIL as Billing specialist
  participant FW as Action firewall

  Note over U,FW: Mon 9 Mar 09:14 — "I was charged twice for INV-771"
  U->>IN: inbound email, thread_id = tnt-42:conv-88213
  IN->>CO: triage — single domain, billing
  CO->>LM: grant lease, billing, idle 4h, absolute 24h, hop 1
  LM->>BIL: LEASED
  BIL->>BIL: get_charges, get_plan — 4 facts, one of them negative
  BIL-->>U: "One is your Pro renewal, one is a seat add-on. Was that intentional?"
  Note over U,FW: checkpoint #7 — lease live, 4 facts, $0.031 spent, 6.2s active
  Note over U,FW: Mon 09:14 → Wed 11:02 PARKED — 0 workers, 0 tokens, 0 active clock
  U->>IN: Wed 11 Mar 11:02 "No, I clicked it by accident. Please refund."
  IN->>LM: lease freshness gate
  LM-->>IN: EXPIRED — idle 49h48m over 4h, absolute 49h48m over 24h
  IN->>CO: re-triage over the FULL history, expire volatile facts, refetch
  Note over CO: NEW FACT — a $49.00 credit was issued Tue 16:20 by a phone agent
  CO->>LM: re-lease billing, same domain, so hops stay at 1
  LM->>BIL: LEASED — 3 immutable facts, 1 refreshed, 1 new
  BIL->>FW: propose_action(refund, $49.00, INV-771)
  FW->>FW: target-scoped duplicate check, not just the conversation key
  FW-->>BIL: DENY — a matching grant was executed Tue 16:20
  BIL-->>U: "You were already credited $49 on Tuesday, arriving in 5-7 business days."
```

Work through the arithmetic. Monday 09:14 to Wednesday 11:02 is 48 hours plus 1 hour 48 minutes =
**49 h 48 m**. Against billing's 4-hour idle TTL that is 12.5× over; against the 24-hour absolute
TTL it is 2.1× over. Either alone would have expired it, and both firing is not redundancy — it says
*two* different things are wrong. The absolute breach is why re-triage cannot be skipped even if
Dana had replied at 12:00 on Monday and again on Wednesday.

### The detail the whole timeline exists to teach

On Tuesday at 16:20, a human phone agent issued Dana the $49 credit — different agent, different
channel, **different conversation entirely.** Part 1 mentioned idempotency in passing; here is why
the obvious version of it fails. The natural key is
`sha256(f"{conversation_id}|{action_type}|{target_id}|{amount_cents}")` — which is
**conversation-scoped**, so it catches replays of *this* conversation's own actions and nothing
else. The phone agent's refund happened under a different conversation id, so it hashes to a
different key, so this check passes cleanly and Dana gets refunded **twice**.

The fix is a second, **target-scoped** check: before executing, ask the action-grant store whether
any grant exists for `(target_id, action_type)` in a recency window, regardless of which
conversation created it — and cross-check the refund history in the Store from §6.

Two checks, two jobs, and you need both:

| Check | Scope | Catches | Misses |
|---|---|---|---|
| Idempotency key | This conversation | Retries, checkpoint replays, the customer double-tapping Confirm | Anything done anywhere else |
| Target duplicate check | This order or invoice, any channel | The phone agent, the self-serve portal, another conversation | Two genuinely distinct refunds on one order — which is why a human can mint an override nonce |

**A design with only the conversation-scoped key double-refunds every customer who also called the
phone line**, and it will never fail in testing, because no test opens a second channel. One more
thing the resume path owes you before letting a specialist speak — the "idempotency sweep" above: if
any action from before the pause is still recorded as *in flight*, it must be reconciled against the
downstream system, not re-issued and not assumed away. [Part 3](EXPLAINED-3-tools-and-safety.md)
walks that crash window step by step.

---

## 8. Budgets, and the one that everybody gets wrong

A conversation with no cap can spend an unbounded amount of your money on one customer. Six caps,
all counted in state, all checked *before* the step runs. Checking before is the whole point: a
budget checked afterwards is telemetry, which tells you what you already spent. A budget checked
before is a control.

| Budget | Cap | Where the number comes from | What happens on breach |
|---|---|---|---|
| **Turns** | 30 soft, 40 hard | p90 is 11 turns and p99 is 30. Past 30 you are outside 99% of real conversations, and the useful prior is "stuck", not "thorough" | Warn, then force the coordinator to take the conversation back, then escalate |
| **Hops** (lease grants) | 4 | A genuinely three-domain problem — Conversation B — needs 3. A fourth handoff is ping-pong | Force arbitration at 3, escalate at 4 |
| **Tool calls** | 25 | The whole read inventory is about 25 tools across five domains. At 25 calls you could have called every tool once | Drop to cached reads, then arbitrate |
| **Tokens** | 60K in + out | After compaction a prompt is ~2.5K, two model calls per turn, 11 turns at p90 ≈ 55K. The cap lands just above p90, so it bites on the tail and nowhere else | Force compaction (§9), then degrade to a cheaper model |
| **Active wall-clock** | 8 min | Not elapsed time. See below — this is the one people get wrong | Escalate on chat; park and retry on email |
| **Dollars** | $0.35 | 3× the $0.11 blended per-conversation target. A conversation costing triple the average is not a cost problem, it is a signal that something is looping | Degrade model tier, then arbitrate, then escalate |

The response ladder is *warn → cheaper model → force arbitration → escalate to a human*, but **which
cap you hit selects where on the ladder you enter.** The hop cap is a ping-pong signal and jumps
straight to arbitration — a cheaper model will not fix two specialists disagreeing about whose
problem this is. The dollar cap is usually a verbosity signal, and a cheaper tier genuinely fixes
it. One rung is never available: **degrading the coordinator's model.** It runs once per
conversation, and it is the thing deciding whether to spend a human. Save $0.004 there and you spend
$40 of agent time on the escalation it got wrong.

### Wall-clock has to count active time

This is the one that ships broken. The natural implementation of an 8-minute wall-clock budget is
`now - conversation.started_at`. Run that against Dana's thread: started Monday 09:14, now Wednesday
11:02, elapsed 49 h 48 m = 2,988 minutes. Against an 8-minute cap that is **373× over**, so the
conversation escalates to a human the instant Dana replies.

And then *every email conversation in the system escalates at the two-day mark*, for no reason
connected to anything going wrong. Your containment rate collapses, your escalation queue fills with
healthy conversations, and the metric that looks broken is the one for the human team.

The counter has to accumulate only while a worker is actually holding a step. In Dana's thread that
is 6.2 s on Monday (intake 0.2 + triage 1.1 + two tool calls 0.7 + two model calls 4.2) and 9.4 s on
Wednesday (re-triage 1.4 + three refetches 1.1 + two model calls 4.8 + firewall 2.1) — **15.6
seconds out of a 480-second budget, 3.3% used.** A conversation parked for six weeks has burned
**zero** wall-clock, which is exactly right. What ends a conversation like that is the absolute
lease TTL from §7, not the budget. **They are different mechanisms answering different questions,
and using one for the other's job breaks both.**

---

## 9. The 30-turn conversation and what compaction may never delete

At p99, a conversation runs 30 turns. Say the transcript reaches 40 messages averaging 350 tokens —
**14,000 tokens** — and the model reads all of it on every call. At two calls a turn, the last six
turns would read 12 × 14,000 = **168,000 tokens**, blowing the 60K budget almost three times over.

So you compact. A compaction node rewrites `messages` down to one summary message (~600 tokens), the
last 6 messages verbatim (~350 each, so 2,100), and the pinned original problem statement (~120) —
**≈ 2,820 tokens.** The remaining twelve calls now read 12 × 2,820 = 33,840 tokens instead of
168,000, a saving of about **134,000 tokens**, which is the difference between finishing the
conversation and hitting the cap at turn 26.

Two implementation notes that are cheap to state and expensive to learn. **Return a replacement, not
an append** — appending a summary to the channel you are shrinking makes it *longer*. And
**compaction is a node, never a reducer**: it calls a model, so a replay produces a different
summary, which is tolerable in a node and fatal in a reducer (§3's purity rule).

### The floor — what the summariser is structurally incapable of deleting

| Must survive compaction | Why it matters | Where it is protected |
|---|---|---|
| Verified facts | Re-deriving them costs a lookup and risks a hallucinated variant (§1) | `facts` — a separate channel, structurally immune |
| Actions taken | Forgetting "$49.00 refunded, RF-88213" risks a duplicate and a furious customer | `actions` |
| Policy decisions, **including denials** | Otherwise the agent contradicts its own refusal two turns later, and the customer screenshots both | `actions` — a denial is a grant with `decision = DENY` |
| **Active promises to the customer** | *"I'll email you when it ships"*, *"we'll waive next month's fee"* | **Nothing, by default** |
| The last 6 turns, verbatim | Pronoun resolution — this is why turn 2's *"that"* still resolves | Tail-preserving compaction |
| The original problem statement | Every summary drifts, and this is what the outcome is graded against | Pinned first user message |

Four of those six survive because they live in their own channel. They are not protected by careful
prompting of the summariser; they are protected by not being in `messages` at all.

The fourth row is the point of the table. **Promises to customers have no channel, so they get
summarised away.** A specialist tells Dana on turn 9 *"I'll email you the moment the seat removal
posts"*, compaction runs at turn 24, and the summary says "discussed seat removal and refund". The
promise is gone — nothing broke, no error fired, and three days later Dana writes in angry about an
email that never came, with nobody able to find where the system agreed to send it.

> **Anything that must survive compaction needs its own channel. If it exists only as prose in**
> `messages`**, it will eventually be deleted.**

Which means promises need a channel — `case.commitments`, extracted at the end of each turn by a
small deterministic pass, not left to the summariser's judgement. Read the table above as a
*template*: whatever else your product commits to in prose, find it and give it a home before
compaction finds it first.

---

## 10. Erasure, when the ledger is append-only on purpose

Dana emails asking for her data to be deleted. This is genuinely hard, and the reason is a direct
collision between two requirements you already agreed to:

- Part 1's turn ledger is **append-only and immutable**, because that is what makes it an audit record.
A ledger you can edit answers no compliance question.
- Erasure requires deleting personal data **everywhere**, including the ledger.

Those are irreconcilable if the ledger contains personal data, so the design ensures it never does.

### Crypto-shredding

So encrypt the personal data with a key held per subject, and erase by **destroying the key**. The
ledger row still exists, its hash chain is intact, its ciphertext is still there and is now
permanently unreadable. Mechanically: at intake the identity resolver mints a `subject_id` and a
per-subject key held in a vault; checkpoints and ledger rows store ciphertext plus that pseudonymous
`subject_id`; erasure destroys the key, deletes the subject's threads, and purges their Store
namespace.

The load-bearing part is the split inside the ledger row. These fields are **never encrypted**:
`action_id`, `amount_cents`, `policy_decision`, `approver_role`, `timestamp`, `idempotency_key`.
None of them is personal data once the customer identifier is a pseudonym. So after Dana's erasure
an auditor can still ask "how many refunds over $200 did the system approve in March, under which
policy version?" and get a complete answer, while the row can no longer be connected to Dana. **This
only works if the ledger schema was split that way on day one** — you cannot retrofit the split onto
an append-only store, because by definition you cannot rewrite the rows that already have a name in
them.

Erasure is then a runbook across six places, not one delete statement — and the honest version of
the table includes what does not fully work.

| Location | Mechanism | Residual risk |
|---|---|---|
| Checkpoints | Destroy the key **and** delete every thread belonging to the subject | Needs a `subject_id → thread_id[]` index. Build it, or erasure is a table scan across every conversation you have ever had |
| Store | Purge the namespace prefix `(tenant, "customer", cust_88213)` | Dana's id cross-referenced inside *another* customer's memories — shared team accounts do this constantly |
| Ledger | Crypto-shred; the cleartext audit fields survive | Correlation attacks across timestamps and amounts |
| Traces and observability | Redact at emit, keep retention short | The usual leak: one tool payload logged verbatim by a debugging change that shipped |
| Warehouse exports | Subject-keyed deletion job | The copy somebody pulled into a notebook |
| **Embeddings** | Delete by subject key | **Derived personal data. You cannot un-embed a vector** |

And the honest caveat, which belongs in the design review and not in a pull request description:
**crypto-shredding is accepted as erasure by most supervisory authorities, but that is a legal
judgement, not a technical fact.** Get counsel to sign the design.

---

## 11. What to take away

**1. Derive the state from the conversation, not from a schema template.** Walk the turns, ask what
would break if the system forgot, and the channels appear with their owners already attached. A
schema you copied has channels nobody owns, and an unowned channel is where the concurrency bugs
live.

**2. Decide what the customer sees versus what the specialist scratches on, and enforce it with
schema.** A subgraph-local channel cannot leak into the shared transcript, because the parent has no
key for it to land in. Convention will fail here, and it will fail in the customer's chat window.

**3. Store facts with provenance — including negative facts — and give each a staleness class.** A
fact citing its tool, call id and observation time can be audited, quoted and expired; a sentence in
a transcript can only be re-read and re-believed. "We checked and it isn't there" is a finding worth
20 tokens. And three coarse buckets — immutable, slow, volatile — let resume expire the volatile
ones, which is the refetch that catches the change you did not know about.

**4. A concurrent-write error under fan-out is the design working.** Adding a reducer to silence
`InvalidUpdateError` on `lease` is the real bug: it picks one of two contradictory grants of
authority and tells nobody. The channel that should scare you is `messages`, which raises nothing
and hands the customer three replies — so it has to be prevented by type, not caught at runtime.

**5. Long-term memory becomes a security surface the moment it feeds a policy check.**
Cross-conversation refund history is what stops the five-conversations-until-one-agent-agrees
attack, which means poisoning that memory raises your own future ceiling. Only tool-verified
*structured* fields get promoted — free text that arrived through a real tool call is still free
text.

**6. Split idle TTL from absolute TTL, and count active time, not elapsed time.** Idle TTL asks
whether the facts still hold; absolute TTL asks whether the grant has lived too long. And an
8-minute *elapsed* budget escalates every email conversation on day two — Dana's thread used 15.6
seconds of active time across 49 hours and 48 minutes.

**7. Scope idempotency to the target, not just to the conversation.** A conversation-scoped key
catches replays and nothing else. The phone agent who refunded Dana on Tuesday was in a different
conversation, and only a target-scoped check sees them.

**8. Anything that must survive compaction needs its own channel.** Facts, actions and denials
survive because they have homes. Promises to customers do not, which is exactly why naive
implementations lose them — and losing a promise costs you the customer even when everything else
worked.

**9. Design the audit record pseudonymous on day one.** Crypto-shredding lets an append-only ledger
coexist with erasure, but only if the audit-relevant fields never contained a name, and you cannot
retrofit that split onto rows you can no longer rewrite.

---

## Where to go next

[Part 3 — Tools, Money, and Adversaries](EXPLAINED-3-tools-and-safety.md) takes the `propose_action`
boundary from Part 1 and the fact/claim split from this part, and turns them into the thing that
actually stops a wrong refund: eight deterministic gates, one write credential, and a policy engine
no amount of persuasion can reach. The dense reference versions of this material are
[docs/04-agent-runtime.md](docs/04-agent-runtime.md) and
[docs/05-state-and-memory.md](docs/05-state-and-memory.md). The lease, the fact/claim types, the
channel reducers and the `InvalidUpdateError` invariant are all runnable:

```bash
python3 reference_impl/state.py
```

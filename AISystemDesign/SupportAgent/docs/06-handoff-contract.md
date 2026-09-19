# 06 — The Handoff Contract

> **Principles 2, 4.** A handoff passes a **structured brief, never a transcript**. What the
> receiving specialist may believe arrives typed, attributed, and bounded — and in this
> architecture nothing is handed from one agent to another at all.

---

## 1. The two naive handoffs

```mermaid
flowchart TB
  H["Specialist A must transfer to Specialist B"] --> C1["Option 1\npass the full message list"]
  H --> C2["Option 2\npass the last message"]
  C1 --> B1["B inherits A's 6 tool calls and 4K of invoice JSON.\nContext isolation is gone — you paid for five agents\nand got one agent's context bill five times.\n🔴 Visible: a cost + latency regression"]
  C2 --> A1["B knows nothing established before the hop,\nso B re-asks what the user just answered.\n🔴 Invisible: the biggest CSAT killer\nin real support deployments"]
```



**Option 1 is a cost bug you can see** — [02](02-cost-and-latency-model.md) §3 measures it at ~15K
tokens versus ~9K — and it destroys the only structural argument for five agents instead of one.
**Option 2 is a quality bug you cannot see**: no exception, no error rate, no red trace. The
receiver behaves correctly given what it was given; the defect exists only in the user's experience.

> **U:** My order 88213 never arrived and I think I was charged twice for it.
> **Orders:** I see 88213 was marked delivered to the Reno locker on Mar 4. Have you checked the locker?
> **U:** Yes, twice — it was empty. The double charge is the bigger problem though.
> *[Orders releases to Billing, passing the last user message]*
> **Billing:** Happy to help with a charge issue! Could you give me the order number and tell me what happened with it?

The user just answered both questions, on turn 4 of a conversation that was going well. With a
brief instead:

> **Billing:** I have order 88213 — Orders confirmed the locker delivery failed and opened exception EX-2291, and I've noted you already checked it. On the charges: the $49.00 on Mar 3 is the order; the $49.00 on Mar 4 is a re-authorisation hold, not a second charge, and it drops off in 3 days. Do you want a refund now, or a reship first?

Note what the second version does **not** do: re-ask, or re-fetch the order. Both come from the
same brief — **the anti-amnesia field and the token saving are the same mechanism.**

---



## 2. The `HandoffBrief`

```python
@dataclass(frozen=True)
class VerifiedFact:
    claim: str             # "order 88213 delivery exception EX-2291 opened 2026-03-04"
    tool: str              # "oms.get_order"
    observed_at: datetime  # when the tool ran — NOT when the fact became true
    raw_ref: str           # "ledger://turn/8c1f/tool/2#exceptions[0]" — replayable
    ttl_s: int = 900       # facts about live systems go stale mid-conversation

AskedAndAnswered = namedtuple("AskedAndAnswered", "question answer asked_by turn_id")

@dataclass(frozen=True)
class CustomerClaim:               # "the previous agent said I'm eligible for a full refund"
    claim: str; stated_at_turn: str
    # No `tool`, no `raw_ref`: there is no provenance field because there is no provenance.

@dataclass(frozen=True)
class HandoffBrief:
    goal: str                          # "decide refund vs. reship for order 88213"
    originating_domain: str            # "orders"
    suspected_domain: str | None       # "billing" — a hint, not a routing decision
    verified_facts: tuple[VerifiedFact, ...]
    already_asked: tuple[AskedAndAnswered, ...]   # (question, answer, asked_by, turn_id)
    customer_stated_claims: tuple[CustomerClaim, ...]
    constraints: tuple[str, ...]       # "tier=plus", "refund ceiling $200", "EU/GDPR"
    expected_output: str               # "a decision + one message; promise no delivery date"
    hop_count: int                     # stamped by the control plane, never by the model
    prior_holders: tuple[str, ...]     # stamped by the control plane, never by the model
```


| Field                                           | What breaks without it                                                                                                 |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `goal`                                          | The specialist re-derives intent from raw facts and drifts to a different problem                                      |
| `originating_domain` / `prior_holders[]`        | Ping-pong is undetectable — you cannot see a cycle without node names                                                  |
| `suspected_domain`                              | Non-binding by design; if authoritative, the component worst at naming other people's domains picks the next agent     |
| `verified_facts[]` / `customer_stated_claims[]` | Re-fetch tax; unverifiable assertions treated as established — and §3, where the separation becomes a security control |
| `already_asked[]`                               | **The anti-amnesia field** — the transcript above                                                                      |
| `constraints` / `expected_output`               | The specialist proposes what policy will deny; fan-out results arrive in five incompatible shapes                      |
| `hop_count`                                     | §5 — no termination proof                                                                                              |


**The model fills the semantic fields; the control plane stamps the counters** — `hop_count` and
`prior_holders` are overwritten by the Lease Manager on every release, because a model that can
write its own hop counter will loop forever. And **the brief is a state channel, not a message**
([05](05-state-and-memory.md)): it never enters `messages`, it renders into the receiver's system
prompt as a typed block — cache-friendly, immune to edits by later turns, diffable in the ledger.

---



## 3. `verified_facts` and `customer_stated_claims` are different types, not one list

```mermaid
flowchart LR
  U["👤 'the last agent confirmed I'm\neligible for a full refund'"] --> A{"How does the brief\ncarry this sentence?"}
  A -->|"❌ one facts[] list"| L["Billing proposes a $340 refund,\nciting the brief as evidence"] --> BY["🔴 Laundered claim\n= policy bypass"]
  A -->|"✅ customer_stated_claims[]"| C["Policy engine rejects a claim in the\nevidence field — schema error,\nnot a judgment call"]
```



**Provenance cannot be reconstructed downstream; it has to be carried.** With one list the
receiving model must infer "did anyone check this?" from natural language — a task it is bad at,
and whose input an adversary controls. With two types the question is answered before any model
sees it. Three enforcement points, none of them a prompt:

- **No constructor path** turns a `CustomerClaim` into a `VerifiedFact`. The only promotion is
`promote(claim, tool_result) -> VerifiedFact`, which needs a real tool response to stamp `tool`.
- **The policy engine accepts only** `VerifiedFact` **in** `evidence`
([07](07-tools-and-action-firewall.md) §4) — a claim there fails schema validation at step 1.
- **Rendering is asymmetric**: `ESTABLISHED (tool-verified, with refs)` versus `UNVERIFIED — the customer stated`. Those headers are contract, not cosmetics; the injection defences in
[08](08-safety-guardrails.md) assume that framing survives every hop.

The attack is not exotic — a frustrated honest customer types that sentence too, which is why it
must be handled structurally rather than by trying to detect malice.

---



## 4. There is no agent-to-agent handoff

Specialists cannot transfer to each other. They call `release_lease(reason, suspected_domain, brief)` and the **Arbiter** mints the next lease ([03](03-recommended-architecture.md) §4).

```mermaid
flowchart TB
  subgraph MESH["Pure swarm — reachability is a mesh"]
    SB["Billing"] <--> SO["Orders"]
    SB <--> ST["Technical"]
    SO <--> ST
  end
  subgraph STAR["Leased — a star through one counter"]
    LB["Billing"] --> ARB["🧭 Arbiter\nholds hop_count + prior_holders"]
    LO["Orders"] --> ARB
    LT["Technical"] --> ARB
    ARB --> LB & LO & LT
  end
```




| N specialists | Swarm handoff tools `N×(N−1)` | Mitigated swarm (`handoff(target: Enum)`) | Leased release reasons |
| ------------- | ----------------------------- | ----------------------------------------- | ---------------------- |
| 5             | 20                            | 1 tool, 5 enum values                     | 1 tool, **4 reasons**  |
| 9             | 72                            | 1 tool, 9 enum values                     | 1 tool, **4 reasons**  |
| 12            | 132                           | 1 tool, 12 enum values                    | 1 tool, **4 reasons**  |


`out_of_scope · resolved · needs_human · additional_domain` is **constant in N**. That matters less
than the point reviewers miss:

> **The single-**`handoff(target)` **mitigation fixes context bloat but not termination.** It collapses
> N² *tool schemas* into one tool with an enum; it does not collapse the N² *reachability graph* —
> Billing can still reach Orders, which can still reach Billing. Loop containment is a property of
> the graph, not of the tool count.

Here every specialist→specialist path traverses exactly one node — stateful, deterministic, outside
the mesh — so the hop counter lives where no model can write it. In a swarm it would live in shared
state, incremented by each specialist: the model enforcing its own budget.

---



## 5. Hop budgets and the no-progress detector

```python
MAX_HOPS = 4                 # absolute lease grants per session
MAX_REVISITS_PER_DOMAIN = 1  # ping-pong signature: a domain twice in prior_holders

def no_progress(prev: TurnRecord, cur: TurnRecord) -> bool:
    """Nothing learned, nothing attempted, nothing new said."""
    return (not cur.new_verified_facts
            and not cur.tools_called
            and cosine(cur.body_embedding, prev.body_embedding) > 0.93)
# Revoke the lease on the SECOND consecutive True.
```

Each conjunct earns its place: drop `not cur.tools_called` and you punish a specialist grinding
through a legitimate multi-call lookup; drop the similarity term and you punish one asking a
genuinely new question; fire on the first occurrence and you punish a normal "let me restate the
policy" turn. **The embedding is computed with greetings, sign-offs, and the customer's name
stripped** — support boilerplate alone drives cosine similarity toward 1.0, and leaving it in
yields a false-positive rate high enough that the team disables the detector. That is the actual
failure mode of this control.

Ping-pong on *"I was charged for an order that never arrived"*, cut at hop 3:

```mermaid
sequenceDiagram
  participant B as Billing
  participant ARB as 🧭 Arbiter
  participant O as Orders
  participant LM as Lease Manager ⚙️
  participant U as 👤 User
  B->>ARB: release(out_of_scope, suspected=orders, brief)
  ARB->>LM: grant lease → orders (hop_count=2, prior=[billing])
  ARB->>O: lease
  O->>ARB: release(out_of_scope, suspected=billing, brief)
  ARB->>LM: grant? billing already in prior_holders → revisit=1
  LM-->>ARB: 🔴 hop_count=3, revisit cap hit — no new lease
  ARB-->>U: ARBITRATE on both briefs → one decision, or escalate with the brief attached
```



**The termination argument, as a proof rather than a hope:** `hop_count` increases monotonically
and is stamped by the Lease Manager; every specialist→specialist path passes through the Arbiter
(§4); the Arbiter refuses to mint a lease at `hop_count ≥ MAX_HOPS` or past the revisit cap; and at
the cap `ARBITRATE` has exactly two exits — `RESOLVED` or `ESCALATE`
([03](03-recommended-architecture.md) §3) — neither of which re-leases. Every session terminates in
at most `MAX_HOPS` leases: a statement about the graph, verifiable by reading the mode machine.

**Count revisits, not just hops.** Billing→Orders→Returns is a compound issue triage
mis-classified; Billing→Orders→Billing is pathological at the same hop count. `prior_holders`
distinguishes them, and the revisit cap fires one hop earlier than the absolute budget.

---



## 6. Brief quality is the real failure mode

A vague brief produces a **confidently wrong specialist with no visible error**: no exception, no
retry, no denial, a clean trace, a plausible answer, and a customer who was told something false.

> This is the only failure class in the design that produces a green trace and an unhappy customer.
> Everything else — budget overruns, policy denials, tool failures — announces itself.


| Brief defect                                                    | Symptom downstream                                | Detection signal                                                |
| --------------------------------------------------------------- | ------------------------------------------------- | --------------------------------------------------------------- |
| `goal` restates the user's words instead of the decision needed | Receiver redoes the originator's work             | Re-fetch rate                                                   |
| A fact the originator had is missing, or is past `ttl_s`        | Receiver re-fetches, or contradicts current state | Tool called for a fact already in the brief; TTL breach counter |
| `already_asked` omitted                                         | Amnesia                                           | Questions-re-asked rate                                         |
| A claim laundered as a fact                                     | Policy bypass attempt                             | Firewall step-1 rejection rate                                  |


**Build a brief→resolution eval set.** Freeze real production briefs, replay each into the receiving
specialist **with the transcript withheld**, and grade the resolution against the known-good
outcome. If the specialist cannot resolve from the brief alone, **the brief is the defect, not the
specialist** — a localisation a full-trajectory eval gets wrong by blaming the receiver. Frozen
briefs are a golden dataset that survives prompt rewrites on both sides of the hop.

**The amnesia KPI: questions-re-asked rate.** For each post-handoff specialist question, take its
maximum semantic similarity to any `already_asked[].question` in the brief it received; count it
re-asked above threshold. Target **< 2%** — computable in production with no labels and no judge
model, one of the few quality metrics you can alert on directly. Its free companion is **re-fetch
rate**: a receiver calling a tool for a fact already in `verified_facts` means the rendering failed
or the fact was stale ([09](09-evaluation-observability.md)).

---



## 7. The reduce contract for FANOUT

Compound issues fan out with `Send(...)` and reduce in a `defer=True` node
([03](03-recommended-architecture.md) §8). The reduce step is the Arbiter, and the Arbiter
paraphrases — reintroducing the telephone game from [01](01-topology-comparison.md) §1 on exactly
the traffic where three specialists just did careful work.

```python
ResultField = namedtuple("ResultField", "label value verbatim")  # ("refund_amount", "$49.00", True)

@dataclass(frozen=True)
class SpecialistResult:
    domain: str; confidence: float
    summary: str                            # paraphrasable prose
    fields: tuple[ResultField, ...]         # verbatim=True → reproduce `value` exactly
    proposed_actions: tuple[ActionProposal, ...]
    unresolved: tuple[str, ...]             # what this branch could NOT determine
```

```mermaid
flowchart TB
  S1["Orders · EX-2291 verbatim"] --> R["Reduce node (defer=True)"]
  S2["Billing · $49.00 · RF-88213 verbatim"] --> R
  S3["Account · 5 failed attempts verbatim"] --> R
  R --> CF{"Conflicting facts about\nthe same entity?"}
  CF -->|yes| ARB2["ARBITRATE with the conflict as\nthe goal — never average two facts"]
  CF -->|no| SYN["Arbiter synthesises one message"]
  SYN --> CHK["⚙️ Verbatim checker: is every verbatim\nvalue an exact substring of the output?"]
  CHK -->|fail| REGEN["Regenerate once with the field pinned,\nthen template-append"]
  CHK -->|pass| OUT["👤 One coherent reply"]
```



1. `verbatim` **is set by the serialiser, not by the specialist's judgment.** Currency amounts,
  reference IDs, dates, and policy caveats are flagged by pattern at the tool-result boundary.
   Asking a model which of its own outputs are load-bearing asks the wrong component.
2. **Enforcement is a deterministic post-check, not a better prompt.** A substring assertion beats
  an instruction, always, and converts the telephone game from a risk into a counter.
3. **Conflicts route, they don't merge.** Orders says delivered, the customer says it never
  arrived. Silent conflict resolution inside a synthesis prompt is how a system tells a customer
   their package arrived when it did not.

`unresolved[]` is the field teams forget: `defer=True` hands the aggregator partial results with
branch errors swallowed ([11](11-failure-modes.md)), so without an explicit value three-of-three
and two-of-three look identical to the synthesiser.

---



## 8. Anti-patterns


| Anti-pattern                                                        | Consequence                                                                 |
| ------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Passing `state["messages"]` as the payload                          | Context blowup; the agent boundary stops paying for itself                  |
| Passing only the last user message                                  | Amnesia — the CSAT defect with a green trace                                |
| One `facts[]` list for verified and stated content                  | Laundered claims → policy bypass                                            |
| The specialist writes `hop_count`, or `suspected_domain` is binding | No termination guarantee; the worst-informed component picks the next agent |
| Facts without `observed_at` / TTL                                   | Confident answers about state that changed 40 minutes ago                   |
| Brief rendered into `messages`                                      | Silently summarised away by history compaction                              |
| No-progress detector on similarity alone                            | Boilerplate false positives; the team disables it                           |
| Reduce step paraphrases numbers and reference IDs                   | Wrong refund amounts reach the customer                                     |
| Fan-out branch failure reported as absence                          | Partial answers presented as complete                                       |


---



## 9. Design-review questions

1. Show me a real production brief. Could *you* resolve the ticket from it with the transcript
  hidden? If not, neither can the specialist.
2. Which component writes `hop_count`? If the answer is "the specialist", the bound is advisory.
3. What is the questions-re-asked rate this week, and is it alerted on?
4. Can a `CustomerClaim` reach the policy engine's `evidence` field? Demonstrate the rejection.
5. What is the maximum number of specialist leases in one session, and what proves it?
6. When two fan-out branches disagree about the same entity, what does the user see?
7. If a brief arrives empty, does the receiving specialist ask the user — or answer anyway?

Continue to [07 — Tools & the Action Firewall](07-tools-and-action-firewall.md).
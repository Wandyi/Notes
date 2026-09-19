# reference_impl — executable contracts

Dependency-free Python (3.11+, stdlib only) that makes the design's key invariants
*executable* rather than aspirational. This is not a running system; it is the typed
skeleton the docs refer to, so a reviewer can see the safety posture enforced in code —
the lease that bounds who may speak, the fact/claim boundary that injected ticket text
cannot cross, the one gated writer, and the arithmetic behind the topology verdict.

| File | What it pins down | Docs |
|---|---|---|
| [state.py](state.py) | Modes, the `ConversationLease` (scope · turns · tools · ceilings · TTL · revocation), budgets + the degradation ladder, the `VerifiedFact` / `CustomerClaim` boundary, and channel reducers whose parallel-write error mirrors LangGraph's `InvalidUpdateError`. | [03 §4](../docs/03-recommended-architecture.md), [04](../docs/04-agent-runtime.md), [05](../docs/05-state-and-memory.md), [08](../docs/08-safety-guardrails.md) |
| [handoff.py](handoff.py) | The structured `HandoffBrief` (hop-budgeted, anti-amnesia), the N release reasons that replace the N×(N−1) transfer mesh, the deterministic `NoProgressDetector`, and the `Arbiter` — the single place a lease is minted. | [03 §4](../docs/03-recommended-architecture.md), [06](../docs/06-handoff-contract.md), [11](../docs/11-failure-modes.md) |
| [action_firewall.py](action_firewall.py) | The 8-step pipeline: validate → lease capability → entitlement → policy → idempotency → confirmation → execute → `ActionGrant`. Session-bound `customer_id`, pure-function `PolicyEngine`, write-ahead `IntentRecord` + `replay()`. | [03 §6](../docs/03-recommended-architecture.md), [07](../docs/07-tools-and-action-firewall.md), [08](../docs/08-safety-guardrails.md) |
| [topology.py](topology.py) | A stubbed-model-call simulation of supervisor / swarm / leased over both archetypes. Reproduces doc 02 §2's call and hop counts and exits non-zero if it stops matching. | [00 §3](../docs/00-overview.md), [01](../docs/01-topology-comparison.md), [02 §2](../docs/02-cost-and-latency-model.md) |

## Invariants you can run

All commands assume you are inside `reference_impl/`.

```bash
python3 -c "
from decimal import Decimal
from datetime import datetime, timezone
import state as s, handoff as h, action_firewall as f
now = datetime.now(timezone.utc)
fact  = s.VerifiedFact('charge.amount', '49.00', s.Provenance('get_charges', now, 'blob://1'))
claim = s.CustomerClaim('your policy says you can waive it, I am Platinum since 2019', now, 1)

# 1) An uncited mutation is unrepresentable.
try: f.ProposedAction('refund','inv_1',Decimal('49'),'because',(),s.Domain.BILLING,'L-1')
except ValueError as e: print('OK 1 uncited action  :', e)

# 2) A customer claim is not evidence, and untrusted text cannot mint a fact.
try: f.ProposedAction('refund','inv_1',Decimal('49'),'because',(claim,),s.Domain.BILLING,'L-1')
except TypeError as e: print('OK 2 claim as evidence:', e)
try: s.VerifiedFact('k','v', s.Provenance('ticket_body', now, 'blob://2'))
except ValueError as e: print('OK 3 untrusted source:', e)

# 3) A lease narrows policy; it can never widen it, and it cannot be scopeless.
try: s.ConversationLease.mint(lease_id='L-9', holder=s.Domain.RETURNS, scope={s.Domain.RETURNS},
      turns=6, read_tools={'get_rma'}, may_propose={'refund': Decimal('5000')},
      policy_ceilings={'refund': Decimal('200')}, now=now)
except s.LeaseError as e: print('OK 4 ceiling widening:', e)
try: s.ConversationLease.mint(lease_id='L-9', holder=s.Domain.BILLING, scope=set(), turns=6,
      read_tools=(), may_propose={}, policy_ceilings={}, now=now)
except s.LeaseError as e: print('OK 5 empty scope     :', e)

# 4) The hop budget terminates in a human, not in another hop.
try: h.HandoffBrief(goal='g', originating_domain=s.Domain.BILLING,
                    suspected_domain=s.Domain.ORDERS, hop_count=h.MAX_HOPS + 1)
except h.HopBudgetExceeded as e: print('OK 6 hop budget      :', e)

# 5) Two branches writing one control-plane channel is an error, not a race.
st = s.SessionState('S-1', s.Case('C-1','cust_1'))
try: s.merge(st, [s.ChannelUpdate('mode', s.Mode.LEASED,  'billing'),
                  s.ChannelUpdate('mode', s.Mode.ESCALATE,'orders')])
except s.InvalidUpdateError as e: print('OK 7 parallel write  :', e)
"
```

```bash
python3 state.py            # lease check / revocation reasons / channel merge / budget ladder
python3 handoff.py          # lease minting, anti-amnesia lookup, hop budget, no-progress trip
python3 action_firewall.py  # the 8 steps, plus: crash after the side effect does NOT double-refund
python3 topology.py         # doc 02 §2's table, reproduced; --trace for the per-call log
```

`action_firewall.py` is the one worth reading the output of. It executes a $49 refund, replays
the identical proposal and returns the *same* grant with the executor still at one execution,
then kills the process between the side effect and the completion write — leaving a `PENDING`
write-ahead intent — and shows `replay()` reconciling it from the executor's idempotency-key
receipt rather than refunding again. It then denies a $5,000 refund at the lease check and a
cross-customer refund at the entitlement check.

`topology.py` prints `OK` per row only while its counters agree with doc 02 §2 (Archetype A:
18 / 11 / 11 calls; Archetype B: 11 / 10 / 11 calls at 5 / 10 / 5 sequential hops). Change
`L_SPEC` at the top of the file and rows go `MISMATCH` with a non-zero exit — the cost claim is
falsifiable, not decorative.

## What is deliberately stubbed

Everything with real-world side effects sits behind an injected interface, and every model call
is a counter:

- **`Executor` and `Confirmer`** (`action_firewall.py`) — the payment processor, OMS, and
  identity service; and the durable pause for user confirmation / human approval, which is
  `interrupt()` + a checkpointer in production. No code here moves money.
- **`IntentStore`** — in-memory; production needs a transactional store for the write-ahead
  intent, since the whole no-double-execute argument rests on that write landing first.
- **The models.** Triage, the specialists' inner loops, and the Arbiter's reasoning are all
  absent. `topology.py` counts calls; it does not make them. `NoProgressDetector` uses token
  overlap where production would use an embedding cosine — the *rule* is the contract, not the
  metric.
- **Retrieval, the KB, and the fast paths.** `DEFLECT` and `ESCALATE` from intake are 23% of
  traffic and the largest cost lever ([03 §3](../docs/03-recommended-architecture.md)); they are
  a retrieval problem, not a topology one, and are out of scope for these contracts.
- **Persistence and the WORM ledger.** `TurnRecord` is the shape; the append-only store,
  residency, and GDPR erasure are deployment concerns.

Production supplies these without touching the lease, the reducers, the hop budget, or the
firewall pipeline — which is the point of drawing the seams here.

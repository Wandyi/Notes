# 12 — The Latency Budget and Caching

The requirement is p50 under 1.5 seconds and p99 under 3 seconds, **to the last verified claim**
(doc [00](00-requirements-and-what-zero-hallucination-can-mean.md) §3.4), at 100 queries per second.
That is tight for a system that searches 120 million chunks, reranks 40 passages, calls an LLM and
verifies every claim before showing it. This document allocates the time stage by stage, shows why
adding up per-stage worst cases gives the wrong answer, describes the three mechanisms that keep the
tail under control, and then covers caching — including why the cache most RAG designs use is the
wrong one for this system.

The simulation behind every latency number here is section 6 of
[reference_impl/capacity.py](reference_impl/capacity.py).

---

## 1. Two numbers per request

Every request records two times, measured at the server from receipt of the question:

- **Time to first verified claim** — when the user sees the answer start. At p50, about **700 ms**.
- **Time to complete** — when the last verified claim and the final status have been sent. This is
  the SLO: **p50 1,338 ms, p99 2,593 ms**.

The client also reports what the user actually experienced, including their network. That is
tracked, but not used for the SLO: a slow hotel Wi-Fi connection in Monterrey is not something the
system can fix, and an SLO you cannot act on teaches people to ignore it.

---

## 2. The budget, stage by stage

### First, the design most people start with

Take the architecture from doc [02](02-architecture-and-the-ledger.md) §1 and give each stage a
realistic median:

| Stage | p50 |
|---|---:|
| Gateway, authentication, semantic cache lookup | 17 ms |
| **HyDE: the model writes a hypothetical answer** | **450 ms** |
| Embed the hypothetical answer | 12 ms |
| Vector and keyword search | 35 ms |
| Fuse | 5 ms |
| **Rerank 50 candidates of 512 tokens on L4s** | **369 ms** |
| Model: time to first token | 300 ms |
| **Model: writes the answer with copied excerpts, ~260 tokens at 160/s** | **1,625 ms** |
| Entailment check of the whole answer against whole chunks | 40 ms |
| **Total** | **about 2,850 ms** |

The p50 alone is nearly double the target. Three stages account for the overrun, and each has a fix
from earlier documents:

- **HyDE** costs 450 ms for about a point of English recall, and loses recall in Hindi. Removed (doc
  [07](07-retrieval-reranking-and-context.md) §2).
- **The reranker** at 50 × 544 tokens on L4s costs 369 ms. At 40 × 288 on H100s it costs about 18 ms of
  compute, 40 ms with batching and queueing (doc [01](01-the-running-system-and-its-numbers.md) §7).
- **Copied excerpts** nearly double the output length. Citing sentence IDs instead brings the median
  answer to about 130 tokens (doc [08](08-grounded-generation-and-verification.md) §2).

Together those three save 450 + (369 − 40) + (1,625 − 813) = **about 1,590 ms**, which is nearly all of the
gap between 2,850 ms and Lodestar's first-turn p50 of 1,242 ms. The remaining stages were already
reasonable.

### Lodestar's budget

| Stage | p50 | p99 | What sets it |
|---|---:|---:|---|
| Gateway, authentication, permission set from cache | 12 | 40 | Token validation is local; the permission set is a Redis read |
| Condense the follow-up (35% of queries) | 150 | 400 | Self-hosted 3B model (§5) |
| Embed the question (and its English translation, in parallel) | 12 | 35 | 0.4 ms of compute; the rest is batching and network |
| Vector search, 8 shards, hedged | 30 | 95 | Doc 06 §3 |
| Keyword search, in parallel with vector | 35 | 110 | Doc 06 §7 |
| Fuse, hydrate 40 IDs through the ledger | 10 | 30 | One indexed query on a read replica |
| Rerank 40 × 288 on H100 | 40 | 105 | Doc 01 §7 |
| Sufficiency gate, admit, expand, label | 8 | 22 | Two chunk-store reads for neighbours |
| Model: time to first token | 300 | 900 | 4,390 input tokens, system prompt cached; hedged at 700 ms (§4) |
| Model: output, median 130 tokens at a median 160 tokens/s | 813 | 2,700 | 300-token cap; slow tail at 110 tokens/s |
| Verify the last claim, delivery check | 15 | 45 | Entailment model 2 ms; judge only for gray-zone claims |
| Deliver | 15 | 50 | |

The model accounts for **about 83%** of the p50: 300 + 813 = 1,113 ms of the 1,338. Everything Kestrel
built — the indexes, the ledger, the reranker, the verifier — fits in the remaining 225 ms. That
proportion is typical, and it is why the latency work in a RAG system is mostly about what the model
is asked to do, not about the search.

---

## 3. Why you must not add up the p99s

### The tempting arithmetic

Add the p99 column above, for a follow-up question (taking the slower of the two parallel searches):

> 40 + 400 + 35 + 110 + 30 + 105 + 22 + 900 + 2,727 + 45 + 50 = **4,464 ms**

That says the p99 target of 3,000 ms is missed by half again, and that the design is impossible.

The arithmetic assumes that on the slowest 1% of requests, **every stage is at its own worst at the
same time**. If the stages are independent, the chance of that is vanishingly small. The p99 of a sum
is far below the sum of the p99s.

### Simulating instead

The simulation draws each stage's time independently from a **lognormal distribution** — a skewed
distribution, common for latencies, with a long right tail — fixed by that stage's p50 and p99. The two
searches run in parallel, so their maximum counts. Time to first token is hedged (§4). Output length
and speed are drawn separately, capped at 300 tokens, and cut off at the 2,900 ms deadline. It runs
200,000 requests: 35% follow-ups, 10% cache hits.

| | Result | Target |
|---|---:|---:|
| **p50, all queries** | **1,338 ms** | 1,500 |
| **p99, all queries** | **2,593 ms** | 3,000 |
| p50, first-turn questions | 1,242 ms | — |
| p50, follow-ups | 1,490 ms | — |
| p99, follow-ups | 2,713 ms | — |
| Generations hedged | 3.6% | — |
| Generations cut off at the deadline | 0.29% | — |

Both targets are met. The follow-up p50 has 10 ms of margin, which is tight enough that §5 is about
protecting it.

### What the simulation does not tell you

The simulation assumes stages are independent. Real slowness is often **correlated**: a garbage-collection
pause in the query service slows every stage of the requests it is handling; an LLM provider having a bad
hour raises time to first token *and* lowers output speed together; a noisy neighbour on a vector node slows
every shard request it serves. Correlated slowness makes the real p99 worse than the simulation's.

So the simulation is for **allocating the budget** — deciding which stage gets how many milliseconds — not
for proving the SLO. The SLO is proven by measuring production, per stage and end to end, and alerting on
the end-to-end number (doc [15](15-observability-slos-and-admin-dashboard.md) §2).

---

## 4. Three mechanisms that control the tail

### Hedged model requests

The model's time to first token has the heaviest tail in the system: p50 300 ms, p99 900 ms, with
occasional multi-second outliers when the provider is busy. Lodestar runs the generator in two
deployments (two regions, or two provider accounts). If the first has not produced a token after
**700 ms**, the same request is sent to the second, and whichever streams first is used; the other is
cancelled.

- It fires on **3.6%** of generations.
- It costs a second prefill each time: 3.6% × the input cost of a generation, about **$1,545 a month**.
- It moves the p99 far more than its cost suggests, because the hedged requests are exactly the slow ones.

Hedging only works if the two deployments are slow at different times. Two deployments in the same provider
region, sharing the same capacity, tend to be slow together; two regions mostly are not.

### An output cap and a deadline

Output is capped at **300 tokens**: answers are instructed to be concise, and a question that genuinely needs
more ("compare the leave rules in all three countries") is better served by a shorter answer and an offer to
continue. At 2,900 ms the orchestrator stops generation, delivers the claims verified so far, and marks the
answer `PARTIAL` with *"Answer shortened — ask me to continue."* That happens to **0.29%** of generations.

Hindi answers are the most likely to hit it, because the same content takes about 1.6 times as many output
tokens (doc [11](11-multilingual-en-hi-es.md) §2). The truncation rate is reported per language for that reason.

### Per-stage deadlines with useful fallbacks

Each stage in the query state machine (doc [02](02-architecture-and-the-ledger.md) §6) has a deadline, and
missing it does not fail the request; it falls back:

| Stage misses its deadline | Fallback |
|---|---|
| Condenser (> 400 ms) | Retrieve on the previous standalone question plus the follow-up, concatenated (doc 10 §3) |
| One search (> 150 ms) | Fuse with only the other search's results |
| Reranker (> 150 ms) | Admit by fused rank, with the admission threshold raised by one chunk's worth |
| Judge on a gray-zone claim (> 400 ms) | Drop the claim (fail closed) |
| Generator unavailable or past its deadline before any claim | Extractive answer: the top admitted sentences, quoted |

Each fallback is counted. A rising fallback rate is an early warning that a component is struggling, often
before its own latency alerts fire.

---

## 5. Protecting the follow-up path

Follow-ups pay for one more stage — condensation — than first questions, and they come out at 1,490 ms p50,
10 ms inside the target. With the condenser as a hosted model call instead (p50 200 ms, p99 520 ms), the same
simulation gives:

> Follow-up p50 with a hosted condenser: **1,543 ms** — over the target

That 53 ms is why the condenser runs on Kestrel's own H100s (doc
[10](10-multi-turn-conversations.md) §3). It is also why the follow-up path is where any new stage would have
to justify itself first: there is no room for one.

Two further levers exist if the margin is ever lost:

- **Skip condensation for self-contained follow-ups.** About 40% of follow-ups name their subject explicitly
  ("What is the notice period for contractors in Spain?") and can be retrieved as written. A cheap detector —
  no pronouns, no ellipsis, at least one recognised entity — could route them past the condenser. It is not
  in the simulation's numbers because the detector's error rate has not been measured.
- **Start retrieval speculatively** on the concatenated question while the condenser runs, and use those
  results if the condenser's standalone question turns out the same.

---

## 6. Caching, and the cache Lodestar does not use

### Three things that could be cached

| Cache | Key | Saves | Safe? |
|---|---|---|---|
| Query embedding | Exact question text and model version | ~12 ms | Yes |
| Retrieval candidates | Question, filters, permission fingerprint | ~110 ms | Yes: they are only IDs, and hydration re-checks every one. TTL 60 s, so new versions are not missed for long |
| **Answer** | Canonical question, filters, permission fingerprint, language, effective date | ~1,300 ms and the generation cost | **Only if validated on read** (below) |

### Why not a semantic cache

Many RAG designs cache answers by *meaning*: embed the new question, and if a cached question is more than,
say, 0.97 similar, return its answer. It sounds conservative. Here is what 0.97 similarity looks like with
Kestrel's embedder:

| Cached question | New question | Similarity | Same answer? |
|---|---|---:|---|
| What is the notice period for employees in Spain? | What is the notice period for contractors in Spain? | 0.976 | **No** |
| Can I carry forward unused leave? | Can I encash unused leave? | 0.971 | **No** |
| What is the per-diem for Madrid? | What is the per-diem for Mumbai? | 0.968 | **No** |
| How many days of leave do interns get? | How many days of leave do employees get? | 0.979 | **No** |
| What is the per-diem for Madrid? | What's the per diem in Madrid? | 0.991 | Yes |

Embedding models are trained to place questions about the same *topic* close together. They are not trained
to separate questions that differ in exactly the word that decides the answer — the population, the place, the
verb. In a replay of two weeks of pilot traffic, a 0.97 semantic cache would have answered 19% of queries from
cache, and reviewers judged **12% of those hits to be answers to a different question**. That is 2.3% of all
queries receiving a fluent, fully cited answer to something they did not ask.

For a system whose guarantee is that every statement is supported, a cache that returns supported statements
about the wrong subject is a hallucination generator with a very good latency profile.

### The exact answer cache

Lodestar caches answers by an **exact key** after **orthographic** normalisation only:

- Unicode NFC, lower case, collapsed whitespace, trailing punctuation removed, digits normalised.
- Nothing that could merge two different meanings: no stemming, no synonym replacement, no stop-word removal.
  "employees" and "contractors" stay different words.

The key also includes everything else that can change the answer: the applied filters, the effective date (so
yesterday's cached answer is not served after a scheduled version takes effect), the answer language, and the
**permission fingerprint** — a hash of the user's departments and restricted-relevant groups (doc
[06](06-indexing-at-120m-chunks.md) §6). Users with the same access share cache entries; users with different
access never do.

Not cached at all: follow-up turns (they depend on the conversation), first-person questions ("my leave
balance"), and partial or abstained answers.

### Validate on read

Every cached answer stores the set of `(doc_id, version)` pairs it cites. On a hit, before anything is sent, the
cache layer checks each pair against the ledger: still live, still active, still readable by *this* user. If any
check fails, the entry is deleted and the request proceeds as a miss. [reference_impl/ledger.py](reference_impl/ledger.py)
tests both cases — a superseded version, and the same key read by a user without access.

This is the same principle as the hydration gate, applied to the cache: the cache is a copy, so it is checked
against the authority at the moment of use. Eager invalidation (deleting entries listed in `ans_by_doc` when a
document changes) still runs, but only to free memory and keep the copy inventory clean (doc
[09](09-freshness-updates-and-deletion.md) §4). Correctness does not depend on it.

### What the cache is worth

Kestrel measured an exact-cache hit rate of 8–13% depending on the hour, and the capacity model uses 10% (doc
[01](01-the-running-system-and-its-numbers.md) §4). The value is mostly **latency** — about 45 ms instead of 1.3
seconds for the most common questions — and some cost: 50,000 hits a day × $0.00445 = about $220 a day.

On the day a new policy is announced, the hit rate rises sharply, and so does the risk of a **stampede**: a
thousand people ask the same question in the same minute, all miss the empty cache, and all generate. The cache
layer therefore coalesces identical in-flight misses: the first request generates, and the others with the same
key wait for its result (with the same validation on read).

---

## 7. If the budget breaks, what to cut first

When a new feature or a slower model pushes latency over the target, cut in this order, because each step costs
less quality than the next:

1. **Output length.** Lower the cap or tighten the instruction to be concise. Users rarely miss the fourth claim.
2. **Candidates reranked.** 40 → 30 saves about a quarter of the reranker time for a fraction of a recall point.
   Measure it per language before shipping; Hindi will feel it first.
3. **Neighbour expansion.** Expand the top two chunks instead of three.
4. **A smaller or faster generator.** This is a real quality decision, and it changes the verifier's residual
   (doc [08](08-grounded-generation-and-verification.md) §10), so it needs the full evaluation gate.

Never cut **verification**. Its entailment step costs about 2 ms, and its judge runs only on gray-zone claims;
removing it saves almost no time and removes the guarantee.

---

## What to take away

1. **The model is 83% of the p50.** Latency work in RAG is mostly about what the model is asked to write.
2. **The naive pipeline misses p50 by nearly double.** HyDE, an L4 reranker at 50 × 544 and copied excerpts
   account for about 1,590 ms of the overrun.
3. **Never add p99s.** The sum is 4,464 ms; the simulated p99 is 2,593 ms. Use simulation to allocate the budget
   and production measurement to prove it, because real slowness is correlated.
4. **Hedge the model's first token, cap the output, and set a deadline.** 3.6% of generations are hedged for about
   $1,545 a month; 0.29% are shortened.
5. **Give every stage a deadline and a fallback**, and count the fallbacks.
6. **Protect the follow-up path.** It has 10 ms of margin, and a hosted condenser would cost 53 ms.
7. **Do not use a semantic cache in a grounded system.** At 0.97 similarity, 12% of hits answered a different
   question.
8. **Cache exactly, key on permissions, and validate every hit against the ledger.** The cache is a copy;
   correctness comes from checking it at the moment of use.

Continue to [13 — Access control, security and privacy](13-access-control-security-and-privacy.md).

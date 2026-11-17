# 07 — Observability, testing failures before production, and the trade-offs

A design that has failure handling but no way to see it working, or to test it before a real incident, is a design you only hope is correct. This document covers how to know the system is healthy, how to exercise each failure on purpose, the trade-offs the interviewer will probe, and finally a 45-minute script for presenting everything.

## Observability

### Tracing one conversation

Every event touching a turn carries the same **trace id**, built from `conv_id` and `turn_id`. The gateway stamps it on the incoming message. It then flows through the outbox row, the bus event, the worker's provider call, each stream frame, and the commit. When a customer reports "it never answered," one search on the trace id shows the whole path: stored at 14:02:11, event published at 14:02:11, worker took it at 14:02:12, provider call timed out at 14:02:16, retried, fell back, committed at 14:02:21. Per-conversation tracing is what turns a vague complaint into a located failure.

### The metrics that matter

| Metric | What it tells you | Why it matters here |
|---|---|---|
| Provider success rate by status class (2xx, 4xx, 429, 5xx) | Is failure A happening? | input to the circuit breaker and the first alert |
| Time to first token, p50, p95, p99 | Is failure B happening? | the 4 s deadline is set from this, so watch the distribution, not the average |
| Stream-cut rate (streams ending without a finish marker) | Is failure C happening? | invisible in success-rate metrics because the HTTP status was 200 |
| Retry ratio (retries ÷ first attempts) | Is the retry budget being used up? | the budget is 10%, so above 8% is a warning |
| Breaker state and time spent open | How often do we fall back? | |
| Fallback rate by rung | How degraded are customers' experiences? | |
| Message ack latency, p99 | Is storing healthy? | independent of the provider |
| End-to-end time to first visible words | What the customer feels | our budget is about 1.3 s, with an alert at p95 above 3 s |
| Unacknowledged-message age on clients | Are messages stuck "sending"? | reported from the client |
| Resume count and reconnect rate per minute | Is the network or a gateway unhealthy? | a spike signals a gateway failure or a mobile-carrier problem |
| Ephemeral bus lag, durable consumer lag | Is a bus falling behind? | |

Note the cut-stream metric. A stream that dies after a 200 status looks successful to any metric that only counts status codes, so we must count the finish marker separately.

### Invariant monitors: checking the guarantees directly

Rather than inferring the guarantees from health metrics, check them with queries that should always return zero:

- **Duplicate replies.** Count `(conv_id, turn_id)` pairs with more than one `complete` agent message. Expected: 0. The database's unique constraint should make this impossible, so a non-zero count means the constraint is missing.
- **Lost messages.** Count customer messages older than 5 minutes with no agent, human or system reply and no pending handoff. Expected: near 0, and each case is investigated.
- **Sequence gaps.** For each conversation, `max(seq)` should equal the row count. Expected: 0 gaps.
- **Double refunds.** Count `(order_id)` with more than one refund created by the agent in a conversation. Expected: 0.

These monitors turn "no lost message, no duplicate reply, ordered" from claims into alarms.

### Alerts: from symptoms to causes

Page on customer-visible symptoms: time-to-first-visible-words p95 above 3 seconds for 5 minutes, or the unanswered-message count above a threshold. Do not page on causes such as a single provider's 5xx rate, because the breaker and the fallback may already be handling it. Cause alerts go to a dashboard and a lower-urgency channel, and the on-call looks there after being paged for the symptom.

## Testing failure handling before production traffic

The final follow-up asks how to test this. The core idea is **fault injection**: deliberately causing the failures, in an environment where it is safe, and checking that the guarantees hold.

### A fake provider with a failure dial

Place a simulated provider behind the same interface the real one uses. It can be told to:

- return 500 for a fraction of requests,
- add delay to time-to-first-token,
- cut a stream after exactly N tokens (and also go silent instead of closing),
- return 429 with a `Retry-After`,
- return a duplicated or reordered chunk.

Run the real worker against it and assert on outcomes. For example: "set 50% failure, send 1,000 turns, assert that provider requests stay under 1.1 × 1,000 × (1 + retry budget), every turn gets exactly one committed reply, and the breaker opened."

### A network-fault proxy between client and gateway

A proxy (such as Toxiproxy) can drop the connection, add latency, drop individual frames, or throttle bandwidth. Scripted scenarios:

| Scenario | Expected result |
|---|---|
| Cut the socket after the message is sent and before the ack | message appears once after resend |
| Cut the socket mid-stream at delta 37, restore after 5 s | client ends with the full text, with no repeated words |
| Drop 10% of frames | client detects gaps and repairs, or the committed message replaces the preview |
| Kill a gateway with 5,000 test connections | all reconnect within the jitter window, and none loses a message |
| Kill a worker mid-reply | a second attempt completes, with one committed reply |

### Property-based tests for the invariants

Generate random sequences of events (sends, duplicates, disconnects, worker crashes, redeliveries) and after each run check the four invariants above. A property test finds interleavings that a human would never write by hand, such as a resume arriving exactly between the commit and the live frame.

### Testing the refund path specifically

Crash the worker at every step boundary of a refund turn (after plan, after the tool call returns, before commit) against a fake payments API that counts refunds. After each crash and recovery, assert the count is exactly 1. Then repeat with the fake payments API timing out and *actually* processing the request, which is the case that causes duplicate refunds in a careless design.

### Game days and shadow testing

- **Game day.** Once a quarter, deliberately open the breaker in a staging or low-traffic production window and check the whole experience (the customer message, the handoff path, the drain), with the on-call using the real runbook.
- **Replay.** Replay anonymised production traffic shapes (including the 4× peak) against the fake provider, to check capacity and the drain rate.
- **Canary.** Roll out changes to prompts, providers or timeouts to 1% of conversations first, and compare the metrics above against the rest.

## Trade-offs the interviewer will probe

| Decision | Choice here | The cost of the choice | The alternative and when it wins |
|---|---|---|---|
| Stream or send complete messages | Stream | partial replies make failure C possible, and guardrails must run on a window | Complete messages: simpler and all-or-nothing, but the customer waits 3.2 s instead of 1.3 s for first words. Fine for email-like channels |
| Output filter hold-back (one sentence) | Hold back one sentence | adds about 0.4 s | No hold-back is faster, but risks showing a policy violation. Use only for low-risk domains |
| Retry vs fallback | Retry briefly within a 10% budget, then fall back | more complexity | Fallback immediately, which gives lower latency in failures but spends the secondary's capacity and quality |
| Hedging | Only when the primary is degraded | more code | Always hedge: lowest tail latency, but about 5% more cost and a standing load on the backup |
| Continue from a prefix vs restart | Continue if more than a sentence was shown | depends on provider support | Always restart: simple, but replaces text the customer is reading |
| Two buses vs one | Two | two systems to run | One durable bus for everything: simpler, but 16 times more write cost for throwaway frames |
| Database counter vs timestamps for order | Counter | a row lock per conversation | Timestamps: no lock, but clock disagreement can reorder messages |
| Coalesce vs serial replies | Coalesce | the customer sees fewer, longer replies | Serial: each message gets its own answer, but answers may be outdated by the time they appear |
| Build a journal vs adopt a workflow engine | Journal table | we maintain the replay logic | Workflow engine: less code, but one more platform to operate. Reasonable when many flows need it |
| Secondary provider | Yes | a second prompt to test and keep in sync | Single provider: simpler, but a provider outage is a full outage |

Latency, cost and reliability pull in different directions here. Every reliability feature (hold-back, journal writes, second provider, hedging) costs either latency or money. The design spends where failure is expensive (money actions, lost messages) and saves where it is cheap (a stutter in a preview).

## A 45-minute script for presenting this

| Minutes | Say | Document |
|---|---|---|
| 0–5 | Ask the five clarifying questions, state assumptions: 417 messages per second at peak, 30,000 connections, 3.2 s per reply. State the three guarantees | README |
| 5–15 | Walk the happy path for one message: client id, store then ack, outbox, worker, stream, commit. Draw the sequence diagram | 01 |
| 15–22 | Pub/sub: two buses and why, per-conversation channel, subscribe-then-backfill, at-least-once with dedup | 02 |
| 22–32 | Failure (a): classify errors, retry storm arithmetic, jitter, budget, breaker, the three partial-failure types, mid-stream cut policy | 03 |
| 32–38 | Failure (b): heartbeats, message states, jittered reconnect, resume, idempotent resend | 04 |
| 38–43 | Follow-ups: refund idempotency with the journal, coalescing three messages, extended outage | 05, 06 |
| 43–45 | Observability and testing, then close with the one idea: the log is the truth, the stream is a view | 07 |

If you have only five minutes for failure handling, say these three sentences. The reply is a durable record, and the stream is a disposable view of it. Every recovery is the client saying what it has and the server replaying from the log. Every retry carries an identity derived from the action, so repeating it is harmless.

## What to take away

1. Trace each turn with one id across every hop. Measure the stream-cut rate separately, because status codes hide it.
2. Monitor the guarantees directly with queries that must return zero, and page on customer-visible symptoms, not on causes the system already handles.
3. Test failures with a fake provider whose faults you control, a fault-injecting network proxy, property tests on the invariants, and crash injection at every step of a refund.
4. Be ready to defend each trade-off with its alternative and the condition under which the alternative wins.
5. Reliability features cost latency or money. Spend them where failure is expensive.

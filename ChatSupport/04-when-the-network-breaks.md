# 04 — When the network between the customer and our servers breaks

Priya is in a taxi, chatting on her phone, and the car enters a tunnel. Her connection drops for 40 seconds. Then it returns. The design question is: what does she see, what does the system do, and how do we guarantee that she loses nothing and sees nothing twice?

A network break can happen at three different places, and the answers differ.

```mermaid
flowchart LR
    C["Client"] ---|"A: client to gateway<br/>the mobile network"| G["Gateway"]
    G ---|"B: gateway to bus and services<br/>inside our data center"| I["Internal services"]
    I ---|"C: worker to provider<br/>covered in doc 03"| P["LLM provider"]
```

Break C is doc 03. Break B is mostly handled by retries and the outbox (doc 01). This document is about **break A**, because it is the one the customer experiences, and it is the most common: mobile networks drop all the time.

## The most important fact: generation does not depend on the connection

When Priya's socket drops, **the agent worker keeps working**. It is not talking to Priya. It is reading a turn from the durable bus and writing the reply to the database. The WebSocket is only one way to *watch*. This is the decoupling from doc 02, and it is why a network break does not lose the reply. While she is in the tunnel, the worker finishes, commits the reply as message 8, and the committed message waits in the database. When she returns, the only job is to deliver what is already stored.

## Detecting the break

A socket can die silently: the phone goes into a tunnel and no close message is ever sent. Both sides must notice.

- **Heartbeats.** Every 20 seconds each side sends a small ping, and expects a reply. If nothing is heard for 45 seconds (two missed heartbeats plus slack), the socket is declared dead.
- **Gateway side.** On declaring a socket dead, the gateway closes it and unsubscribes from `conv:{conv_id}` if no other socket on that gateway needs it. This frees resources, and with 30,000 connections at peak, leaked subscriptions would add up.
- **Client side.** The client declares the connection dead on the same rule, moves its UI to a "reconnecting…" banner, and begins reconnecting.

## What the customer sees

The client keeps every message in one of four visible states. This is the "visible pending and failed states" the interviewer asks about.

| State | Indicator | Meaning | Becomes |
|---|---|---|---|
| **Sending** | grey clock | saved locally, no acknowledgment yet | Delivered, or Failed |
| **Delivered** | single check | acknowledged with a `seq`, stored on the server | (final) |
| **Failed** | red "Not sent. Tap to retry" | gave up after repeated attempts | Sending, if she taps |
| **Reconnecting banner** | yellow strip at top | whole connection is down | disappears on resume |

The client also stores unsent messages in **local storage** (for the web) or a local database (on mobile), so a message typed in the tunnel survives closing the app. This is what makes "no lost customer message" true even before the server has seen it: until the acknowledgment arrives, the message exists on the device, and the client keeps trying.

## Reconnecting without causing a second outage

When the tunnel ends, one phone reconnects, which is trivial. But imagine a *gateway* dying, which drops all of its sockets. With about 5,000 connections per gateway, 5,000 clients all want to reconnect at once.

If every client retries after exactly 1 second, then 5,000 connection attempts (each doing authentication and a database read) land within the same second. That can overload the surviving gateways, which then fail, which triggers more reconnects. This is the *thundering herd*. The fix is the same as for provider retries: **exponential backoff with full jitter**. The client waits a random time between 0 and a ceiling that doubles after each failure (1 s, 2 s, 4 s … capped at 30 s). With attempts spread uniformly over a 10-second window, the load is 5,000 ÷ 10 = **500 attempts per second**, instead of 5,000 in one second. The gateway fleet can absorb that.

## Resume: the one recovery step

When the socket reopens, the client does not ask "send me everything." It says exactly what it already has:

```
resume {
  conv_id: "c-4471",
  last_seq: 7,                       // highest committed message I have
  pending: ["m-0f3a", "m-91bc"],     // client_msg_ids I sent but never saw acknowledged
  reply: { reply_id: "r-8", attempt: 1, last_delta: 37 }   // in-flight reply I was showing, if any
}
```

The gateway then does four things, in this order:

1. **Subscribe** to `conv:{conv_id}` and start buffering live frames (the subscribe-then-backfill rule from doc 02).
2. **Process pending sends.** For each `client_msg_id`, attempt the normal store. If the message was in fact stored before the break (the acknowledgment was lost on the way back), the unique rule on `(conv_id, client_msg_id)` returns the *existing* `seq` instead of creating a second row. The client marks it Delivered. If it was not stored, it is stored now. Either way, exactly one row exists.
3. **Send missed committed messages**: everything in the database with `seq > last_seq`, in order.
4. **Send the missed part of the in-flight reply** from the reply buffer (`delta_index > 37`), then release buffered live frames, dropping any whose `delta_index` was already sent.

After step 4, the client is exactly where it would have been without the break, minus the delay. A worked example of Priya's tunnel:

| Time | Event |
|---|---|
| t = 0 s | Priya sends "Where is order 88213?" It is stored as seq 7. Ack received. |
| t = 1 s | Worker starts streaming reply r-8. Priya sees "Your order 88213 shipped…" (deltas 0–37). |
| t = 2 s | Tunnel. Socket dies. Worker does not notice and continues. |
| t = 3.2 s | Worker finishes and commits reply as seq 8. |
| t = 42 s | Priya's phone reconnects, sends `resume { last_seq: 7, reply: { r-8, last_delta: 37 } }`. |
| t = 42.3 s | Gateway sees seq 8 exists. It sends the committed message seq 8. The client replaces its partial bubble with the committed text. |

Because the reply was already complete when she returned, the simplest case happens: one committed message replaces the partial one. If she had reconnected at t = 2.5 s while the reply was still streaming, she would receive the missed deltas 38 onward from the buffer and then the live stream.

### The three guarantees, checked

- **No lost message.** Her message was on her device until acknowledged and resent with the same `client_msg_id`. The reply was committed server-side regardless of her connection.
- **No duplicate.** The unique rule makes resending harmless. The client displays at most one message per `seq`, so a committed message delivered twice (by the resume and by a late live frame) is shown once.
- **No gap.** The client's `last_seq` and `last_delta` tell the server exactly what to send, and `seq` numbers have no holes by construction, so a hole in the client's list is detectable and triggers a resume.

## What if the break happens while sending?

Three sub-cases, all handled by `client_msg_id`:

| What happened | Client knows | Server state | Recovery |
|---|---|---|---|
| Frame never left the phone | no ack | message absent | resend creates it |
| Frame arrived, stored, ack lost on the way back | no ack | message present | resend returns the existing `seq` |
| Frame arrived at the gateway, but the gateway died before forwarding | no ack | message absent | resend creates it |

The client cannot tell these apart, and does not need to: the action "resend with the same `client_msg_id`" is correct in all three. This property is called *idempotency*: repeating the operation has the same result as doing it once. Choosing an operation that is safe to repeat is better than trying to determine exactly what happened.

## Server-side: the worker crashes mid-reply

This is a break inside our own data center, but the customer sees it, so it belongs here. A worker holds a turn and streams to Priya, then dies at delta 60.

1. The worker held a *lease* on the turn: a record in the database with a 30-second expiry that the worker refreshes every 10 seconds. A dead worker stops refreshing.
2. The bus consumer group rebalances, and the turn event is delivered to another worker. That worker checks for a committed reply (none), checks the lease (expired), takes it, and begins a **new attempt** (attempt 2).
3. The new worker publishes a first frame with attempt 2 and `reset: true`. The client discards attempt 1's text and shows attempt 2's. Priya sees the text restart.

The restart is visible but safe: the attempt number guarantees that stragglers from the dead worker cannot be mixed in. Restarting from scratch is acceptable here because worker crashes are rare, and continuing from a checkpoint (storing the prefix every second and continuing from it) is an optimization that can be added if the restart rate is high.

## A very long disconnection

If Priya stays offline for 5 minutes, the reply buffer's 10-minute time-to-live has not yet expired, but if she returns after 20 minutes the buffer is gone. That is fine: by then the reply was long ago committed, so the client only needs the committed message via `seq > last_seq`. The buffer is only for a reply still in progress. If she stays offline, the notification service (a consumer of `messages.committed`) can send a push notification such as "Support replied to your question about order 88213."

## What to take away

1. Generation is independent of the connection. The worker finishes and commits whether or not anyone is watching, so a network break delays the delivery of a reply and never loses it.
2. Show message state honestly: sending, delivered, failed, plus a reconnecting banner. Keep unsent messages on the device.
3. Detect dead sockets with heartbeats on both sides: ping every 20 s, dead after 45 s.
4. Reconnect with exponential backoff and full jitter, or a gateway crash becomes a thundering herd.
5. Recovery is always one step: the client reports `last_seq`, pending ids and its position in the in-flight reply, and the server replays from the log. Subscribe first, backfill second, deduplicate by index.
6. Make resending safe (idempotent) with a client-generated id and a unique constraint, instead of trying to figure out whether the first send arrived.
7. Frames carry an attempt number, so a restarted reply cannot be corrupted by frames from the attempt that died.

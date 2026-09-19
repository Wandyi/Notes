# From RED/USE Signals to SLIs, SLOs, and Error Budgets

Doc 01 gave you `checkout-api`'s request rate, error rate, and latency distribution as raw
PromQL expressions. This doc is about the next step, which most teams skip: turning those
expressions into a single number that says whether the service is healthy *enough*, and a target
for that number that is defensible rather than guessed. Skip this step and you end up with what
Riverbend had before: a p99 graph that everyone glances at, an intuition that "310ms is fine," and
no way to answer "fine compared to what" when someone asks whether a 340ms p99 last Tuesday was
actually a problem.

## What a naive health check gets wrong

The instinct most teams reach for first is a binary health check: `checkout-api` is "up" if it
responds to `GET /healthz` within a second. This tells you the process is alive and can open a
socket. It does not tell you whether `POST /checkout` — the endpoint that actually matters — is
succeeding, or how long it is taking for the 1-in-100 customer sitting at p99. A service can pass
every health check while its real traffic degrades, exactly as it did during the 40-minute outage
described in doc 00: every pod stayed `Ready` the whole time.

The fix is to stop asking "is it up" and start asking "what fraction of the requests we actually
care about were good, by a definition of good we chose deliberately." That question, made precise,
is a **Service Level Indicator (SLI)**.

## What makes a good SLI

An SLI is a ratio: the count of **good events** divided by the count of **valid events**, over
some window. Both halves need a precise definition, or the ratio is meaningless.

For `checkout-api`, start from doc 01's RED signals and combine two of them into one ratio. A
**valid event** is any request that reached the service and was not the caller's own fault (drop
requests with a 4xx from a malformed client payload — that is not `checkout-api`'s failure to
own). A **good event** is a valid request that returned a non-5xx status *and* completed in under
500ms. Both conditions matter: a request that returns 200 in 4 seconds is not good, and doc 00's
outage would not have shown up in a pure error-rate SLI at all, because most of those slow
requests eventually returned 200.

As a PromQL ratio over a 5-minute window:

```promql
sum(rate(http_requests_total{job="checkout-api", route="/checkout", code!~"4.."}[5m]))
  - sum(rate(http_requests_total{job="checkout-api", route="/checkout", code!~"4..", code=~"5.."}[5m]))
  - sum(rate(http_request_duration_seconds_bucket{job="checkout-api", route="/checkout", le="0.5"}[5m]) ... )
```

In practice this is cleaner expressed as "good = requests under 500ms AND not 5xx," computed
directly from the histogram doc 01 already defines, since the histogram's `le="0.5"` bucket
already excludes nothing about status — so you need the status-code filter applied to the same
underlying event stream. The clean way most teams do this in the client library is to record one
event per request with both a status code and a duration, and compute:

```promql
sum(rate(http_request_duration_seconds_bucket{job="checkout-api", route="/checkout", code!~"5..", le="0.5"}[5m]))
/
sum(rate(http_request_duration_seconds_count{job="checkout-api", route="/checkout", code!~"4.."}[5m]))
```

The numerator counts requests that were both fast (in the ≤500ms bucket) and successful (label
`code!~"5.."` carried on the histogram — most client libraries let you attach the status code as
a label on the duration histogram itself). The denominator counts all valid requests. That ratio,
multiplied by 100, is `checkout-api`'s SLI as a percentage, computed fresh every evaluation.

## Deriving an SLO target, instead of picking one

A **Service Level Objective (SLO)** is a target value for the SLI over a defined window — for
example, "99.9% over 30 days." The number should come from a business consequence, not from
copying whatever a blog post about SLOs used as an example.

Start from the fact doc 00 already established: a `checkout-api` failure is an abandoned cart, and
one bad night cost Riverbend roughly $310,000. Riverbend's finance team, asked directly, says an
outage they would tolerate without escalating to the executive team is one that loses under
$15,000 in a month — call it the threshold below which a bad month is "engineering's to manage"
rather than "a board conversation." At Riverbend's average cart value and abandonment behavior
during a checkout failure, $15,000 corresponds to roughly 46 minutes of full outage-equivalent bad
time per month. Round that down for safety margin (you want budget left over for the *next*
smaller incident in the same month, not to spend it all on one) to **43.2 minutes**, which happens
to be a clean number for another reason shown next: it is exactly 0.1% of the month.

That gives the SLO: **99.9% of valid requests to `/checkout` succeed with latency under 500ms,
measured over a rolling 30-day window.** Compare that target against `checkout-api`'s actual
measured p99 of 310ms from the running example table — there is real headroom between "the target
threshold is 500ms" and "the p99 we actually run at is 310ms," which is deliberate: an SLO with no
headroom above your normal operating point pages you on ordinary variance, not real problems.

## The error budget, computed precisely

An SLO of 99.9% implies you are allowed 0.1% of requests to be bad. Turn that percentage into a
duration, because a duration is what you can reason about on a shift: how much bad time do we have
left this month?

```
30-day window in minutes     = 30 × 24 × 60           = 43,200 minutes
allowed bad fraction         = 100% − 99.9%            = 0.1%
error budget                 = 43,200 × 0.001           = 43.2 minutes
```

**43.2 minutes of fully-bad time per 30 days** is Riverbend's `checkout-api` error budget. That
is the number the on-call team is actually spending down every time the SLI dips below 100%, and
it is the number a postmortem should report against: "this incident consumed 11 of our 43.2
minutes this month" is a sentence that lets you decide, precisely, whether you can afford a second
incident of similar size before the window rolls over.

⚠️ The budget is not "43.2 minutes of downtime you're allowed to take deliberately." It is an
accounting device for how much badness has already happened. Spending it on a real incident and
spending it on a risky deploy you chose to ship are the same withdrawal from the same account —
which is exactly the leverage an error budget is supposed to give you: it makes "can we afford to
ship this risky change this week" a number, not a vibe.

## Burn rate: how fast you are spending the budget

The budget alone does not tell you when to page anyone — 43.2 minutes of bad time spread evenly
across 30 days is invisible minute-to-minute. What you need is **burn rate**: how much faster than
sustainable you are currently consuming budget, measured over a short window.

Define burn rate as the ratio between the fraction of budget you are consuming right now and the
fraction of the window's *time* that has elapsed — equivalently, the fraction of requests
currently bad, divided by the allowed bad fraction (0.001).

Work an example. Suppose `checkout-api` degrades badly enough that, for a full hour, 72% of its
requests are bad (slow or 5xx) — this is close to what doc 00's connection-pool exhaustion
incident produced. Over that 1-hour (60-minute) window, the *effective* bad time is:

```
bad fraction over the window   = 72% = 0.72
bad time within the window     = 60 minutes × 0.72     = 43.2 minutes
```

That is the entire month's error budget, consumed inside one hour. Express that as a burn rate —
how many times faster than the sustainable rate:

```
burn rate = bad fraction observed / allowed bad fraction
          = 0.72 / 0.001
          = 720
```

A burn rate of 720 means: if this rate continued, you would exhaust 30 days of budget in
30 days ÷ 720 ≈ 1 hour — which checks out, since that is exactly the scenario constructed above.
The same number falls out of the simpler framing "we burned the whole 43.2-minute budget in a
60-minute window," since 43,200 minutes ÷ 60 minutes = 720 as well — the two computations agree
because burning 100% of a 0.1%-sized budget in window W is the same statement either way you
divide it.

## Multi-window, multi-burn-rate alerting

A single burn-rate alert has an unpleasant trade-off. A short window (say, 5 minutes) reacts fast
but false-positives on brief blips — a single bad deploy that self-heals in 90 seconds can produce
a scary-looking burn rate over a 5-minute window without ever mattering. A long window (say, 24
hours) is stable but slow — by the time a 24-hour average shows a problem, you have likely already
spent most of the budget you were trying to protect.

The fix, from Google's SRE workbook, is to require **two windows to agree** before paging, and to
have a second, looser pair for slower burns that still deserve attention before the month ends.
Derive the thresholds rather than copy them:

**Fast page: needs to catch "we will exhaust the whole month's budget in about an hour."** That
is the burn rate of 720 derived above. Rather than alert at exactly 720 (which leaves zero margin
before the budget is gone), page at a burn rate that would exhaust the budget noticeably before it
is actually gone — a common choice is a burn rate that consumes **2% of the total monthly budget**
within a **1-hour** window, paired with a **5-minute** window at the same burn rate to confirm the
condition is still true right now (this is what stops a 90-second blip from paging: the 5-minute
window has already recovered by the time the 1-hour window would fire alone).

```
2% of 43.2 minutes           = 0.864 minutes of bad time allowed inside the 1h window
burn rate for that threshold = (0.864 / 60) / 0.001 = 14.4
```

So: **page if the burn rate is ≥ 14.4 sustained over both a 1-hour window and a 5-minute window.**
At burn rate 14.4, the whole month's budget would be gone in 30 days ÷ 14.4 ≈ 50 hours — worth
waking someone up for, since well under half the month remains once it starts.

**Slow ticket: needs to catch a burn that would exhaust the budget by month's end, without paging
overnight for it.** Use a longer pair — a 6-hour window and a 3-day window — at a lower burn rate,
say a burn rate that consumes **10% of the monthly budget** over 6 hours:

```
10% of 43.2 minutes            = 4.32 minutes of bad time allowed inside the 6h window
burn rate for that threshold   = (4.32 / 360) / 0.001 = 1.2... 
```

At this depth you are tuning a specific number against a specific team's tolerance for
after-hours pages, and this is where teams reasonably diverge — the derivation method is the
part that transfers, not the exact multiplier. What must not diverge is the two-window pairing
itself: a single-window burn-rate alert, at any threshold, will eventually either false-positive
on noise or arrive too late on a genuine slow burn, because one window cannot distinguish "brief
spike, already over" from "sustained degradation, still happening."

## How a USE-based SLO differs

Everything above assumed a success-ratio SLI, which fits RED signals naturally: good events over
valid events. A resource from doc 02 does not have "events" in the same sense, so a USE-based SLO
is usually phrased on **saturation** instead: the fraction of time a resource spends below a
capacity threshold, rather than the fraction of requests that succeed.

For `orders-db`, a reasonable saturation SLO is: **connections in use stay below 90% of
`max_connections` (540 of 600) for at least 99.5% of any 30-day window.** This is a genuinely
different kind of promise — it says nothing about whether any given query succeeded, only that
the database was not close to running out of a specific finite resource. It is less common in
practice than a request-based SLO, for a real reason: nobody outside engineering cares directly
about connection headroom, the way finance cared about abandoned carts. Where it earns its keep is
capacity planning — "we breached our connection-headroom SLO three times last quarter" is a much
more concrete argument for a bigger instance class than "the graph looked spiky sometimes."

⚠️ Do not build a USE-based SLO for a resource whose saturation the service already protects
against with backpressure (for example, a bounded queue that sheds load rather than growing
unboundedly). In that case the *service's* RED-based SLO already reflects the consequence of
saturation — a second, resource-level SLO on top of it mostly duplicates the same signal a step
removed from what anyone actually acts on.

## What to take away

1. An SLI is a ratio of good events to valid events, and both halves need a precise, written-down
   definition — "good" for `checkout-api` means non-5xx *and* under 500ms, not just "not an error."
2. An SLO target should be derived from a business consequence (Riverbend's $15,000/month
   tolerance) and converted into a duration via the error budget, not chosen because it is a round
   number other companies use.
3. The error budget is `window_minutes × (1 − SLO)` — for a 99.9% monthly SLO, that is
   43,200 × 0.001 = 43.2 minutes, and it is spent by incidents and by risky deploys alike.
4. Burn rate is `observed bad fraction ÷ allowed bad fraction`. A burn rate of 720 means the whole
   month's budget would be gone in about an hour at that rate.
5. Multi-window multi-burn-rate alerting requires two windows (a short one and a long one) to
   agree before paging, because a single window cannot tell a brief blip from a sustained burn.
6. A USE-based SLO is possible (saturation staying under a threshold) but rarer, and mostly useful
   for capacity planning conversations rather than as a page-worthy signal.

# Schedules, Time Zones, and Missed Firings

Time is where CronJobs go wrong most often, and the failures are quiet. A wrong schedule does
not crash anything — it just runs your work at a different time than you intended, or stops
running it and tells nobody. This doc builds cron syntax from scratch, then covers the three
time-related traps that account for most real incidents: time zone and daylight saving, the
missed-schedule wall, and simultaneous firings.

## Cron expressions from first principles

A Kubernetes schedule is a **five-field** expression. There is no seconds field, which surprises
people coming from Quartz or from `robfig/cron`'s optional six-field mode.

```
┌───────────── minute        0-59
│ ┌─────────── hour          0-23
│ │ ┌───────── day of month  1-31
│ │ │ ┌─────── month         1-12  (or JAN-DEC)
│ │ │ │ ┌───── day of week   0-6   (0 = Sunday; or SUN-SAT)
│ │ │ │ │
* * * * *
```

Read an expression as a **match predicate, not an interval.** This single reframing prevents
most schedule bugs. The controller does not think "start now, then wait an hour." It thinks:
"of all the minutes in the calendar, which ones match all five fields?" Every minute that
matches is a firing.

Each field accepts four constructs, which compose:

| Construct | Example | Means |
|---|---|---|
| Wildcard | `*` | every value in the field's range |
| Single value | `10` | exactly that value |
| List | `1,15,30` | any of those values |
| Range | `9-17` | every value in the inclusive range |
| Step | `*/5`, `0-30/10` | every *n*th value across the field, or across the given range |

Now the schedules from the Riverbend fleet, each derived rather than just stated:

- **`*/5 * * * *`** (`session-reaper`). Minute matches 0, 5, 10, … 55 — twelve values. Hour,
  day, month, weekday are all wildcards. So it matches 12 minutes per hour × 24 hours =
  **288 firings a day**, at :00, :05, :10 and so on past every hour.
- **`10 * * * *`** (`invoice-rollup`). Minute must be exactly 10, everything else free.
  **24 firings a day**, at 00:10, 01:10, 02:10, …
- **`0 2 * * *`** (`payout-settlement`). Minute 0 and hour 2. **One firing a day**, at 02:00.
- **`0 6 * * 1-5`** (`partner-sftp-export`). Minute 0, hour 6, weekday in Monday–Friday.
  **Five firings a week**, at 06:00 on weekdays.
- **`0 4 * * 0`** (`db-vacuum`). Minute 0, hour 4, weekday 0 = Sunday. **One firing a week.**

### The step-value trap

`*/5` behaves intuitively because 5 divides 60. `*/7` does not. Work out the matches: 0, 7, 14,
21, 28, 35, 42, 49, 56 — then the hour rolls over and the next match is 0. So the gaps are
seven minutes eight times and then **four minutes once per hour**. The job you thought ran
"every 7 minutes" has an irregular cycle, which matters if the work takes six minutes.

The general rule: `*/n` produces even intervals only when `n` divides the field's range size
(60 for minutes, 24 for hours). Safe minute steps are 1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30.
Safe hour steps are 1, 2, 3, 4, 6, 8, 12. Anything else has a short gap at the boundary.

### ⚠️ The day-of-month / day-of-week OR rule

This is the highest-consequence syntax trap in cron, and it is inherited from classic Unix cron
rather than invented by Kubernetes.

The five fields are normally combined with AND — every field must match. **But if both
day-of-month and day-of-week are restricted (neither is `*`), they are combined with OR.**

So `0 3 1 * 1` does not mean "3am on the first of the month, if it's a Monday." It means "3am on
the first of the month, **or** 3am on any Monday" — which is roughly five firings a month, not
the one you wanted. A schedule intended to run rarely runs constantly.

The practical rule: **restrict at most one of day-of-month and day-of-week.** If you genuinely
need "the first Monday of the month", cron cannot express it. Schedule it for every Monday and
have the job itself exit early when the date is past the 7th. That check is three lines of code
and it is far more readable than any cron trick.

### Shorthand descriptors

Kubernetes accepts the classic macros: `@yearly` (equivalent to `@annually`), `@monthly`,
`@weekly`, `@daily` (equivalent to `@midnight`), and `@hourly`.

They read nicely, but they are all pinned to minute 0 and hour 0, which is precisely the worst
choice at fleet scale — see the thundering-herd section below. `@daily` across 80 CronJobs means
80 simultaneous firings at midnight. Prefer explicit expressions so that you can spread them.

### What is *not* supported

Coming from Quartz or Spring's scheduler, you will reach for syntax that does not exist here:

- **No seconds field.** `*/30 * * * * *` (six fields) is rejected.
- **No `L`** (last day of month), **no `W`** (nearest weekday), **no `#`** (nth weekday of the
  month). "Run on the last day of the month" is not expressible; run daily and exit early
  unless tomorrow is the 1st.
- **No year field.**
- Avoid `?`. Some cron libraries treat it as a synonym for `*`; relying on it makes your
  manifest non-portable across the tooling in your pipeline for no benefit.

Since there is no `--dry-run` that *evaluates* a schedule, validate syntax against the API
server before you merge:

```bash
kubectl apply --dry-run=server -f cronjob.yaml
```

This catches malformed expressions (the API server parses the schedule at admission time) but it
cannot catch a *valid expression that means something you did not intend* — such as the OR rule
above. For semantic checking, compute the next several firings with a library or a scratch
script, and write them into the pull request description. Reviewing "next five firings: Mon
06:00, Tue 06:00, …" catches intent bugs that reviewing `0 6 * * 1-5` does not.

## Time zones

### What happens when you do not set one

If `.spec.timeZone` is absent, the schedule is interpreted in **the local time zone of the
kube-controller-manager process**. Not the cluster's, not yours — the controller's.

On most managed clusters that is UTC, and it is easy to conclude the default is UTC. It is not;
it is an implementation detail of how the control plane containers were built, and on
self-managed clusters it can be whatever the host's `/etc/localtime` says. Two nasty properties
follow: your schedule can change meaning during a control-plane upgrade, and it can differ
between your dev cluster and production.

**Always set `timeZone` explicitly**, even when the value is `"Etc/UTC"`. It costs one line and
converts an invisible dependency into a declared one.

### The `timeZone` field

`.spec.timeZone` takes an IANA time zone name (`America/New_York`, `Asia/Kolkata`, `Etc/UTC`).
It went alpha in 1.24, beta in 1.25, and GA in 1.27, so on any currently supported cluster it is
available — but if you are on something older, check before relying on it.

```yaml
spec:
  schedule: "0 2 * * *"
  timeZone: "America/New_York"   # 02:00 New York time, whatever that is in UTC today
```

Three caveats:

⚠️ **You cannot combine `timeZone` with a `CRON_TZ=` or `TZ=` prefix inside the schedule
string.** Before the field existed, people discovered that the underlying cron library accepted
`CRON_TZ=America/New_York 0 2 * * *` and used that as a workaround. Validation now rejects
specifying both, and the prefix form was never a supported Kubernetes API. Migrate to the field.

⚠️ **The name is resolved by the control plane, against the control plane's time zone
database.** An unknown or misspelled name is rejected at admission (good), but a zone whose
rules changed recently — several countries have altered DST policy in the last few years —
depends on the controller image's `tzdata` being current. This is a real, if rare, source of
"the job ran an hour off" after a government changed the rules and before the cluster was
upgraded.

⚠️ **The pod's own time zone is unrelated.** Setting `timeZone` on the CronJob does not set `TZ`
inside your container. If your code formats dates or computes "yesterday", it is using the
container's zone, which is UTC unless you changed it. A job scheduled in `America/New_York` that
internally computes "yesterday" in UTC will, for runs between 00:00 and 05:00 local, disagree
with itself about which day it is processing. Pass the intended date in explicitly — doc 05
makes this the centre of its argument.

### Daylight saving: the failure you get twice a year

Riverbend originally scheduled `payout-settlement` as `0 2 * * *` in `America/New_York`, because
finance wanted payouts to land before the US business day. Consider what the calendar does to
that schedule.

On the second Sunday in March, New York clocks jump from 01:59:59 to 03:00:00. **The local time
02:00 does not exist on that date.** Since a cron expression is a match predicate over local
wall-clock time, nothing matches, and the firing is skipped. One day a year, 18,000 sellers do
not get paid, and the only signal is the absence of a run.

On the first Sunday in November, clocks go from 01:59:59 back to 01:00:00, so **01:00–01:59
happens twice.** A schedule of `0 1 * * *` is ambiguous on that date: whether it fires once or
twice depends on the cron implementation's handling of repeated local times, and you should not
want to find out empirically with a job that moves money.

The two robust options, in order of preference:

1. **Schedule in UTC and let the local time float.** `0 6 * * *` with `timeZone: "Etc/UTC"` is
   always exactly 24 hours after the previous run, always exists, and is never ambiguous. It
   lands at 02:00 New York in winter and 01:00 in summer. If finance can accept a floating local
   time, this is strictly the best answer, and it is what Riverbend switched to.
2. **Keep local time but avoid the DST window.** If the local hour genuinely matters — a report
   that must be on someone's desk at 08:00 their time — then schedule between **03:00 and 23:00
   local**, where no zone's DST transition can create a nonexistent or repeated hour. `0 2` is
   dangerous; `0 5` is not.

The rule to remember: **anything scheduled between midnight and 03:00 local in a DST-observing
zone will, once or twice a year, either not run or run twice.** If correctness depends on the
run count, do not schedule it there.

## Missed firings, and the wall that stops your CronJob forever

This is the trap that causes "the CronJob just stopped and nobody noticed for four days."

### How the controller handles a missed firing

Recall from doc 00 that each pass computes the scheduled times between `lastScheduleTime` and
now. Normally there is exactly one. But if the control plane was down, or the CronJob was
suspended, or the controller was starved, there may be several.

**The controller fires at most the most recent one and discards the rest. It never backfills.**
If `invoice-rollup` misses 03:10, 04:10, and 05:10 because the control plane was unavailable,
you get one run — for 05:10 — and the 03:00 and 04:00 hours of invoice data are simply not
rolled up until somebody notices. Planning for this is doc 05's backfill section.

### `startingDeadlineSeconds`: run late, or skip?

By default, a missed firing runs as soon as the controller notices, however late that is. For
`session-reaper` that is harmless. For a job that emails a "good morning" digest, running at
14:00 because the control plane was busy is worse than not running at all.

`.spec.startingDeadlineSeconds` expresses that preference: **if we cannot start within this many
seconds of the scheduled time, skip this firing entirely.**

```yaml
spec:
  schedule: "0 6 * * 1-5"
  startingDeadlineSeconds: 900   # start by 06:15 or not at all
```

Choosing the value is a product decision disguised as a config field. Ask: *how late is still
useful?* For `partner-sftp-export`, the partner's ingestion window closes at 07:00, so anything
after 06:45 is wasted work that will be rejected at the far end — 2700 seconds (45 minutes) is
the honest answer. For `payout-settlement`, a late payout is much better than a missing one, so
you want a generous deadline, not a tight one.

⚠️ Do not set it below about 10 seconds. The controller does not evaluate every CronJob
continuously, so a very short deadline can expire before the CronJob is next considered, turning
every firing into a skip. You get a job that never runs and no obvious reason why.

### The 100-missed-schedules wall

Here is the part that surprises everyone. The controller has to enumerate missed firings to find
the most recent one. To avoid unbounded work, it gives up after 100:

```
Cannot determine if job needs to be started: too many missed start time (> 100).
Set or decrease .spec.startingDeadlineSeconds or check clock skew.
```

It emits a `TooManyMissedTimes` event and **stops scheduling that CronJob.** It does not recover
on its own. Advancing past the wall requires the window it searches to shrink, and the window
only shrinks if `lastScheduleTime` advances — which requires a firing — which is exactly what
has stopped happening. That is the deadlock.

Now the arithmetic that makes this a practical hazard rather than a theoretical one. With
`startingDeadlineSeconds` unset, the search window starts at `lastScheduleTime`, so the missed
count is however many firings fit in the outage:

| CronJob | Schedule | Firings per hour | Hours to reach 100 missed |
|---|---|---|---|
| `session-reaper` | `*/5 * * * *` | 12 | **8h 20m** |
| `invoice-rollup` | `10 * * * *` | 1 | 100h ≈ 4.2 days |
| `payout-settlement` | `0 2 * * *` | 1/24 | 100 days |

Eight hours and twenty minutes. Suspend `session-reaper` on a Friday afternoon to do some
database maintenance, unsuspend it on Monday morning, and it is past the wall — permanently
stopped, with a schedule that looks perfectly normal in `kubectl get cronjob`. Riverbend lost
three days of session reaping to exactly this before adding the field.

**The fix is to set `startingDeadlineSeconds` on every CronJob**, because when it is set the
search window is bounded by the deadline instead of by `lastScheduleTime`. With
`startingDeadlineSeconds: 200` on a five-minute schedule, the controller only ever looks back
200 seconds — at most one missed firing — so the count cannot approach 100 no matter how long
the outage was.

Read that message's own advice literally: "set or decrease `.spec.startingDeadlineSeconds`". The
error is telling you the remedy. Doc 04 has the recovery procedure for a CronJob already stuck
behind the wall.

The general sizing heuristic, which also keeps the deadline meaningful:

> Set `startingDeadlineSeconds` to the longest delay after which the run is still worth doing,
> and never larger than about 50 × the schedule interval. For a 5-minute job that is 200s; for
> an hourly job, 1800s is usually both meaningful and safe.

## Clock skew

The controller compares the schedule against its own host's clock. If that clock is wrong, every
schedule is wrong by the same offset, and if it jumps *backwards*, previously fired schedules can
be recomputed as missed — which is the other way the 100-missed wall gets hit, and the reason the
error message mentions skew.

You mostly cannot do anything about this on a managed control plane except know the symptom: all
CronJobs cluster-wide shifting together, or a burst of `TooManyMissedTimes` events across
unrelated CronJobs. On self-managed clusters, NTP on the control plane nodes is the fix, and
control-plane clock skew belongs in your monitoring.

One reassurance: in a highly available control plane with three `kube-controller-manager`
replicas, only the leader runs the CronJob controller. Leader election prevents three controllers
from firing the same schedule three times. Duplicate firings from HA control planes are not a
thing you need to defend against.

## Simultaneous firings, and how to spread them

Riverbend's 412 CronJobs were written by 30 teams, and people naturally choose round numbers. An
audit found **96 CronJobs whose schedule fired at exactly 00:00 UTC** — a mix of `0 0 * * *`,
`@daily`, and `0 0 * * 0`.

What that produces, in sequence: 96 Jobs created within the same second, 96+ pods hitting the
scheduler at once, a scale-up request to the cluster autoscaler for the capacity that 96
simultaneous pods need, several minutes of pods Pending while nodes boot, then 96 image pulls
saturating the registry and node network. Individually trivial jobs; collectively a
self-inflicted load spike every midnight. And because every one of them has the *same*
`startingDeadlineSeconds`, if the spike is bad enough they can all skip together.

There is no jitter field on a CronJob. You spread firings yourself, and the trick is to do it
**deterministically** rather than randomly, so that the manifest in git says exactly when the
job runs:

```python
# Generate a per-job minute offset from the job's own name.
# Same name always yields the same minute, so schedules are stable across regenerations.
import zlib
def minute_for(name: str) -> int:
    return zlib.crc32(name.encode()) % 60

minute_for("catalog-reindex")   # e.g. 37  ->  schedule: "37 3 * * *"
minute_for("db-vacuum")         # e.g. 12  ->  schedule: "12 4 * * 0"
```

Spreading 96 jobs across 60 minutes leaves about 1.6 jobs per minute instead of 96 in one
second — a 60× reduction in peak concurrency for no loss of function, since none of these jobs
cared about the exact minute. If they are generated from a Helm chart or a template, compute the
offset at template time so the rendered manifest is explicit and reviewable.

⚠️ **Do not implement jitter with `sleep $((RANDOM % 300))` at the top of the container.** It
looks equivalent and is worse in three ways: the pod occupies its memory request while sleeping,
the sleep counts against `activeDeadlineSeconds`, and your run duration metrics become
meaningless because they now include a random constant. Spread the *schedule*, not the work.

For work that genuinely must be staggered by dependency rather than by clock — "reindex only
after the rollup finishes" — you are describing a pipeline, and doc 11 argues you should use a
workflow engine rather than guessing at offsets that will drift as runtimes change.

## What to take away

1. A cron expression is a match predicate over wall-clock minutes, not an interval timer. Read
   every schedule as "which minutes match all five fields?"
2. `*/n` is only evenly spaced when `n` divides the field range. `*/7` has a four-minute gap
   every hour.
3. If both day-of-month and day-of-week are restricted, they are OR'd, not AND'd. Restrict at
   most one, and express "first Monday of the month" in code, not in cron.
4. Always set `timeZone` explicitly. With it unset you inherit the controller-manager's local
   zone, which is not guaranteed to be UTC and can change under you.
5. Never schedule correctness-critical work between midnight and 03:00 local in a DST zone. Use
   UTC and accept a floating local time, or move to 03:00–23:00 local.
6. Always set `startingDeadlineSeconds` — not mainly to skip late runs, but because it bounds
   the missed-schedule search and so prevents the 100-missed wall that stops a CronJob forever.
   A `*/5` schedule reaches that wall after only 8h20m of suspension.
7. Spread firings deterministically from the job name. Round-number schedules concentrate a
   whole fleet into one second.

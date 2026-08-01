# Time and Timezones

Go's `time` package is more precise and more subtle than most languages' — `time.Time` carries a monotonic clock reading alongside wall-clock time, layouts are built from a specific reference instant instead of format specifiers, and durations are just `int64` nanoseconds with all the arithmetic footguns that implies. None of this shows up as a compile error. It shows up as tests that are flaky near midnight, timestamps that are wrong by exactly one timezone offset, or a background loop that leaks a timer every second forever. This file covers the time-specific mistakes that make it into production because they're invisible until the exact conditions that trigger them.

## 1. Calling `time.Now()` Directly Inside Business Logic

**The Problem:** Business logic that calls `time.Now()` internally can't be tested deterministically — every test run depends on the actual wall-clock moment it happens to execute, which makes it impossible to reliably test time-dependent branches (expiry, scheduling, "is it business hours") and produces tests that are flaky specifically near boundaries like midnight or month-end.

**❌ Bad**
```go
func IsExpired(createdAt time.Time) bool {
	return time.Now().Sub(createdAt) > 24*time.Hour // BUG: not injectable, not fakeable
}

func TestIsExpired(t *testing.T) {
	createdAt := time.Now().Add(-25 * time.Hour)
	if !IsExpired(createdAt) {
		t.Error("expected expired")
	}
	// works today, but any assertion tied to "now" is only as stable as the
	// clock is at the moment the test happens to run
}
```

**Why it's wrong:**
- There's no way to test the exact boundary (`23:59:59` vs `24:00:01` old) deterministically — the test result depends on real elapsed wall-clock time between two `time.Now()` calls, however small.
- Any logic that behaves differently around DST transitions, month boundaries, or leap years can't be exercised in a test at all unless the test happens to run during that exact window.

**✅ Good**
```go
type Clock interface {
	Now() time.Time
}

type realClock struct{}

func (realClock) Now() time.Time { return time.Now() }

func IsExpired(clk Clock, createdAt time.Time) bool {
	return clk.Now().Sub(createdAt) > 24*time.Hour
}

// in tests:
type fakeClock struct{ t time.Time }

func (f fakeClock) Now() time.Time { return f.t }

func TestIsExpired(t *testing.T) {
	fixed := time.Date(2024, 3, 1, 12, 0, 0, 0, time.UTC)
	clk := fakeClock{t: fixed}

	createdAt := fixed.Add(-25 * time.Hour)
	if !IsExpired(clk, createdAt) {
		t.Error("expected expired")
	}
}
```

**Why it works / Explanation:** Injecting a `Clock` (or, for simpler cases, just accepting a `now time.Time` parameter directly) moves "what time is it" out of the function and into something the caller controls. Production code wires in `realClock{}`; tests wire in a fake with an exact, fixed instant, so every boundary condition — exactly 24 hours, one nanosecond before, across a DST change — becomes reproducible and independent of when the test suite happens to run.

**Design principle:** Dependency injection isn't just for databases and HTTP clients — time is an external dependency too, and testable code treats it as one.

---

## 2. Comparing `time.Time` Values with `==`

**The Problem:** `time.Time` is a struct that can carry a monotonic clock reading in addition to its wall-clock value. Two `time.Time` values representing the exact same instant can still compare unequal with `==` if only one of them has a monotonic reading attached — which happens routinely when one value came from `time.Now()` and the other was parsed from a string or round-tripped through JSON.

**❌ Bad**
```go
t1 := time.Now() // carries a monotonic reading

data, _ := json.Marshal(t1)
var t2 time.Time
json.Unmarshal(data, &t2) // parsed from RFC3339 text — no monotonic reading

fmt.Println(t1 == t2)     // BUG: false, even though both represent the same instant
fmt.Println(t1.Equal(t2)) // true
```

**Why it's wrong:**
- `==` on `time.Time` compares the struct fields directly, including the monotonic reading — so a value straight from `time.Now()` and the "same" value after a round trip through JSON, a database, or `time.Parse` will frequently compare unequal even though they refer to the identical point in time.
- This is exactly the kind of bug that passes every test written with two independently-constructed `time.Now()`-ish values and then fails unpredictably once a value has actually been through serialization — e.g. a cache-invalidation check or a deduplication key comparison that "sometimes" doesn't match.

**✅ Good**
```go
if t1.Equal(t2) {
	fmt.Println("same instant")
}

// If t1/t2 are used as map keys or in a struct compared with ==,
// strip the monotonic reading explicitly and normalize the Location:
t1Clean := t1.UTC().Round(0) // Round(0) strips the monotonic reading
```

**Why it works / Explanation:** `Equal` compares the instant in time the two values represent, ignoring both the monotonic reading and the `Location` the value happens to be expressed in — it's the correct comparison for "are these the same moment," which is almost always what's actually meant. The Go documentation for `time.Time` calls this out explicitly: don't use `==`, and don't rely on monotonic readings surviving serialization, because encoders (JSON, gob, etc.) always strip them.

**Design principle:** Use the type's provided semantic-equality method instead of a structural `==` whenever a type can validly represent the same logical value in more than one internal representation.

---

## 3. Leaking Timers From `time.After` Inside a Loop

**The Problem:** `time.After` allocates a brand-new `time.Timer` every time it's called, and that timer isn't eligible for garbage collection until it actually fires. Calling `time.After` inside a `select` that runs repeatedly in a loop — rather than once outside it — creates and abandons a new timer on every iteration, all of which sit around consuming memory until their (often long) duration elapses. This is the same underlying resource as an unstopped `time.Ticker`, but it's worth calling out on its own because the leak comes from a completely idiomatic-*looking* line of code, not from an obviously missing `Stop()`.

**❌ Bad**
```go
func consume(ch <-chan Message) {
	for {
		select {
		case msg := <-ch:
			process(msg)
		case <-time.After(5 * time.Second): // BUG: new timer allocated every loop iteration
			fmt.Println("no message for 5s")
		}
	}
}
```

**Why it's wrong:**
- Every time the `msg` case fires (which, on a busy channel, could be thousands of times a second), a fresh 5-second timer is created and then simply discarded — it still has to sit in the runtime's timer heap and fire 5 seconds later before it can be collected.
- Under sustained load this is a steady, load-proportional memory and scheduler-overhead leak: `runtime.NumGoroutine()` stays flat (there's no goroutine per timer), but heap allocations and pending-timer count climb continuously, which is easy to miss if you're only watching goroutine counts for leaks.

**✅ Good**
```go
func consume(ch <-chan Message) {
	timer := time.NewTimer(5 * time.Second)
	defer timer.Stop()

	for {
		select {
		case msg := <-ch:
			process(msg)
			if !timer.Stop() {
				<-timer.C // drain if it already fired before we could stop it
			}
			timer.Reset(5 * time.Second)
		case <-timer.C:
			fmt.Println("no message for 5s")
			timer.Reset(5 * time.Second)
		}
	}
}
```

**Why it works / Explanation:** Creating one `time.Timer` outside the loop and calling `Reset` on it reuses the same underlying timer for the entire life of the loop instead of allocating a new one per iteration. The `Stop`-then-drain dance before `Reset` matters because resetting a timer that might have already fired (and whose value is sitting unread in `timer.C`) without draining it first can cause a stale fire to be observed on the next iteration — this is the pattern the standard library's own docs recommend for reusing timers safely.

**Design principle:** Treat timers like any other reusable resource — allocate once, reset in place, and `Stop()` when done, rather than allocating fresh inside a hot loop.

---

## 4. Server-Local Timezone Assumptions Instead of Explicit UTC

**The Problem:** Calling `time.Now()` without normalizing to UTC ties your stored/compared timestamps to whatever timezone the machine running the code happens to be configured with — which is frequently different between a developer's laptop, a CI runner, and a production container, and can even change for the *same* machine across a DST transition.

**❌ Bad**
```go
func logEvent(name string) {
	t := time.Now() // BUG: local to whatever TZ the process/container is set to
	db.Exec(`INSERT INTO events (name, occurred_at) VALUES ($1, $2)`, name, t)
}
```

**Why it's wrong:**
- If the dev laptop is set to `Asia/Kolkata` (UTC+5:30) and the production container is set to `UTC` (or vice versa, or the container's base image changes its default TZ data), the exact same code path stores visibly different wall-clock values for the same real-world moment — bugs that only reproduce in one environment and not the other.
- Comparisons like `occurred_at BETWEEN $start AND $end` become environment-dependent too, and around DST transitions a server left in a zone that observes DST can even see local time appear to go backward or skip an hour, corrupting any "is this before/after" logic that assumes monotonically increasing local time.

**✅ Good**
```go
func logEvent(name string) {
	t := time.Now().UTC() // always store and compare in UTC
	db.Exec(`INSERT INTO events (name, occurred_at) VALUES ($1, $2)`, name, t)
}

// Only convert to a local zone at the presentation layer, on purpose:
func displayTime(t time.Time, userLoc *time.Location) string {
	return t.In(userLoc).Format("Jan 2, 2006 3:04 PM")
}
```

**Why it works / Explanation:** Normalizing every stored or compared timestamp to UTC removes the ambiguity entirely — UTC has no DST transitions and is the same everywhere, so the same instant produces the same stored value regardless of which machine or container ran the code. Local time becomes purely a display concern, applied explicitly and only when rendering something for a specific user, never baked into how time is stored or compared internally.

**Design principle:** Store and compute in a single canonical timezone (UTC); treat any other timezone as a presentation-layer transformation applied at the boundary, never as internal state.

---

## 5. `time.Parse` Layout String Confusion

**The Problem:** Go doesn't use format specifiers like `YYYY-MM-DD` — layouts are written by formatting one specific reference instant (`Mon Jan 2 15:04:05 MST 2006`, i.e. `01/02 03:04:05PM '06 -0700`) the way you want your dates to look. Anyone coming from `strftime`-style or Python/Java date formatting reaches for the wrong syntax almost on reflex, and the failure mode ranges from a loud parse error to a silent misparse that swaps fields.

**❌ Bad**
```go
// Loud failure: not a valid Go layout at all.
_, err := time.Parse("YYYY-MM-DD", "2024-01-15")
fmt.Println(err)
// parsing time "2024-01-15" as "YYYY-MM-DD": cannot parse "2024-01-15" as "YYYY"

// Silent, worse failure: a *valid* layout, but the wrong one for the source
// data's actual field order (source is DD/MM/YYYY, layout assumes MM/DD/YYYY).
layout := "01/02/2006" // MM/DD/YYYY
input := "03/04/2024"  // intended: 3 April 2024, from a DD/MM/YYYY source
t, _ := time.Parse(layout, input)
fmt.Println(t) // 2024-03-04 00:00:00 +0000 UTC — parsed as March 4th, not April 3rd!
```

**Why it's wrong:**
- The first case fails loudly and immediately, which is annoying but at least safe — you find out right away that the layout is nonsense.
- The second case is far more dangerous: `"01/02/2006"` and `"03/04/2024"` are both individually valid, so `time.Parse` happily returns a `time.Time` — just the wrong one, with month and day silently swapped, and nothing about the return value hints that anything went wrong.

**✅ Good**
```go
// Use the correct reference-based layout for the source format:
layout := "02/01/2006" // DD/MM/YYYY, matching the actual source data
t, err := time.Parse(layout, "03/04/2024")
if err != nil {
	log.Fatal(err)
}
fmt.Println(t) // 2024-04-03 — April 3rd, correctly

// Prefer a named standard layout whenever the source honors it —
// there's no ambiguity to get wrong in the first place:
t2, err := time.Parse(time.RFC3339, "2024-04-03T00:00:00Z")
```

**Why it works / Explanation:** Go's reference-time layout scheme means there is no room for "MM vs DD" ambiguity once you write the layout correctly — but that also means you have to actually know the source format's real field order, not guess based on convention. Whenever the upstream system can be made to send (or already sends) a standard format like RFC3339, use `time.RFC3339` and skip hand-rolled layouts entirely — it's the single most effective way to eliminate this class of bug.

**Design principle:** Verify the exact field order of external date formats against real sample data before writing a layout — never assume a locale's date convention matches your own.

---

## 6. Duration Arithmetic Mistakes

**The Problem:** `time.Duration` is just an `int64` count of nanoseconds — functions like `time.Sleep` take a `Duration`, not "a number of seconds," so a bare integer literal without multiplying by the right unit constant compiles fine and does something wildly different from what was intended.

**❌ Bad**
```go
time.Sleep(5) // BUG: 5 nanoseconds, not 5 seconds — returns almost instantly
```

**Why it's wrong:**
- `5` is a valid `time.Duration` value — it's just 5 nanoseconds, not 5 seconds, and the compiler has no way to know that's not what you meant, since `Duration` is just `int64` under the hood.
- The bug is easy to miss in a quick manual test (the function returns, nothing panics) and only becomes obvious when the "5-second backoff" or "5-second poll interval" it was supposed to implement turns out to be hammering a downstream service thousands of times a second instead.
- The same unit confusion also produces overflow in the other direction: multiplying a `Duration` by a very large integer (e.g. computing a duration from a user-supplied "number of hours" without bounds-checking) can overflow `int64` nanoseconds and silently wrap into a small or negative duration instead of erroring.

**✅ Good**
```go
time.Sleep(5 * time.Second) // explicit unit — 5 seconds

// Guard against overflow when the multiplier is untrusted/user-supplied:
func safeDuration(hours int64) (time.Duration, error) {
	const maxHours = 24 * 365 * 10 // sanity bound, adjust to your domain
	if hours < 0 || hours > maxHours {
		return 0, fmt.Errorf("hours out of range: %d", hours)
	}
	return time.Duration(hours) * time.Hour, nil
}
```

**Why it works / Explanation:** Always multiplying by an explicit `time.*` unit constant (`time.Second`, `time.Millisecond`, etc.) makes the intended unit visible at the call site and impossible to get wrong by omission — Go's own idiom for duration literals is exactly this multiplication, not a bare number. When the multiplier itself is computed from untrusted input, bounds-check it before multiplying so an unreasonable value produces an explicit error instead of a silently wrapped `int64`.

**Design principle:** Never write a bare numeric literal where a `Duration` is expected — the unit constant isn't decoration, it's the only thing making the value's meaning unambiguous.

---

## 7. `AddDate` Month-Length Rollover Surprises

**The Problem:** `AddDate(years, months, days)` adds to the calendar fields and then normalizes the result — it does not clamp to the target month's actual last day. Adding one month to January 31st doesn't produce "the last day of February"; it overflows past February entirely and lands in March, because "February 31st" isn't a real date and Go normalizes it forward instead of erroring or clamping.

**❌ Bad**
```go
t := time.Date(2023, time.January, 31, 0, 0, 0, 0, time.UTC)
next := t.AddDate(0, 1, 0)
fmt.Println(next) // 2023-03-03 00:00:00 +0000 UTC — BUG: not "end of February", not even in February
```

**Why it's wrong:**
- February 2023 has 28 days; "January 31 + 1 month" computes as "February 31," which normalizes by rolling the extra 3 days into March, landing on March 3rd — silently, with no error, and easy to miss unless you print the actual result.
- Any "same day next month" billing, subscription-renewal, or reporting logic built on a naive `AddDate(0, 1, 0)` will drift for every month-end date, and the drift amount depends on the target month's length (0 days into March for a 31-day-to-31-day transition, but 2-3 days for transitions into February).

**✅ Good**
```go
// If you specifically need "the last day of next month" semantics,
// compute it from month boundaries instead of from AddDate on the day itself.
func lastDayOfNextMonth(t time.Time) time.Time {
	firstOfThisMonth := time.Date(t.Year(), t.Month(), 1, 0, 0, 0, 0, t.Location())
	firstOfMonthAfterNext := firstOfThisMonth.AddDate(0, 2, 0)
	return firstOfMonthAfterNext.AddDate(0, 0, -1)
}

t := time.Date(2023, time.January, 31, 0, 0, 0, 0, time.UTC)
fmt.Println(lastDayOfNextMonth(t)) // 2023-02-28 — correct
```

**Why it works / Explanation:** Rather than adding a month directly to a day-of-month that might not exist in the target month, the fix anchors on the first day of a month (which always exists) and derives "last day" by stepping to the first day of the *following* month and subtracting one day — a calculation that's always well-defined regardless of month length or leap years. Whenever calendar-exact "same day" or "end of month" semantics matter (billing cycles, recurring schedules), test explicitly against month-end dates like the 29th–31st, since those are exactly where naive `AddDate` usage breaks.

**Design principle:** Don't assume calendar arithmetic is uniform across months — anchor date math on boundaries that are always well-defined (the 1st of a month) rather than on day numbers that may not exist in every month.

---

## Key Takeaways
- `time.Now()` called directly inside business logic makes tests non-deterministic — inject a `Clock` or pass `now time.Time` explicitly.
- `time.Time` carries an optional monotonic reading, so `==` can report unequal for the same instant — always compare with `.Equal()`.
- `time.After` inside a loop allocates and leaks a timer every iteration — create one `time.Timer`/`time.Ticker` outside the loop and `Reset` it.
- `time.Now()` without `.UTC()` ties stored/compared timestamps to the local machine's timezone — store and compare in UTC, convert to local only for display.
- Go's layout strings are reference-time based, not format specifiers — verify field order against real data, and prefer `time.RFC3339` when possible.
- A bare numeric literal where a `Duration` is expected uses nanoseconds — always multiply by an explicit `time.*` unit constant.
- `AddDate` normalizes overflowing days into the next month instead of clamping — anchor "end of month" logic on month boundaries, not day arithmetic.

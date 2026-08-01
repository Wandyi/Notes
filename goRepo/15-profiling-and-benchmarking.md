# Profiling and Benchmarking Go Programs

Go ships with excellent, built-in profiling and benchmarking tools (`pprof`, `testing.B`, `go tool trace`), but they are easy to misuse in ways that produce confident, wrong conclusions — a benchmark that measures nothing, a profile type that can't see the problem, a "faster" number that's just machine noise. Every mistake here costs the same thing: engineering time spent chasing the wrong target, or worse, shipping a "fix" that didn't fix anything. This file covers the measurement pitfalls that make profiling and benchmarking lie to you.

## 1. No Profiling Instrumented in Production at All

**The Problem:** Teams often only reach for profiling after an incident is already underway, discover there is no instrumentation in the running binary, and lose the incident window waiting on a new build/rollout just to be able to look. Wiring up `net/http/pprof` ahead of time costs almost nothing and turns "we need to ship a build to investigate" into "we can look right now."

**❌ Bad**
```go
package main

import (
	"log"
	"net/http"
)

func main() {
	http.HandleFunc("/api/orders", ordersHandler)
	log.Fatal(http.ListenAndServe(":8080", nil)) // BUG: no pprof endpoints registered anywhere
}
```

**Why it's wrong:**
- When a CPU spike or memory-growth incident hits, there is no way to capture a live profile without shipping a new build with instrumentation added — by the time it deploys, the incident may already be over, or worse, still ongoing but unobserved.
- A common follow-up mistake, once someone does add `net/http/pprof`, is registering it on the *same* public-facing mux that serves real traffic — `net/http/pprof`'s `init()` registers handlers on `http.DefaultServeMux`, so passing `nil` as the handler to a public listener exposes `/debug/pprof/*` to the internet, leaking stack traces and giving anyone a free CPU-exhaustion lever via the profile-duration endpoints.

**✅ Good**
```go
package main

import (
	"log"
	"net/http"
	_ "net/http/pprof" // registers /debug/pprof/* on http.DefaultServeMux
)

func main() {
	// Public-facing API on its own mux — pprof is NOT reachable here.
	mux := http.NewServeMux()
	mux.HandleFunc("/api/orders", ordersHandler)
	go func() { log.Fatal(http.ListenAndServe(":8080", mux)) }()

	// Internal-only admin listener, bound to localhost, serves pprof via DefaultServeMux.
	log.Fatal(http.ListenAndServe("127.0.0.1:6060", nil))
}
```
```bash
# From a host that can reach 127.0.0.1:6060 (e.g. via SSH tunnel or bastion):
go tool pprof http://127.0.0.1:6060/debug/pprof/profile?seconds=30
go tool pprof http://127.0.0.1:6060/debug/pprof/heap
```

**Why it works / Explanation:** Serving the public API on its own `http.ServeMux` keeps pprof off that listener entirely, while a second listener bound to `127.0.0.1` (or gated behind auth/an internal-only network segment) makes `/debug/pprof/*` reachable only from the host itself or an authenticated tunnel. Profiling is now always available when an incident starts, without ever being exposed to untrusted traffic.

**Design principle:** Instrument profiling capability before you need it, and treat `/debug/pprof` as a privileged internal endpoint, never a route on a public listener.

---

## 2. Confusing the Different Profile Types

**The Problem:** `pprof` exposes several distinct profile types that answer different questions. Reaching for the wrong one — most commonly, grabbing a CPU profile to investigate a *memory* or *goroutine-leak* symptom — produces a profile that looks clean, leading to "I profiled it and found nothing" even though the problem is real and visible in a different profile.

**❌ Bad**
```go
func startWorker(jobs <-chan Job) {
	go func() {
		for j := range jobs {
			result := process(j)
			archive <- result // BUG: in some deploys, nothing reads `archive`; goroutines pile up here
		}
	}()
}
```
```bash
# Symptom: goroutine count climbs steadily over hours. Reaching for the
# profile everyone reaches for first:
go tool pprof http://localhost:6060/debug/pprof/profile?seconds=30
(pprof) top
# Nothing stands out — CPU usage is low and evenly spread. A goroutine
# blocked on a channel send is *idle*, not spinning, so it barely
# registers in a CPU profile at all.
```

**✅ Good**
```bash
go tool pprof http://localhost:6060/debug/pprof/goroutine
(pprof) top
# Shows 40,000 goroutines all parked at the same call site:
#   startWorker.func1 -> archive <- result
# — the exact leak location, invisible in the CPU profile above.
```

**Why it works / Explanation:** The goroutine profile is a full snapshot of every live goroutine's stack, which makes a mass of goroutines stuck at the same line immediately obvious — exactly the signal a CPU profile cannot show for blocked (not spinning) goroutines. Match the profile type to the symptom:
- **CPU profile** (`/debug/pprof/profile`) — where wall-clock execution time goes; use for "this service/request is CPU-bound and slow."
- **Heap profile** (`/debug/pprof/heap`) — where live allocations are; use for "memory keeps growing" or "which code path allocates the most."
- **Goroutine profile** (`/debug/pprof/goroutine`) — a snapshot of every goroutine and its stack; use for "goroutine count is growing" or to find leaks/deadlocks.
- **Block profile** (`/debug/pprof/block`) — time spent blocked on channel ops, mutexes, or select; use when throughput is low despite low CPU (contention hiding as idle time). Requires enabling via `runtime.SetBlockProfileRate`.
- **Mutex profile** (`/debug/pprof/mutex`) — specifically which mutexes are most contended; use once lock contention is already suspected and you need to find the exact `sync.Mutex`. Requires `runtime.SetMutexProfileFraction`.

**Design principle:** Pick the profile type that matches the symptom's category (CPU-bound vs. memory-bound vs. blocked/leaked vs. lock-contended) before concluding "there's nothing there."

---

## 3. Misreading Flat vs. Cumulative Time

**The Problem:** `pprof`'s `top` output and flame graphs report both "flat" time (spent in that function's own code) and "cumulative" time (flat time plus everything it called). Skimming only the top of a cumulative-sorted list, or the widest frame near the root of a flame graph, routinely points at a thin wrapper instead of the actual hot leaf doing the work.

**❌ Bad**
```go
func HandleOrder(ctx context.Context, o Order) error {
	return processOrder(ctx, o) // thin wrapper: almost all its time is cumulative, not flat
}

func processOrder(ctx context.Context, o Order) error {
	validate(o)
	return persist(ctx, o)
}

func persist(ctx context.Context, o Order) error {
	data := serialize(o) // BUG (perf-wise): naive reflection-based serialize is the actual hot leaf
	return db.Write(ctx, data)
}
```
```bash
(pprof) top10 -cum
Showing nodes accounting for 4.80s, 96% of 5s total
      flat  flat%     cum   cum%
     0.02s  0.4%   4.80s  96.0%  HandleOrder
     0.03s  0.6%   4.75s  95.0%  processOrder
     0.05s  1.0%   4.60s  92.0%  persist
     3.90s 78.0%   3.90s  78.0%  serialize
```
An engineer skimming the top of this `-cum` list sees `HandleOrder` at 96% cumulative and spends a day "optimizing" the wrapper — adding caching at that layer, restructuring its call shape — when `HandleOrder` itself does almost no work (0.4% flat). The 78% of flat time is entirely inside `serialize`.

**✅ Good**
```bash
(pprof) top10 -flat
Showing nodes accounting for 3.90s, 78% of 5s total
      flat  flat%     cum   cum%
     3.90s 78.0%   3.90s  78.0%  serialize
     0.60s 12.0%   0.60s  12.0%  db.Write
     0.05s  1.0%   4.60s  92.0%  persist
```

**Why it works / Explanation:** Sorting (or reading a flame graph) by flat time immediately identifies `serialize` as the function actually doing the work — that's what gets rewritten (e.g., switching from reflection-based marshaling to a generated marshaler). Cumulative time is useful for deciding *which subtree* of the call graph to drill into; flat time tells you *which function inside that subtree* to actually fix. In a flame graph, always drill down past the wide top-level frame to the widest leaf frame beneath it.

**Design principle:** Attribute cost to the function actually doing the work — cumulative time tells you where to look, flat time tells you what to fix.

---

## 4. Missing `b.ResetTimer()` After Expensive Setup

**The Problem:** Any code executed inside a benchmark function before the `for i := 0; i < b.N; i++` loop still runs under the benchmark's timer, because the timer starts automatically the moment the benchmark function begins. Expensive setup (loading a large fixture, building test data) pollutes the measured per-op time — and does so on every calibration pass, since the testing framework calls the benchmark function repeatedly while calibrating `b.N`.

**❌ Bad**
```go
func BenchmarkProcessLargeFile(b *testing.B) {
	data := loadFixtureFile("testdata/1gb_sample.csv") // ~2s to read and parse
	for i := 0; i < b.N; i++ {
		processCSV(data)
	}
	// BUG: the ~2s setup cost is included in the timer that started when
	// BenchmarkProcessLargeFile began, skewing ns/op — especially badly
	// during early calibration passes where b.N is still small.
}
```

**Why it's wrong:**
- The reported `ns/op` includes a share of the fixture-loading time, which has nothing to do with the cost of `processCSV` itself — the very thing the benchmark claims to measure.
- Because the framework recalibrates `b.N` by re-invoking the benchmark function with increasing iteration counts, the ~2s setup cost is paid again on each recalibration pass, further distorting the measurement, particularly when the real per-op cost is small relative to setup.

**✅ Good**
```go
func BenchmarkProcessLargeFile(b *testing.B) {
	data := loadFixtureFile("testdata/1gb_sample.csv") // setup: excluded from the measurement
	b.ResetTimer()                                     // discard elapsed time and alloc counts from setup
	for i := 0; i < b.N; i++ {
		processCSV(data)
	}
}
```

**Why it works / Explanation:** `b.ResetTimer()` zeroes the elapsed-time (and allocation) counters immediately before the timed loop begins, so only the work inside the loop counts toward the reported `ns/op`. This matters most whenever setup cost is comparable to, or larger than, the cost of a single iteration.

**Design principle:** Only the code under test should live inside the measurement window — isolate fixture/setup cost from the metric explicitly.

---

## 5. Dead-Code Elimination Silently Voiding a Benchmark

**The Problem:** If a benchmark's loop body computes a result that is never observed anywhere, the compiler is free to conclude that the computation has no effect and optimize it away (partially or entirely) — producing a suspiciously tiny or near-zero `ns/op` that measures loop overhead rather than the work you intended to benchmark.

**❌ Bad**
```go
func BenchmarkSquare(b *testing.B) {
	for i := 0; i < b.N; i++ {
		square(42) // BUG: result discarded — nothing observes it
	}
}

func square(x int) int {
	return x * x
}
```
`square` is small and trivially inlinable, and since its result here has no observable effect and it has no side effects, the compiler is free to optimize the multiplication away entirely — the benchmark ends up mostly timing the empty loop, reporting a near-zero `ns/op` that says nothing about the real cost of calling `square` at an actual call site where its result *is* used.

**✅ Good**
```go
var benchResult int // package-level sink: gives the computed value an observable use

func BenchmarkSquare(b *testing.B) {
	var r int
	for i := 0; i < b.N; i++ {
		r = square(42)
	}
	benchResult = r // assigned once, after the loop
}
```

**Why it works / Explanation:** Assigning the final result to a package-level variable after the loop gives the value an observable use from the compiler's perspective — it "escapes" the function and could be read by anything, which prevents the compiler from proving the work inside the loop is dead. Assigning to a local inside the loop and only writing the package-level sink once, after the loop, also avoids adding an extra memory write to every single iteration that wouldn't exist at the real call site.

**Design principle:** Give every benchmark's result an escape hatch (a package-level sink) so the work being measured can never be proven unnecessary and optimized away.

---

## 6. Forgetting `b.ReportAllocs()` / `-benchmem`

**The Problem:** A benchmark's default output reports timing only (`ns/op`). Without allocation reporting, a benchmark can look "fast" while hiding heavy per-call allocation — often the dominant real cost in a garbage-collected runtime under sustained concurrent load.

**❌ Bad**
```go
func BenchmarkParseConfig(b *testing.B) {
	raw := sampleConfigBytes()
	for i := 0; i < b.N; i++ {
		parseConfig(raw) // BUG: allocation behavior is invisible in the benchmark output
	}
}
```
```bash
$ go test -bench BenchmarkParseConfig .
BenchmarkParseConfig-8    500000    2381 ns/op
# Looks fine in isolation — says nothing about GC pressure.
```

**✅ Good**
```go
func BenchmarkParseConfig(b *testing.B) {
	raw := sampleConfigBytes()
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		parseConfig(raw)
	}
}
```
```bash
$ go test -bench BenchmarkParseConfig -benchmem .
BenchmarkParseConfig-8    500000    2381 ns/op    1840 B/op    23 allocs/op
# 23 allocations per parse — at 10k req/s that's 230,000 allocs/sec of
# GC work, completely invisible in the ns/op number alone.
```

**Why it works / Explanation:** `b.ReportAllocs()` (equivalently, running with the `-benchmem` flag, which enables it for every benchmark in the run) adds `B/op` (bytes allocated per operation) and `allocs/op` (allocation count per operation) to the output. Allocation count matters independently of speed, because its cost compounds: every allocation is both immediate allocator work now and future GC scan/collection work later.

**Design principle:** Treat allocation count as a first-class benchmark metric, not an afterthought — a function that looks "fast" in `ns/op` can still tank throughput under concurrent load purely through GC contention.

---

## 7. Benchmarking with Unrealistic Input Sizes

**The Problem:** A benchmark run against a convenient small input (10 items) can report a blazing-fast number while hiding an algorithm whose complexity is quadratic or worse — a shape that only becomes visible, and only becomes a production incident, once the input size matches real traffic (millions of items).

**❌ Bad**
```go
func BenchmarkDedupe(b *testing.B) {
	items := generateItems(10) // BUG: production batches are 1M+ items
	for i := 0; i < b.N; i++ {
		dedupe(items)
	}
}

func dedupe(items []string) []string {
	var out []string
	for _, item := range items {
		found := false
		for _, o := range out { // O(n) scan per item -> O(n^2) overall
			if o == item {
				found = true
				break
			}
		}
		if !found {
			out = append(out, item)
		}
	}
	return out
}
```
At `n=10` this benchmark reports a trivially fast number and nobody notices the nested-loop shape; it ships, and a production batch of 1,000,000 items grinds the service to a halt.

**✅ Good**
```go
func BenchmarkDedupe(b *testing.B) {
	for _, n := range []int{10, 1_000, 100_000, 1_000_000} {
		items := generateItems(n)
		b.Run(fmt.Sprintf("n=%d", n), func(b *testing.B) {
			b.ReportAllocs()
			for i := 0; i < b.N; i++ {
				dedupe(items)
			}
		})
	}
}
```
```bash
$ go test -bench BenchmarkDedupe -benchmem .
BenchmarkDedupe/n=10-8            5000000        240 ns/op
BenchmarkDedupe/n=1000-8            50000      28400 ns/op
BenchmarkDedupe/n=100000-8            200    2840000 ns/op
BenchmarkDedupe/n=1000000-8             2  284000000 ns/op   # quadratic blowup is now obvious
```

**Why it works / Explanation:** Sub-benchmarks via `b.Run`, driven by a table of realistic sizes, make the growth curve visible directly in the output. A linear or log-linear algorithm's `ns/op` scales gently across decades of input size; `dedupe`'s roughly 100x-per-10x-input jump is the unmistakable signature of its nested-loop, O(n²) shape.

**Design principle:** Benchmark across the realistic range of production input sizes, not just a convenient small N — asymptotic complexity bugs are invisible at toy scale.

---

## 8. Manual Timing Instead of `testing.B`

**The Problem:** Hand-rolling timing with `time.Now()`/`time.Since()` inside an ordinary test throws away everything the benchmarking harness exists to provide: automatic iteration-count calibration, a run long enough to be statistically stable, and integration with tools built to compare runs rigorously.

**❌ Bad**
```go
func TestPerf(t *testing.T) {
	start := time.Now()
	for i := 0; i < 1000; i++ { // BUG: arbitrary fixed count, no warm-up, single sample
		_ = computeHash(payload)
	}
	elapsed := time.Since(start)
	t.Logf("1000 iterations took %v (%v/op)", elapsed, elapsed/1000)
}
```

**Why it's wrong:**
- A fixed iteration count of 1000 either finishes too fast to measure accurately (dominated by timer resolution and scheduling noise) or takes far longer than necessary, depending on the machine — `testing.B` calibrates `b.N` automatically to run long enough for a stable measurement.
- There is no warm-up, so cold caches and lazy initialization on the first few iterations skew the average.
- A single sample with no variance reporting cannot distinguish a real regression from ordinary machine noise, and there is no `-count`/`benchstat`-compatible output to make that distinction later.
- This runs as part of ordinary `go test`, not `go test -bench`, so it slows down the regular test suite every time, for a measurement that isn't even statistically rigorous.

**✅ Good**
```go
func BenchmarkComputeHash(b *testing.B) {
	for i := 0; i < b.N; i++ {
		_ = computeHash(payload)
	}
}
```
```bash
go test -bench BenchmarkComputeHash -benchtime=2s -count=5 .
```

**Why it works / Explanation:** `testing.B` calibrates `b.N` to find a run length that produces a stable measurement, runs only when explicitly requested via `-bench` (never as a side effect of plain `go test`), and integrates directly with tooling (`-count`, `benchstat`) built specifically for statistically comparing benchmark runs.

**Design principle:** Use the language's benchmarking harness instead of hand-rolled timing — it exists specifically to eliminate the measurement pitfalls that hand-rolled timing falls into.

---

## 9. Ignoring `go tool trace`

**The Problem:** `pprof` profiles are aggregated over the whole sampling window — they show *what* consumed CPU or memory in total, but not *when*. An intermittent problem (a latency spike every few seconds, a stall correlated with GC) can be completely invisible in an aggregate profile that just shows comfortable average CPU usage.

**❌ Bad**
```go
func startMemoryTrimmer() {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for range ticker.C {
		debug.FreeOSMemory() // BUG: forces an eager, synchronous GC + OS memory release every 10s
	}
}
```
```bash
# Symptom: p99 latency spikes ~40ms every 10 seconds, but average CPU
# usage sits at a comfortable 30%. Reaching for a CPU profile to investigate:
go tool pprof http://localhost:6060/debug/pprof/profile?seconds=30
(pprof) top
# Nothing stands out — debug.FreeOSMemory()'s cost is real but brief, and
# it gets smeared across the 30s sampling window, so it never shows up
# as a hot function.
```

**✅ Good**
```bash
curl -o trace.out "http://localhost:6060/debug/pprof/trace?seconds=30"
go tool trace trace.out
# The timeline view shows a clear ~40ms GC-related pause every 10 seconds,
# lined up exactly with startMemoryTrimmer's ticker — a correlation a
# time-aggregated CPU profile could never surface.
```

**Why it works / Explanation:** `go tool trace` captures a fine-grained, timestamped event log — goroutine scheduling, GC start/stop, syscalls, blocking events — rendered as a timeline. It answers "what was happening at the moment of the spike," which is a different question from "what consumed the most resources on average" that `pprof` answers. Once the trace points at the ticker-driven `debug.FreeOSMemory()` call, the actual fix is to remove the blind periodic call and let normal GC pacing (or `GOGC`/`GOMEMLIMIT` tuning) handle memory pressure instead.

**Design principle:** Reach for `go tool trace` for time-correlated or intermittent behavior (scheduling stalls, GC pause timing) and `pprof` for aggregate resource attribution — they answer different questions and neither substitutes for the other.

---

## 10. Trusting Benchmarks Run on a Noisy Machine

**The Problem:** A laptop running a browser, an IDE indexer, and Slack in the background — or a shared, oversubscribed CI runner — introduces enough scheduling and thermal jitter that a single benchmark run's small percentage difference is frequently indistinguishable from noise. Treating one before/after run as a real result is a common way to ship a "3% faster" change that did nothing.

**❌ Bad**
```go
func BenchmarkEncode(b *testing.B) {
	payload := sampleEvent()
	for i := 0; i < b.N; i++ {
		encode(payload)
	}
}
```
```bash
# Laptop with a browser, Slack, and an IDE indexer running in the background.
go test -bench BenchmarkEncode -benchtime=1x .
BenchmarkEncode-8   1   812345 ns/op

go test -bench BenchmarkEncode -benchtime=1x .   # after a code change
BenchmarkEncode-8   1   791203 ns/op
# BUG: claiming "2.6% faster!" from two single-sample runs on a noisy
# machine — well within normal scheduler/thermal/background-process jitter.
```

**✅ Good**
```go
func BenchmarkEncode(b *testing.B) {
	payload := sampleEvent()
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		encode(payload)
	}
}
```
```bash
# Before the change:
go test -bench BenchmarkEncode -benchtime=2s -count=10 . > old.txt
# After the change:
go test -bench BenchmarkEncode -benchtime=2s -count=10 . > new.txt

benchstat old.txt new.txt
# name          old time/op    new time/op    delta
# Encode-8         798µs ± 3%     742µs ± 2%   -7.02%  (p=0.000 n=10+10)
```

**Why it works / Explanation:** `-count=10` runs each benchmark 10 independent times, and `benchstat` computes means, variance (the `± %`), and a statistical significance test (`p`) comparing the two distributions. A real improvement shows a delta clearly larger than the variance band with a low p-value; a single-sample comparison on a shared or noisy machine cannot tell a genuine change from background jitter.

**Design principle:** Never trust a benchmark delta without a variance measure and a significance test — run multiple samples and let statistics, not one lucky run, decide.

---

## Key Takeaways
- Wire up `net/http/pprof` on an internal-only listener before an incident happens, never on a public-facing mux.
- Match the profile type (CPU, heap, goroutine, block, mutex) to the symptom category, or the profile will show nothing.
- Sort/read by flat time to find the function actually doing the work; cumulative time only tells you which subtree to explore.
- Call `b.ResetTimer()` after expensive setup so setup cost doesn't pollute the measured `ns/op`.
- Give benchmark results an observable sink (a package-level variable) so the compiler can't prove the work is dead and eliminate it.
- Use `b.ReportAllocs()`/`-benchmem` — allocation count is often the real production cost, and it's invisible in timing alone.
- Benchmark across a realistic range of input sizes via `b.Run` sub-benchmarks to catch algorithms that are fine at small N but quadratic in practice.
- Use `testing.B`, not hand-rolled `time.Now()`/`time.Since()` timing, to get calibration, warm-up, and tooling integration for free.
- Reach for `go tool trace` (not `pprof`) when the question is about timing/scheduling behavior rather than aggregate resource usage.
- Run multiple samples (`-count`) and compare with `benchstat` instead of trusting a single run on a noisy machine.

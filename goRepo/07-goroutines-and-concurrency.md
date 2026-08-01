# Goroutines and Concurrency

Goroutines make concurrency cheap to spin up and dangerously easy to get subtly wrong. Because the failure modes — leaks, races, silent panics, thread exhaustion — rarely show up in a quick local test, they surface for the first time under production load, at 3am, under a debugger you don't have attached. This file walks through the goroutine-specific mistakes that take down otherwise-healthy services: goroutines that block forever and slowly exhaust memory, panics that crash the whole process instead of just one request, and correctness bugs that only appear once real concurrency is in play.

## 1. Goroutine Leaks From Abandoned Channel Operations

**The Problem:** A goroutine blocks on a channel send (or receive) waiting for a partner that will never show up, because the original caller already gave up — typically due to a timeout — with no way to tell the goroutine to stop. The goroutine, and everything it's holding onto, lives forever.

**❌ Bad**
```go
func fetchWithTimeout(url string) (string, error) {
	resultCh := make(chan string) // unbuffered
	go func() {
		result := slowFetch(url) // may take 5s
		resultCh <- result       // BUG: nobody may ever be here to receive
	}()

	select {
	case res := <-resultCh:
		return res, nil
	case <-time.After(2 * time.Second):
		return "", errors.New("timeout waiting for fetch")
	}
}
```

**Why it's wrong:**
- If `slowFetch` takes longer than 2 seconds, `fetchWithTimeout` returns via the `time.After` case and nobody ever calls `<-resultCh` again — the goroutine is stuck on `resultCh <- result` permanently.
- Every timed-out call leaks one goroutine plus its stack and any memory `slowFetch` allocated; under sustained load this is a slow, steady memory/goroutine-count climb that eventually OOMs the process (visible as `runtime.NumGoroutine()` trending up forever, never down).

**✅ Good**
```go
func fetchWithTimeout(ctx context.Context, url string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()

	resultCh := make(chan string, 1) // buffered: the goroutine can always complete its send
	go func() {
		result := slowFetch(url)
		select {
		case resultCh <- result:
		case <-ctx.Done():
			// caller already gave up; drop the result instead of blocking forever
		}
	}()

	select {
	case res := <-resultCh:
		return res, nil
	case <-ctx.Done():
		return "", ctx.Err()
	}
}
```

**Why it works / Explanation:** The buffered channel means the goroutine's send can always succeed immediately even if nobody is listening anymore, so it never blocks past that point. The `select` on `ctx.Done()` inside the goroutine is the belt-and-suspenders version: it lets the goroutine notice cancellation and stop doing further work (not just unblock a send), which matters if `slowFetch` itself is cancelable. The rule of thumb: any goroutine you spawn that the caller might stop waiting for needs either a buffered "escape hatch" or an explicit cancellation signal it actively checks.

**Design principle:** Every goroutine you start needs a defined way to end — cancellation via `context.Context` is the standard Go idiom for "stop waiting, and tell the worker to stop too."

---

## 2. Loop Variable Capture When Spawning Worker Goroutines

**The Problem:** Spawning one goroutine per loop iteration and referencing the loop variable directly inside the closure captures the *variable*, not a snapshot of its value at that iteration. On Go versions before 1.22, every closure shares the same loop variable, so by the time the goroutines actually run, they can all see the final value.

**❌ Bad**
```go
func startWorkers(ids []int) {
	for _, id := range ids {
		go func() {
			fmt.Println("worker starting for id:", id) // BUG: captures the loop variable itself
		}()
	}
	time.Sleep(time.Second) // just to let workers print before main exits
}
```

**Why it's wrong:**
- On Go < 1.22, `id` is a single variable reused across every iteration of the `range` loop. All the closures capture a reference to that one variable, so most (or all) of them print whatever `id` happened to be when they finally got scheduled — usually the last element, not "their" element.
- This is a classic silent-wrong-output bug: no panic, no crash, just workers processing the wrong item, which is far more dangerous in production than a bug that fails loudly.

**✅ Good**
```go
func startWorkers(ids []int) {
	for _, id := range ids {
		go func(id int) { // id is now a parameter: a fresh copy per goroutine
			fmt.Println("worker starting for id:", id)
		}(id)
	}
	time.Sleep(time.Second)
}
```

**Why it works / Explanation:** Passing `id` as a function argument evaluates and copies its current value at the moment `go func(id int)(id)` is scheduled, so each goroutine gets its own independent copy no matter when it actually runs. Note that Go 1.22 changed `for` loop semantics so that `id` (and `i` in a classic three-clause loop) is a new variable per iteration, which fixes this class of bug even without the explicit parameter — but writing it explicitly is still good practice: it works on every Go version and makes the intent obvious to readers who don't have the loop semantics memorized.

**Design principle:** Make each goroutine's inputs explicit arguments rather than implicit closures over shared loop state — it removes any ambiguity about what each goroutine actually sees.

---

## 3. Unbounded Goroutine Creation Under Load

**The Problem:** Spawning one goroutine per incoming request or per item in a large batch, with no cap, works fine in a test with a handful of items and falls over in production when the input is large or the downstream work is slow — thousands of goroutines pile up, each consuming stack memory and putting pressure on the scheduler and GC.

**❌ Bad**
```go
func handleBatch(items []Item) {
	for _, item := range items {
		go process(item) // BUG: no limit — one goroutine per item, no matter how many
	}
}
```

**Why it's wrong:**
- If `items` has a million entries (or `process` is slow enough that goroutines pile up faster than they finish), this creates a million concurrent goroutines. Each has a few KB of stack minimum, so memory use balloons, GC pause times grow, and the scheduler thrashes across OS threads.
- Downstream resources (DB connections, HTTP clients, file descriptors) get hammered by unbounded concurrency, often causing cascading failures in *other* services, not just this process.

**✅ Good**
```go
func handleBatch(ctx context.Context, items []Item) error {
	g, ctx := errgroup.WithContext(ctx)
	g.SetLimit(20) // at most 20 concurrent workers, regardless of len(items)

	for _, item := range items {
		item := item // pre-1.22 safety; harmless to keep even on 1.22+
		g.Go(func() error {
			return process(ctx, item)
		})
	}
	return g.Wait() // returns the first non-nil error, if any
}
```

**Why it works / Explanation:** `errgroup.Group.SetLimit` (from `golang.org/x/sync/errgroup`) turns the group into a bounded worker pool: `g.Go` blocks once 20 goroutines are already in flight, so concurrency is capped no matter how large `items` is. It also gives you error propagation and context cancellation for free — if one call fails, `ctx` is canceled and `Wait()` returns the error. A hand-rolled equivalent uses a buffered channel of size N as a semaphore (`acquire := make(chan struct{}, 20)`) with the same blocking-acquire/release pattern.

**Design principle:** Concurrency should be bounded by a deliberate limit tied to real capacity (DB pool size, downstream rate limits, CPU count) — never left to scale with input size.

---

## 4. Calling `wg.Add` After the Goroutine Has Already Started

**The Problem:** `sync.WaitGroup.Add` must happen-before the corresponding goroutine's `Done`, and critically, before `Wait` could possibly observe the counter. Calling `Add` from inside the goroutine itself races against `Wait`, which may run first and see a counter of zero.

**❌ Bad**
```go
func processBatch(ids []string) {
	var wg sync.WaitGroup
	for _, id := range ids {
		go func(id string) {
			wg.Add(1) // BUG: registers after the goroutine may already be running
			defer wg.Done()
			process(id)
		}(id)
	}
	wg.Wait() // can return before a single Add() call has executed
}
```

**Why it's wrong:**
- Goroutine scheduling is nondeterministic. `wg.Wait()` on the main goroutine can run before any spawned goroutine gets scheduled far enough to call `Add(1)`. `Wait` sees a counter of 0 and returns immediately — `processBatch` returns while all the "work" is still pending or hasn't even started.
- In pathological interleavings this can also trip `sync: negative WaitGroup counter` panics, since `Add` and `Done` calls race against each other with no ordering guarantee.

**✅ Good**
```go
func processBatch(ids []string) {
	var wg sync.WaitGroup
	for _, id := range ids {
		wg.Add(1) // always call Add before starting the goroutine
		go func(id string) {
			defer wg.Done()
			process(id)
		}(id)
	}
	wg.Wait()
}
```

**Why it works / Explanation:** Calling `Add(1)` on the main goroutine, synchronously, before each `go` statement guarantees the counter reflects every pending goroutine before `Wait` is ever called. There is no window where `Wait` can observe a stale counter, because every increment happens strictly before the goroutine that will later decrement it is even created.

**Design principle:** `Add` and `go` are a pair — always write `Add` immediately before the `go` statement it corresponds to, never inside the goroutine body.

---

## 5. An Unrecovered Panic in a Goroutine Crashes the Whole Process

**The Problem:** `recover()` only catches panics within the same goroutine's call stack. A panic in a spawned goroutine that nobody recovers propagates to the top of *that* goroutine's stack and crashes the entire program — it does not matter how many other goroutines are running fine, or whether the goroutine that spawned it has its own recover.

**❌ Bad**
```go
func startWorker(jobs <-chan Job) {
	go func() {
		for job := range jobs {
			handle(job) // BUG: if handle panics, the entire process terminates
		}
	}()
}
```

**Why it's wrong:**
- Say `handle` panics on a malformed job (nil pointer, bad type assertion, index out of range). There is no `recover` anywhere in this goroutine's stack, so the Go runtime prints the panic and stack trace and terminates the process — taking down every other in-flight request and goroutine with it, not just this one job.
- This is especially dangerous in HTTP servers: `net/http` recovers panics *inside a request handler* automatically, but that protection does not extend to goroutines the handler spawns for background work — those crash the whole server.

**✅ Good**
```go
func startWorker(jobs <-chan Job) {
	go func() {
		for job := range jobs {
			handleSafely(job)
		}
	}()
}

func handleSafely(job Job) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("recovered from panic handling job %v: %v", job.ID, r)
		}
	}()
	handle(job)
}
```

**Why it works / Explanation:** Wrapping each unit of work in its own `defer recover()` means a panic while processing one job is contained to that job — the worker logs it and moves on to the next item in `jobs` instead of taking the whole process down. The key insight is that recovery must happen *in the same goroutine, at or above the frame that panics* — you cannot recover a goroutine's panic from outside it, so every independent goroutine entry point (worker loops, background tasks spawned from a handler, timers) needs this guard.

**Design principle:** Treat every `go func(){...}()` as an independent fault domain — give each one its own panic recovery, the same way you'd wrap a top-level `main` in error handling.

---

## 6. Fire-and-Forget Goroutines With No Error Handling or Observability

**The Problem:** Spawning a goroutine and never checking what happened to it — no return value, no logging, no metric — means real failures vanish without a trace. The calling code looks like it "handled" the async work, but it never actually observes success or failure.

**❌ Bad**
```go
func SaveUser(u User) error {
	if err := db.Insert(u); err != nil {
		return err
	}
	go sendWelcomeEmail(u) // BUG: errors from this call disappear silently
	return nil
}
```

**Why it's wrong:**
- If `sendWelcomeEmail` fails (SMTP timeout, bad template, provider outage), there is no code path that ever sees the error — it's discarded the moment the goroutine returns. Nobody gets paged, no metric fires, no log line appears.
- Debugging "some users report never getting a welcome email" becomes archaeology: there's no record that the send was even attempted, let alone why it failed.

**✅ Good**
```go
func SaveUser(ctx context.Context, u User, logger *slog.Logger) error {
	if err := db.Insert(u); err != nil {
		return err
	}
	go func() {
		if err := sendWelcomeEmail(ctx, u); err != nil {
			logger.Error("failed to send welcome email",
				"user_id", u.ID, "error", err)
			// increment a metric here too, e.g. emailFailures.Inc()
		}
	}()
	return nil
}
```

**Why it works / Explanation:** The background goroutine still doesn't block `SaveUser`'s response, but its outcome is now observable: failures are logged with enough context (`user_id`, the error) to act on, and a metrics counter would make them alertable. For work where "fire and forget" isn't acceptable even with logging (e.g. financial operations), the better fix is not to fire-and-forget at all — use a durable queue or outbox pattern so the work survives a process restart.

**Design principle:** Asynchronous does not mean unobserved — every background operation needs a defined way to surface its failure, even if nothing waits for its success.

---

## 7. Not Threading `context.Context` Through Call Chains

**The Problem:** When intermediate functions in a call chain don't accept and pass along a `context.Context`, there is no way for an upstream caller to cancel a slow operation buried several layers down — timeouts and shutdown signals simply can't reach it.

**❌ Bad**
```go
func GetUser(id string) (*User, error) {
	return queryDB(id) // no ctx parameter — nothing upstream can cancel this
}

func queryDB(id string) (*User, error) {
	return db.Query("SELECT * FROM users WHERE id = ?", id) // BUG: uses an internal background context
}
```

**Why it's wrong:**
- If the HTTP request that triggered `GetUser` is canceled (client disconnected, request timeout fired), that signal has nowhere to go — `queryDB` keeps running the query to completion regardless, holding a DB connection and CPU time for work whose result nobody will ever use.
- Under load, this compounds: canceled requests keep consuming backend resources anyway, so the system does strictly more work than necessary right when it's already struggling.

**✅ Good**
```go
func GetUser(ctx context.Context, id string) (*User, error) {
	return queryDB(ctx, id)
}

func queryDB(ctx context.Context, id string) (*User, error) {
	return db.QueryContext(ctx, "SELECT * FROM users WHERE id = ?", id)
}
```

**Why it works / Explanation:** Threading `ctx` through every function in the chain — and using the `*Context` variants of standard library calls (`QueryContext`, `DoContext`-style APIs, etc.) — means a cancellation or deadline set far upstream (an HTTP handler's request context, a CLI's signal handler) actually reaches the code doing the blocking work. The database driver can then abort the in-flight query immediately instead of running it to completion for nothing.

**Design principle:** `context.Context` should be the first parameter of any function that does I/O or might block, all the way down the call stack — "ctx" isn't decoration, it's the cancellation plumbing.

---

## 8. Data Races From Unsynchronized Shared State

**The Problem:** Multiple goroutines reading and writing the same variable without a mutex or atomic operation is a data race, even if it "usually" produces a plausible-looking number — the read-modify-write sequence (`counter++`) is not a single atomic step, so concurrent increments can overwrite each other.

**❌ Bad**
```go
func main() {
	var counter int
	var wg sync.WaitGroup
	for i := 0; i < 1000; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			counter++ // BUG: unsynchronized read-modify-write from 1000 goroutines
		}()
	}
	wg.Wait()
	fmt.Println(counter) // prints something less than 1000, and varies between runs
}
```

**Why it's wrong:**
- `counter++` compiles to a read, an increment, and a write. Two goroutines can both read the same value, increment it independently, and both write back the same result — one increment is lost. Run this enough times and the final count is unpredictably less than 1000.
- `go run -race main.go` (or `go test -race`) will flag this immediately with a `DATA RACE` report showing both goroutines' stacks — this is exactly the kind of bug that's invisible without the race detector and devastating in production (silently wrong totals, balances, counts).

**✅ Good**
```go
func main() {
	var counter atomic.Int64
	var wg sync.WaitGroup
	for i := 0; i < 1000; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			counter.Add(1)
		}()
	}
	wg.Wait()
	fmt.Println(counter.Load()) // deterministically 1000
}
```

**Why it works / Explanation:** `atomic.Int64` (Go 1.19+'s typed atomics) performs the increment as a single indivisible hardware operation, so there is no window where two goroutines can both read the same stale value. For anything beyond a simple counter — multiple related fields, more complex invariants — a `sync.Mutex` guarding the whole critical section is the more general fix. Either way: run `go test -race` in CI on any package with goroutines; it catches this entire class of bug for the cost of a build flag.

**Design principle:** Any variable touched by more than one goroutine needs an explicit synchronization mechanism — atomics for single values, mutexes for compound state — there is no such thing as a "probably fine" unsynchronized shared variable.

---

## 9. Misunderstanding `GOMAXPROCS` in Containers

**The Problem:** More goroutines does not mean more parallelism — CPU-bound work can only run in parallel on as many logical CPUs as `GOMAXPROCS` allows, and in containers with a cgroup CPU quota, the Go runtime's automatic detection has historically gotten this number wrong, leading to over-scheduling and throttling.

**❌ Bad**
```go
func processCPUBound(data []Item) {
	workers := runtime.NumCPU() // BUG: reports the *host's* CPU count, not the container's cgroup quota
	ch := make(chan Item, len(data))
	for _, d := range data {
		ch <- d
	}
	close(ch)

	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for item := range ch {
				cpuIntensiveWork(item)
			}
		}()
	}
	wg.Wait()
}
```

**Why it's wrong:**
- A container capped at 2 CPUs by its cgroup quota, running on a 64-core host, can still have `runtime.NumCPU()` (and, on older Go versions, the default `GOMAXPROCS`) report a number far higher than 2. Spawning that many CPU-bound workers causes constant context-switch thrashing — the OS scheduler fights over 2 real cores while pretending it has dozens.
- The symptom in production looks like "the pod is CPU-throttled and latency is terrible" even though the code "correctly" uses `NumCPU()` — the metric it's reading doesn't mean what the code assumes it means inside a container.

**✅ Good**
```go
import (
	"log"
	"runtime"

	_ "go.uber.org/automaxprocs" // on init, sets GOMAXPROCS to match the cgroup CPU quota
)

func processCPUBound(data []Item) {
	workers := runtime.GOMAXPROCS(0) // respects the value automaxprocs (or GOMAXPROCS env) set
	log.Printf("sizing worker pool to GOMAXPROCS=%d", workers)

	ch := make(chan Item, len(data))
	for _, d := range data {
		ch <- d
	}
	close(ch)

	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for item := range ch {
				cpuIntensiveWork(item)
			}
		}()
	}
	wg.Wait()
}
```

**Why it works / Explanation:** `runtime.GOMAXPROCS(0)` returns the value the scheduler is actually using for parallelism, and importing `go.uber.org/automaxprocs` for its side effect makes that value cgroup-aware — it reads the container's real CPU quota at startup and calls `runtime.GOMAXPROCS` accordingly, instead of trusting `NumCPU()`'s host-wide view. Note this only matters for CPU-bound work; I/O-bound workers (waiting on network/disk) can usefully run far more goroutines than `GOMAXPROCS`, since they spend most of their time blocked, not competing for a core.

**Design principle:** Size CPU-bound concurrency to `GOMAXPROCS`, not to goroutine count or host CPU count — and make sure `GOMAXPROCS` itself reflects the actual resource limits your process runs under.

---

## 10. Adding Goroutines and Channels to Inherently Sequential Work

**The Problem:** Wrapping a computation in a goroutine and a channel only pays off if there's independent work to overlap. When each step depends on the previous step's result, there's no parallelism to extract — the concurrency machinery just adds overhead, complexity, and new failure modes for zero benefit.

**❌ Bad**
```go
func computeTotal(a, b, c int) int {
	ch := make(chan int)
	go func() {
		ch <- a + b // BUG: wrapped in a goroutine for no reason — nothing else runs concurrently with it
	}()
	x := <-ch      // immediately blocks waiting for the only goroutine we just started
	return x + c   // this step depends entirely on x, so it couldn't have overlapped with anything anyway
}
```

**Why it's wrong:**
- The caller blocks on `<-ch` right after starting the goroutine, so there is zero overlap — this runs no faster than a direct function call, but now pays goroutine scheduling overhead and channel synchronization cost for nothing.
- It also adds real complexity: a reader now has to reason about channel lifetime, potential leaks, and synchronization for a computation that is, and always was, three additions in a row.

**✅ Good**
```go
func computeTotal(a, b, c int) int {
	x := a + b
	return x + c
}
```

**Why it works / Explanation:** Since `c` is only combined with `x` after `x` is known, this is a strictly sequential dependency chain — there is no independent work for two goroutines to do at the same time. A plain function call is not just simpler, it is also faster, because it skips goroutine creation and channel synchronization entirely.

**Design principle:** Reach for goroutines and channels only when there's genuinely independent work to run concurrently — "don't communicate by sharing memory, share memory by communicating" is about *how* to coordinate concurrent work, not a mandate to manufacture concurrency where none exists.

---

## Key Takeaways
- Every goroutine needs a defined way to stop — use buffered channels or `context.Context` so an abandoned caller doesn't leave it blocked forever.
- Pass loop variables as goroutine function arguments so each spawned goroutine captures its own value, not a shared loop variable.
- Bound goroutine creation with a semaphore or `errgroup.SetLimit` — never spawn one goroutine per input item with no cap.
- Call `wg.Add` synchronously before `go`, never inside the goroutine it's tracking.
- Wrap every independent goroutine's work in its own `defer recover()` — panics don't cross goroutine boundaries to be caught elsewhere.
- Give fire-and-forget goroutines a way to report failure (logging, metrics, error channel) instead of discarding errors silently.
- Thread `context.Context` through every function in a call chain that does I/O, so cancellation actually reaches the blocking call.
- Synchronize every variable shared across goroutines with a mutex or atomic type, and run `go test -race` to catch what review misses.
- Size CPU-bound worker pools to `GOMAXPROCS` (made cgroup-aware via `automaxprocs` or a recent Go runtime), not to `NumCPU()` or arbitrary goroutine counts.
- Don't wrap inherently sequential, dependent computations in goroutines and channels — that adds overhead and complexity with no possible speedup.

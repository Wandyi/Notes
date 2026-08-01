# The `sync` Package

The `sync` package gives Go programs low-level, high-performance concurrency primitives — but every one of them comes with sharp, easy-to-violate invariants: mutexes must not be copied, `WaitGroup` counters must be incremented before the goroutine they track starts, `Once` only ever runs once no matter what happens inside it. This file covers the `sync`-specific mistakes that produce deadlocks, silent no-ops, and data races that pass code review but fail in production under real concurrent load.

## 1. Copying a `sync.Mutex` by Value

**The Problem:** A `sync.Mutex` (and `sync.WaitGroup`, `sync.RWMutex`) carries internal state that must stay shared across every goroutine using it. Copying the struct that embeds one — via a value receiver, a return by value, or storing it in a slice that reallocates — duplicates that state, so the copies no longer protect the same critical section at all.

**❌ Bad**
```go
type Counter struct {
	mu    sync.Mutex
	value int
}

func (c Counter) Incr() { // BUG: value receiver copies c (and c.mu) on every call
	c.mu.Lock()
	c.value++
	c.mu.Unlock()
} // the lock and the increment both happened on a throwaway copy
```

**Why it's wrong:**
- Because `Incr` has a value receiver, Go copies the entire `Counter` — including `mu` — into a new local variable every time `Incr` is called. Each call locks and increments its own private copy; the real `Counter.value` a caller thinks it's incrementing never changes, and no two calls are ever actually mutually exclusive with each other.
- `go vet ./...` (which runs automatically as part of `go test`) flags this immediately with a `copylocks` error: `Incr passes lock by value: Counter contains sync.Mutex` — this is a check worth never disabling, since it catches exactly this class of bug at compile-review time instead of in production.

**✅ Good**
```go
type Counter struct {
	mu    sync.Mutex
	value int
}

func (c *Counter) Incr() { // pointer receiver: every call shares the same mu and value
	c.mu.Lock()
	defer c.mu.Unlock()
	c.value++
}
```

**Why it works / Explanation:** A pointer receiver means every call to `Incr` operates on the same underlying `Counter`, so `mu` genuinely serializes access to the same `value` across every caller. The same rule applies anywhere a struct containing a lock might get copied implicitly — returning it by value, appending it to a `[]Counter` that later reallocates, or passing it as a non-pointer function argument all silently duplicate the lock. The fix is always the same: use `*T` for any type embedding a `sync` primitive, and never let a value of that type get copied after its first use.

**Design principle:** Types containing `sync.Mutex`, `sync.RWMutex`, or `sync.WaitGroup` must be used only through a pointer — run `go vet` in CI so an accidental copy fails the build instead of silently breaking mutual exclusion.

---

## 2. `sync.WaitGroup.Add` Timing Bugs

**The Problem:** `Add` must be called, and observed to complete, before the corresponding `Wait()` call could possibly run — which means it must happen on the goroutine that starts the worker, before `go`, never inside the worker itself.

**❌ Bad**
```go
func processBatch(ids []string) {
	var wg sync.WaitGroup
	for _, id := range ids {
		go func(id string) {
			wg.Add(1) // BUG: registers after the goroutine may already be scheduled to run
			defer wg.Done()
			process(id)
		}(id)
	}
	wg.Wait() // may return before a single Add() call has executed
}
```

**Why it's wrong:**
- Goroutine scheduling is nondeterministic — `wg.Wait()` on the calling goroutine can run before any of the spawned goroutines get far enough to call `Add(1)`. `Wait` sees a counter of zero and returns immediately, so `processBatch` returns to its caller while every item's processing is still pending or hasn't started at all.
- The Go race detector will often (though not always, depending on timing) flag this as a data race between the concurrent `Add` and `Wait` calls — and in the worst case, this exact pattern can trigger a `sync: negative WaitGroup counter` panic if `Done` manages to run before its matching `Add`.

**✅ Good**
```go
func processBatch(ids []string) {
	var wg sync.WaitGroup
	for _, id := range ids {
		wg.Add(1) // called synchronously, before the goroutine that will later call Done
		go func(id string) {
			defer wg.Done()
			process(id)
		}(id)
	}
	wg.Wait()
}
```

**Why it works / Explanation:** Calling `Add(1)` on the parent goroutine immediately before each `go` statement guarantees the counter reflects every pending unit of work before `Wait` is ever reached — there's no interleaving where `Wait` can observe a stale, too-low count, because the increments happen strictly earlier in program order on the same goroutine that will eventually call `Wait`.

**Design principle:** `Add` and `go` belong together as a single unit at the call site — write `wg.Add(1)` on the line directly above the `go` statement it accounts for, every time.

---

## 3. Forgetting `defer mu.Unlock()`

**The Problem:** Any exit from a function between `mu.Lock()` and a manually-placed `mu.Unlock()` — an early `return`, a panic — skips the unlock. The mutex stays locked forever, and every future call that tries to acquire it blocks permanently.

**❌ Bad**
```go
func (a *Account) Withdraw(amount int) error {
	a.mu.Lock()
	if amount > a.balance {
		return errors.New("insufficient funds") // BUG: returns without ever unlocking a.mu
	}
	a.balance -= amount
	a.mu.Unlock()
	return nil
}
```

**Why it's wrong:**
- The moment `amount > a.balance` is true once, the function returns through the early `return` and `a.mu.Unlock()` is never reached — `a.mu` stays locked for the remaining lifetime of the program.
- Every subsequent call to `Withdraw` (or any other method that locks `a.mu`) blocks forever waiting for a lock that will never be released — one bad withdrawal request permanently deadlocks the entire `Account`, and by extension anything serialized behind it.

**✅ Good**
```go
func (a *Account) Withdraw(amount int) error {
	a.mu.Lock()
	defer a.mu.Unlock() // runs on every exit path: normal return, early return, or panic
	if amount > a.balance {
		return errors.New("insufficient funds")
	}
	a.balance -= amount
	return nil
}
```

**Why it works / Explanation:** `defer a.mu.Unlock()` is registered once, immediately after acquiring the lock, so it executes no matter how the function exits — including through a panic that unwinds the stack. This decouples "how many exit points does this function have" from "will the lock actually get released," which matters a lot once a function grows past its first `if err != nil { return }`.

**Design principle:** Always pair `Lock()` with `defer Unlock()` on the very next line — never let any code, however short, sit between acquiring a lock and deferring its release.

---

## 4. Mutexes Aren't Reentrant — Recursive Locking Self-Deadlocks (and `RWMutex` Writer Starvation)

**The Problem:** None of Go's mutex types are reentrant: a goroutine that already holds a lock and calls `Lock()` (or, for `RWMutex`, even `RLock()`) again — directly or through a method it calls while already holding the lock — blocks on itself forever. `sync.RWMutex` has a specific, documented version of this: to avoid starving writers, once a writer is waiting, new `RLock()` calls block until that writer proceeds — which means a goroutine that recursively calls `RLock()` while a writer is queued in between deadlocks against its own earlier `RLock()`.

**❌ Bad**
```go
type Cache struct {
	mu   sync.RWMutex
	data map[string]string
}

func (c *Cache) Get(key string) string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.lookupRelated(key)
}

func (c *Cache) lookupRelated(key string) string {
	c.mu.RLock() // BUG: recursive RLock — this goroutine already holds a read lock from Get
	defer c.mu.RUnlock()
	return c.data[key]
}
```

**Why it's wrong:**
- If another goroutine calls `c.mu.Lock()` (a writer) after `Get`'s outer `RLock()` succeeds but before `lookupRelated`'s inner `RLock()` runs, Go's `RWMutex` blocks that inner `RLock()` until the pending writer gets its turn — but the writer can't proceed until `Get`'s outer `RUnlock()` runs, which never happens until `lookupRelated` returns. Both goroutines now wait on each other forever.
- This is explicitly called out in the standard library docs for `RWMutex`: recursive read-locking is unsafe specifically because of the writer-starvation prevention built into `Lock()` — it is not a hypothetical edge case, it's documented behavior.
- More generally, calling plain `mu.Lock()` twice from the same goroutine (e.g., a public method calling another method that also locks) deadlocks unconditionally — there is no special case where Go mutexes tolerate re-acquisition by their current holder.

**✅ Good**
```go
type Cache struct {
	mu   sync.RWMutex
	data map[string]string
}

func (c *Cache) Get(key string) string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.data[key] // inline the lookup instead of recursively re-locking
}

func (c *Cache) lookupRelatedLocked(key string) string {
	// Caller must already hold c.mu (read or write) — no locking happens in here.
	return c.data[key]
}
```

**Why it works / Explanation:** Removing the second `RLock()` call entirely — either by inlining the logic or by splitting it into a `*Locked` helper that documents "caller already holds the lock" as part of its contract — eliminates the recursive acquisition altogether. The general pattern for code that needs to call locking logic from within an already-locked context is exactly this: keep a public, locking entry point and a private, non-locking variant that assumes the lock is already held, and never have the locking variant call itself indirectly.

**Design principle:** Treat every `sync` mutex as strictly non-reentrant — structure code with locked/unlocked method pairs rather than ever calling back into a method that re-acquires a lock the current goroutine already holds.

---

## 5. `sync.Once.Do` Swallows Panics Permanently

**The Problem:** If the function passed to `once.Do` panics, `Once` still marks itself as having run — the internal "done" flag is set via a `defer` that executes regardless of whether the function panicked. Every subsequent call to `Do` becomes a silent no-op instead of retrying the initialization that failed.

**❌ Bad**
```go
var (
	once   sync.Once
	client *APIClient
)

func getClient() *APIClient {
	once.Do(func() {
		client = newAPIClient() // BUG: if this panics (e.g. missing config), Once still finishes as "done"
	})
	return client // every future call returns nil, forever, with no retry and no error
}
```

**Why it's wrong:**
- If `newAPIClient()` panics on its first call (a missing environment variable, an unreachable dependency at startup), `once.Do`'s internal bookkeeping marks the `Once` as executed regardless — this is by design in the standard library implementation, not a bug in `Once` itself.
- Every later call to `getClient()` now returns `nil` silently, forever, without ever attempting initialization again — if the panic is recovered somewhere up the call stack (say, HTTP middleware that recovers panics per-request), the program keeps running with a permanently broken, un-retried dependency and no error signal pointing at why.

**✅ Good**
```go
var (
	once    sync.Once
	client  *APIClient
	initErr error
)

func getClient() (*APIClient, error) {
	once.Do(func() {
		defer func() {
			if r := recover(); r != nil {
				initErr = fmt.Errorf("client init panicked: %v", r)
			}
		}()
		client, initErr = newAPIClientSafe()
	})
	return client, initErr
}
```

**Why it works / Explanation:** Recovering inside the function passed to `Do` and capturing the failure into `initErr` means callers get an explicit, checkable error instead of a silently nil client — the failure is surfaced rather than swallowed. Note this still doesn't retry initialization on a later call (that's what `Once` means: exactly once, ever) — if retry-on-failure is actually required, don't reach for `Once` at all; use an `atomic.Pointer` or a mutex-guarded "attempted" flag that you explicitly reset or re-check on failure.

**Design principle:** Never let the function passed to `sync.Once.Do` panic — recover and record the failure explicitly, since `Once` treats "ran and panicked" identically to "ran successfully" and will never call the function again either way.

---

## 6. Reaching for `sync.Map` by Default

**The Problem:** `sync.Map` is a specialized data structure optimized for one specific access pattern — the standard library's own documentation describes it as best suited for cases where entries are written once and read many times, or where goroutines operate on largely disjoint sets of keys. Using it as a general-purpose "concurrent map" replacement trades away type safety and often loses on performance for the common read/write-mixed workload.

**❌ Bad**
```go
var cache sync.Map // BUG: reached for by default "because it's the concurrent-safe map"

func recordHit(key string) {
	v, _ := cache.LoadOrStore(key, new(int64))
	counter := v.(*int64)      // type assertion required at every call site — no compile-time safety
	atomic.AddInt64(counter, 1)
}
```

**Why it's wrong:**
- Every value stored in a `sync.Map` is `any`, so every read requires a type assertion (`v.(*int64)` here) scattered throughout the codebase — a mistake in that assertion (wrong type, wrong pointer-ness) is a runtime panic that a regular typed map would have caught at compile time.
- For a workload like this one — many keys, both reads and writes happening continuously, no particular "write-once, read-many" or "disjoint key set per goroutine" access pattern — `sync.Map`'s internal design (which optimizes for exactly those two patterns) provides no real advantage, and can be measurably slower than a plain map under a mutex for typical mixed read/write traffic.

**✅ Good**
```go
type HitCounter struct {
	mu     sync.RWMutex
	counts map[string]int64
}

func NewHitCounter() *HitCounter {
	return &HitCounter{counts: make(map[string]int64)}
}

func (h *HitCounter) RecordHit(key string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.counts[key]++
}

func (h *HitCounter) Get(key string) int64 {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return h.counts[key]
}
```

**Why it works / Explanation:** A regular `map[string]int64` guarded by a `sync.RWMutex` is fully typed — no `any`, no assertions, no risk of a panic from an unexpected stored type — and for a typical mixed read/write workload it's simple to reason about and often just as fast, or faster, than `sync.Map`. Reserve `sync.Map` for the access patterns its documentation actually targets: caches that are populated once and read heavily afterward, or sharded/disjoint key access across goroutines — verify with a benchmark on your actual workload rather than assuming "concurrent" implies "use `sync.Map`."

**Design principle:** Default to a typed `map` plus `sync.RWMutex` for concurrent map access; reach for `sync.Map` only for its specific documented sweet spot, and only after confirming it actually wins on your access pattern.

---

## 7. Mixing Atomic and Non-Atomic Access to the Same Variable

**The Problem:** Atomicity is a property of every access to a variable, not a property you can apply to just some of them. If even one goroutine reads or writes a field directly while others use `atomic` operations on it, the direct access is an unsynchronized data race with the atomic ones — the atomic calls provide no protection against a plain, non-atomic read or write happening concurrently.

**❌ Bad**
```go
type Stats struct {
	requests int64
}

func (s *Stats) recordFast() {
	atomic.AddInt64(&s.requests, 1) // atomic write
}

func (s *Stats) recordSlow() {
	s.requests++ // BUG: plain, non-atomic read-modify-write on the same field
}

func (s *Stats) Total() int64 {
	return s.requests // BUG: plain, non-atomic read — races with the atomic writes above
}
```

**Why it's wrong:**
- `atomic.AddInt64` only guarantees atomicity for the call sites that use it. `recordSlow`'s plain `s.requests++` and `Total`'s plain `return s.requests` bypass that entirely — they're ordinary memory accesses that can race with the atomic add from another goroutine, corrupting the count or reading a torn/stale value.
- `go test -race` will flag this as a data race the moment a test exercises `recordFast` and `recordSlow` (or `Total`) concurrently — "some accesses are atomic" does not make the non-atomic ones safe; it just makes the bug harder to spot by reading the code, since part of it looks correctly synchronized.

**✅ Good**
```go
type Stats struct {
	requests atomic.Int64 // Go 1.19+ typed atomic — every access goes through its methods
}

func (s *Stats) recordFast() {
	s.requests.Add(1)
}

func (s *Stats) recordSlow() {
	s.requests.Add(1) // same method as every other access — no plain reads/writes possible
}

func (s *Stats) Total() int64 {
	return s.requests.Load()
}
```

**Why it works / Explanation:** Using the typed `atomic.Int64` as the field itself (rather than a plain `int64` accessed sometimes through `atomic.*Int64` functions and sometimes not) makes non-atomic access a compile error — there's no `s.requests++` possible anymore, because `requests` is no longer an `int64`, it's an `atomic.Int64` whose only operations are its synchronized methods (`Add`, `Load`, `Store`, `CompareAndSwap`). The type system now enforces the invariant that used to depend on every caller remembering to use `atomic.*` consistently.

**Design principle:** Every single access to a shared variable must go through the same synchronization mechanism — prefer Go 1.19+'s typed atomics (`atomic.Int64`, `atomic.Bool`, etc.) as the field type itself, so inconsistent access becomes a compile error instead of a race.

---

## 8. Hand-Rolled Double-Checked Locking

**The Problem:** A manual "check without a lock, then lock and check again" optimization for lazy initialization is a well-known pattern in other languages, but a naive Go port is unsafe: an unsynchronized read of a shared pointer has no ordering guarantee under the Go memory model, so it can observe a stale or partially-published value written by another goroutine.

**❌ Bad**
```go
var (
	mu   sync.Mutex
	inst *Singleton
)

func GetInstance() *Singleton {
	if inst == nil { // BUG: unsynchronized read — races with the write inside the lock below
		mu.Lock()
		if inst == nil {
			inst = newSingleton()
		}
		mu.Unlock()
	}
	return inst
}
```

**Why it's wrong:**
- The outer `if inst == nil` check reads `inst` with no synchronization at all — no mutex, no atomic operation — so it has no happens-before relationship with the write `inst = newSingleton()` performed by another goroutine under the lock. Under the Go memory model, that read is a genuine data race with that write, and `go test -race` will report it as such the moment two goroutines call `GetInstance` concurrently during initialization.
- The "optimization" this pattern is chasing — skip locking once `inst` is already set — is exactly the part that's unsafe to do with a plain pointer read; the lock-free fast path is precisely where the missing synchronization lives.

**✅ Good — `sync.Once`, the tool built for exactly this**
```go
var (
	once sync.Once
	inst *Singleton
)

func GetInstance() *Singleton {
	once.Do(func() {
		inst = newSingleton()
	})
	return inst
}
```

**✅ Good — `atomic.Pointer`, if you need a lock-free fast path explicitly**
```go
var instPtr atomic.Pointer[Singleton]

func GetInstance() *Singleton {
	if p := instPtr.Load(); p != nil {
		return p
	}
	inst := newSingleton()
	instPtr.CompareAndSwap(nil, inst) // if another goroutine won the race, we discard our extra instance
	return instPtr.Load()
}
```

**Why it works / Explanation:** `sync.Once` is specifically designed to make "run this initialization exactly once, and have every caller — concurrent or not — see the fully-initialized result" both correct and simple; it handles the synchronization internally so callers never need to reason about memory ordering themselves. `atomic.Pointer[T]` is the right tool if you specifically need a genuinely lock-free read path after initialization: its `Load`/`Store`/`CompareAndSwap` operations carry the memory-ordering guarantees that a plain pointer read/write lacks, so a `Load()` after another goroutine's successful `CompareAndSwap` is guaranteed to observe the fully-constructed value.

**Design principle:** Never hand-roll synchronization around a plain variable read as a "fast path" — use `sync.Once` for one-time initialization, or `atomic.Pointer`/`atomic.Value` when you specifically need a lock-free read after initial setup.

---

## 9. Holding a Lock While Calling Into Callback Code

**The Problem:** Calling a user-supplied callback, or sending on a channel, while still holding a mutex is risky: if that code path ever tries to re-acquire the same lock (directly, or indirectly through another call back into your type), it deadlocks — and even if it never does, you've serialized all callback execution behind a lock that has nothing to do with the callback's own work.

**❌ Bad**
```go
type EventBus struct {
	mu        sync.Mutex
	listeners []func(Event)
}

func (b *EventBus) Subscribe(fn func(Event)) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.listeners = append(b.listeners, fn)
}

func (b *EventBus) Publish(e Event) {
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, fn := range b.listeners {
		fn(e) // BUG: calling unknown code while still holding b.mu
	}
}
```

**Why it's wrong:**
- If any listener registered via `Subscribe` calls `b.Subscribe(...)` or `b.Publish(...)` again from within its callback — a very natural thing for event-driven code to do — that call tries to acquire `b.mu`, which the current goroutine already holds, and deadlocks on itself (see gotcha #4 above).
- Even without that specific reentrancy, every listener now executes serially, one at a time, while `b.mu` stays locked for the entire duration of `Publish` — if one listener is slow (a network call, a slow computation), it blocks every other goroutine that just wants to `Subscribe` or `Publish`, for reasons entirely unrelated to the event bus's own bookkeeping.

**✅ Good**
```go
func (b *EventBus) Publish(e Event) {
	b.mu.Lock()
	listeners := make([]func(Event), len(b.listeners))
	copy(listeners, b.listeners)
	b.mu.Unlock() // lock released before any callback runs

	for _, fn := range listeners {
		fn(e) // callbacks run with no lock held — safe even if they call back into EventBus
	}
}
```

**Why it works / Explanation:** Copying `listeners` while holding the lock, then releasing the lock before invoking any of them, shrinks the critical section down to just the bookkeeping it actually needs to protect — the slice copy — and runs all the unknown, potentially slow, potentially reentrant callback code with no lock held at all. A listener that calls back into `Subscribe` or `Publish` now succeeds instead of deadlocking, and one slow listener no longer blocks unrelated callers of the `EventBus`.

**Design principle:** Keep critical sections as small as possible and never call into code you don't control while holding a lock — copy the data you need under the lock, release it, then do the actual work.

---

## Key Takeaways
- Never copy a struct containing a `sync.Mutex`, `RWMutex`, or `WaitGroup` — use pointer receivers and pointer types, and let `go vet`'s `copylocks` check enforce it in CI.
- Call `wg.Add` synchronously before `go`, never from inside the goroutine it's tracking, or `Wait` can return before the work has even started.
- Pair every `Lock()` with an immediate `defer Unlock()` so a panic or early return can never leave the mutex permanently held.
- Treat Go's mutexes as non-reentrant, including `RWMutex`'s recursive-`RLock` case documented specifically because of writer-starvation prevention — restructure with locked/unlocked method pairs instead of calling back into a locking method.
- Recover panics inside `sync.Once.Do`'s function and record the failure explicitly — `Once` marks itself done even if the function panicked, and will never retry.
- Default to a typed `map` plus `sync.RWMutex`; reserve `sync.Map` for its documented sweet spot of write-once/read-many or disjoint-key access patterns.
- Make every access to a shared variable go through the same synchronization mechanism — a Go 1.19+ typed atomic as the field itself prevents an accidental non-atomic access from compiling.
- Don't hand-roll double-checked locking around a plain variable — use `sync.Once` for one-time init, or `atomic.Pointer`/`atomic.Value` for a genuinely lock-free read path.
- Release locks before calling into callbacks or unknown code — copy what you need under the lock, then operate on the copy afterward.

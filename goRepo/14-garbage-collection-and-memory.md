# Garbage Collection and Memory Management Pitfalls in Go

Go's garbage collector hides manual memory management, but it does not make memory free — every allocation still costs CPU to create and CPU to scan/collect later, and "no leaks" in the C sense does not mean "no leaks" in the practical sense. Production Go services routinely die from ever-growing RSS, GC-pause-driven latency spikes, and OOM kills caused by patterns that look perfectly idiomatic at a glance. This file covers the allocation, retention, and lifecycle mistakes that turn a correct program into a memory- or GC-bound one.

## 1. GC Pressure from Excessive Small Heap Allocations

**The Problem:** Building data structures out of individually heap-allocated nodes (linked lists, trees) or slices of pointers (`[]*Struct`) turns every element into a separate object that the garbage collector must allocate, track, and scan for pointers on every mark phase. This is an easy trap because pointer-based structures feel natural, and the allocation cost is invisible at the call site.

**❌ Bad**
```go
type Node struct {
	Value int
	Next  *Node
}

func buildList(n int) *Node {
	var head *Node
	for i := 0; i < n; i++ {
		head = &Node{Value: i, Next: head} // BUG: n separate heap allocations, each scanned by GC
	}
	return head
}
```

**Why it's wrong:**
- Each `&Node{...}` is a distinct heap allocation; building a list of `n` items means `n` allocator calls and `n` objects the GC's mark phase must visit and follow pointers from, on every single collection cycle for as long as the list stays reachable.
- The same problem hides in `[]*Struct`: the GC must scan every pointer in the slice *and* every field of every pointed-to struct, versus `[]Struct` where the collector scans one contiguous block (and skips it entirely if the struct has no pointer fields).
- Nodes end up scattered wherever the allocator happened to place them, so traversal suffers cache misses that a contiguous slice would not.

**✅ Good**
```go
func buildValues(n int) []int {
	values := make([]int, 0, n) // one contiguous allocation, no pointers to scan
	for i := 0; i < n; i++ {
		values = append(values, i)
	}
	return values
}
```

**Why it works / Explanation:** A single preallocated slice replaces `n` individual allocations with one. Because `int` contains no pointers, the GC does not need to scan the slice's contents at all — only the slice header (which typically lives on the stack or inside a parent struct). Sequential memory layout also gives good cache locality during iteration, which pointer-chasing through `Next` never can.

**Design principle:** Prefer "struct of arrays" / value slices over pointer-heavy linked structures in hot paths — collapse many small allocations into one, and let plain non-pointer data pass the GC by untouched.

---

## 2. Goroutine Leaks as a Form of Memory Leak

**The Problem:** A goroutine that blocks forever (on a channel send/receive with no counterpart) never gets collected — its entire stack, and everything it references, stays reachable and alive for the lifetime of the process. This shows up in `pprof`'s heap profile as ever-growing memory even though, from a language-semantics view, "nothing is technically leaked": the GC correctly considers the blocked goroutine's stack a root. See the goroutines document for the concurrency-correctness angle; here the point is purely the memory consequence.

**❌ Bad**
```go
func handleRequest(data []byte) <-chan Result {
	resultCh := make(chan Result)
	go func() {
		result := heavyProcess(data) // data and result stay referenced by this goroutine's stack
		resultCh <- result           // BUG: blocks forever if nobody ever reads resultCh
	}()
	return resultCh
}

func caller(data []byte) {
	ch := handleRequest(data)
	select {
	case res := <-ch:
		use(res)
	case <-time.After(time.Second):
		return // caller gives up — but the goroutine above is still blocked on the send, forever
	}
}
```

**Why it's wrong:**
- If the caller times out (as above) or otherwise stops reading from `ch`, the spawned goroutine is stuck forever on `resultCh <- result`. It can never be garbage collected because it is still running — its stack, `data`, and `result` all stay alive.
- Call `handleRequest` once per incoming HTTP request under load and you leak one goroutine (plus its referenced payload) per timed-out request; the heap profile shows steady, unbounded growth with no single large object to blame, which makes this leak shape harder to spot than a simple "big object retained" leak.

**✅ Good**
```go
func handleRequest(data []byte) <-chan Result {
	resultCh := make(chan Result, 1) // buffered: the send below can never block
	go func() {
		result := heavyProcess(data)
		resultCh <- result // always succeeds immediately, goroutine exits right after
	}()
	return resultCh
}
```

**Why it works / Explanation:** A buffer of size 1 guarantees the goroutine's single send completes immediately regardless of whether — or when — the caller reads it, so the goroutine always runs to completion and exits. For longer-running work, pair this with `context` cancellation so the work itself can be aborted early, not just its delivery unblocked.

**Design principle:** Every goroutine must have a guaranteed exit path under every caller behavior (including caller abandonment) — treat goroutines as memory owners, since a leaked goroutine is a leaked reference graph.

---

## 3. `time.Ticker`/`time.Timer` Not Stopped

**The Problem:** `time.NewTicker` starts an internal runtime timer that keeps firing on its channel forever. Dropping the local `*Ticker` variable does **not** stop it — the runtime's timer heap keeps it alive independently of your reference to it — so it keeps consuming resources (and, prior to Go 1.23, keeps the ticker itself unreachable-but-running) until `Stop()` is called explicitly.

**❌ Bad**
```go
func pollStatus(url string) {
	ticker := time.NewTicker(5 * time.Second) // BUG: never stopped
	for range ticker.C {
		if checkStatus(url) {
			return // function returns, but the ticker's internal timer keeps firing forever
		}
	}
}
```

**Why it's wrong:**
- `pollStatus` returning does not stop the ticker; the runtime timer keeps waking up every 5 seconds and sending to `ticker.C` indefinitely, and the channel/timer stay reachable from runtime-internal state even though the caller's variable is gone.
- If `pollStatus` is called repeatedly (e.g., once per incoming request, or once per retry loop), each call leaks its own ticker — the count of live, silently-firing timers grows without bound, and each one does real work (waking a goroutine, sending on a channel) that shows up as unexplained background CPU and goroutine-adjacent scheduling load.

**✅ Good**
```go
func pollStatus(url string) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop() // releases the underlying timer resources on every exit path
	for range ticker.C {
		if checkStatus(url) {
			return
		}
	}
}
```

**Why it works / Explanation:** `defer ticker.Stop()` guarantees the ticker is stopped on every return path, including early returns and panics, so the runtime's timer is removed and becomes eligible for cleanup as soon as the function is done with it. The same rule applies to `time.NewTimer` — always `defer timer.Stop()` unless you have already drained/consumed the timer's single fire.

**Design principle:** Any API that hands you a "keeps running until stopped" handle (`Ticker`, `Timer`, and similarly `context.WithCancel`'s cancel func) must have its stop/cancel function called on every code path — pair acquisition with `defer` immediately, not "eventually."

---

## 4. Large Object Retained via a Small Reference

**The Problem:** An unbounded cache — a plain `map[string][]byte` with no eviction — keeps every value it has ever stored alive for the life of the process, because the map itself never becomes unreachable. A "small" cache variable can silently keep gigabytes of large payloads alive; this is the caching-specific case of the more general sub-slice retention leak covered in the slices document, broadened here to any cache without bounded size or TTL.

**❌ Bad**
```go
var assetCache = make(map[string][]byte)

func getAsset(key string) []byte {
	if v, ok := assetCache[key]; ok {
		return v
	}
	data := loadFromDisk(key) // e.g. several MB per asset
	assetCache[key] = data    // BUG: never evicted — grows forever, one entry per unique key ever seen
	return data
}
```

**Why it's wrong:**
- Map entries live for the process's entire lifetime; every unique key ever requested adds another multi-megabyte value that is never freed, even if it is never requested again.
- The heap profile shows growth proportional to the number of unique keys seen, not to the size of the "cache" as a concept — which makes the leak easy to miss during code review, since `assetCache` itself looks like an innocuous, small package-level variable.

**✅ Good**
```go
type lruCache struct {
	mu       sync.Mutex
	capacity int
	ll       *list.List
	items    map[string]*list.Element
}

type entry struct {
	key   string
	value []byte
}

func newLRUCache(capacity int) *lruCache {
	return &lruCache{capacity: capacity, ll: list.New(), items: make(map[string]*list.Element)}
}

func (c *lruCache) Get(key string) ([]byte, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[key]; ok {
		c.ll.MoveToFront(el)
		return el.Value.(*entry).value, true
	}
	return nil, false
}

func (c *lruCache) Put(key string, value []byte) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[key]; ok {
		c.ll.MoveToFront(el)
		el.Value.(*entry).value = value
		return
	}
	el := c.ll.PushFront(&entry{key, value})
	c.items[key] = el
	if c.ll.Len() > c.capacity {
		oldest := c.ll.Back()
		c.ll.Remove(oldest)
		delete(c.items, oldest.Value.(*entry).key)
	}
}
```

**Why it works / Explanation:** Bounding the cache to a fixed `capacity` (or, alternatively, attaching a TTL and sweeping expired entries) guarantees that old, large entries are evicted and dropped from the map, at which point they become unreachable and eligible for collection. The cache's memory footprint is now bounded by a number you chose, not by "however many unique keys production traffic happens to generate."

**Design principle:** Any long-lived cache must be paired with an explicit eviction policy (size cap, LRU, or TTL) — an unbounded cache is a memory leak with a friendlier name.

---

## 5. `GOGC` Tuning

**The Problem:** `GOGC` controls the heap-growth ratio the runtime targets before triggering the next garbage collection: with the default `GOGC=100`, the runtime aims to start the next GC cycle when the heap has grown to roughly twice the live-object size measured after the last collection. Leaving it at the default in a workload whose tradeoffs don't match — e.g., a batch job with memory headroom and no latency SLA — leaves real throughput on the table.

**❌ Bad**
```go
func main() {
	// GOGC defaults to 100: GC triggers whenever the heap doubles since the
	// last collection. For an allocation-heavy batch job with memory to
	// spare, this means far more GC cycles than necessary, stealing CPU
	// from useful work with no latency benefit to show for it.
	results := runBatchETL(hugeDataset) // BUG: no GC tuning for a throughput-oriented job
	persist(results)
}
```

**Why it's wrong:**
- Every extra GC cycle spends CPU walking live objects that will simply be walked again shortly after — cycles a latency-insensitive batch job doesn't need to pay if it has spare memory.
- The default is tuned as a general-purpose compromise; it is not tuned for "this specific job runs alone on a box with 32 GB free and just needs to finish fast."

**✅ Good**
```go
import "runtime/debug"

func main() {
	// We have headroom (batch job runs alone, plenty of RAM): let the heap
	// grow to 5x live size before collecting, trading memory for fewer,
	// cheaper-per-byte-processed GC cycles.
	prev := debug.SetGCPercent(400)
	defer debug.SetGCPercent(prev) // restore if this process later shares work with other tenants

	results := runBatchETL(hugeDataset)
	persist(results)
}
```
```bash
GOGC=400 ./etl-job   # equivalent: set via environment instead of code
```

**Why it works / Explanation:** Raising `GOGC` widens the gap between "live heap" and "heap size that triggers the next GC," so the collector runs less often but has more garbage to reclaim per run — higher throughput, higher peak memory. Lowering `GOGC` does the opposite: more frequent, smaller collections, lower peak memory, more CPU spent in the collector. Reach for this knob for memory-rich, latency-insensitive workloads (batch/offline jobs) when raising it, or for memory-constrained services running many instances per host when lowering it.

**Design principle:** `GOGC` is a memory-vs-CPU tradeoff knob, not a fix for incorrect allocation behavior — tune it only after allocation patterns are already sound; otherwise you're just changing how often the same excessive garbage gets collected.

---

## 6. `GOMEMLIMIT` as a Memory Ceiling Safety Net

**The Problem:** `GOGC` reacts to a *ratio* (heap growth relative to live size), not an absolute number of bytes. In a container with a hard memory limit, a burst of allocations can push RSS past that limit before the ratio-based heuristic has any reason to collect aggressively, and the kernel/orchestrator OOM-kills the process — a hard, ungraceful failure with no chance for the Go runtime to react.

**❌ Bad**
```go
func main() {
	// Running in a Kubernetes pod with resources.limits.memory: 512Mi.
	// GOGC=100 only reacts to heap growth ratio, not absolute bytes: a
	// burst of large allocations can push RSS past 512Mi before GC catches
	// up, and the kernel OOM-kills the process. BUG: no absolute ceiling set.
	startServer()
}
```

**Why it's wrong:**
- There is no signal telling the Go runtime "you are approaching a hard external limit" — it only knows about heap-growth ratios, so it can be perfectly well-behaved by its own metric and still get killed by the OS.
- OOM kills are abrupt (SIGKILL) — no graceful shutdown, no final flush of buffers/logs, just a dead process and a restart loop if the burst is recurring.

**✅ Good**
```go
import "runtime/debug"

func main() {
	// Belt-and-suspenders: cap Go's own memory use safely under the
	// container's 512Mi limit, independent of GOGC. As usage approaches
	// this limit, the GC collects more aggressively, overriding GOGC's
	// target ratio when necessary to stay under the ceiling.
	debug.SetMemoryLimit(450 << 20) // ~450 MiB, with margin below the 512Mi cgroup limit
	startServer()
}
```
```bash
GOMEMLIMIT=450MiB ./server   # equivalent: set via environment instead of code
```

**Why it works / Explanation:** `GOMEMLIMIT` (Go 1.19+) gives the runtime an absolute soft ceiling. `GOGC` still governs "normal" pacing under that ceiling, but as live+garbage memory approaches the `GOMEMLIMIT` value, the collector runs more frequently and aggressively to keep total usage under it — a genuine safety net independent of the ratio heuristic. It is a *soft* limit: the GC cannot force live memory below what your program actually holds onto, so set it with real margin below the hard container limit, and don't set it so tight that the collector thrashes (constant GC cycles for little memory recovered).

**Design principle:** In constrained environments, combine `GOGC` for throughput tuning with `GOMEMLIMIT` as an OOM safety net — they solve different problems and are meant to be used together, not as alternatives.

---

## 7. `runtime.SetFinalizer` Pitfalls

**The Problem:** A finalizer registered with `runtime.SetFinalizer` runs only after the GC determines the object is unreachable — which can be seconds, minutes, or (if the process exits normally before the next relevant GC cycle) never. Relying on a finalizer as the primary way to release an OS-visible resource (file handles, sockets, locks) is a latent bug: the resource stays held for an unpredictable, unbounded amount of time.

**❌ Bad**
```go
type LogFile struct {
	f *os.File
}

func OpenLogFile(path string) (*LogFile, error) {
	f, err := os.Create(path)
	if err != nil {
		return nil, err
	}
	lf := &LogFile{f: f}
	runtime.SetFinalizer(lf, func(l *LogFile) {
		l.f.Close() // BUG: relying on the finalizer as the *only* cleanup path
	})
	return lf, nil
}
```

**Why it's wrong:**
- The finalizer only runs after a GC cycle proves `lf` unreachable; under light allocation pressure that GC cycle might not happen for a long time, so the file descriptor stays open far longer than the object's logical lifetime — under heavy concurrent use this exhausts the process's file-descriptor limit.
- Finalized objects take at least two GC cycles to fully reclaim (one to run the finalizer, one more to actually free the memory), adding GC bookkeeping overhead that a plain object doesn't have.
- If the finalizer function accidentally makes `lf` reachable again (e.g., appends it to a global slice for "debugging"), the object is "resurrected" and the finalizer will never fire again, permanently leaking the file handle.
- Finalizers are not guaranteed to run at all before an abrupt process exit (`os.Exit`, a crash), so "the finalizer will eventually close it" is not even a reliable worst-case guarantee.

**✅ Good**
```go
type LogFile struct {
	f *os.File
}

func OpenLogFile(path string) (*LogFile, error) {
	f, err := os.Create(path)
	if err != nil {
		return nil, err
	}
	return &LogFile{f: f}, nil
}

func (l *LogFile) Close() error {
	return l.f.Close()
}

func useLogFile(path string) error {
	lf, err := OpenLogFile(path)
	if err != nil {
		return err
	}
	defer lf.Close() // deterministic cleanup at a known point, on every exit path
	_, err = lf.f.WriteString("started\n")
	return err
}
```

**Why it works / Explanation:** An explicit `Close()` called via `defer` runs deterministically, at a precisely known point, on every return path including panics — no dependency on GC timing at all. Finalizers are appropriate only as a last-resort safety net (e.g., logging a warning that a caller forgot to `Close()`, or freeing cgo-allocated memory with no other release point), never as the primary cleanup mechanism for anything the caller is expected to close themselves.

**Design principle:** Use `defer`-based explicit cleanup for anything holding an OS-visible resource; finalizers exist to catch bugs in that discipline, not to replace it.

---

## 8. `sync.Pool` Misuse

**The Problem:** `sync.Pool` is often treated as a guaranteed object cache ("put it in the pool, get free reuse"), but the runtime is free to evict and drop pooled items at any time — notably, items can be cleared around garbage collection cycles. `sync.Pool` only amortizes allocation cost for short-lived, high-churn objects under sustained load; it must never hold objects that require explicit cleanup or that carry state that must not leak between uses.

**❌ Bad**
```go
var bufPool = sync.Pool{
	New: func() any { return new(bytes.Buffer) },
}

func writeResponse(w http.ResponseWriter, userID string) {
	buf := bufPool.Get().(*bytes.Buffer)
	buf.WriteString("user:")
	buf.WriteString(userID)
	w.Write(buf.Bytes())
	bufPool.Put(buf) // BUG: buffer not reset before returning to the pool
}
```

**Why it's wrong:**
- The buffer is never reset before `Put`, so the next caller's `Get` can receive a buffer that still contains the *previous* request's bytes; a subsequent `WriteString` appends onto stale data rather than starting clean — a correctness bug (potentially leaking one user's data into another response), not just a performance one.
- The code implicitly assumes `Get`/`Put` behaves like a fixed-capacity cache with guaranteed reuse. It doesn't: under memory pressure the runtime can drop pooled items, `New` gets invoked again, and any capacity planning based on "the pool will have N buffers ready" is unfounded.

**✅ Good**
```go
var bufPool = sync.Pool{
	New: func() any { return new(bytes.Buffer) },
}

func writeResponse(w http.ResponseWriter, userID string) {
	buf := bufPool.Get().(*bytes.Buffer)
	buf.Reset() // clear any leftover data before use, defensively
	defer func() {
		buf.Reset() // clear this request's data before returning it
		bufPool.Put(buf)
	}()

	buf.WriteString("user:")
	buf.WriteString(userID)
	w.Write(buf.Bytes())
}
```

**Why it works / Explanation:** Resetting on both `Get` (defense in depth) and before `Put` (avoid holding onto potentially sensitive data or oversized backing arrays across reuse) makes the pool safe to reuse regardless of what a previous caller left behind. The pool is used purely to amortize allocation cost for a short-lived, high-churn `*bytes.Buffer` — nothing about correctness depends on any particular object surviving in the pool.

**Design principle:** `sync.Pool` is a garbage-collector-aware allocation cache, not a resource manager or a guaranteed cache — pair it with strict reset discipline and never store anything with cleanup obligations (open files, live connections) in it.

---

## 9. String Concatenation with `+`/`+=` in a Loop

**The Problem:** Strings in Go are immutable, so every `+`/`+=` concatenation allocates a brand-new string sized for the combined content and copies both operands into it. Doing this repeatedly inside a loop turns what looks like simple string building into O(n²) total bytes copied.

**❌ Bad**
```go
func buildCSVRow(fields []string) string {
	var row string
	for i, f := range fields {
		if i > 0 {
			row += "," // BUG: allocates a new string and copies all prior content, every iteration
		}
		row += f
	}
	return row
}
```

**Why it's wrong:**
- Each `+=` allocates a new backing array the size of `len(row) + len(addition)` and copies the entire existing `row` into it before appending — the further along the loop you are, the more bytes get re-copied for no new reason.
- Over `n` fields with average length `L`, total bytes copied grows roughly as O(n² · L), which for large inputs (a CSV row with hundreds of fields, or building a large log line field-by-field) shows up as a surprisingly large allocation count and CPU cost for "just concatenating some strings."

**✅ Good**
```go
func buildCSVRow(fields []string) string {
	var b strings.Builder
	b.Grow(estimateSize(fields)) // optional: preallocate to avoid the Builder's own regrowth
	for i, f := range fields {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteString(f)
	}
	return b.String()
}

func estimateSize(fields []string) int {
	n := len(fields) - 1 // commas
	for _, f := range fields {
		n += len(f)
	}
	return n
}
```

**Why it works / Explanation:** `strings.Builder` accumulates into a single growable byte buffer and only produces the final `string` once, via a no-copy internal conversion — turning the O(n²) copy pattern into O(n). Calling `Grow` with an upfront size estimate avoids even the Builder's own incremental regrowth.

**Design principle:** Never mutate-by-reassignment an immutable type inside a loop; use the mutable-buffer-then-freeze pattern (`strings.Builder`, `bytes.Buffer`) whenever building up a string incrementally.

---

## 10. Interface Boxing Causing Unexpected Heap Allocation

**The Problem:** Assigning a small concrete value (an `int`, a small struct) to an `interface{}`/`any` variable can force a heap allocation to hold the "boxed" value, because an interface value is a `(type, data)` pair and the data pointer must point somewhere that outlives the assignment. Escape analysis frequently cannot prove the boxed value is safe to keep on the stack once it is stored behind an interface, which is a subtle, easy-to-miss cost of "just use `any`" designs in hot paths.

**❌ Bad**
```go
func buildValues(n int) []any {
	values := make([]any, 0, n)
	for i := 0; i < n; i++ {
		values = append(values, i) // BUG: boxing an int into `any` typically heap-allocates
	}
	return values
}

func sum(values []any) int {
	total := 0
	for _, v := range values {
		total += v.(int) // unboxing back out again
	}
	return total
}
```

**Why it's wrong:**
- Each `append(values, i)` stores `i` behind an `any`, and because the resulting interface value escapes into a slice that outlives the loop iteration, the compiler must heap-allocate a home for that `int` rather than keeping it inline — for `n` values, that's `n` extra tiny heap objects purely to represent numbers that would otherwise cost zero extra allocations sitting inline in a plain `[]int`.
- The GC now has `n` additional small objects to track and scan on every cycle, and the type assertion on the way out (`v.(int)`) adds a runtime check that a concrete-typed slice never needs.

**✅ Good**
```go
func buildValues(n int) []int {
	values := make([]int, 0, n)
	for i := 0; i < n; i++ {
		values = append(values, i) // stored inline in the slice — no boxing, no allocation per element
	}
	return values
}

func sum(values []int) int {
	total := 0
	for _, v := range values {
		total += v
	}
	return total
}
```

**Why it works / Explanation:** A concretely-typed slice (`[]int`) stores each value inline in the backing array with no per-element interface header and no per-element allocation. When a container genuinely needs to be generic over element type, prefer Go generics (`[]T`) over `any`/`interface{}` — generics let the compiler work with the concrete type at each call site instead of forcing everything through boxed interface values.

**Design principle:** Reserve `any`/`interface{}` for genuine dynamic polymorphism; in hot paths, prefer concrete types or generics to avoid boxing allocations and preserve the compiler's ability to keep values on the stack.

---

## Key Takeaways
- Pointer-heavy structures (linked lists, `[]*Struct`) multiply allocation count and GC scan work — prefer contiguous value slices.
- A goroutine blocked forever keeps its whole stack and everything it references alive — leaked goroutines are memory leaks that show up as unbounded heap growth.
- `time.NewTicker`/`time.NewTimer` keep running until `Stop()` is called explicitly — dropping the variable does not stop them; always `defer Stop()`.
- Unbounded caches (maps with no eviction/TTL) let a "small" reference keep large payloads alive forever — always pair a cache with a size cap or TTL.
- `GOGC` tunes the memory-vs-CPU tradeoff of GC frequency via heap-growth ratio — raise it for throughput-oriented, memory-rich workloads; lower it under memory pressure.
- `GOMEMLIMIT` is an absolute soft ceiling that complements `GOGC`, acting as an OOM safety net in containers with hard memory limits.
- `runtime.SetFinalizer` runs at an unpredictable, unbounded delay (or not at all) — never substitute it for explicit `Close()`/`defer` cleanup of real resources.
- `sync.Pool` does not guarantee object retention and must never hold objects needing cleanup — always reset state on `Get` and before `Put`.
- String concatenation with `+=` in a loop is O(n²) due to immutability — use `strings.Builder` instead.
- Boxing small values into `any`/`interface{}` can force per-element heap allocations — prefer concrete types or generics in hot paths.

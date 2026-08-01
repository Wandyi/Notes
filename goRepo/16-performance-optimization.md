# Go Performance Optimization Patterns and Pitfalls

Go's simplicity and predictable runtime make it easy to reason about performance, but that same simplicity tempts engineers into "obvious" optimizations that a profiler would have told them not to bother with — while the actual bottleneck sits somewhere unglamorous. This file covers the concrete, recurring performance patterns worth knowing (allocation reduction, dispatch cost, cache behavior, struct layout) and, above all, the discipline of measuring before applying any of them.

## 1. Optimizing Before Profiling ("Premature Optimization")

**The Problem:** Without a profile, intuition about "what's slow" is usually wrong. Time spent hand-optimizing code based on a guess is time not spent finding — and fixing — the actual bottleneck, which is frequently somewhere unglamorous like a query pattern rather than in the arithmetic everyone assumes is hot. This is the umbrella principle for every other gotcha in this file: none of the patterns below should be applied speculatively.

**❌ Bad**
```go
// An engineer notices ProcessOrders is "slow" and, without profiling,
// assumes the arithmetic loop must be the bottleneck. They spend an
// afternoon hand-unrolling it:
func sumTotals(prices []float64) float64 {
	var total float64
	i := 0
	for ; i+4 <= len(prices); i += 4 { // BUG: manual unrolling based on a guess, not a profile
		total += prices[i] + prices[i+1] + prices[i+2] + prices[i+3]
	}
	for ; i < len(prices); i++ {
		total += prices[i]
	}
	return total
}

func ProcessOrders(ctx context.Context, orderIDs []int) (float64, error) {
	var grandTotal float64
	for _, id := range orderIDs {
		order, err := db.QueryOrder(ctx, id) // BUG: one network round-trip per order — the actual bottleneck
		if err != nil {
			return 0, err
		}
		grandTotal += sumTotals(order.LineItemPrices)
	}
	return grandTotal, nil
}
```

**Why it's wrong:**
- A CPU profile of `ProcessOrders` under real load would show `sumTotals` as a rounding error — a handful of float additions — while `db.QueryOrder`, called once per order in a loop (the classic N+1 query pattern), dominates cumulative time with N sequential network round-trips; `pprof`'s `top` would put driver/network code far above `sumTotals`.
- The manual unrolling adds code complexity and a new bug surface (easy to get the tail-loop bounds wrong) in exchange for zero measurable improvement, because the loop was never the bottleneck in the first place — effort spent here is effort not spent fixing the real N+1.

**✅ Good**
```go
func sumTotals(prices []float64) float64 {
	var total float64
	for _, p := range prices { // plain, obviously-correct loop — profiling showed this isn't hot
		total += p
	}
	return total
}

func ProcessOrders(ctx context.Context, orderIDs []int) (float64, error) {
	orders, err := db.QueryOrdersBatch(ctx, orderIDs) // one round-trip for all orders
	if err != nil {
		return 0, err
	}
	var grandTotal float64
	for _, order := range orders {
		grandTotal += sumTotals(order.LineItemPrices)
	}
	return grandTotal, nil
}
```
```bash
go tool pprof -top http://localhost:6060/debug/pprof/profile?seconds=30
# flat  flat%   cum%  function
# ...     ...    ...  (DB driver read/write dropped from ~85% cum to ~5% after batching)
```

**Why it works / Explanation:** Batching the DB call removes N-1 network round-trips — the real cost — while the arithmetic loop stays simple and readable, precisely because profiling confirmed it never needed optimizing.

**Design principle:** Profile first, always — every other pattern in this file is something to recognize *after* a profile points at it, never a checklist to apply blindly.

---

## 2. `string` <-> `[]byte` Conversions in Hot Loops

**The Problem:** Strings are immutable and byte slices are mutable, so in the general case a conversion between them must copy the underlying bytes — the runtime cannot safely alias the same memory for both. Doing this conversion repeatedly inside a hot loop turns an O(1)-looking type conversion into an O(n) copy, paid over and over.

**❌ Bad**
```go
func countWordOccurrences(logLines [][]byte, word string) int {
	count := 0
	for _, line := range logLines {
		s := string(line) // BUG: copies the entire line's bytes into a new string, every iteration
		if strings.Contains(s, word) {
			count++
		}
	}
	return count
}
```

**Why it's wrong:**
- `string(line)` allocates a new backing array and copies every byte of `line`, because a `string` must be immutable while `line` ([]byte) is mutable — the compiler cannot just alias the memory, since a concurrent mutation of `line` after the conversion would otherwise violate string immutability.
- Doing this once per line, over millions of log lines, means millions of full-line copies purely to satisfy a type conversion, dominating both CPU (the copy itself) and allocations (GC pressure from a new string per line).

**✅ Good**
```go
func countWordOccurrences(logLines [][]byte, word string) int {
	wordBytes := []byte(word) // convert once, outside the loop
	count := 0
	for _, line := range logLines {
		if bytes.Contains(line, wordBytes) { // operate on []byte directly — no per-line copy
			count++
		}
	}
	return count
}

// A narrow, compiler-recognized exception: using string(b) directly as a
// map index expression is special-cased to skip the allocation entirely.
func lookupCount(counts map[string]int, key []byte) int {
	return counts[string(key)] // no copy: the compiler recognizes this exact shape
}
```

**Why it works / Explanation:** Staying in `[]byte` throughout the hot path (`bytes.Contains` instead of `strings.Contains`) avoids the conversion entirely. When a string is genuinely needed only as a map lookup key — not stored anywhere — the Go compiler special-cases the exact expression `m[string(b)]` to skip the allocation, but that optimization applies only to that literal shape: assigning `s := string(b)` first and indexing with `s` afterward still allocates.

**Design principle:** Pick one representation (`[]byte` or `string`) for a hot path and stay in it end-to-end; know the narrow compiler-recognized exceptions rather than assuming all conversions are free.

---

## 3. `fmt.Sprintf` for Simple String Building in Hot Paths

**The Problem:** `fmt.Sprintf` parses a format string at runtime and uses reflection-adjacent type switching to render each argument — real overhead compared to direct, type-specific conversions like `strconv.Itoa` for simple cases. This overhead is negligible in a cold path (an error message, a CLI log line) but adds up fast in a hot one.

**❌ Bad**
```go
func formatKey(shardID int, userID int64) string {
	return fmt.Sprintf("shard:%d:user:%d", shardID, userID) // BUG: format-string parsing on every call
}
```
```bash
go test -bench BenchmarkFormatKeySprintf -benchmem
BenchmarkFormatKeySprintf-8    3000000    412 ns/op    48 B/op    2 allocs/op
```

**✅ Good**
```go
func formatKey(shardID int, userID int64) string {
	var b strings.Builder
	b.WriteString("shard:")
	b.WriteString(strconv.Itoa(shardID))
	b.WriteString(":user:")
	b.WriteString(strconv.FormatInt(userID, 10))
	return b.String()
}
```
```bash
go test -bench BenchmarkFormatKeyBuilder -benchmem
BenchmarkFormatKeyBuilder-8   12000000     98 ns/op    32 B/op    1 allocs/op
```

**Why it works / Explanation:** `fmt.Sprintf` has to parse the format string, box its variadic arguments into `[]any`, and dispatch on each argument's dynamic type at runtime. `strconv.Itoa`/`strconv.FormatInt` plus `strings.Builder` skip all of that — direct, type-specific conversions with no format-string parsing and no interface boxing of the arguments, roughly 4x faster and with half the allocations in this benchmark.

**Design principle:** Reserve `fmt.Sprintf` for convenience in cold paths (logging, error messages, CLI output); in hot paths, use direct typed conversions.

---

## 4. Overusing `interface{}`/`any` in Hot Paths

**The Problem:** Storing values behind an interface for dispatch (`[]Shape` calling `.Area()`) means every call goes through the interface's method table at runtime instead of a direct, statically-known call — a cost the compiler cannot inline across, on top of any boxing allocation incurred when the concrete values were stored behind the interface in the first place.

**❌ Bad**
```go
type Shape interface {
	Area() float64
}

type Circle struct{ R float64 }

func (c Circle) Area() float64 { return math.Pi * c.R * c.R }

func totalArea(shapes []Shape) float64 { // BUG (in a hot path): every call is a dynamic dispatch
	var total float64
	for _, s := range shapes {
		total += s.Area() // virtual call through the interface's method table; cannot be inlined
	}
	return total
}
```

**Why it's wrong:**
- Each element of `[]Shape` is an interface value (a type descriptor plus a data pointer); calling `Area()` dispatches through that method table at runtime rather than a direct call, which the compiler cannot inline across.
- If the concrete `Circle` values were boxed onto the heap to be stored behind the interface (common once they escape into a `[]Shape`), the loop also pays pointer-chasing and cache-miss cost on top of the dispatch overhead — in a loop run millions of times per second, this indirection is measurable.

**✅ Good**
```go
type Circle struct{ R float64 }

func (c Circle) Area() float64 { return math.Pi * c.R * c.R }

func totalCircleArea(circles []Circle) float64 { // concrete type: direct, inlinable call
	var total float64
	for _, c := range circles {
		total += c.Area()
	}
	return total
}
```

**Why it works / Explanation:** With a concrete `[]Circle`, each `Area()` call is a direct, statically-known call the compiler can inline, and the values live inline in the slice with no interface header or boxing. If truly heterogeneous shapes must coexist in one collection, the interface-based version is a legitimate, necessary design — the point is not "never use interfaces," it's "don't reach for `[]Shape`/`any` by default in a hot loop when the elements are actually homogeneous," where a concrete or generic (`func Sum[T Areaer](items []T) float64` instantiated per concrete type) slice avoids paying for polymorphism you don't need.

**Design principle:** Reserve dynamic interface dispatch for genuine runtime polymorphism; in hot paths over homogeneous data, concrete types (or generics instantiated over a concrete type) let the compiler keep the call direct and inlinable.

---

## 5. Reflection-Heavy Serialization (`encoding/json`) as a Bottleneck

**The Problem:** `encoding/json`'s `Marshal`/`Unmarshal` walk struct fields and tags via reflection on every call. For most services this cost is irrelevant; for a service marshaling hundreds of thousands of objects per second, it can become a genuine, profiler-confirmed bottleneck — at which point (and only at which point) reaching for a hand-written `MarshalJSON` or a code-generation tool (`easyjson`, `ffjson`) is justified.

**❌ Bad**
```go
type Event struct {
	ID        string         `json:"id"`
	Timestamp time.Time      `json:"timestamp"`
	Payload   map[string]any `json:"payload"`
}

func serializeEvents(events []Event) ([][]byte, error) {
	out := make([][]byte, 0, len(events))
	for _, e := range events {
		b, err := json.Marshal(e) // BUG (only once profiling confirms this is hot): reflection on every call
		if err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	return out, nil
}
```
```bash
go tool pprof http://localhost:6060/debug/pprof/profile?seconds=30
# ~40% of CPU time inside encoding/json's reflect-based struct walking,
# on a service marshaling 500k events/sec.
```

**✅ Good**
```go
// Hand-written MarshalJSON for the hot fixed fields; production code should
// also escape/validate ID for embedded quote characters, omitted here for brevity.
func (e Event) MarshalJSON() ([]byte, error) {
	var b bytes.Buffer
	b.WriteString(`{"id":"`)
	b.WriteString(e.ID)
	b.WriteString(`","timestamp":"`)
	b.WriteString(e.Timestamp.Format(time.RFC3339))
	b.WriteString(`","payload":`)
	payload, err := json.Marshal(e.Payload) // dynamic map: still reflection here, but it's the small part
	if err != nil {
		return nil, err
	}
	b.Write(payload)
	b.WriteByte('}')
	return b.Bytes(), nil
}
```

**Why it works / Explanation:** `encoding/json`'s reflect-based path re-derives struct layout and tag information on every call (mitigated somewhat by internal caching, but still materially slower than direct field writes). Once profiling confirms serialization is a genuine top-N hot spot at production volume, a hand-written `MarshalJSON` — or a code-generation tool that produces the equivalent code at build time — trades source verbosity and a manual-sync maintenance burden for real throughput.

**Design principle:** Pay the cost of reflection avoidance (hand-written or generated marshaling) only where profiling proves the reflection cost is real; default to `encoding/json` everywhere else, since cargo-culting a code generator onto every struct "just in case" adds build complexity and a class of sync bugs for types that were never actually a bottleneck.

---

## 6. Not Preallocating Slices/Maps When the Size Is Known

**The Problem:** `append` on a slice with insufficient capacity reallocates a larger backing array and copies every existing element — repeated growth-and-copy that is entirely avoidable whenever the final (or a worst-case upper-bound) size is knowable ahead of the loop.

**❌ Bad**
```go
func collectActiveUserIDs(users []User) []int {
	var ids []int // BUG: starts at nil/cap 0, must repeatedly grow and copy as it fills
	for _, u := range users {
		if u.Active {
			ids = append(ids, u.ID) // triggers a reallocation at each capacity-doubling boundary
		}
	}
	return ids
}
```
```bash
BenchmarkCollectNoPrealloc-8   10000   118000 ns/op   81920 B/op   14 allocs/op
```

**✅ Good**
```go
func collectActiveUserIDs(users []User) []int {
	ids := make([]int, 0, len(users)) // upper bound known: at most len(users) active users
	for _, u := range users {
		if u.Active {
			ids = append(ids, u.ID)
		}
	}
	return ids
}
```
```bash
BenchmarkCollectPrealloc-8    50000    24000 ns/op    8192 B/op    1 allocs/op
```

**Why it works / Explanation:** Preallocating with a known-or-estimable upper bound (`make([]T, 0, n)`) performs a single allocation upfront, eliminating both the repeated backing-array copies and the extra allocator calls — in this benchmark, roughly 5x fewer allocations and 4x lower latency. The same idea applies to `make(map[K]V, n)`, which sizes the initial bucket layout to avoid incremental rehashing as the map grows.

**Design principle:** When the final or worst-case size is knowable ahead of a loop, tell the allocator up front — it is never wrong to preallocate a reasonable upper bound, only sometimes unnecessary.

---

## 7. False Sharing in Concurrent Code

**The Problem:** When multiple goroutines on different CPU cores frequently write to *different* fields (or array elements) that happen to sit on the same CPU cache line, each write invalidates the other cores' cached copy of that line — cross-core cache-coherency traffic that silently tanks throughput even though each goroutine only ever touches its own, logically independent data.

**❌ Bad**
```go
type Counters struct {
	a int64 // written frequently by goroutine A
	b int64 // written frequently by goroutine B
	c int64 // written frequently by goroutine C
	d int64 // written frequently by goroutine D
}
// BUG: a, b, c, d total only 32 bytes — all four sit on the same 64-byte
// cache line. Even though each goroutine writes only its own field, every
// write invalidates the whole line for the other three cores.

func runWorkers(c *Counters) {
	var wg sync.WaitGroup
	fields := []*int64{&c.a, &c.b, &c.c, &c.d}
	for _, f := range fields {
		wg.Add(1)
		go func(counter *int64) {
			defer wg.Done()
			for i := 0; i < 100_000_000; i++ {
				atomic.AddInt64(counter, 1)
			}
		}(f)
	}
	wg.Wait()
}
```

**✅ Good**
```go
const cacheLinePad = 64 - 8 // pad each counter out to a full 64-byte cache line

type PaddedCounter struct {
	value int64
	_     [cacheLinePad]byte // ensures the next counter starts on a new cache line
}

type Counters struct {
	a PaddedCounter
	b PaddedCounter
	c PaddedCounter
	d PaddedCounter
}

func runWorkers(c *Counters) {
	var wg sync.WaitGroup
	counters := []*int64{&c.a.value, &c.b.value, &c.c.value, &c.d.value}
	for _, counter := range counters {
		wg.Add(1)
		go func(v *int64) {
			defer wg.Done()
			for i := 0; i < 100_000_000; i++ {
				atomic.AddInt64(v, 1)
			}
		}(counter)
	}
	wg.Wait()
}
```

**Why it works / Explanation:** Padding each counter out to the size of a CPU cache line (typically 64 bytes on x86-64) guarantees every goroutine's hot counter lives on its own cache line, so cores writing their own counter no longer invalidate each other's cached copy of a shared line. The fix is invisible in the application logic — no single line "looks wrong" — and only shows up as sublinear throughput scaling across cores in a profile or benchmark.

**Design principle:** When scaling per-core/per-goroutine counters, ensure independently-written hot fields don't share a cache line — padding is the standard fix, and it only matters once benchmarking across core counts reveals the sublinear scaling.

---

## 8. Excessive `defer`/`panic`-`recover` in Extremely Hot Inner Loops

**The Problem:** Modern Go (1.14+) optimizes simple, unconditional defers ("open-coded defers") to be nearly free, so `defer` itself is not automatically expensive — but wrapping *each iteration* of a hot inner loop in its own `defer`/`recover`, rather than scoping recovery to a meaningful unit of work, defeats that optimization and pays real overhead for protection that gains nothing at that granularity.

**❌ Bad**
```go
func sumSquares(values []int) (total int) {
	for _, v := range values {
		func() {
			defer func() { // BUG: a fresh defer + recover for every single element
				if r := recover(); r != nil {
					total += 0
				}
			}()
			total += v * v
		}()
	}
	return total
}
```

**Why it's wrong:**
- Wrapping each loop iteration in its own closure with a `defer`/`recover` pays that machinery's cost — closure allocation, deferred-call bookkeeping — millions of times for work that never panics in practice.
- `recover()` here isn't protecting against anything real; defensive programming applied at a per-element granularity means the overhead swamps the tiny amount of work (one multiply-add) it wraps, and the per-iteration closure also defeats the compiler's open-coded-defer optimization, which applies to simple, non-looped defers in a function's top-level body.

**✅ Good**
```go
func sumSquares(values []int) (total int, err error) {
	defer func() { // one defer/recover for the whole batch, not per element
		if r := recover(); r != nil {
			err = fmt.Errorf("sumSquares: recovered from panic: %v", r)
		}
	}()
	for _, v := range values {
		total += v * v
	}
	return total, nil
}
```

**Why it works / Explanation:** Moving the `defer`/`recover` to the function boundary means it is paid once per call — over potentially millions of elements — instead of once per element, and it sits in a shape (top-level, unconditional) the compiler can open-code efficiently.

**Design principle:** Scope `panic`/`recover` and `defer` to a meaningful unit of work (a request, a batch), not to each iteration of a hot inner loop — and confirm the actual cost with a benchmark rather than assuming `defer` itself is the problem.

---

## 9. Struct Field Ordering Affecting Allocator Size Classes

**The Problem:** Go's allocator serves small objects from a fixed set of size classes (8, 16, 24, 32, 48, ... bytes). A struct's declared field order determines how much alignment padding the compiler inserts, which in turn determines which size class an individual heap allocation of that struct lands in — a struct that's a few bytes over a size-class boundary purely due to avoidable padding wastes far more memory per allocation than its visible fields suggest.

**❌ Bad**
```go
type Task struct {
	Done     bool  // offset 0, 1 byte
	Priority int64 // needs 8-byte alignment: 7 bytes of padding inserted before it
	Retries  bool
	Urgent   bool
	Archived bool
	Blocked  bool
}
// Layout: 1B + 7B pad + 8B + 1B + 1B + 1B + 1B = 20 bytes, rounded up to a
// multiple of 8 -> unsafe.Sizeof(Task{}) == 24, landing in the 24-byte
// allocator size class. BUG: the scattered bools force padding that pushes
// the struct one size class higher than necessary.

func newTasks(n int) []*Task {
	tasks := make([]*Task, n)
	for i := range tasks {
		tasks[i] = &Task{Priority: int64(i)} // each allocation costs a full 24-byte size class
	}
	return tasks
}
```

**Why it's wrong:**
- The four `bool` fields are scattered around the `int64` field, forcing the compiler to insert 7 bytes of alignment padding before `Priority` so it starts on an 8-byte boundary; the struct ends up needing 24 bytes even though it only holds 12 bytes of actual data (`1+8+1+1+1+1` = 13, rounded for alignment).
- Every one of the `n` individual `*Task` allocations pays for that padding — at scale, that's real, avoidable heap growth purely from field order, with zero change to the data the struct represents.

**✅ Good**
```go
type Task struct {
	Done     bool // group all 1-byte fields together...
	Retries  bool
	Urgent   bool
	Archived bool
	Blocked  bool
	Priority int64 // ...5 bytes, then 3 bytes of padding to align this at offset 8
}
// Layout: 5B + 3B pad + 8B = 16 bytes -> unsafe.Sizeof(Task{}) == 16,
// fitting Go's 16-byte size class instead of 24 — a 33% reduction per
// allocation with no change to the struct's data.

func newTasks(n int) []*Task {
	tasks := make([]*Task, n)
	for i := range tasks {
		tasks[i] = &Task{Priority: int64(i)}
	}
	return tasks
}
```

**Why it works / Explanation:** Grouping same-alignment fields (all four 1-byte bools) together minimizes the alignment padding the compiler must insert before the 8-byte `Priority` field, dropping the struct's total size from 20 (rounded to the 24-byte class) to 16 bytes (exactly the 16-byte class) — an 8-byte, 33% savings per allocation purely from reordering, which compounds across every one of the `n` allocations in `newTasks`.

**Design principle:** Field order is free to change and never affects correctness (absent `unsafe`/cgo layout assumptions) — default to grouping fields by size/alignment to minimize padding and land in the smallest allocator size class.

---

## 10. Reduce Allocations Before Reducing CPU Cycles

**The Problem:** It's tempting to micro-optimize arithmetic or branches before addressing allocation-heavy code, but on a garbage-collected runtime an allocation's true cost is the allocation itself *plus* its share of future GC work (marking and scanning every live pointer on every subsequent collection cycle until it's freed). A "slightly slower"-looking allocation-free version frequently beats a "faster-looking" version that allocates, once sustained-load GC overhead is included.

**❌ Bad**
```go
func buildSummaries(orders []Order) []string {
	summaries := make([]any, 0, len(orders)) // BUG: any-typed slice boxes every summary
	for _, o := range orders {
		summaries = append(summaries, fmt.Sprintf("order %d: $%.2f", o.ID, o.Total)) // allocates per order
	}
	result := make([]string, len(summaries))
	for i, s := range summaries {
		result[i] = s.(string) // unboxing: a pointless round trip through `any`
	}
	return result
}
```

**Why it's wrong:**
- Two full passes over `orders`, an unnecessary `any`-boxing round trip that buys nothing (the values are always strings, never anything else), and `fmt.Sprintf` per order — each of these adds allocations that cost time to create *and* add live objects the GC must scan on every subsequent collection until they're freed.
- Under sustained request load, the aggregate GC overhead from this allocation pattern (extra passes, boxed slice, formatted strings) frequently costs more than the actual string-building CPU work itself.

**✅ Good**
```go
func buildSummaries(orders []Order) []string {
	result := make([]string, 0, len(orders)) // single preallocated, correctly-typed slice, no boxing
	for _, o := range orders {
		var b strings.Builder
		b.WriteString("order ")
		b.WriteString(strconv.Itoa(o.ID))
		b.WriteString(": $")
		b.WriteString(strconv.FormatFloat(o.Total, 'f', 2, 64))
		result = append(result, b.String())
	}
	return result
}
```

**Why it works / Explanation:** A single preallocated, correctly-typed slice eliminates the second pass and the `any`-boxing round trip entirely; replacing `fmt.Sprintf` with direct `strconv` calls (see gotcha #3 above) removes the reflection-adjacent formatting overhead. The combined effect is fewer allocations overall — cheaper right now (less allocator work) and cheaper later (less live and garbage memory for every future GC cycle to walk).

**Design principle:** When both a CPU-time fix and an allocation-count fix are available, cut allocations first — GC cost compounds across every future collection cycle, while CPU-cycle savings on already-cheap code rarely move the needle. Then re-profile before optimizing further.

---

## Key Takeaways
- Profile before optimizing anything — every pattern below is something to recognize after a profile points at it, not a checklist to apply blindly.
- `string`/`[]byte` conversions copy in the general case — stay in one representation through a hot path, and know the narrow compiler-recognized zero-copy exceptions.
- `fmt.Sprintf` costs real overhead versus `strconv`/`strings.Builder` for simple formatting — reserve it for cold paths.
- Dynamic interface dispatch (`any`/`interface{}`) blocks inlining and adds call overhead versus concrete types or generics over homogeneous data.
- Reflection-based `encoding/json` can be a genuine bottleneck at high volume — reach for hand-written/generated marshaling only after profiling proves it.
- Preallocate slices and maps (`make(..., 0, n)`) whenever the final or worst-case size is known, to avoid repeated growth-and-copy.
- False sharing — independent goroutines writing adjacent fields on the same cache line — silently tanks concurrent throughput; pad hot per-goroutine counters.
- Scope `defer`/`panic`/`recover` to a meaningful unit of work, not to each iteration of a hot inner loop.
- Struct field order changes allocator size-class fit (e.g., 24 bytes vs. 16) with zero effect on correctness — group same-alignment fields to minimize padding.
- Reduce allocation count before chasing CPU cycles — allocation cost includes both the allocation and all future GC scan/collection work it causes.

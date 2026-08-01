# Pointers and Memory

Go hides pointers and memory management behind a friendly, garbage-collected surface, but the underlying mechanics — escape analysis, the heap/stack boundary, and the memory model's rules around concurrent access — still leak through in ways that affect correctness and performance. Misunderstanding them leads to classic bugs like closures that all read the same loop variable, data races on "just a pointer assignment," avoidable allocations that add up to serious GC pressure, or memory that can't be reclaimed because something innocuous is still holding a reference. This file covers the pointer- and memory-related gotchas most likely to bite you in a production Go service.

## 1. Escape analysis basics

**The Problem:** The compiler's escape analysis decides, per variable, whether it can live on the stack (cheap, no GC involvement) or must be allocated on the heap (more expensive, tracked by the garbage collector). A variable escapes whenever its address outlives the function that created it — for example, by being stored somewhere long-lived or boxed into an interface. This is often accidental: a seemingly innocent line of code can force an allocation that wasn't obviously necessary.

**❌ Bad**
```go
package main

import "fmt"

type Metric struct {
	Name  string
	Value float64
}

func (m Metric) String() string {
	return fmt.Sprintf("%s=%.2f", m.Name, m.Value)
}

var sink []fmt.Stringer // package-level - anything stored here must outlive this function

func recordBad() {
	for i := 0; i < 1_000_000; i++ {
		m := Metric{Name: "cpu", Value: float64(i)} // BUG: boxed into an interface and stored globally
		sink = append(sink, m)                       // -> escapes to the heap on every iteration
	}
}

func main() {
	recordBad()
	fmt.Println(len(sink))
}
```

**Why it's wrong:**
- Storing `m` into the package-level `sink` (as an interface value) forces the compiler to heap-allocate a fresh `Metric` on every loop iteration, since the compiler can't prove the value's lifetime ends with the function.
- A million heap allocations in a tight loop translates directly into GC pressure — more frequent collections, more CPU spent on GC rather than useful work, and a real, measurable throughput cliff under load.

**✅ Good**
```go
package main

import "fmt"

type Metric struct {
	Name  string
	Value float64
}

func recordGood() float64 {
	total := 0.0
	for i := 0; i < 1_000_000; i++ {
		m := Metric{Name: "cpu", Value: float64(i)} // never leaves this function or takes its address
		total += m.Value                             // -> stays on the stack, no heap allocation
	}
	return total
}

func main() {
	fmt.Println(recordGood())
}
```

**Why it works / Explanation:** Because `m` is only ever read within the loop body and never escapes the function (no address taken, no interface boxing, no storage in a longer-lived structure), the compiler keeps it on the stack — often it doesn't need to allocate storage for it at all across iterations. Running `go build -gcflags="-m" .` prints the compiler's actual escape decisions (`m escapes to heap` vs. `m does not escape`), which is the fastest way to confirm this without guessing.

**Design principle:** Minimize what a hot-path value is exposed to — avoid storing it in long-lived structures or boxing it into an interface unless genuinely necessary — and use `-gcflags="-m"` to verify, rather than assume, where allocations happen.

---

## 2. Returning a pointer to a local variable is safe

**The Problem:** Developers coming from C/C++ sometimes avoid returning `&localVar` out of habit, fearing a dangling pointer once the function returns. In Go this fear is misplaced: escape analysis detects that the address outlives the function and automatically promotes the variable to the heap, so the pointer is always valid. The real cost isn't safety — it's the heap allocation and GC bookkeeping that comes with it.

**❌ Bad**
```go
package main

import "fmt"

type Config struct {
	Timeout int
}

func fillConfig(out *Config, timeout int) { // BUG: awkward out-param, motivated by a misplaced fear of returning &local
	out.Timeout = timeout
}

func main() {
	var c Config
	fillConfig(&c, 30)
	fmt.Println(c.Timeout)
}
```

**Why it's wrong:**
- There is no actual memory bug being avoided here — the workaround exists only because of a misunderstanding carried over from languages without escape analysis.
- The API is clunkier than necessary: callers must pre-declare a zero value and remember to pass its address, instead of just receiving a ready-to-use result.

**✅ Good**
```go
package main

import "fmt"

type Config struct {
	Timeout int
}

func newConfig(timeout int) *Config {
	c := Config{Timeout: timeout} // local variable
	return &c                     // safe and idiomatic: escape analysis promotes c to the heap automatically
}

func main() {
	c := newConfig(30)
	fmt.Println(c.Timeout)
}
```

**Why it works / Explanation:** The Go compiler proves that `&c`'s lifetime extends past `newConfig`'s return, so it allocates `c` on the heap instead of the stack — there is no dangling pointer, ever. The only real tradeoff is that this specific allocation now costs a heap allocation instead of a stack slot; for a single small `Config`, that cost is negligible, and returning by value is only worth considering if profiling shows this allocation actually matters.

**Design principle:** Trust escape analysis for correctness — it's not a bug class in Go — and choose between pointer and value returns based on measured allocation cost, not inherited fear from other languages.

---

## 3. Pointer to loop variable captured by a closure

**The Problem:** Before Go 1.22, a `for` loop reused the same variable across all iterations, so a closure (typically launched as a goroutine) that referenced the loop variable captured its address, not a snapshot of its value. By the time the goroutines actually ran, the loop had usually already finished, leaving every closure reading the loop variable's final value.

**❌ Bad**
```go
package main

import (
	"fmt"
	"sync"
)

func main() {
	var wg sync.WaitGroup
	nums := []int{1, 2, 3}

	for _, n := range nums {
		wg.Add(1)
		go func() {
			defer wg.Done()
			fmt.Println(n) // BUG (pre-Go 1.22): every goroutine shares the same loop variable n
		}()
	}
	wg.Wait()
	// pre-1.22 output is typically "3 3 3" in some order, not "1 2 3"
}
```

**Why it's wrong:**
- All three goroutines close over the same variable `n`; by the time any of them runs, the loop has very likely already advanced `n` to its last value (or finished entirely), so all three print the same number instead of each printing its own.
- This class of bug is nondeterministic and timing-dependent — it can pass a quick manual test and still fail intermittently in production once goroutines are scheduled differently under load.

**✅ Good**
```go
package main

import (
	"fmt"
	"sync"
)

func main() {
	var wg sync.WaitGroup
	nums := []int{1, 2, 3}

	for _, n := range nums {
		n := n // shadow: each iteration now has its own copy of n
		wg.Add(1)
		go func() {
			defer wg.Done()
			fmt.Println(n) // prints 1, 2, 3 in some order
		}()
	}
	wg.Wait()
}
```

**Why it works / Explanation:** `n := n` declares a new variable scoped to that single iteration, so each goroutine's closure captures its own independent copy instead of the shared loop variable. As of Go 1.22, the language itself changed `for` loop semantics so each iteration gets its own copy of `n` automatically, making the original "bad" snippet safe by default on modern Go — but understanding the old behavior still matters when reading pre-1.22 code, third-party libraries pinned to older Go versions, or similar closure-capture pitfalls in other languages (e.g. `var` vs. `let` in JavaScript).

**Design principle:** When a closure captures a loop variable and outlives a single iteration (goroutines, deferred functions, stored callbacks), make the per-iteration copy explicit — don't rely solely on which Go version you happen to be compiling with.

---

## 4. Unnecessary pointer indirection for small, immutable structs

**The Problem:** Passing a pointer to a small struct out of habit — "pointers are more efficient" — often backfires. A pointer forces a heap allocation (since its address is taken) and an extra indirection/cache miss on every access, whereas a small struct passed by value can be copied for free in registers or on the stack with no allocation at all.

**❌ Bad**
```go
package main

import (
	"fmt"
	"math"
)

type Point struct{ X, Y int }

func distance(a, b *Point) float64 { // BUG: forces a heap allocation just to pass 16 bytes around
	dx := a.X - b.X
	dy := a.Y - b.Y
	return math.Sqrt(float64(dx*dx + dy*dy))
}

func main() {
	p1, p2 := &Point{1, 2}, &Point{4, 6}
	fmt.Println(distance(p1, p2))
}
```

**Why it's wrong:**
- Taking `&Point{...}` forces the compiler to consider heap allocation for each `Point`, purely so `distance` can receive a pointer it never needs to mutate through.
- Every field access inside `distance` now goes through a pointer dereference instead of operating on data already sitting in registers or on the stack, adding avoidable cache-miss potential in hot paths.

**✅ Good**
```go
package main

import (
	"fmt"
	"math"
)

type Point struct{ X, Y int }

func distance(a, b Point) float64 { // 16 bytes copied on the stack, no heap allocation, no indirection
	dx := a.X - b.X
	dy := a.Y - b.Y
	return math.Sqrt(float64(dx*dx + dy*dy))
}

func main() {
	p1, p2 := Point{1, 2}, Point{4, 6}
	fmt.Println(distance(p1, p2))
}
```

**Why it works / Explanation:** `Point` is two `int`s — 16 bytes on most platforms — cheap enough to copy that passing it by value is at least as fast as passing a pointer, without any of the allocation or aliasing concerns. There's also no risk of `distance` accidentally mutating the caller's data, since it only ever sees a copy.

**Design principle:** Reserve pointers for when you need to mutate the caller's data, share genuinely large structures, or represent optional/nil values — for small, immutable data, pass by value.

---

## 5. Held references preventing garbage collection

**The Problem:** A slice re-slicing operation (`data[:n]`) doesn't copy — the result still shares the same underlying backing array as the original. If that smaller slice is kept alive (e.g. stored in a long-lived cache) while the original, much larger slice is discarded everywhere else, the entire backing array stays reachable and un-collectible for as long as the small slice lives.

**❌ Bad**
```go
var cache = map[string][]byte{}

func extractID(data []byte) []byte {
	// data is a 10MB buffer read from disk; we only want the first 8 bytes
	return data[:8] // BUG: the returned slice still points into the full 10MB backing array
}

func remember(key string, data []byte) {
	cache[key] = extractID(data) // the entire 10MB buffer stays reachable through this one slice
}
```

**Why it's wrong:**
- `data[:8]` shares the exact same backing array as `data` — the garbage collector sees the 8-byte slice referencing all 10MB and keeps the whole thing alive, even though 9,999,992 bytes of it are never touched again.
- Because `cache` is long-lived (and grows with every call to `remember`), this quietly accumulates megabytes of unreachable-in-spirit-but-technically-reachable memory, showing up as a slow, hard-to-diagnose memory leak rather than an obvious bug.

**✅ Good**
```go
var cache = map[string][]byte{}

func extractID(data []byte) []byte {
	id := make([]byte, 8)
	copy(id, data[:8]) // independent backing array - the original 10MB buffer can now be collected
	return id
}

func remember(key string, data []byte) {
	cache[key] = extractID(data)
}
```

**Why it works / Explanation:** Allocating a fresh, small backing array and copying just the needed bytes into it breaks the reference to the original 10MB buffer entirely. Once nothing else refers to that original buffer, the garbage collector is free to reclaim all of it, leaving only the 8 bytes actually needed in the cache.

**Design principle:** Be deliberate about what a long-lived reference actually keeps alive — when re-slicing a large buffer for long-term storage, copy the small piece you need instead of slicing in place.

---

## 6. `unsafe.Pointer` misuse

**The Problem:** `unsafe.Pointer` lets you bypass Go's type system entirely, which is occasionally necessary for genuine low-level work but is easy to misuse: converting between incompatible pointer types relies on layout assumptions the compiler no longer checks, and converting a pointer to `uintptr` produces a plain integer the garbage collector no longer tracks — holding onto that integer across a call that can trigger a GC or stack growth risks it referring to memory that has since moved or been reclaimed.

**❌ Bad**
```go
import (
	"runtime"
	"unsafe"
)

func floatBits(f float64) uint64 {
	p := unsafe.Pointer(&f)
	return *(*uint64)(p) // BUG: reinterprets memory directly, bypassing the type system
}

func addressAcrossGC(s []byte) uintptr {
	p := unsafe.Pointer(&s[0])
	addr := uintptr(p) // BUG: a uintptr is just an integer - the GC no longer tracks what it points to
	runtime.GC()        // s's backing array could be moved or collected before addr is used again
	return addr
}
```

**Why it's wrong:**
- `floatBits` works today only because it assumes `float64` and `uint64` have identical size and bit layout on the current platform/compiler — a correctness assumption the type system would otherwise have verified for you, and one the standard library already handles safely.
- `addressAcrossGC` violates the documented `unsafe.Pointer` rules: converting to `uintptr` and holding it across a potential garbage-collection point (any call, in general) is explicitly unsafe, because nothing keeps the referenced memory alive or fixed in place while it's just an integer.

**✅ Good**
```go
import (
	"math"
	"unsafe"
)

func floatBits(f float64) uint64 {
	return math.Float64bits(f) // safe, correct, and doesn't touch unsafe.Pointer at all
}

func bytePointer(s []byte) unsafe.Pointer {
	return unsafe.Pointer(&s[0]) // kept as unsafe.Pointer, not uintptr, so the GC keeps tracking it
}
```

**Why it works / Explanation:** `math.Float64bits` does the same bit-reinterpretation the standard library has already verified is correct and portable, with no `unsafe` in the calling code at all. Where a raw address genuinely must be retained, keeping it as `unsafe.Pointer` (not `uintptr`) ensures the garbage collector continues to treat it as a live reference to its target.

**Design principle:** Treat `unsafe.Pointer` as a last resort for narrow, carefully reviewed low-level code — never reach for it as a shortcut around the type system, and never let a pointer's identity degrade to a bare `uintptr` across anything that might trigger garbage collection.

---

## 7. Mutating through a value receiver

**The Problem:** A method with a value receiver operates on a copy of the struct — any mutation inside the method changes that copy and is discarded the moment the method returns, leaving the caller's original value untouched. This is a frequent source of confusion when a type inconsistently mixes pointer- and value-receiver methods.

**❌ Bad**
```go
package main

import "fmt"

type Counter struct {
	count int
}

func (c Counter) Inc() { // value receiver - operates on a copy of Counter
	c.count++ // BUG: mutates the copy; the caller's Counter is untouched
}

func main() {
	c := Counter{}
	c.Inc()
	c.Inc()
	fmt.Println(c.count) // BUG: prints 0, not 2
}
```

**Why it's wrong:**
- `c.Inc()` compiles and runs without any error — there's no panic, just a value that silently never changes, which is far harder to notice than a crash.
- This is exactly the kind of bug that survives code review, because the call site (`c.Inc()`) looks identical whether `Inc` has a value or pointer receiver — you have to check the method declaration to know which one it is.

**✅ Good**
```go
package main

import "fmt"

type Counter struct {
	count int
}

func (c *Counter) Inc() { // pointer receiver - operates on the original
	c.count++
}

func main() {
	c := Counter{}
	c.Inc()
	c.Inc()
	fmt.Println(c.count) // prints 2
}
```

**Why it works / Explanation:** With a pointer receiver, `c.Inc()` (Go automatically takes `&c` for you here) operates directly on the caller's `Counter`, so the increment persists. Any method that needs to mutate the receiver's fields must use a pointer receiver — there is no other way to make the change visible to the caller.

**Design principle:** If a method mutates state, it must use a pointer receiver — and per the receiver-consistency principle from the structs file, every other method on that type should too, to avoid this exact confusion.

---

## 8. Atomicity and visibility issues in concurrent code

**The Problem:** Reading and writing a plain pointer field from multiple goroutines without synchronization is a data race, even though a pointer assignment "feels" like a single, atomic operation. The Go memory model gives no guarantee about when (or whether) one goroutine's write becomes visible to another goroutine's read without an explicit synchronization point — and the race detector will flag it even if it happens to work in casual testing.

**❌ Bad**
```go
package main

import (
	"fmt"
	"math/rand"
	"time"
)

type Config struct {
	Timeout int
}

var current *Config // BUG: plain pointer, read and written from multiple goroutines with no synchronization

func updater() {
	for {
		current = &Config{Timeout: rand.Intn(100)} // plain write - races with any concurrent read
		time.Sleep(time.Second)
	}
}

func reader() {
	for {
		c := current // plain read - data race; `go run -race` flags this immediately
		if c != nil {
			fmt.Println(c.Timeout)
		}
	}
}

func main() {
	go updater()
	go reader()
	time.Sleep(3 * time.Second)
}
```

**Why it's wrong:**
- Without synchronization, the Go memory model does not guarantee `reader` ever observes `updater`'s writes at all, or observes them in a consistent order — this is a genuine data race, not just a theoretical concern, and `go run -race` reports it as one.
- On some architectures and under some compiler optimizations, this can also produce subtly corrupted reads (e.g. observing a half-initialized `*Config` if the compiler or CPU reorders operations), not merely "stale but valid" data.

**✅ Good**
```go
package main

import (
	"fmt"
	"math/rand"
	"sync/atomic"
	"time"
)

type Config struct {
	Timeout int
}

var current atomic.Pointer[Config]

func updater() {
	for {
		current.Store(&Config{Timeout: rand.Intn(100)}) // synchronized write
		time.Sleep(time.Second)
	}
}

func reader() {
	for {
		c := current.Load() // synchronized read - always sees a complete, consistent value
		if c != nil {
			fmt.Println(c.Timeout)
		}
	}
}

func main() {
	go updater()
	go reader()
	time.Sleep(3 * time.Second)
}
```

**Why it works / Explanation:** `atomic.Pointer[Config]` (available since Go 1.19) provides `Store`/`Load` operations with well-defined memory-model guarantees — every `Load` sees a complete, previously-`Store`d value, with no partial writes and no race. It replaces the plain pointer field with a type that's built specifically for exactly this cross-goroutine sharing pattern.

**Design principle:** Never share mutable pointer state across goroutines through a plain field — use `atomic.Pointer[T]`, a mutex, or channel-based handoff, and let `go run -race` (or `go test -race`) verify the fix rather than trusting that it "looks fine" under light testing.

---

## 9. False sharing from struct field layout in concurrent code

**The Problem:** CPU caches operate on fixed-size lines (typically 64 bytes). When two fields written by different goroutines happen to land on the same cache line, every write from one goroutine invalidates the other core's cached copy of that line — even though the goroutines never touch each other's field. The result is a severe, purely layout-driven performance cliff with no logical data race involved.

**❌ Bad**
```go
package main

import (
	"sync"
	"sync/atomic"
)

type Counters struct {
	a int64 // written by goroutine A
	b int64 // written by goroutine B - BUG: sits on the same CPU cache line as a
}

func main() {
	c := &Counters{}
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		for i := 0; i < 100_000_000; i++ {
			atomic.AddInt64(&c.a, 1)
		}
	}()
	go func() {
		defer wg.Done()
		for i := 0; i < 100_000_000; i++ {
			atomic.AddInt64(&c.b, 1)
		}
	}()
	wg.Wait()
	// every write to a invalidates the cache line holding b on the other core, and vice versa
}
```

**Why it's wrong:**
- `a` and `b` are logically independent (no data race, correct final values), but because they're adjacent 8-byte fields they almost certainly share a 64-byte cache line, so the two goroutines fight over that cache line on every single increment.
- This shows up purely as a throughput problem under concurrent load — it passes every correctness test and the race detector cleanly, so it's easy to miss until someone profiles multi-core scaling and finds it far below expectations.

**✅ Good**
```go
package main

import (
	"sync"
	"sync/atomic"
)

type Counters struct {
	a int64
	_ [56]byte // padding: pushes b onto its own 64-byte cache line
	b int64
}

func main() {
	c := &Counters{}
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		for i := 0; i < 100_000_000; i++ {
			atomic.AddInt64(&c.a, 1)
		}
	}()
	go func() {
		defer wg.Done()
		for i := 0; i < 100_000_000; i++ {
			atomic.AddInt64(&c.b, 1)
		}
	}()
	wg.Wait()
	// a and b now live on separate cache lines - no more cross-core invalidation traffic
}
```

**Why it works / Explanation:** The explicit `[56]byte` padding field pushes `b` onto a different 64-byte cache line than `a` (8 bytes for `a` + 56 bytes padding = 64 bytes before `b` starts), so each core's cache can hold its own line without the other core's writes constantly invalidating it.

**Design principle:** For structs whose fields are hammered concurrently by different goroutines, consider cache-line padding to prevent false sharing — a purely mechanical, layout-level fix for a purely mechanical, layout-level performance problem.

---

## Key Takeaways
- A variable escapes to the heap when its address outlives its function (returned, stored globally, boxed into an interface) — use `-gcflags="-m"` to confirm, and minimize unnecessary escapes on hot paths.
- Returning `&localVar` is safe and idiomatic in Go thanks to escape analysis; the only real cost is a heap allocation, not a correctness risk.
- Pre-Go 1.22, closures capturing a loop variable all see its final value — shadow with `n := n` per iteration, or rely on Go 1.22+'s per-iteration loop variables.
- Passing pointers to small, immutable structs out of habit adds a heap allocation and indirection for no benefit — pass small values directly.
- A small slice sharing a backing array with a much larger buffer keeps the entire buffer alive — copy out just what you need before storing it long-term.
- `unsafe.Pointer` bypasses the type system and the GC's tracking guarantees — use it only for narrow, reviewed cases, and never let a pointer degrade to a bare `uintptr` across a potential GC point.
- A value-receiver method mutates only its own copy — mutating methods must use pointer receivers.
- Sharing a plain pointer across goroutines without synchronization is a data race regardless of how atomic it feels — use `atomic.Pointer[T]` or another synchronization primitive.
- Adjacent fields written by different goroutines can suffer false sharing on the same CPU cache line — pad struct layout to separate them when profiling shows contention.

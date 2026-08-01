# Arrays in Go: Production Pitfalls

Go's fixed-size arrays are used far less often than slices, but they show up in exactly the places where getting them wrong is expensive: protocol headers, hashes, cryptographic digests, and anywhere a fixed-size value type is cheaper than a heap-allocated slice. Their value semantics — full-copy on assignment, comparability with `==`, and length being part of the type — are the opposite of slices' reference-like behavior, and mixing up the two mental models is the root of most array bugs in production code.

## 1. Arrays are value types — full copies on assignment and call

**The Problem:** Unlike a slice (a small header pointing at shared backing memory), an array *is* its data. Assigning an array, or passing one to a function, copies every element. This is both a correctness trap (mutations don't propagate) and a performance trap (large arrays are expensive to copy).

**❌ Bad**
```go
// import "fmt"

type Board [3][3]int

func mutate(b Board) {
	b[0][0] = 9 // BUG: mutates the local copy only
}

func main() {
	original := Board{}
	mutate(original)
	fmt.Println(original[0][0]) // 0 -- caller's board is untouched
}
```

**Why it's wrong:**
- `mutate` receives a full copy of `original` (all 9 ints), so writing to `b[0][0]` has zero effect on the caller's value — a silent no-op bug that's easy to miss because the code compiles and runs without error.
- For large arrays (e.g., `[100000]float64`), passing by value copies that entire block on every call and every assignment — a real, easy-to-overlook performance cliff, especially in recursive functions or hot loops.

**✅ Good**
```go
func mutate(b *Board) {
	b[0][0] = 9 // indexing through a pointer-to-array auto-dereferences
}

func main() {
	original := Board{}
	mutate(&original)
	fmt.Println(original[0][0]) // 9 -- correctly mutated
}
```

**Why it works / Explanation:** Passing `*Board` instead of `Board` avoids the copy entirely — the function operates on the caller's actual memory. This is the array equivalent of passing a slice or a pointer to a struct: if a function needs to observe or make mutations, or if the array is large enough that copying is itself expensive, pass a pointer.

**Design principle:** Value vs. reference semantics — arrays are values; make that explicit with a pointer when mutation or copy-avoidance is the intent.

---

## 2. Array length is part of the type

**The Problem:** `[5]int` and `[10]int` are different, incompatible types — not "the same array type with different lengths." You cannot write one ordinary function that accepts both, and passing the wrong size is a compile error, not a runtime one (which is good, but still surprises people expecting slice-like flexibility).

**❌ Bad**
```go
func sum5(v [5]float64) float64 {
	total := 0.0
	for _, x := range v {
		total += x
	}
	return total
}

func sum10(v [10]float64) float64 { // BUG: near-duplicate function, only because the type differs
	total := 0.0
	for _, x := range v {
		total += x
	}
	return total
}
```

**Why it's wrong:**
- `sum5(someArrayOfLen10)` is a compile error — `[5]float64` and `[10]float64` are unrelated types despite having the same element type. This forces either duplicated functions per size (as above) or awkward workarounds, and as of Go 1.22 the language still does not let you parameterize a generic function directly over an array's *length*.

**✅ Good**
```go
// import "fmt"

func sum[T int | float64](xs []T) T {
	var total T
	for _, x := range xs {
		total += x
	}
	return total
}

func main() {
	v5 := [5]float64{1, 2, 3, 4, 5}
	v10 := [10]float64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
	fmt.Println(sum(v5[:]), sum(v10[:])) // one function handles both sizes, via slicing
}
```

**Why it works / Explanation:** Go generics let you parameterize over element type, not over array length. The practical fix is to write the function against a slice (`[]T`) and convert any array to a slice at the call site with `arr[:]` — one function then works for arrays of any length, plus real slices.

**Design principle:** Prefer the more general type at API boundaries — accept `[]T`, and let callers slice their fixed-size arrays, rather than baking a specific size into a function's signature.

---

## 3. Arrays ARE comparable with `==`

**The Problem:** Unlike slices, arrays (with comparable element types) support `==` and `!=` directly, and can be used as map keys or struct fields compared for equality. This is a genuine, useful capability — the pitfall is *not* knowing about it and reaching for `reflect.DeepEqual`-style helpers where a plain `==` (or a map lookup) would do.

**❌ Bad**
```go
// import ( "crypto/sha256"; "reflect" )

func isDuplicateSlow(data []byte, seen [][32]byte) bool {
	sum := sha256.Sum256(data) // returns [32]byte
	for _, s := range seen {
		if reflect.DeepEqual(s, sum) { // BUG: needlessly slow and roundabout
			return true
		}
	}
	return false
}
```

**Why it's wrong:**
- `reflect.DeepEqual` (or a manual byte-by-byte loop) works, but it's slower than the built-in comparison and — worse — it makes it hard to use the checksum as a map key, which is the natural, O(1) way to do duplicate detection, turning what should be O(1) per check into O(n).

**✅ Good**
```go
// import "crypto/sha256"

type Checksum = [32]byte

func isDuplicate(seen map[Checksum]bool, data []byte) bool {
	sum := sha256.Sum256(data) // [32]byte -- directly usable as a map key
	if seen[sum] {
		return true
	}
	seen[sum] = true
	return false
}
```

**Why it works / Explanation:** `[32]byte` is a comparable type (fixed size, comparable element type), so it can be used directly as a map key or compared with `==` — an O(1) lookup replaces an O(n) scan, and the code reads more directly. This is exactly why standard-library hash functions like `sha256.Sum256` return arrays, not slices — slices can't do this.

**Design principle:** Use the right tool for equality — fixed-size, comparable values (checksums, coordinates, fixed IDs) are a natural fit for array types precisely because slices aren't comparable.

---

## 4. Array-to-slice conversion pitfalls

**The Problem:** Slicing an array (`arr[:]`) produces a slice backed by that array — but *which* array depends entirely on whether `arr` is the original variable, a pointer to it, or a value copy (e.g., a function parameter). Slicing a copy gives you a slice into the copy, not the original.

**❌ Bad**
```go
// import "fmt"

func zeroOut(arr [5]int) []int {
	s := arr[:] // slices the local copy `arr`, NOT the caller's array
	for i := range s {
		s[i] = 0
	}
	return s
}

func main() {
	original := [5]int{1, 2, 3, 4, 5}
	zeroOut(original)
	fmt.Println(original) // [1 2 3 4 5] -- BUG: caller's array is untouched
}
```

**Why it's wrong:**
- `arr` inside `zeroOut` is a full copy (per gotcha #1), so `arr[:]` slices that copy. Every mutation through `s` affects only memory nobody outside the function can see — the function silently does nothing useful for the caller, despite compiling and running cleanly.

**✅ Good**
```go
func zeroOut(arr *[5]int) {
	s := arr[:]         // slicing through a pointer-to-array dereferences it automatically;
	for i := range s {  // the slice is backed by the ORIGINAL array
		s[i] = 0
	}
}

func main() {
	original := [5]int{1, 2, 3, 4, 5}
	zeroOut(&original)
	fmt.Println(original) // [0 0 0 0 0] -- correctly mutated
}
```

**Why it works / Explanation:** `arr[:]` where `arr` is `*[5]int` is shorthand for `(*arr)[:]` — it slices through the pointer into the original array, not a copy. Whether a slicing operation reaches the "real" data or a throwaway copy depends entirely on whether you slice a value parameter or a pointer parameter.

**Design principle:** Be explicit about aliasing intent — if a function is meant to mutate a caller's array, its signature should take a pointer, not a value.

---

## 5. Ranging over an array value copies the whole array

**The Problem:** `for i, v := range arr` where `arr` is an array (not a slice, not a pointer) evaluates `arr` once at the start of the loop — and because arrays are values, that evaluation makes a full copy for the duration of the iteration. Mutating the "original" array variable mid-loop has no effect on what the loop sees.

**❌ Bad**
```go
// import "fmt"

func main() {
	data := [3]int{1, 2, 3}
	for i, v := range data {
		if i == 0 {
			data[1] = 999 // BUG: mutating `data` mid-loop...
		}
		fmt.Println(i, v) // ...has no effect on `v` here
	}
	// Prints 0 1 / 1 2 / 2 3 -- the loop iterated over a SNAPSHOT taken before it started,
	// not the live, mutated `data`.
}
```

**Why it's wrong:**
- Developers coming from slices expect `range` to always observe live backing memory — that's true for slices (which share the backing array) but not for a bare array value, which is copied once up front. Code that mutates the array it's currently ranging over (expecting the change to be visible later in the same loop) silently doesn't see its own update.

**✅ Good**
```go
func main() {
	data := [3]int{1, 2, 3}
	for i := range data { // range over the array still copies once, but...
		if i == 0 {
			data[1] = 999 // ...indexing `data` directly always reads the live value
		}
		fmt.Println(i, data[i]) // Prints 0 1 / 1 999 / 2 3
	}

	// Or range over a pointer to the array to avoid the copy entirely:
	for i, v := range &data {
		fmt.Println(i, v)
	}
}
```

**Why it works / Explanation:** Indexing `data[i]` inside the loop always reads current, live memory, sidestepping the snapshot entirely. Ranging over `&data` (a pointer to the array) avoids making the copy in the first place — Go dereferences the pointer per access instead of copying the whole array up front, which also matters for large arrays where the copy itself would be costly.

**Design principle:** Know what `range` evaluates once — for arrays, that "once" includes a full-value copy, not just an index bound.

---

## 6. Converting a slice to an array (or array pointer) can panic

**The Problem:** Go 1.17 added conversions from a slice to a pointer-to-array (`(*[N]T)(s)`), and Go 1.20 added direct slice-to-array value conversion (`[N]T(s)`). Both are extremely convenient for parsing fixed-size fields out of a byte buffer — and both **panic at runtime** if the slice is shorter than `N`.

**❌ Bad**
```go
// import "fmt"

func firstFour(b []byte) [4]byte {
	return [4]byte(b) // BUG: panics if len(b) < 4
}

func main() {
	buf := []byte{1, 2, 3} // only 3 bytes -- e.g., a truncated network read
	fmt.Println(firstFour(buf))
	// panic: runtime error: cannot convert slice with length 3 to array or pointer to array with length 4
}
```

**Why it's wrong:**
- This looks like a harmless type conversion (similar to `int64(someInt32)`), but it's a length-checked operation that panics — a crash-causing landmine when parsing untrusted or truncated input (partial network reads, malformed files) instead of a graceful error.

**✅ Good**
```go
func firstFour(b []byte) ([4]byte, error) {
	if len(b) < 4 {
		return [4]byte{}, fmt.Errorf("need at least 4 bytes, got %d", len(b))
	}
	return [4]byte(b), nil // safe: length already checked
}
```

**Why it works / Explanation:** Checking `len(b)` before converting turns a potential panic into a normal, recoverable error — appropriate for any conversion whose input length isn't already guaranteed by the caller.

**Design principle:** Validate before you convert — treat length-checked conversions as fallible operations whenever the input size isn't already guaranteed.

---

## 7. Large local arrays: zero-init cost and escape-to-heap surprises

**The Problem:** A local array declaration like `var buf [65536]byte` is zero-initialized by the runtime every time it's declared, and "just a local variable" is not necessarily stack memory — if the compiler's escape analysis determines the array's address outlives the function call, it moves to the heap, turning an apparently cheap local into a per-call heap allocation.

**❌ Bad**
```go
func processChunks(chunks [][]byte, sink func([]byte)) {
	for _, c := range chunks {
		var scratch [65536]byte // BUG: zero-initialized (and possibly heap-allocated) every iteration
		n := copy(scratch[:], c)
		sink(scratch[:n]) // if sink retains the slice, scratch must escape to the heap
	}
}
```

**Why it's wrong:**
- The runtime clears all 64KB of `scratch` on every loop iteration even though `copy` immediately overwrites the first `n` bytes — wasted work that scales with the number of chunks.
- If `sink` stores its argument somewhere that outlives the call (a cache, a channel, a background goroutine), escape analysis forces `scratch` onto the heap, so what looks like a stack-only, allocation-free loop is actually allocating 64KB per iteration — a hidden perf cliff only visible via `go build -gcflags="-m"` or a heap profiler.

**✅ Good**
```go
func processChunks(chunks [][]byte, sink func([]byte)) {
	scratch := make([]byte, 65536) // allocate once, reuse across iterations
	for _, c := range chunks {
		n := copy(scratch, c)
		sink(scratch[:n]) // caller must not retain this slice past the call, or copy it
	}
}
```

**Why it works / Explanation:** Hoisting the buffer out of the loop means it's zeroed and (potentially) allocated once, not once per chunk. If `sink` needs to retain the data, it must copy it explicitly — which is one allocation for the caller to own, rather than one per iteration hidden inside this function.

**Design principle:** Measure, don't assume, allocation behavior — "local array" does not mean "stack, free, and cheap"; check with escape analysis when it's in a hot path.

---

## 8. When to actually prefer arrays over slices

**The Problem:** Reaching for a slice by default is usually right, but for genuinely fixed-size data — protocol headers, hashes, small coordinate/vector types — an array avoids a heap allocation, is naturally comparable, and documents the fixed size in the type itself.

**❌ Bad (over-using slices for fixed-size data)**
```go
type PacketHeader struct {
	Magic   []byte // BUG: unconstrained length, needs a separate heap allocation
	Version uint8
	Flags   uint8
	Length  uint16
}
```

**Why it's wrong:**
- `Magic []byte` doesn't communicate that it must always be exactly 4 bytes — that constraint lives only in comments or validation code, not the type system, and every `PacketHeader` requires an extra heap allocation just for those 4 bytes.
- `PacketHeader` values aren't comparable with `==` because slices aren't comparable, ruling out simple equality checks or use as a map key even though the logical content is small and fixed-size.

**✅ Good**
```go
// import ( "encoding/binary"; "fmt" )

type PacketHeader struct {
	Magic   [4]byte // e.g. "GOPK" -- fixed size, no separate allocation
	Version uint8
	Flags   uint8
	Length  uint16
}

func parseHeader(buf []byte) (PacketHeader, error) {
	if len(buf) < 8 {
		return PacketHeader{}, fmt.Errorf("buffer too short: %d bytes", len(buf))
	}
	var h PacketHeader
	copy(h.Magic[:], buf[:4])
	h.Version = buf[4]
	h.Flags = buf[5]
	h.Length = binary.BigEndian.Uint16(buf[6:8])
	return h, nil
}
```

**Why it works / Explanation:** `[4]byte` makes the fixed size part of the type itself (self-documenting and compiler-enforced), stores inline with no separate heap allocation, and makes `PacketHeader` comparable with `==` and usable as a map key if needed — properties a `[]byte` field could never give you.

**Design principle:** Let the type system encode your invariants — a value that's always exactly N bytes should be typed as `[N]byte`, not `[]byte`.

---

## Key Takeaways
- Arrays copy entirely on assignment or function call — mutations to a value parameter never reach the caller; pass a pointer when that matters.
- Array length is part of the type (`[5]int` ≠ `[10]int`); write functions against slices (and convert with `arr[:]`) to handle multiple sizes generically.
- Arrays with comparable element types support `==` and work as map keys — slices don't; use fixed-size arrays for hashes, checksums, and coordinates.
- Slicing an array reaches the original data only if you slice through a pointer; slicing a value parameter slices a throwaway copy.
- `range` over a bare array value copies the whole array once up front — mutations to the array mid-loop aren't seen unless you index directly or range over a pointer.
- Converting a slice to an array or array-pointer (Go 1.17+/1.20+) panics at runtime if the slice is too short — check length first.
- Large local arrays are zero-initialized every time they're declared and may silently escape to the heap — hoist them out of hot loops.
- Prefer arrays over slices for genuinely fixed-size data (protocol headers, hashes) to avoid heap allocation and gain comparability.

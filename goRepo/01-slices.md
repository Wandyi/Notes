# Slices in Go: Production Pitfalls

Slices look like simple, safe wrappers around arrays, but their header-plus-backing-array design is the single biggest source of subtle Go bugs in production: silent data corruption between "unrelated" slices, memory that won't get garbage collected, and mutations that mysteriously don't (or do) propagate back to the caller. Most of these bugs don't show up in unit tests with small inputs — they surface under load, with larger datasets, or after a refactor changes a slice's capacity. Understanding the slice header (pointer, length, capacity) and exactly when `append` reallocates is the difference between code that "usually works" and code that is actually correct.

## 1. Nil slice vs empty slice

**The Problem:** `var s []int` (nil, length 0) and `s := []int{}` (non-nil, length 0) behave the same for `len()`, indexing, and `range`, so it's easy to assume they're interchangeable — until they hit `encoding/json` or an API contract that distinguishes `null` from `[]`.

**❌ Bad**
```go
// import ( "encoding/json"; "fmt"; "strings" )

type UserListResponse struct {
	Users []string `json:"users"`
}

func getUsers(names []string, filter string) UserListResponse {
	var users []string // BUG: nil slice, not an empty one
	for _, n := range names {
		if strings.Contains(n, filter) {
			users = append(users, n)
		}
	}
	return UserListResponse{Users: users}
}

func main() {
	b, _ := json.Marshal(getUsers([]string{"alice", "bob"}, "nobody-matches"))
	fmt.Println(string(b)) // {"users":null}
}
```

**Why it's wrong:**
- `json.Marshal` encodes a nil slice as `null`, not `[]`; a frontend doing `response.users.map(...)` or a strict JSON-schema consumer expecting an array type will error or crash on `null`.
- `len(users) == 0` is true for both the nil and empty case, so code reviews and simple assertions don't catch the discrepancy — it only shows up at the serialization boundary.

**✅ Good**
```go
func getUsers(names []string, filter string) UserListResponse {
	users := make([]string, 0) // always non-nil, marshals to []
	for _, n := range names {
		if strings.Contains(n, filter) {
			users = append(users, n)
		}
	}
	return UserListResponse{Users: users}
}
```

**Why it works / Explanation:** `make([]string, 0)` produces a slice with a non-nil (if zero-capacity) header, and `encoding/json` marshals any non-nil slice — empty or not — as `[]`. Appending to it works identically to appending to a nil slice, so there's no downside to always initializing this way when the value crosses a serialization boundary.

**Design principle:** Contract stability at API boundaries — a field's JSON shape shouldn't depend on incidental Go zero-value semantics.

---

## 2. `append` aliasing / shared backing array

**The Problem:** Sub-slicing a slice (`s[a:b]`) doesn't copy data — it creates a new header pointing at the same backing array. If two sub-slices both have spare capacity, appending to one can silently overwrite memory that the other slice is still reading.

**❌ Bad**
```go
// import "fmt"

func splitIntoBatches(data []int, size int) [][]int {
	var result [][]int
	for i := 0; i < len(data); i += size {
		end := i + size
		if end > len(data) {
			end = len(data)
		}
		result = append(result, data[i:end]) // BUG: each batch aliases data's backing array
	}
	return result
}

func main() {
	data := make([]int, 6, 10) // len 6, cap 10 -- extra headroom is the trap
	for i := range data {
		data[i] = i
	}
	batches := splitIntoBatches(data, 3) // [[0 1 2] [3 4 5]]

	batches[0] = append(batches[0], 99) // "just adding to batch 0"
	fmt.Println(batches[1])             // [99 4 5] -- batch 1 corrupted!
}
```

**Why it's wrong:**
- `data[0:3]` has capacity 10 (inherited from `data`), so appending to `batches[0]` writes into `data[3]` instead of allocating new memory — and `data[3]` is also `batches[1][0]`.
- The corruption is silent: no panic, no error, just wrong data that surfaces later (e.g., a "batch" of work items now contains a value from a different batch), and it only manifests when the source slice happens to have spare capacity, so it can pass tests on inputs built with `[]int{...}` literals (which often have `len == cap`) and fail only in production with slices built via `append`.

**✅ Good**
```go
func splitIntoBatches(data []int, size int) [][]int {
	var result [][]int
	for i := 0; i < len(data); i += size {
		end := i + size
		if end > len(data) {
			end = len(data)
		}
		result = append(result, data[i:end:end]) // full slice expr: cap == len
	}
	return result
}
```

**Why it works / Explanation:** The three-index slice expression `data[i:end:end]` caps the sub-slice's capacity at its own length, so it has zero spare room. Any subsequent `append` to a batch is forced to allocate a fresh backing array instead of writing into `data`, making each batch independent.

**Design principle:** Ownership and aliasing — a function that hands out sub-slices should not leave the caller able to accidentally write into someone else's view of the same memory.

---

## 3. Memory leak via sub-slicing a large slice

**The Problem:** Keeping only a small sub-slice alive doesn't release the memory of the larger backing array it was sliced from — the sub-slice's pointer still refers into that same array, so the whole allocation stays reachable and un-collectable.

**❌ Bad**
```go
var longLivedCache = map[string][]byte{}

func parseHeader(buf []byte) []byte {
	return buf[:16] // BUG: still points into buf's full backing array
}

func loadAndParseHeader(data []byte) []byte {
	// data was e.g. loaded via os.ReadFile("huge.bin") -- imagine 500 MB
	return parseHeader(data) // caller only wanted 16 bytes...
}

func main() {
	data := make([]byte, 500*1024*1024) // stand-in for a huge loaded file
	header := loadAndParseHeader(data)
	longLivedCache["last-header"] = header // ...but 500 MB stays retained by the GC root
}
```

**Why it's wrong:**
- `header` is a slice whose pointer field still points inside the 500 MB array; as long as `header` is reachable (e.g., stored in a long-lived cache), the entire 500 MB array is reachable too, so the garbage collector can never free it.
- This is invisible in profiling until you look at heap dumps — `len(header)` reports 16, giving a false sense that memory usage is small.

**✅ Good**
```go
func parseHeader(buf []byte) []byte {
	header := make([]byte, 16)
	copy(header, buf[:16]) // explicit copy breaks the reference to buf's array
	return header
}
```

**Why it works / Explanation:** `copy` allocates a brand-new, minimally-sized backing array and copies just the bytes needed. Once `loadAndParseHeader` returns, `data` (the 500 MB slice) has no remaining references and becomes eligible for garbage collection. Note that a three-index slice (`buf[:16:16]`) only limits *capacity for future appends* — it still points at the same original array, so it does **not** fix this particular leak; only an explicit copy does.

**Design principle:** Defensive copying — when a small view is derived from a large, long-lived resource, decouple its lifetime explicitly instead of relying on incidental sharing.

---

## 4. Capacity growth and the cost of un-preallocated `append`

**The Problem:** `append` on a slice with no spare capacity must allocate a new, larger backing array and copy every existing element into it. Growth is amortized O(1) per element, but each individual growth step is an O(n) copy, and repeatedly growing a large slice from empty does real, avoidable work.

**❌ Bad**
```go
func buildIDs(n int) []int {
	var ids []int // len 0, cap 0
	for i := 0; i < n; i++ {
		ids = append(ids, i) // BUG: repeated reallocation + copy as capacity is exceeded
	}
	return ids
}
```

**Why it's wrong:**
- Without a capacity hint, the runtime grows the backing array in stages (roughly doubling for small slices, tapering to ~1.25x for larger ones), and each stage copies every element accumulated so far — for `n = 1,000,000` that's several megabyte-scale copies, not just 1,000,000 single-element writes.
- In a hot path (e.g., building a response list per request), this shows up as extra GC pressure and CPU time that a one-line change eliminates.

**✅ Good**
```go
func buildIDs(n int) []int {
	ids := make([]int, 0, n) // preallocate exact capacity
	for i := 0; i < n; i++ {
		ids = append(ids, i) // no reallocation, ever
	}
	return ids
}
```

**Why it works / Explanation:** `make([]int, 0, n)` allocates the backing array once, up front, sized for the known upper bound. Every subsequent `append` just writes into existing capacity — no copying, no reallocation. When the final size is known or boundable, preallocating is always at least as fast and often dramatically faster.

**Design principle:** Amortized cost is not free cost — know your data's size when you can, and pay for the allocation once.

---

## 5. Passing slices to functions — mutation surprises

**The Problem:** A slice header (pointer, length, capacity) is passed to functions *by value*, but the pointer inside it refers to shared backing memory. Mutating elements by index is always visible to the caller; but `append` inside the function only *sometimes* affects memory the caller can see, and never changes what the caller's own slice variable considers its length.

**❌ Bad**
```go
// import "fmt"

func mutateViaAppend(s []int) {
	s = append(s, 100) // BUG: reassigns the *local* s; caller's variable is untouched either way
}

func main() {
	noHeadroom := make([]int, 3, 3) // len == cap
	copy(noHeadroom, []int{1, 2, 3})
	mutateViaAppend(noHeadroom)
	fmt.Println(noHeadroom) // [1 2 3] -- append reallocated internally, caller sees nothing

	withHeadroom := make([]int, 3, 5) // spare capacity
	copy(withHeadroom, []int{1, 2, 3})
	mutateViaAppend(withHeadroom)
	fmt.Println(withHeadroom)     // [1 2 3] -- len is still 3, still looks unaffected...
	fmt.Println(withHeadroom[:4]) // [1 2 3 100] -- ...but the backing array was mutated!
}
```

**Why it's wrong:**
- In the no-headroom case, `append` inside the function reallocates, so the write never touches the caller's backing array at all — fully invisible, as expected.
- In the headroom case, `append` writes `100` directly into the caller's backing array at index 3 — but because the caller's slice *header* (its `len`) was passed by value and never reassigned, the caller doesn't see the new element through normal use... until it re-slices or appends itself, at which point stray, unexpected data resurfaces. Whether a callee's `append` is "visible" therefore depends on capacity headroom the caller may not even know about.

**✅ Good**
```go
func appendOne(s []int) []int {
	return append(s, 100) // caller receives the authoritative new header
}

func main() {
	s := []int{1, 2, 3}
	s = appendOne(s) // correct: always use the returned slice
	fmt.Println(s)   // [1 2 3 100]
}
```

**Why it works / Explanation:** Any function that grows a slice should return the resulting slice, and callers must always reassign it (`s = appendOne(s)`), exactly like the standard library's own `append`. This sidesteps the headroom ambiguity entirely — the caller's `len`/`cap` are always updated consistently with what actually happened.

**Design principle:** Least surprise — never rely on incidental capacity to make a mutation "work"; make ownership of the returned value explicit.

---

## 6. `copy()` misuse

**The Problem:** `copy(dst, src)` copies `min(len(dst), len(src))` elements and returns that count — it never grows `dst`, never errors, and never panics on a length mismatch. Forgetting this leads to silent truncation or silently-empty results.

**❌ Bad**
```go
func cloneBytes(src []byte) []byte {
	var dst []byte // BUG: len 0, cap 0
	copy(dst, src) // copies min(0, len(src)) == 0 elements
	return dst      // always empty, no error, no panic
}

func firstN(src []byte, n int) []byte {
	dst := make([]byte, n)
	copy(dst, src) // BUG: if len(src) < n, dst is zero-padded silently
	return dst
}
```

**Why it's wrong:**
- `cloneBytes` looks like a clone helper but always returns an empty slice — a classic copy-paste bug where `dst` was never sized, and nothing in the program signals the mistake.
- `firstN` silently zero-pads when `src` is shorter than expected instead of surfacing that the caller's assumption ("there are at least `n` bytes") was wrong — a bug that hides real data-integrity problems upstream.

**✅ Good**
```go
func cloneBytes(src []byte) []byte {
	dst := make([]byte, len(src)) // size dst to match src first
	n := copy(dst, src)
	if n != len(src) {
		panic("unreachable: copy count must equal len(src) here")
	}
	return dst
}
```

**Why it works / Explanation:** Sizing `dst` with `make([]byte, len(src))` guarantees `copy` transfers every element, and checking the returned count is a cheap sanity check for cases where the destination size is computed rather than derived directly from the source. (Go 1.21+ also offers `slices.Clone(src)` for this exact use case.)

**Design principle:** Fail loud, not quiet — a length mismatch that matters to correctness should be checked, not silently absorbed.

---

## 7. Removing an element from a slice — order and stale-pointer leaks

**The Problem:** There are two common removal patterns — order-preserving shift and swap-with-last — and both have a subtle memory-leak trap: if the slice holds pointers or interfaces, the vacated slot still holds a live reference after the "removal," keeping that object reachable from the GC's point of view via the backing array.

**❌ Bad**
```go
type Job struct{ ID string }

func removeUnordered(jobs []*Job, idx int) []*Job {
	last := len(jobs) - 1
	jobs[idx] = jobs[last]
	return jobs[:last] // BUG: jobs[last] still holds a live *Job pointer, just out of "logical" range
}
```

**Why it's wrong:**
- The backing array's memory (including the slot beyond the new length) is one allocation; the GC scans the whole allocation for pointers, not just the first `len` elements. The dropped `*Job` therefore stays reachable and un-collectable for as long as anything keeps a reference to that backing array — a slow, hard-to-diagnose memory leak in long-running services that repeatedly remove items from large slices of pointers.

**✅ Good**
```go
func removeUnordered(jobs []*Job, idx int) []*Job {
	last := len(jobs) - 1
	jobs[idx] = jobs[last]
	jobs[last] = nil // clear the stale reference so GC can reclaim it
	return jobs[:last]
}

func removeOrdered(jobs []*Job, idx int) []*Job {
	copy(jobs[idx:], jobs[idx+1:])
	jobs[len(jobs)-1] = nil // same fix, order-preserving version
	return jobs[:len(jobs)-1]
}
```

**Why it works / Explanation:** Explicitly nil-ing the vacated slot removes the last live reference to the removed pointer from the backing array, letting the GC collect the pointed-to object as soon as nothing else references it. Use `removeUnordered` (O(1)) when order doesn't matter; use `removeOrdered` (O(n)) when it does.

**Design principle:** Explicit resource release — clearing a reference you no longer logically own is cheap insurance against a GC leak.

---

## 8. Comparing slices

**The Problem:** Slices are not comparable with `==` (except against the literal `nil`) — this is a compile error, not a runtime surprise, but it catches people used to comparing arrays or other languages' arrays/lists directly.

**❌ Bad**
```go
a := []int{1, 2, 3}
b := []int{1, 2, 3}

if a == b { // compile error: invalid operation: a == b (slice can only be compared to nil)
	fmt.Println("equal")
}
```

**Why it's wrong:**
- This doesn't compile at all, so it's caught early — but developers sometimes "fix" it by writing a manual loop that gets the edge cases wrong (e.g., forgetting to check lengths first, or mishandling nil vs. empty), or by reaching for `==` after refactoring an array to a slice and being confused by the sudden compile error.

**✅ Good**
```go
// import "slices" // Go 1.21+

a := []int{1, 2, 3}
b := []int{1, 2, 3}

fmt.Println(slices.Equal(a, b)) // true -- element-wise comparison

// For slices of non-comparable element types, or pre-1.21 code:
// import "reflect"
fmt.Println(reflect.DeepEqual(a, b)) // true, but slower and less type-safe
```

**Why it works / Explanation:** `slices.Equal` does the length check and element-wise comparison correctly (and treats nil and empty slices of equal length as equal), and is the idiomatic Go 1.21+ choice. `reflect.DeepEqual` works for arbitrary types (including nested slices/maps) but is slower and can have surprising results for types with custom equality semantics.

**Design principle:** Make illegal states unrepresentable — the compiler forcing an explicit comparison function is a feature, not friction; use it instead of routing around it.

---

## 9. Range loop capturing gotchas with slices of structs

**The Problem:** `for _, v := range s` copies each element into `v`; if `s` holds struct values (not pointers), mutating `v` mutates only the copy. Separately, prior to Go 1.22, the loop's iteration variables were reused across iterations rather than freshly created, which broke code that captured them in closures or goroutines.

**❌ Bad**
```go
type Account struct{ Balance int }

func applyBonus(accounts []Account) {
	for _, acc := range accounts {
		acc.Balance += 10 // BUG: mutates the copy `acc`, not the slice element
	}
}

func main() {
	accounts := []Account{{Balance: 100}, {Balance: 200}}
	applyBonus(accounts)
	fmt.Println(accounts) // [{100} {200}] -- unchanged!
}
```

**Why it's wrong:**
- `acc` is a fresh copy of each `Account` on every iteration; incrementing its field has no effect on `accounts[i]`. This compiles cleanly and runs without error, so it's easy to miss in review — the bug is purely semantic.

**✅ Good**
```go
func applyBonus(accounts []Account) {
	for i := range accounts {
		accounts[i].Balance += 10 // mutates the actual element
	}
}
```

**Why it works / Explanation:** Indexing into the slice (`accounts[i]`) reaches the real backing-array element, so the mutation is visible to the caller. As for the loop-variable-reuse issue:

```go
// Go < 1.22: all (or most) goroutines below tend to print the LAST account's balance,
// because `acc` was one variable reused across iterations, and the goroutines
// typically run after the loop has already finished.
for _, acc := range accounts {
	go func() {
		fmt.Println(acc.Balance) // BUG on Go < 1.22
	}()
}

// Fix on Go < 1.22: shadow the variable inside the loop body.
for _, acc := range accounts {
	acc := acc // creates a new variable per iteration
	go func() {
		fmt.Println(acc.Balance)
	}()
}
```
Go 1.22 changed the language spec so that `for` loops create fresh copies of their iteration variables on every pass, making the shadowing workaround unnecessary — but code targeting older Go versions (or vendored dependencies pinned to an older `go` directive) still needs it.

**Design principle:** Value vs. reference semantics — know whether you're holding a copy or a handle before you mutate it.

---

## 10. `sort.Slice` gotchas

**The Problem:** `sort.Slice` is not guaranteed stable — equal elements can end up in a different relative order than they started in, and that order isn't even guaranteed consistent across Go versions. Separately, comparator closures built inside a loop can capture a shared variable rather than the value it held on that iteration.

**❌ Bad**
```go
// import "sort"

type Employee struct{ Name, Dept string }

func main() {
	employees := []Employee{
		{"Alice", "Eng"}, {"Bob", "Eng"}, {"Carol", "Sales"}, {"Dave", "Eng"},
	}

	sort.Slice(employees, func(i, j int) bool {
		return employees[i].Dept < employees[j].Dept
	})
	// BUG: relative order among Alice/Bob/Dave (all "Eng") is NOT guaranteed preserved
	fmt.Println(employees)
}
```

**Why it's wrong:**
- `sort.Slice` uses an unstable algorithm; two elements comparing equal under `less` can be swapped relative to each other. Code that (incorrectly) relies on "records with the same department stay in insertion order" will pass on some inputs/Go versions and silently produce a different order on others — a classic source of flaky tests and hard-to-reproduce ordering bugs in reports or paginated APIs.

**✅ Good**
```go
sort.SliceStable(employees, func(i, j int) bool {
	return employees[i].Dept < employees[j].Dept
}) // ties keep their original relative order
```

Comparator closures built in a loop have a similar trap:
```go
func fieldValue(e Employee, field string) string {
	if field == "Name" {
		return e.Name
	}
	return e.Dept
}

func buildComparators(employees []Employee) []func(i, j int) bool {
	var comparators []func(i, j int) bool
	for _, field := range []string{"Name", "Dept"} {
		field := field // BUG if omitted: every closure below would capture the SAME `field`
		comparators = append(comparators, func(i, j int) bool {
			return fieldValue(employees[i], field) < fieldValue(employees[j], field)
		})
	}
	// Without the shadow, calling comparators[0] later would sort by "Dept" (the last
	// value `field` held), not "Name" -- because all closures reference one shared variable.
	return comparators
}
```

**Why it works / Explanation:** `sort.SliceStable` guarantees equal elements retain their input order, at the cost of being somewhat slower than `sort.Slice`. Shadowing the loop variable (`field := field`) — or relying on Go 1.22+'s per-iteration variable scoping — ensures each closure captures its own value instead of a variable that keeps changing.

**Design principle:** Determinism where it's promised — only rely on ordering guarantees the API actually documents, and don't let deferred closures outlive the loop variable's intended value.

---

## 11. Two-dimensional / jagged slices — allocation patterns

**The Problem:** A `[][]T` grid can be built as many independently-allocated rows or as one contiguous backing array sliced into rows. The former is simpler but costs one allocation per row and hurts cache locality; the latter is a single allocation with much better locality, at the cost of rows sharing capacity in ways that matter if you later `append` to an individual row.

**❌ Bad (for large, fixed-size grids)**
```go
func newGrid(rows, cols int) [][]float64 {
	grid := make([][]float64, rows)
	for r := range grid {
		grid[r] = make([]float64, cols) // BUG (for perf): one allocation per row
	}
	return grid
}
```

**Why it's wrong:**
- For a 1000x1000 grid, this is 1000 separate heap allocations instead of one — more GC bookkeeping, worse cache locality (rows are scattered across the heap instead of contiguous), and slower to zero-initialize as a batch.

**✅ Good**
```go
func newGrid(rows, cols int) [][]float64 {
	data := make([]float64, rows*cols) // one contiguous allocation
	grid := make([][]float64, rows)
	for r := range grid {
		grid[r] = data[r*cols : (r+1)*cols]
	}
	return grid
}
```

**Why it works / Explanation:** All the numeric data lives in one backing array, so allocation is O(1) calls instead of O(rows), and iterating row-by-row (or the whole grid in row-major order) benefits from CPU cache prefetching. The one caveat: if code later does `grid[r] = append(grid[r], x)` and a row has no spare capacity (it won't, here, since each row's cap equals its len), that row reallocates independently and detaches from the shared array — which is safe, just worth knowing so it isn't mistaken for a bug.

**Design principle:** Data locality — prefer one large, contiguous allocation over many small ones when the access pattern is predictable and performance matters.

---

## Key Takeaways
- Nil vs. empty slices look identical under `len()` but marshal differently to JSON (`null` vs `[]`) — initialize with `make(..., 0)` at API boundaries.
- Sub-slicing shares the backing array; `append` on one sub-slice can silently corrupt a sibling that has spare capacity — use `s[low:high:max]` to cap capacity when handing out sub-slices.
- A small slice sliced from a huge one keeps the huge backing array alive for the GC — copy out what you need instead of just re-slicing.
- Un-preallocated `append` triggers repeated O(n) copy-and-grow cycles — use `make([]T, 0, n)` when the size is known or boundable.
- Passing a slice to a function makes index-based mutation visible to the caller, but `append`'s effects depend on hidden capacity headroom — return the new slice instead of relying on that.
- `copy()` silently truncates to `min(len(dst), len(src))` and never panics — size `dst` correctly and check the returned count when it matters.
- Removing elements can leave a stale pointer/interface in the vacated slot, keeping objects reachable — nil it out explicitly.
- Slices can't be compared with `==`; use `slices.Equal` or `reflect.DeepEqual`.
- Ranging by value copies struct elements (mutations are no-ops); pre-Go 1.22, loop variables were also reused across iterations, breaking closures/goroutines.
- `sort.Slice` isn't stable and comparator closures built in loops can capture a shared variable — use `sort.SliceStable` and shadow loop variables.
- For large fixed-size 2D data, one contiguous backing array beats many per-row allocations for both allocation count and cache locality.

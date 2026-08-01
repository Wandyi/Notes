# Maps in Go: Production Pitfalls

Go's built-in map is a hash table with a deceptively simple API, but several of its behaviors are easy to get wrong in ways that don't show up until production traffic, larger data volumes, or concurrent access patterns exercise the edge cases: nil-map panics, nondeterministic iteration order, unsafe concurrent access that crashes the whole process, and value-type semantics that make in-place struct mutation silently fail. Knowing these up front avoids debugging sessions that only reproduce under load or "sometimes."

## 1. Nil map — reads are safe, writes panic

**The Problem:** A nil map (the zero value of a map type) behaves like an empty map for reads — `m[k]` returns the zero value, `len(m)` is 0, ranging over it does nothing — but any write to it panics. This asymmetry catches people who tested only the read path.

**❌ Bad**
```go
type Cache struct {
	data map[string]int // BUG: zero value is nil; never initialized
}

func (c *Cache) Incr(key string) {
	c.data[key]++ // panic: assignment to entry in nil map
}

func main() {
	c := &Cache{}  // data is nil
	c.Incr("hits") // panics
}
```

**Why it's wrong:**
- `&Cache{}` compiles fine and looks fully constructed — there's no compile-time signal that `data` needs explicit initialization, so the panic only appears the first time `Incr` (or any write) is actually called, potentially deep in production.

**✅ Good**
```go
func NewCache() *Cache {
	return &Cache{data: make(map[string]int)} // always initialize in the constructor
}

func main() {
	c := NewCache()
	c.Incr("hits") // fine: data is a real, initialized map
}
```

**Why it works / Explanation:** Routing construction through `NewCache` guarantees `data` is always a real map before any write happens. As a rule: reads (`v := m[k]`, `v, ok := m[k]`, `len(m)`, `range m`) are always safe on a nil map, and even `delete(m, k)` on a nil map is a documented no-op — only assigning into the map (`m[k] = v`, `m[k]++`) panics.

**Design principle:** Make invalid states unreachable — initialize maps in constructors so a zero-value struct is never silently half-usable.

---

## 2. Map iteration order is randomized

**The Problem:** Go deliberately randomizes map iteration order on every run (and even between successive `range` loops over the same map) specifically to stop code from accidentally depending on it. Any logic that builds an ordered result (a string, a report, a cache key) directly from `range` over a map will be nondeterministic.

**❌ Bad**
```go
func cacheKey(prices map[string]float64) string {
	key := ""
	for k := range prices { // BUG: order varies from call to call
		key += k + ","
	}
	return key
}
```

**Why it's wrong:**
- The same logical map produces a different `key` string on different runs (and sometimes different calls in the same run), so two equivalent `prices` maps won't reliably produce the same cache key — cache misses, duplicate cache entries, or flaky test assertions like `assert.Equal(t, "a,b,c", cacheKey(m))` that fail depending on runtime hash seeding.

**✅ Good**
```go
// import ( "fmt"; "sort"; "strings" )

func cacheKey(prices map[string]float64) string {
	keys := make([]string, 0, len(prices))
	for k := range prices {
		keys = append(keys, k)
	}
	sort.Strings(keys) // impose a deterministic order

	var b strings.Builder
	for _, k := range keys {
		fmt.Fprintf(&b, "%s=%.2f,", k, prices[k])
	}
	return b.String()
}
```

**Why it works / Explanation:** Collecting the keys into a slice and sorting them before building the string makes the output depend only on the map's *contents*, not on hash-table internals — the same logical map always yields the same key, run after run.

**Design principle:** Determinism where it's needed — never let an intentionally-unordered data structure drive an operation (caching, hashing, serialization) that requires a stable order.

---

## 3. Concurrent map read/write without synchronization

**The Problem:** Go maps are not safe for concurrent use. Reading and writing (or writing and writing) the same map from different goroutines without synchronization isn't just a data race that produces wrong values — the runtime's built-in concurrent-access detector will typically crash the entire process with a fatal error that `recover()` cannot catch.

**❌ Bad**
```go
counts := make(map[string]int)

go func() {
	for {
		counts["x"]++ // write, unsynchronized
	}
}()

go func() {
	for {
		_ = counts["x"] // read, unsynchronized
	}
}()

// fatal error: concurrent map read and map write
// (this terminates the whole process -- it is not a recoverable panic)
```

**Why it's wrong:**
- Unlike a normal panic, this is a runtime `fatal error` — it cannot be caught with `recover()`, so a single unsynchronized map access under load can take down an entire server process, not just the offending goroutine or request.

**✅ Good**
```go
// import "sync"

type SafeCounter struct {
	mu     sync.RWMutex
	counts map[string]int
}

func NewSafeCounter() *SafeCounter {
	return &SafeCounter{counts: make(map[string]int)}
}

func (c *SafeCounter) Incr(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.counts[key]++
}

func (c *SafeCounter) Get(key string) int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.counts[key]
}
```

**Why it works / Explanation:** `sync.RWMutex` lets any number of readers proceed concurrently but guarantees exclusive access during writes, eliminating the race entirely. `sync.Map` is a reasonable alternative, but it's specifically optimized for append-mostly workloads with largely disjoint keys per goroutine (e.g., caches where each goroutine mostly reads/writes its own keys) — for balanced read/write access to overlapping keys, a plain map plus a mutex is usually both simpler and faster.

**Design principle:** Shared mutable state needs explicit synchronization — a map has no built-in safety net, and the failure mode is a process crash, not a quiet bug.

---

## 4. Map key comparability

**The Problem:** Map keys must be comparable — slices, maps, and function values can never be map keys (a compile error), because Go can't hash or `==`-compare them. Struct types are comparable (and valid keys) as long as every field they contain is itself comparable, which makes composite keys easy to get right.

**❌ Bad**
```go
// None of these compile:
// var byTags   map[[]string]int          // invalid: slice
// var byGroups map[map[string]bool]int   // invalid: map
// var byFunc   map[func()]int            // invalid: func
```

**Why it's wrong:**
- These are compile errors ("invalid map key type"), which is good — but developers sometimes respond by serializing the intended key to a string (e.g., `strings.Join(tags, ",")`) as a workaround, which reintroduces separator-collision bugs (`["a,b", "c"]` vs `["a", "b,c"]` hashing to the same string) instead of using a proper composite struct key.

**✅ Good**
```go
type PermissionKey struct {
	UserID     int64
	ResourceID string
}

func main() {
	perms := make(map[PermissionKey]bool)
	perms[PermissionKey{UserID: 42, ResourceID: "doc-1"}] = true

	if perms[PermissionKey{UserID: 42, ResourceID: "doc-1"}] {
		fmt.Println("allowed")
	}
}
```

**Why it works / Explanation:** `PermissionKey` contains only comparable fields (`int64`, `string`), so it's automatically comparable and hashable, making it a safe, collision-free map key — two `PermissionKey` values are equal if and only if all their fields are equal, with none of the string-joining ambiguity of a manually-serialized key.

**Design principle:** Model the key as data, not as a serialized string — a struct of comparable fields is exact; string concatenation is lossy.

---

## 5. Deleting or inserting keys while ranging over a map

**The Problem:** The Go spec explicitly guarantees that deleting the current key during a `range` is safe and won't be revisited. Inserting a new key during that same iteration, however, has *unspecified* behavior — the new key may or may not be produced later in the same loop.

**❌ Bad**
```go
m := map[string]int{"a": 1}

for k := range m {
	if k == "a" {
		m["b"] = 2 // BUG: whether "b" is visited by this same range is unspecified
	}
}
```

**Why it's wrong:**
- This might visit `"b"` on some runs and not others (it can depend on map size, current bucket layout, and Go version internals) — code that relies on newly-inserted keys being seen (or not seen) in the same loop is relying on undefined behavior, and can behave differently after an unrelated Go upgrade.

**✅ Good**
```go
// Deleting during range: explicitly safe per the Go spec.
for k, v := range m {
	if v < 0 {
		delete(m, k) // fine -- guaranteed not to be revisited, guaranteed not to skip others
	}
}

// Inserting: collect first, mutate after the loop.
toAdd := map[string]int{}
for k, v := range m {
	if v > 100 {
		toAdd[k+"-flag"] = 1
	}
}
for k, v := range toAdd {
	m[k] = v
}
```

**Why it works / Explanation:** Deletion during `range` is a documented, safe pattern — use it freely. Insertion is not; the safe pattern is to compute the set of keys to add in a separate collection during the read pass, then apply them in a second pass after the original iteration has completed.

**Design principle:** Only rely on behavior the spec actually promises — "seems to work in testing" is not a substitute for a documented guarantee when it comes to iteration semantics.

---

## 6. Map memory doesn't shrink after bulk deletes

**The Problem:** Deleting entries from a map marks their slots as available for reuse, but the underlying bucket arrays that were allocated to hold the map's peak size are not returned to the OS or shrunk — a map that briefly held millions of entries and now holds a handful still occupies (roughly) the memory footprint of its peak.

**❌ Bad**
```go
// import "fmt"

big := make(map[int]string)
for i := 0; i < 1_000_000; i++ {
	big[i] = fmt.Sprintf("value-%d", i)
}
for i := 0; i < 1_000_000; i++ {
	delete(big, i)
}
fmt.Println(len(big)) // 0
// BUG: process RSS stays roughly at its peak -- big's bucket arrays are still allocated
```

**Why it's wrong:**
- `len(big) == 0` gives the false impression that the memory has been reclaimed; in reality the map's internal bucket storage — sized for its historical peak — remains allocated as long as `big` itself is reachable, which can look like a memory leak in long-running services that periodically load-and-clear large maps.

**✅ Good**
```go
// Rebuild a fresh map to actually release the old buckets.
fresh := make(map[int]string, len(big)) // size hint for the new, smaller population
for k, v := range big {
	fresh[k] = v
}
big = fresh // the old map (and its buckets) become garbage and are reclaimed by the GC
```

**Why it works / Explanation:** Once nothing references the old map, its entire bucket allocation becomes eligible for garbage collection — assigning a freshly-built, appropriately-sized map to the same variable is the practical way to reclaim that memory. Note: Go 1.21's `clear(m)` builtin empties all entries (equivalent to deleting every key) in one call, but it has the same limitation — it does not shrink or release the underlying bucket memory either.

**Design principle:** Right-size long-lived collections — after a large, temporary population, rebuild rather than assume deletion reclaims space.

---

## 7. Getting a value from a map returns a copy for value types

**The Problem:** `m[k]` yields a *copy* of the stored value when the value type is a struct (or other value type) — you cannot assign to a field of that result directly (`m[k].Field = x` is a compile error for map indexing), and even if you capture it in a variable first, mutating that variable never touches what's stored in the map.

**❌ Bad**
```go
type Account struct{ Balance int }

func main() {
	accounts := map[string]Account{"alice": {Balance: 100}}

	// accounts["alice"].Balance = 200 // compile error: cannot assign to struct field

	acc := accounts["alice"]
	acc.Balance = 200                      // BUG: mutates the local copy `acc` only
	fmt.Println(accounts["alice"].Balance) // 100 -- unchanged
}
```

**Why it's wrong:**
- The compiler actually stops the most direct version of this mistake (`accounts["alice"].Balance = 200` doesn't compile, because a map index expression isn't addressable) — but the version that reads into a local variable first compiles fine and *looks* correct, while silently doing nothing to the map's stored value.

**✅ Good**
```go
// Option A: read, modify, write back the whole struct.
acc := accounts["alice"]
acc.Balance = 200
accounts["alice"] = acc // write the modified copy back

// Option B: store pointers, so the map holds a stable handle to shared data.
accountsByPtr := map[string]*Account{"alice": {Balance: 100}}
accountsByPtr["alice"].Balance = 200        // OK: dereferences the pointer, mutates the pointee
fmt.Println(accountsByPtr["alice"].Balance) // 200
```

**Why it works / Explanation:** Option A works because reassigning the whole struct value overwrites what's stored in the map. Option B works because the map stores a `*Account`; reading it back still yields a copy of the *pointer*, but that copy points at the same underlying `Account`, so mutating through it is visible everywhere that pointer is shared — including inside the map.

**Design principle:** Know what "copy" means at each layer — a map of structs copies the struct on read; a map of pointers copies only the (cheap) pointer, sharing the pointee.

---

## 8. Comma-ok idiom vs. zero-value confusion

**The Problem:** `v := m[k]` returns the value's zero value both when the key is genuinely absent and when the key is present but was explicitly stored with the zero value — there's no way to tell these two cases apart without the two-value ("comma-ok") form.

**❌ Bad**
```go
scores := map[string]int{"alice": 0, "bob": 5} // alice legitimately has a score of 0

v := scores["carol"] // carol was never added
fmt.Println(v)        // 0

if v == 0 {
	fmt.Println("carol has a zero score") // BUG: wrong -- carol isn't in the map at all
}
```

**Why it's wrong:**
- `v == 0` is true both for "carol, who doesn't exist" and for "alice, whose score really is 0" — this code can't distinguish "absent" from "present with zero value," leading to wrong conclusions (e.g., treating a missing user as if they had a real, zero score).

**✅ Good**
```go
if v, ok := scores["carol"]; ok {
	fmt.Println("carol's score:", v)
} else {
	fmt.Println("carol not found")
}
```

**Why it works / Explanation:** The second return value `ok` is `true` only when the key actually exists in the map, independent of what value is stored there — this is the only reliable way to distinguish "absent" from "present with the zero value."

**Design principle:** Don't overload a single value with two meanings — use the idiom the language provides specifically to separate "presence" from "value."

---

## 9. Using floats as map keys

**The Problem:** Floating-point keys are legal (float types are comparable) but dangerous: binary floating-point representation errors mean two "equal-looking" computed values can differ at the bit level, and `NaN` famously never equals itself (`NaN != NaN`), so a value stored under a `NaN` key can never be retrieved again.

**❌ Bad**
```go
// import ( "fmt"; "math" )

m := make(map[float64]string)

nanKey := math.NaN()
m[nanKey] = "value"
fmt.Println(m[nanKey]) // "" -- BUG: NaN != NaN, so the lookup can never match the stored key
fmt.Println(len(m))    // 1  -- the entry exists but is permanently unreachable

m2 := map[float64]string{}
m2[0.1+0.2] = "result"
fmt.Println(m2[0.3]) // "" -- BUG: 0.1+0.2 == 0.30000000000000004 in float64, not 0.3
```

**Why it's wrong:**
- Both lookups miss despite looking like they should hit: the `NaN` entry is permanently orphaned by IEEE-754 semantics, and the `0.1+0.2` entry is orphaned by ordinary floating-point rounding — neither produces an error, just a silent, confusing miss.

**✅ Good**
```go
// Prefer integer keys (e.g., store money as integer cents) or string keys.
pricesInCents := map[int]string{}
pricesInCents[30] = "thirty cents" // exact, no representation ambiguity

// If a float-derived key is unavoidable, key on a formatted/rounded string instead:
key := fmt.Sprintf("%.2f", 0.1+0.2)
byFormattedKey := map[string]string{key: "result"}
```

**Why it works / Explanation:** Integers have exact equality semantics with no rounding or NaN pitfalls, making them safe, predictable map keys. When a float value must drive a key, rounding it to a fixed, formatted representation first removes the binary-representation ambiguity (at the cost of collapsing values within that rounding precision, which should be an intentional choice).

**Design principle:** Choose key types with exact equality — floats' approximate nature is fundamentally at odds with a hash table's need for exact, stable equality.

---

## 10. Preallocating map capacity for known-size bulk inserts

**The Problem:** Like slices, maps grow by reallocating and rehashing as they exceed their current bucket capacity. When the approximate final size is known ahead of time, `make(map[K]V, sizeHint)` avoids that repeated rehashing work.

**❌ Bad**
```go
type Item struct{ ID string }

func buildIndex(items []Item) map[string]Item {
	idx := make(map[string]Item) // BUG (for perf): no size hint, grows incrementally
	for _, it := range items {
		idx[it.ID] = it
	}
	return idx
}
```

**Why it's wrong:**
- With no capacity hint, the map starts small and grows its bucket array in stages as entries are added, and each growth stage rehashes existing entries into the new bucket layout — for a large `items` slice (tens of thousands of entries or more), that's repeated, avoidable rehashing work that shows up directly in profiling as time spent in map-growth machinery.

**✅ Good**
```go
func buildIndex(items []Item) map[string]Item {
	idx := make(map[string]Item, len(items)) // size hint avoids repeated growth/rehash
	for _, it := range items {
		idx[it.ID] = it
	}
	return idx
}
```

**Why it works / Explanation:** The size hint tells the runtime to allocate bucket storage sized for the expected number of entries up front, so inserts during the bulk-load loop don't trigger incremental regrowth. (The hint is advisory, not a hard cap — the map still grows further if more items are added later — but for a known bulk load it removes essentially all of the growth overhead.)

**Design principle:** Size for the known workload — just as with slices, paying for capacity once beats paying for repeated incremental growth.

---

## Key Takeaways
- A nil map is safe to read but panics on write — always initialize maps (e.g., in a constructor) before any write path can run.
- Map iteration order is randomized on purpose — sort keys first for anything requiring a deterministic order (cache keys, reports, tests).
- Unsynchronized concurrent map access causes an unrecoverable fatal error, not a normal panic — guard with `sync.RWMutex` (or `sync.Map` for append-mostly, disjoint-key workloads).
- Slices, maps, and funcs can't be map keys; structs of comparable fields make safe, exact composite keys — prefer them over string-joined keys.
- Deleting the current key during `range` is spec-guaranteed safe; inserting new keys during `range` has unspecified visibility — collect-then-mutate for inserts.
- Deleting many entries doesn't shrink a map's memory footprint — rebuild into a fresh map to actually reclaim it; `clear()` empties but doesn't shrink either.
- Reading a struct value out of a map yields a copy — mutate-then-reassign the whole value, or store pointers if in-place mutation is needed.
- `v := m[k]` can't distinguish "absent" from "present with zero value" — use the comma-ok form (`v, ok := m[k]`) whenever that distinction matters.
- Float keys are fragile: `NaN` keys can never be retrieved, and rounding error can make computed keys silently miss — prefer integer or string keys.
- Preallocate map capacity with `make(map[K]V, n)` for known-size bulk inserts to avoid repeated rehashing.

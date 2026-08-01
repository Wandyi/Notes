# Common Gotchas & Miscellaneous Pitfalls

Some of the most damaging Go bugs in production don't belong to any one subsystem — they come from language-level defaults that differ from other languages' conventions (switch fallthrough, checked vs. wrapping arithmetic), scoping rules that are easy to misread (`:=` shadowing), and habits (package-level globals, unkeyed struct literals, `os.Exit` in library code) that work fine until they don't. This doc is a grab-bag of exactly those pitfalls: the ones that don't need a dedicated topic file but still show up again and again in code review.

## 1. `iota` Gotchas: Reordering Breaks Persisted Data

**The Problem:** `iota`-based constants get their values from position in the `const` block, not from anything explicit. If those values are ever persisted (written to a database, serialized to disk, sent over the wire) and someone later inserts or reorders a value in the middle of the block, every previously stored value silently takes on a new meaning — with no compiler error, because the code is perfectly valid Go either way.

**❌ Bad**
```go
type Status int

const (
	StatusPending Status = iota // 0
	StatusActive                // 1
	StatusDone                  // 2
)

// ... months later, a "cancelled" status is needed, and someone
// inserts it where it "logically" belongs instead of at the end:

const (
	StatusPending   Status = iota // 0
	StatusCancelled                // 1 -- BUG: newly inserted in the middle
	StatusActive                   // 2, used to be 1
	StatusDone                     // 3, used to be 2
)
```

**Why it's wrong:**
- Every row already stored in the database with `status = 1` meant `StatusActive` under the old numbering; after this change, the exact same stored value `1` now means `StatusCancelled` — existing "active" records are silently reinterpreted as "cancelled" the moment this code deploys.
- Nothing about this change fails to compile or triggers a test failure unless there's a test that specifically pins the old numeric values — the bug is purely semantic and only visible once old data is read back through new code.

**✅ Good**
```go
type Status int

const (
	StatusPending Status = iota // 0
	StatusActive                // 1
	StatusDone                  // 2
	StatusCancelled              // 3 -- always append new values at the end
)
```

Or, safer still for anything that crosses a persistence or wire boundary, avoid positional values entirely:

```go
type Status string

const (
	StatusPending   Status = "pending"
	StatusActive    Status = "active"
	StatusDone      Status = "done"
	StatusCancelled Status = "cancelled"
)
```

**Why it works / Explanation:** Appending new `iota` values only at the end preserves every previously assigned number, so old persisted data keeps its original meaning. For anything that leaves the process boundary — a database column, a JSON API, an event payload — a string-based enum (or explicit, hand-assigned integer values with gaps reserved for future insertions) removes the fragility entirely, at the cost of a few more bytes on the wire.

**Design principle:** Never let an implementation detail (declaration order) become part of your data's on-disk format — pin persisted values explicitly, or use `iota` only for values that never outlive a single process's memory.

**A correct, common use of `iota`: bit flags.**
```go
type Perm uint8

const (
	PermRead Perm = 1 << iota // 1 << 0 = 1
	PermWrite                  // 1 << 1 = 2
	PermExec                   // 1 << 2 = 4
)

func main() {
	p := PermRead | PermWrite
	fmt.Println(p&PermWrite != 0) // true
	fmt.Println(p&PermExec != 0)  // false
}
```
This pattern is safe from the reordering trap in the same way any `iota` use is *not* safe — it just happens to be a case (bit flags used only in-memory, not persisted across versions) where the risk described above doesn't usually apply. If bit-flag values are ever persisted, the same append-only rule applies.

---

## 2. `init()` Function Overuse

**The Problem:** `init()` functions run automatically before `main()`, in an order that's easy to state but easy to get wrong in your head: within a package, `init()`s run in the order their files are given to the compiler (typically alphabetical by filename), and across packages, in import-dependency order. Relying on `init()` for anything beyond trivial, order-independent setup makes initialization sequencing hard to reason about, hard to test in isolation, and hard to control (you can't skip or parameterize an `init()`).

**❌ Bad**
```go
var db *sql.DB

func init() {
	var err error
	db, err = sql.Open("postgres", os.Getenv("DATABASE_URL"))
	if err != nil {
		panic(err) // BUG: panics at program startup, before main() even runs
	}
}
```

**Why it's wrong:**
- Any test that imports this package pays the cost (and risk) of this `init()` running, even a test that has nothing to do with the database — there's no way to substitute a fake connection or skip the `sql.Open` call for a unit test.
- Startup failure surfaces as an unrecoverable panic during package initialization, with no chance for `main()` to log a clean error message, retry, or fall back — and no way to control *when* the connection is established relative to other startup steps.

**✅ Good**
```go
func NewDB(dsn string) (*sql.DB, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, fmt.Errorf("opening database: %w", err)
	}
	return db, nil
}

func main() {
	db, err := NewDB(os.Getenv("DATABASE_URL"))
	if err != nil {
		log.Fatalf("startup failed: %v", err)
	}
	defer db.Close()
	// ... rest of startup
}
```

**Why it works / Explanation:** An explicit constructor function is called exactly when `main` decides to call it, receives its configuration as parameters instead of reading globals/env vars implicitly, returns an error `main` can handle however it likes, and can be called with a fake DSN (or skipped and replaced with a mock `*sql.DB`) in tests. `init()` is fine for genuinely trivial, order-independent registration (e.g. registering a driver via a blank import); reach for an explicit function for anything with a real failure mode.

**Design principle:** Prefer explicit initialization (constructors called from `main`) over implicit initialization (`init()`) whenever setup can fail, needs configuration, or needs to be testable in isolation.

---

## 3. Variable Shadowing With `:=`

**The Problem:** `:=` inside an `if`/`for`/`switch` header (or any nested block) creates new variables scoped to that block whenever at least one name on the left is new — including, classically, `err`. If you already have an outer `err` and use `:=` again inside a nested block, you silently get a second, independent `err` that shadows the outer one for the rest of that block, and the outer `err` never gets updated.

**❌ Bad**
```go
func process() error {
	var err error

	if val, err := doSomething(); err != nil { // BUG: := declares a NEW err, shadowing the outer one
		log.Println(val)
	}

	return err // always nil -- the outer err was never assigned
}

func doSomething() (int, error) {
	return 0, errors.New("boom")
}
```

**Why it's wrong:**
- Because `val` is new, `:=` is legal and creates *both* `val` and a new `err` scoped to the `if` statement — the new `err` shadows the outer `err` for the duration of the `if`, and the assignment to it never touches the outer variable.
- `process()` returns `nil` even though `doSomething()` returned a real error — the caller sees success for an operation that actually failed, silently, with no compiler warning (`go vet -shadow` can catch this, but it isn't part of the default `go vet` checks run by `go test`).

**✅ Good**
```go
func process() error {
	var err error
	var val int

	val, err = doSomething() // = reuses the existing outer err, no new variable
	if err != nil {
		log.Println(val)
		return err
	}

	return nil
}
```

**Why it works / Explanation:** Using `=` instead of `:=` assigns to the existing outer `val` and `err` rather than declaring new ones, so a failure in `doSomething()` is visible through the same `err` the function ultimately returns. When you don't need the outer variable to persist across the block, an alternative fix is to simply `return err` immediately inside the `if`, so shadowing never has a chance to hide anything.

**Design principle:** Be deliberate about `:=` vs `=` any time you're reusing a variable name in a nested scope — `:=` shadows if the name already exists in an outer scope and at least one other name on the left is new; `=` never does.

---

## 4. Integer Overflow Wraps Silently

**The Problem:** Converting an `int` (or `int64`) to a smaller fixed-size type (`int32`, `int16`, `int8`), or letting arithmetic on a fixed-size integer type exceed its range, wraps around silently in Go — there is no runtime panic, no error, just a different (often negative, often nonsensical) number. This is a serious risk anywhere a size, count, or monetary amount crosses a narrower integer type.

**❌ Bad**
```go
func toInt32(n int) int32 {
	return int32(n) // BUG: silently wraps if n doesn't fit in 32 bits
}

func main() {
	big := 3_000_000_000 // fits fine in a 64-bit int
	fmt.Println(toInt32(big)) // -1294967296 -- wrapped, not an error
}
```

**Why it's wrong:**
- `3,000,000,000` exceeds `math.MaxInt32` (2,147,483,647), so the conversion wraps modulo 2³² and produces a negative number with no indication anything went wrong — no panic, no error return, nothing in the type system stops this from compiling and running "successfully."
- This is exactly the kind of bug that hides in normal testing (small numbers) and only appears once real volume — a large row count, a big file size in bytes, a large monetary total in cents — pushes a value past the narrower type's range in production.

**✅ Good**
```go
func toInt32(n int) (int32, error) {
	if n > math.MaxInt32 || n < math.MinInt32 {
		return 0, fmt.Errorf("value %d overflows int32", n)
	}
	return int32(n), nil
}
```

**Why it works / Explanation:** Explicitly bounds-checking against `math.MaxInt32`/`math.MinInt32` before narrowing turns a silent wraparound into a caught, reportable error. Where possible, an even simpler fix is to not narrow at all — keep using `int`/`int64` for counts, sizes, and money throughout, and only narrow at a boundary (e.g. a wire format) that documents the narrower range as a deliberate constraint.

**Design principle:** Treat integer narrowing as a fallible operation (bounds-check it) rather than a free conversion — Go, unlike some languages, does not check arithmetic overflow for you at runtime.

---

## 5. Floating-Point Equality Comparison

**The Problem:** `float64` (and `float32`) values are binary approximations of decimal numbers, so arithmetic that's exact on paper often isn't exact in floating-point representation. Comparing the results with `==` after any arithmetic is a bug waiting to happen, because "mathematically equal" and "bit-for-bit identical" are different things for floats.

**❌ Bad**
```go
func main() {
	a := 0.1 + 0.2
	b := 0.3
	fmt.Println(a == b) // false
	fmt.Println(a)       // 0.30000000000000004
}
```

**Why it's wrong:**
- `0.1`, `0.2`, and `0.3` don't have exact binary floating-point representations, so `0.1 + 0.2` accumulates a tiny rounding error and lands on a value that's extremely close to, but not bit-identical to, `0.3`'s own (also approximate) representation.
- Any code that gates behavior on exact float equality after arithmetic — "if the running total equals the expected total, we're done" — will intermittently fail in ways that depend on the exact sequence of operations, making the bug look flaky and hard to reproduce.

**✅ Good**
```go
const epsilon = 1e-9

func almostEqual(a, b float64) bool {
	return math.Abs(a-b) < epsilon
}

func main() {
	a := 0.1 + 0.2
	b := 0.3
	fmt.Println(almostEqual(a, b)) // true
}
```

**Why it works / Explanation:** Comparing `|a - b|` against a small tolerance (`epsilon`) accepts results that are "close enough" given expected floating-point rounding error, rather than demanding bit-for-bit equality. Choose `epsilon` relative to the scale of the values you're comparing — a fixed `1e-9` is fine for small numbers but may be too tight or too loose for very large or very small magnitudes.

**Design principle:** Never use `==` on floats that have been through arithmetic — use an epsilon-based (or scale-relative) comparison, and for money specifically, prefer integer minor units (cents) instead of floats in the first place.

---

## 6. Switch Statements Don't Fall Through by Default

**The Problem:** Unlike C, Java, or JavaScript, Go's `switch` does not fall through to the next case automatically — each case's body runs and then the switch exits, full stop. Falling through requires the explicit `fallthrough` keyword. Developers porting logic from a fallthrough-by-default language routinely get surprised by this in both directions: forgetting `fallthrough` where they meant to use it, or being confused about why an extra unnecessary `break` isn't needed.

**❌ Bad**
```go
func Describe(status int) string {
	switch status {
	case 200:
		return "OK"
	case 201:
	case 202:
		return "Accepted"
		// BUG: someone porting C-style logic expected 201 to "fall into"
		// 202's return. It doesn't -- case 201's body is empty, so status
		// 201 falls out of the switch entirely and hits the line below.
	}
	return "Unknown"
}

func main() {
	fmt.Println(Describe(201)) // "Unknown", not "Accepted"
}
```

**Why it's wrong:**
- Go's `case 201:` with an empty body simply does nothing and exits the switch — it does not continue on to `case 202`'s body the way the same shape of code would in C or Java.
- The bug is silent: no compiler warning, no panic, just a wrong return value for exactly the input (`201`) the author thought they'd handled.

**✅ Good**
```go
func Describe(status int) string {
	switch status {
	case 200:
		return "OK"
	case 201:
		fallthrough // explicitly opt into running case 202's body too
	case 202:
		return "Accepted"
	}
	return "Unknown"
}

func main() {
	fmt.Println(Describe(201)) // "Accepted"
}
```

**Why it works / Explanation:** `fallthrough` is Go's explicit, opt-in mechanism for the C-style behavior — it unconditionally transfers control into the next case's body regardless of that case's own condition. Because it must be written out, every fallthrough in a Go switch is a deliberate decision visible in the source, rather than an accident of omitting a `break`.

**Design principle:** Go inverts the C-family default on purpose (no fallthrough unless asked for) — when porting logic from another language, audit every `switch` for cases that relied on implicit fallthrough and add `fallthrough` explicitly where needed.

---

## 7. Labeled `break`/`continue` for Nested Loops

**The Problem:** A bare `break` or `continue` inside nested loops (or inside a `switch`/`select` that itself sits inside a loop) only affects the innermost enclosing `for`/`switch`/`select` — not any outer loop you might have intended to stop. Reaching for a label is Go's mechanism for controlling an outer loop from an inner block.

**❌ Bad**
```go
func main() {
	for i := 0; i < 3; i++ {
		for j := 0; j < 3; j++ {
			if j == 1 {
				break // BUG: only breaks the inner (j) loop; i keeps looping
			}
			fmt.Println(i, j)
		}
	}
	// prints (0,0) (1,0) (2,0) -- the outer loop ran to completion,
	// which may not have been the intent if the goal was "stop everything"
}
```

**Why it's wrong:**
- `break` binds to the nearest enclosing `for`, `switch`, or `select` — here, the inner `for j` loop — so control returns to the outer `for i` loop's next iteration instead of exiting both loops.
- This is easy to miss in code review because the code reads naturally as "break out of the loop," and it's only wrong if the author's intent was actually "break out of *all* the loops," which the syntax alone doesn't disambiguate.

**✅ Good**
```go
func main() {
outer:
	for i := 0; i < 3; i++ {
		for j := 0; j < 3; j++ {
			if i == 1 && j == 1 {
				break outer // exits the i loop directly
			}
			fmt.Println(i, j)
		}
	}
	// prints (0,0) (0,1) (0,2) (1,0) then stops
}
```

**Why it works / Explanation:** A label (`outer:`) placed immediately before the outer `for` gives `break`/`continue` a specific target to name — `break outer` unwinds all the way out of the labeled loop, regardless of how many loops are nested inside it. The same mechanism works for `continue outer` to skip to the next iteration of the outer loop from deep inside a nested block.

**Design principle:** Whenever "break/continue the outer loop" is the actual intent, say so explicitly with a label — don't rely on a bare `break` and hope the nesting happens to do what you want.

---

## 8. `os.Exit()` (and `log.Fatal`) Skip All Deferred Calls

**The Problem:** `os.Exit()` terminates the process immediately, without running any pending `defer` anywhere in the program — not just in the current function, but in every goroutine. `log.Fatal`/`log.Fatalf` call `os.Exit(1)` internally after logging, so they have exactly the same effect. Any cleanup you were counting on a `defer` to run (flushing buffers, closing files, releasing a lock) simply never happens.

**❌ Bad**
```go
func processFile(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close() // registered

	lock.Lock()
	defer lock.Unlock() // registered

	data, err := io.ReadAll(f)
	if err != nil {
		log.Fatalf("read failed: %v", err)
		// BUG: os.Exit(1) inside log.Fatalf skips BOTH deferred calls above --
		// the lock is never released and the file descriptor is never
		// explicitly closed (the OS reclaims it on process exit, but any
		// external resource, like a distributed lock, is not released cleanly)
	}
	return process(data)
}
```

**Why it's wrong:**
- `log.Fatalf` doesn't return — it logs and then calls `os.Exit(1)` in the same call, so control never reaches the `defer f.Close()`/`defer lock.Unlock()` unwind that would normally happen on a `return`.
- For an in-process mutex this "just" leaks a lock the process is about to end anyway, but for anything backed by an external resource — a file lock, a distributed lock in Redis/etcd, a database advisory lock, a half-written temp file — skipping cleanup can leave that external state stuck for other processes long after this one is gone.

**✅ Good**
```go
func processFile(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()

	lock.Lock()
	defer lock.Unlock()

	data, err := io.ReadAll(f)
	if err != nil {
		return fmt.Errorf("read failed: %w", err) // let the caller decide what to do
	}
	return process(data)
}

func main() {
	if err := processFile("data.txt"); err != nil {
		log.Fatal(err) // fine here: main is the top of the call stack, nothing else is pending
	}
}
```

**Why it works / Explanation:** Returning the error lets `processFile`'s own deferred cleanup run normally as the function unwinds, exactly as intended. `log.Fatal`/`os.Exit` are then only ever called from `main` (or very close to it), at a point where no other function still has pending deferred cleanup that would be skipped.

**Design principle:** Reserve `log.Fatal`/`os.Exit` for `main` (or `main`-adjacent top-level code) — everywhere else, return an error and let the call stack unwind normally so every `defer` gets its chance to run.

---

## 9. Global Mutable Package-Level State

**The Problem:** Package-level `var`s that get mutated at runtime create hidden coupling between call sites that have no direct relationship to each other, and — notoriously — cause test pollution: tests that pass individually but fail (or pass only by accident of ordering) when run together via `go test ./...`, because `go test` runs every test function in a package sequentially in the same process, sharing the same package-level state.

**❌ Bad**
```go
var cache = map[string]string{}

func Get(key string) string { return cache[key] }
func Set(key, val string)    { cache[key] = val }

func TestGetDefault(t *testing.T) {
	Set("mode", "test")
	if got := Get("mode"); got != "test" {
		t.Fatalf("got %q", got)
	}
}

func TestGetEmpty(t *testing.T) {
	// BUG: passes if run alone (go test -run TestGetEmpty), but fails
	// when the full suite runs, because TestGetDefault already set
	// "mode" in the shared global cache.
	if got := Get("mode"); got != "" {
		t.Fatalf("expected empty, got %q", got)
	}
}
```

**Why it's wrong:**
- Both tests mutate and read the same package-level `cache` map — there's no isolation between them, so execution order (which `go test` doesn't guarantee stays fixed as tests are added, reordered, or run with `-shuffle`) determines whether `TestGetEmpty` sees a clean cache or one already populated by another test.
- The same coupling exists in production, not just in tests: any two unrelated request handlers that both touch `cache` are implicitly coupled through it, making it hard to reason about either one in isolation, and impossible to run two independent instances of the logic (e.g. for two different tenants) in the same process.

**✅ Good**
```go
type Store struct {
	mu    sync.Mutex
	cache map[string]string
}

func NewStore() *Store {
	return &Store{cache: make(map[string]string)}
}

func (s *Store) Get(key string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cache[key]
}

func (s *Store) Set(key, val string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cache[key] = val
}

func TestGetEmpty(t *testing.T) {
	store := NewStore() // each test gets its own, independent state
	if got := store.Get("mode"); got != "" {
		t.Fatalf("expected empty, got %q", got)
	}
}
```

**Why it works / Explanation:** Moving the mutable state into a struct constructed via `NewStore()` — dependency injection via constructor parameters instead of a package global — means every caller (including every test) gets its own independent instance, with no shared state to leak between them. Production code benefits the same way: two independently configured `Store`s can coexist in the same process without stepping on each other.

**Design principle:** Prefer dependency injection (explicit state passed to or constructed by the caller) over package-level mutable globals — it's the difference between state you can isolate per-test/per-tenant and state you're stuck sharing everywhere.

---

## 10. Unkeyed Struct Literals Break Silently on Field Reordering

**The Problem:** A positional (unkeyed) struct literal like `Point{1, 2}` compiles today and assigns fields by position. If a later commit reorders the struct's field declarations — for any reason, including an unrelated cleanup — every existing unkeyed literal silently starts assigning the *same* values to *different* fields, with no compiler error, as long as the field types still line up.

**❌ Bad**
```go
type Point struct {
	X int
	Y int
}

func main() {
	p := Point{10, 20} // positional: X=10, Y=20
	fmt.Println(p.X, p.Y) // 10 20
}
```
Months later, someone reorders the fields (e.g. while alphabetizing, or grouping related fields):
```go
type Point struct {
	Y int // BUG: reordered -- no compiler error, since X and Y are both int
	X int
}
```

**Why it's wrong:**
- After the reorder, `Point{10, 20}` now assigns `Y=10, X=20` — exactly swapped from the original intent — and because both fields are the same type (`int`), the compiler has no basis to object; the code compiles and runs, just with silently wrong values.
- This is especially dangerous because the two changes (the literal, and the field reorder) can be made by different people, in different commits, months apart, with neither one aware they're interacting — there's no local signal at either change site that anything is wrong.

**✅ Good**
```go
type Point struct {
	X int
	Y int
}

func main() {
	p := Point{X: 10, Y: 20} // keyed: immune to field reordering
	fmt.Println(p.X, p.Y) // 10 20
}
```

**Why it works / Explanation:** A keyed literal binds each value to a field by name, not position, so reordering the struct's field declarations has zero effect on what a keyed literal assigns. `go vet`'s composite-literal check partially helps here — it flags unkeyed literals for struct types imported from *other* packages by default — but it does not flag unkeyed literals for structs defined in the same package, so it's not a substitute for the habit of always keying struct literals yourself.

**Design principle:** Default to keyed struct literals everywhere, especially for structs outside the current file/package — it costs a few extra characters and makes the code immune to an entire class of silent, compiler-invisible bugs.

---

## Key Takeaways
- Never reorder or insert into the middle of a persisted `iota`-based enum — append only, or use string-based enums for anything that outlives a process.
- Prefer explicit constructor functions called from `main` over `init()` for anything that can fail or needs configuration.
- Watch for `:=` shadowing an outer variable (classically `err`) inside nested `if`/`for` blocks; use `=` when you mean to reuse the outer variable.
- Integer narrowing conversions and fixed-size arithmetic wrap silently on overflow — bounds-check with `math.MaxInt32`/`MinInt32` or use a wider type.
- Never compare `float64`/`float32` with `==` after arithmetic — use an epsilon-based comparison.
- Go's `switch` does not fall through by default — use the explicit `fallthrough` keyword when you need C-style behavior.
- A bare `break`/`continue` only affects the innermost loop/switch/select — use a label to control an outer loop.
- `os.Exit()` and `log.Fatal` skip all pending `defer`s across the whole program — reserve them for `main`, return errors everywhere else.
- Package-level mutable globals create hidden coupling and test pollution across `go test ./...` runs — inject state via constructors instead.
- Unkeyed struct literals silently break if fields are reordered later — always use keyed literals, especially across package boundaries.

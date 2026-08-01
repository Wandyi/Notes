# Testing Practices

Go's testing tools are minimal by design — `testing.T`, `t.Run`, and not much else — which means the discipline around how you use them matters more than in frameworks that enforce structure for you. Bad testing habits don't fail the build; they quietly erode trust in the test suite itself, until "just re-run it" becomes a normal response to CI failures. This file covers the practices that turn a Go test suite from a safety net into a source of flakiness, false confidence, and brittle coupling to implementation details.

## 1. Not Using Table-Driven Tests

**The Problem:** Writing a separate, near-identical test function for every input/output case duplicates setup and assertion logic across the file. Adding a new case means copy-pasting a whole function instead of adding one line, and a failure just says "TestAddNegative failed" instead of naming which specific input broke.

**❌ Bad**
```go
func TestAddPositive(t *testing.T) {
	if got := Add(2, 3); got != 5 {
		t.Errorf("Add(2, 3) = %d, want 5", got)
	}
}

func TestAddNegative(t *testing.T) {
	if got := Add(-2, -3); got != -5 {
		t.Errorf("Add(-2, -3) = %d, want -5", got)
	}
}

func TestAddZero(t *testing.T) {
	if got := Add(0, 0); got != 0 {
		t.Errorf("Add(0, 0) = %d, want 0", got)
	}
}
```

**Why it's wrong:**
- Three functions that differ only in their input/expected values — adding a fourth case means writing a whole new function rather than a single new row of data.
- Nothing ties the three cases together as "the same test, different inputs," so there's no single place to see the full behavior contract of `Add` at a glance.

**✅ Good**
```go
func TestAdd(t *testing.T) {
	cases := []struct {
		name string
		a, b int
		want int
	}{
		{"positive", 2, 3, 5},
		{"negative", -2, -3, -5},
		{"zero", 0, 0, 0},
		{"mixed sign", -2, 3, 1},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Add(tc.a, tc.b); got != tc.want {
				t.Errorf("Add(%d, %d) = %d, want %d", tc.a, tc.b, got, tc.want)
			}
		})
	}
}
```

**Why it works / Explanation:** Adding a new case is now a one-line addition to the `cases` slice, and `t.Run(tc.name, ...)` gives every case its own named subtest — failures report as `TestAdd/mixed_sign`, and `go test -run TestAdd/negative` can target a single case directly. The table itself doubles as readable documentation of the function's behavior across its input space.

**Design principle:** Separate test *data* from test *logic* — one assertion body driven by a table scales better than N copies of the same body with different literals baked in.

---

## 2. `t.Parallel()` Misuse: Shared State and Loop Variable Capture

**The Problem:** Marking subtests parallel with `t.Parallel()` is only safe if they don't share mutable state — and in table-driven tests written for Go versions before 1.22, the classic loop-variable-capture bug means every parallel subtest closure can end up referencing the *same* underlying loop variable, so by the time they actually run, several of them see the last table entry instead of "their own."

**❌ Bad**
```go
func TestValidate(t *testing.T) {
	cases := []struct {
		name    string
		in      string
		wantErr bool
	}{
		{"empty", "", true},
		{"too_long", strings.Repeat("x", 1000), true},
		{"valid", "ok", false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel() // BUG (pre-Go 1.22): tc is captured by reference, not by value
			err := Validate(tc.in)
			if (err != nil) != tc.wantErr {
				t.Errorf("Validate(%q) error = %v, wantErr %v", tc.in, err, tc.wantErr)
			}
		})
	}
}
```

**Why it's wrong:**
- On Go versions before 1.22, `tc` is one variable reused across every loop iteration; `t.Run` starts the subtest but `t.Parallel()` immediately pauses it until the parent test function finishes its loop and calls `t.Run` for every case — by the time the parallel subtests actually execute their bodies, `tc` may have already advanced to the final entry in `cases`, so multiple subtests silently test the same (usually last) input under different subtest names.
- The same category of bug shows up whenever "parallel" tests share any mutable package-level variable, global cache, or shared fixture — two tests that look independent race on the shared state and fail nondeterministically depending on scheduling, which is exactly the kind of failure that's nearly impossible to reproduce locally and shows up only intermittently in CI.

**✅ Good**
```go
func TestValidate(t *testing.T) {
	cases := []struct {
		name    string
		in      string
		wantErr bool
	}{
		{"empty", "", true},
		{"too_long", strings.Repeat("x", 1000), true},
		{"valid", "ok", false},
	}

	for _, tc := range cases {
		tc := tc // capture a per-iteration copy (redundant, but explicit, on Go 1.22+)
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			err := Validate(tc.in)
			if (err != nil) != tc.wantErr {
				t.Errorf("Validate(%q) error = %v, wantErr %v", tc.in, err, tc.wantErr)
			}
		})
	}
}
```

**Why it works / Explanation:** `tc := tc` inside the loop body creates a fresh variable scoped to that single iteration, so each subtest closure captures its own independent copy regardless of how the outer loop continues — this line is unnecessary on Go 1.22+ (where each `for` iteration already gets its own variable) but remains a safe, explicit habit that also protects against related capture bugs and works correctly on every Go version a codebase might still build with. For shared state beyond the loop variable, the fix is the same principle at a larger scale: give each parallel subtest its own instance of anything mutable instead of pointing multiple goroutines at one shared value.

**Design principle:** "Parallel" tests are only actually independent if nothing they touch — loop variables included — is shared mutable state; if two tests can race, they can fail nondeterministically.

---

## 3. Testing Implementation Details Instead of Observable Behavior

**The Problem:** Asserting on private internal state (unexported fields, call counters, cache contents) instead of the function's actual input/output contract makes tests brittle — they break the moment someone refactors the implementation, even when the observable behavior hasn't changed at all, which trains people to distrust or ignore test failures.

**❌ Bad**
```go
type Calculator struct {
	cache     map[int]int
	callCount int
}

func (c *Calculator) Square(n int) int {
	c.callCount++
	if v, ok := c.cache[n]; ok {
		return v
	}
	result := n * n
	c.cache[n] = result
	return result
}

func TestSquare_Brittle(t *testing.T) {
	c := &Calculator{cache: map[int]int{}}
	c.Square(4)

	if c.callCount != 1 { // BUG: asserting on a private implementation detail
		t.Errorf("callCount = %d, want 1", c.callCount)
	}
	if len(c.cache) != 1 { // BUG: coupling the test to caching being implemented this way at all
		t.Errorf("cache size = %d, want 1", len(c.cache))
	}
}
```

**Why it's wrong:**
- If `Calculator` is later refactored to cache differently (a different map key scheme, an LRU eviction, or dropping caching altogether in favor of memoizing elsewhere), this test breaks — even though `Square(4)` still correctly returns `16` every time, which is the only thing callers actually depend on.
- The test provides false precision: it looks like it's verifying caching behavior, but it's really just pinned to today's field names and internal data structure, which is exactly the kind of test that gets deleted or `//nolint`'d in frustration during a refactor instead of updated to reflect the actual contract.

**✅ Good**
```go
func TestSquare_Behavior(t *testing.T) {
	c := &Calculator{cache: map[int]int{}}

	if got := c.Square(4); got != 16 {
		t.Errorf("Square(4) = %d, want 16", got)
	}
	// Calling it again should still return the same, correct result —
	// this tests the *contract* ("Square is idempotent and correct"),
	// not *how* that contract happens to be implemented internally.
	if got := c.Square(4); got != 16 {
		t.Errorf("Square(4) second call = %d, want 16", got)
	}
}
```

**Why it works / Explanation:** The rewritten test only asserts what any caller of `Square` actually depends on — that it returns the correct result, consistently. It survives any internal refactor (removing the cache, changing its data structure, adding metrics) as long as the public behavior stays correct, which is exactly the property a good test should have: it should fail when behavior actually breaks, and stay green through changes that don't affect behavior.

**Design principle:** Test the public contract, not the implementation — a test suite coupled to internals actively resists refactoring instead of enabling it.

---

## 4. Global/Package-Level State Pollution Between Tests

**The Problem:** A package-level variable, cache, or shared `flag` mutated by one test silently affects every test that runs after it in the same process — a bug that's often completely invisible running a single test in isolation (`go test -run TestFoo`) and only appears when the full suite runs together (`go test ./...`), because test execution order determines whether the pollution has "already happened" by the time a given test runs.

**❌ Bad**
```go
var cache = map[string]string{} // package-level, shared by every test in this file

func Lookup(key string) string {
	return cache[key]
}

func TestLookupA(t *testing.T) {
	cache["x"] = "a"
	if got := Lookup("x"); got != "a" {
		t.Errorf("got %q, want %q", got, "a")
	}
	// BUG: cache["x"] is never removed — it stays "a" for the rest of the test binary's life
}

func TestLookupDefault(t *testing.T) {
	// Passes if run alone. Fails if TestLookupA already ran in this process,
	// because cache["x"] is now "a" instead of absent.
	if got := Lookup("x"); got != "" {
		t.Errorf("expected empty default, got %q", got)
	}
}
```

**Why it's wrong:**
- `go test -run TestLookupDefault` passes every time in isolation, giving false confidence, while `go test ./...` (the version that actually runs in CI) fails or passes depending on test execution order — which is exactly the kind of flakiness that erodes trust in a suite ("just re-run CI, it's probably nothing").
- The dependency between the two tests is completely invisible from reading either test function on its own; you have to know that both touch the same package-level `cache` to even suspect the coupling exists.

**✅ Good**
```go
type LookupService struct {
	cache map[string]string
}

func NewLookupService() *LookupService {
	return &LookupService{cache: map[string]string{}}
}

func (s *LookupService) Lookup(key string) string {
	return s.cache[key]
}

func TestLookupA(t *testing.T) {
	svc := NewLookupService() // fresh instance, no shared state with other tests
	svc.cache["x"] = "a"
	if got := svc.Lookup("x"); got != "a" {
		t.Errorf("got %q, want %q", got, "a")
	}
}

func TestLookupDefault(t *testing.T) {
	svc := NewLookupService()
	if got := svc.Lookup("x"); got != "" {
		t.Errorf("expected empty default, got %q", got)
	}
}
```

**Why it works / Explanation:** Replacing the package-level global with an instance constructed fresh per test removes the shared state entirely — there's nothing left for one test to leave behind that another could observe. When a global genuinely can't be removed (e.g. it belongs to a third-party package), the fallback is explicit setup/teardown with `t.Cleanup` to restore it, so any mutation is scoped to the single test that made it: `original := cache["x"]; t.Cleanup(func() { cache["x"] = original })`.

**Design principle:** Tests should be independent and order-agnostic — dependency injection (or, failing that, disciplined cleanup) removes the hidden coupling that shared global state creates.

---

## 5. Manual Cleanup Instead of `t.Cleanup()`

**The Problem:** Cleanup code written at the bottom of a test function only runs if execution reaches that line — any `t.Fatal`, an unexpected `panic`, or an early `return` on the way there skips it, silently leaking temp files, open connections, or background goroutines every time the test fails partway through, which is precisely when you can least afford a leak (during a failing, possibly-looping CI run).

**❌ Bad**
```go
func TestProcessFile_Manual(t *testing.T) {
	f, err := os.CreateTemp("", "test-*.txt")
	if err != nil {
		t.Fatal(err)
	}

	result, err := Process(f.Name())
	if err != nil {
		t.Fatalf("Process failed: %v", err) // BUG: os.Remove below never runs
	}
	if result != "ok" {
		t.Errorf("got %q, want %q", result, "ok") // t.Errorf doesn't stop, but a
		// preceding t.Fatalf on a different case would skip cleanup entirely
	}

	os.Remove(f.Name()) // only reached on the success path
}
```

**Why it's wrong:**
- The moment `Process` returns an error and the test calls `t.Fatalf`, the function stops executing immediately — `os.Remove(f.Name())` on the last line never runs, and the temp file is left on disk.
- In a large test suite this compounds: every test that fails and skips its manual cleanup leaves something behind, and over enough CI runs that's a slow accumulation of orphaned temp files, unclosed listeners, or leaked goroutines that nobody notices until disk space or file descriptor limits start causing unrelated failures.

**✅ Good**
```go
func TestProcessFile_Cleanup(t *testing.T) {
	f, err := os.CreateTemp("", "test-*.txt")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.Remove(f.Name()) }) // guaranteed to run, pass or fail or panic

	result, err := Process(f.Name())
	if err != nil {
		t.Fatalf("Process failed: %v", err) // cleanup still runs even though we bail here
	}
	if result != "ok" {
		t.Errorf("got %q, want %q", result, "ok")
	}
}
```

**Why it works / Explanation:** `t.Cleanup` registers a function that the testing framework guarantees to run when the test (or subtest) finishes, regardless of whether it finished by returning normally, calling `t.Fatal`, or panicking — so the cleanup is registered immediately after the resource is successfully created, right next to the code that created it, and doesn't depend on control flow reaching the bottom of the function.

**Design principle:** Register cleanup immediately adjacent to acquisition, using a mechanism the runtime guarantees to execute — don't rely on normal control flow reaching a cleanup statement that a failure can route around.

---

## 6. Not Using Subtests for Logically Distinct Cases

**The Problem:** A single flat test function that checks several unrelated conditions in sequence stops dead at the first `t.Fatal` — so if the first assertion fails, you never find out whether the second and third would have passed or failed too, turning one CI run into several rounds of "fix one, discover the next" instead of seeing every actual failure at once.

**❌ Bad**
```go
func TestParseConfig(t *testing.T) {
	if _, err := ParseConfig(""); err == nil {
		t.Fatal("expected error for empty input") // if this fails, the two checks below never run
	}
	if _, err := ParseConfig("not: valid: yaml:::"); err == nil {
		t.Fatal("expected error for malformed input")
	}
	if cfg, err := ParseConfig("name: svc\nport: 8080"); err != nil || cfg.Port != 8080 {
		t.Fatalf("valid config parsed incorrectly: cfg=%+v, err=%v", cfg, err)
	}
}
```

**Why it's wrong:**
- These are three logically independent cases (empty input, malformed input, valid input) sharing one test function — a failure in the first `t.Fatal` hides the results of the other two entirely for that run, so a single CI failure might represent one broken case or three, and you can't tell without fixing the first one and re-running.
- There's no way to run just "the malformed input case" via `go test -run` — the whole function is one atomic unit as far as the test runner is concerned.

**✅ Good**
```go
func TestParseConfig(t *testing.T) {
	t.Run("empty input errors", func(t *testing.T) {
		if _, err := ParseConfig(""); err == nil {
			t.Error("expected error for empty input")
		}
	})

	t.Run("malformed input errors", func(t *testing.T) {
		if _, err := ParseConfig("not: valid: yaml:::"); err == nil {
			t.Error("expected error for malformed input")
		}
	})

	t.Run("valid input parses correctly", func(t *testing.T) {
		cfg, err := ParseConfig("name: svc\nport: 8080")
		if err != nil || cfg.Port != 8080 {
			t.Errorf("valid config parsed incorrectly: cfg=%+v, err=%v", cfg, err)
		}
	})
}
```

**Why it works / Explanation:** Each `t.Run` block runs independently — a failure in one subtest doesn't prevent the others from executing and reporting their own pass/fail status, so a single test run surfaces every actually-broken case instead of just the first one encountered. Each subtest is also individually addressable with `go test -run TestParseConfig/malformed_input_errors`, which is useful when iterating on a fix for just one case.

**Design principle:** Give each logically distinct case its own subtest so failures are reported independently — one broken case shouldn't hide the status of unrelated cases.

---

## 7. `reflect.DeepEqual` Pitfalls in Test Assertions

**The Problem:** `reflect.DeepEqual` is stricter than most people expect when comparing "equivalent" values: a `nil` slice and an empty non-nil slice are not deeply equal, `NaN` is never deeply equal to `NaN` (it uses `==` for floats internally), and comparing structs with unexported fields from another package can produce confusing results tied to internal representation rather than logical equivalence.

**❌ Bad**
```go
func FilterPositive(nums []int) []int {
	var result []int // nil until something is appended
	for _, n := range nums {
		if n > 0 {
			result = append(result, n)
		}
	}
	return result
}

func TestFilterPositive(t *testing.T) {
	got := FilterPositive([]int{-1, -2})
	want := []int{} // empty, not nil

	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want) // FAILS: nil != []int{} under DeepEqual
	}
}
```

**Why it's wrong:**
- `FilterPositive([]int{-1, -2})` returns `nil` (the `var result []int` never gets appended to), and `reflect.DeepEqual(nil, []int{})` is `false` — both slices print as `[]` and are equally "empty" from a caller's perspective, but `DeepEqual` treats them as different values.
- This produces a confusing failure message (`got [], want []` — they *look* identical when printed with `%v`) that leaves whoever's debugging it unsure whether the code is actually broken or the test assertion is just too strict, wasting time on a non-bug.

**✅ Good**
```go
import (
	"github.com/google/go-cmp/cmp"
	"github.com/google/go-cmp/cmp/cmpopts"
)

func TestFilterPositive(t *testing.T) {
	got := FilterPositive([]int{-1, -2})
	want := []int{}

	if diff := cmp.Diff(want, got, cmpopts.EquateEmpty()); diff != "" {
		t.Errorf("FilterPositive() mismatch (-want +got):\n%s", diff)
	}
}
```

**Why it works / Explanation:** `cmpopts.EquateEmpty()` tells `cmp.Diff` to treat `nil` and empty slices/maps as equal, matching how most Go code actually treats them (as "no elements," full stop). `cmp.Diff` also produces a readable, field-by-field diff on failure instead of `DeepEqual`'s opaque boolean, and it panics by default on unexported fields rather than silently comparing their memory representation — forcing an explicit decision via `cmp.AllowUnexported(...)` or `cmpopts.IgnoreFields(...)` instead of getting a surprising result. For floating-point comparisons, `cmpopts.EquateNaNs()` and `cmpopts.EquateApprox(...)` handle the `NaN`/precision cases `DeepEqual` gets wrong by default.

**Design principle:** Prefer a comparison tool that lets you state your actual equivalence rules explicitly (`go-cmp` with options) over one with fixed, surprising built-in rules (`reflect.DeepEqual`) — and prefer diff-based failure output over a bare boolean whenever a mismatch needs debugging.

---

## 8. Flaky Tests From Sleep-Based Concurrency Assumptions

**The Problem:** Starting a goroutine and then asserting on its side effect after a hardcoded `time.Sleep` assumes the goroutine will always finish within that arbitrary window — true most of the time on a fast, idle machine, and false often enough under CI load, parallel test execution, or a slow shared runner, producing exactly the kind of intermittent failure that gets dismissed as "flaky, just re-run it" instead of fixed.

**❌ Bad**
```go
func TestAsyncProcessor(t *testing.T) {
	p := NewProcessor()
	var result string

	go func() {
		result = p.Process("input")
	}()

	time.Sleep(10 * time.Millisecond) // BUG: hopes the goroutine finished by now

	if result != "processed: input" {
		t.Errorf("got %q, want %q", result, "processed: input")
	}
}
```

**Why it's wrong:**
- On a fast, quiet machine 10ms is plenty of time and the test passes reliably — under CI load, with many tests running in parallel and contending for CPU, the goroutine can easily still be running when the sleep ends, producing a spurious failure that has nothing to do with an actual bug in `Process`.
- Reading and writing `result` from two goroutines with no synchronization between them (only a timing-based *hope* that the write happens before the read) is also a data race — `go test -race` will flag it even on runs where the timing happens to work out and the assertion passes.

**✅ Good**
```go
func TestAsyncProcessor(t *testing.T) {
	p := NewProcessor()
	done := make(chan string, 1)

	go func() {
		done <- p.Process("input")
	}()

	select {
	case result := <-done:
		if result != "processed: input" {
			t.Errorf("got %q, want %q", result, "processed: input")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for Process to complete")
	}
}
```

**Why it works / Explanation:** The channel receive blocks exactly until the goroutine actually sends its result — no arbitrary guess about how long that will take, and no data race, since the channel operation itself is the synchronization point between the two goroutines. The `time.After` branch is only a safety net against the goroutine genuinely hanging forever (a real bug), not the primary synchronization mechanism, so the test runs as fast as the real work allows on a fast machine and still fails deterministically (rather than hanging forever) if something is actually broken.

**Design principle:** Synchronize on the actual event you care about (a channel send, a `sync.WaitGroup`, a condition becoming true) — never on an arbitrary elapsed duration as a proxy for "probably done by now."

---

## 9. Not Testing Error Paths

**The Problem:** A test suite that only exercises the happy path gives no signal at all about whether error handling actually works — a regression that breaks "return an error on invalid input" (e.g. someone removes a validation check, or a refactor swallows an error) can ship straight through a green test suite if no test ever calls the function with input that's supposed to fail.

**❌ Bad**
```go
func TestParseAge(t *testing.T) {
	got, err := ParseAge("25")
	if err != nil {
		t.Fatal(err)
	}
	if got != 25 {
		t.Errorf("got %d, want 25", got)
	}
	// BUG: no case ever calls ParseAge with invalid input — the error path
	// (negative ages, non-numeric strings, empty input) is completely untested
}
```

**Why it's wrong:**
- If a future change accidentally makes `ParseAge("-5")` return `(-5, nil)` instead of an error, or makes `ParseAge("abc")` return `(0, nil)` instead of failing, this suite stays green — there is no assertion anywhere that would notice.
- Error-handling code paths are exactly the code most likely to atrophy silently: they're exercised rarely in manual testing (nobody manually tries to break things as often as they use the happy path) and, without a test enforcing them, tend to rot the first time someone refactors nearby code without realizing an error case depends on it.

**✅ Good**
```go
func TestParseAge(t *testing.T) {
	cases := []struct {
		name    string
		in      string
		want    int
		wantErr bool
	}{
		{"valid", "25", 25, false},
		{"negative", "-5", 0, true},
		{"non-numeric", "abc", 0, true},
		{"empty", "", 0, true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ParseAge(tc.in)
			if (err != nil) != tc.wantErr {
				t.Fatalf("ParseAge(%q) error = %v, wantErr %v", tc.in, err, tc.wantErr)
			}
			if !tc.wantErr && got != tc.want {
				t.Errorf("ParseAge(%q) = %d, want %d", tc.in, got, tc.want)
			}
		})
	}
}
```

**Why it works / Explanation:** Adding explicit error-case rows to the same table used for the happy path costs almost nothing (each is one more line) and directly encodes the contract "these inputs must fail" as an active, enforced assertion — if a future change breaks any of them, this test fails immediately instead of the regression slipping through unnoticed. Table-driven tests make this cheap enough that there's rarely a good excuse not to include the error cases alongside the success cases.

**Design principle:** A function's error-returning behavior is part of its contract just as much as its success behavior — test both, or the untested half will eventually regress silently.

---

## Key Takeaways
- Duplicated near-identical test functions should be table-driven — one assertion body, one row per case, named subtests for free.
- `t.Parallel()` subtests must not share mutable state, including a table-driven loop variable on pre-1.22 Go — shadow the loop variable and avoid shared globals.
- Assert on observable input/output behavior, not private fields or internal call counts — brittle tests that break on harmless refactors erode trust in the suite.
- Package-level state mutated by one test silently leaks into later tests in the same binary — prefer dependency injection over shared globals, or clean up explicitly.
- Manual end-of-function cleanup gets skipped on `t.Fatal`/panic — use `t.Cleanup()` so cleanup always runs.
- A flat test function stops at the first failure and hides the status of later checks — split logically distinct cases into `t.Run` subtests.
- `reflect.DeepEqual` treats nil vs. empty slices, NaN, and unexported fields in surprising ways — prefer `go-cmp`'s `cmp.Diff` with explicit options.
- Sleep-based synchronization in tests is inherently racy and flaky under load — synchronize on channels, `sync.WaitGroup`, or polling with a timeout instead.
- A test suite with no error-path assertions can't catch error-handling regressions — add explicit error-case rows alongside the happy path.

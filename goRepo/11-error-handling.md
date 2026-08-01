# Error Handling: Production Pitfalls

Go's explicit `error` return values put error handling directly in the reader's face — which is a strength, but only if every error is actually checked, wrapped, and compared correctly. In production systems, getting this wrong shows up as silent data corruption, crashes that should have been graceful HTTP 4xx responses, and multi-day debugging sessions caused by an error message that reads fine but has lost the structured information a caller needed to react to it. This file covers the mistakes that are easy to write, easy to miss in review, and expensive to discover only after they've shipped.

## 1. Ignoring errors by assigning to _

**The Problem:** Discarding an error with `_` feels harmless when you're prototyping, but in real code paths — especially around database iteration — the discarded error is often the only signal that something went wrong partway through an operation that otherwise "looks" successful.

**❌ Bad**
```go
func loadNames(db *sql.DB) ([]string, error) {
	rows, _ := db.Query("SELECT name FROM users") // BUG: query error ignored
	defer rows.Close()

	var names []string
	for rows.Next() {
		var name string
		rows.Scan(&name) // BUG: scan error ignored
		names = append(names, name)
	}
	return names, nil // BUG: rows.Err() never checked
}
```

**Why it's wrong:**
- If `db.Query` fails, `rows` is `nil`; the very next call, `rows.Next()`, panics with a nil pointer dereference instead of returning the graceful error the caller expected.
- `for rows.Next()` also exits when iteration is stopped early by a dropped connection or read error, not only when rows are exhausted — without checking `rows.Err()` after the loop, that failure is indistinguishable from "no more rows," and the function returns a truncated result set as if it succeeded.

**✅ Good**
```go
func loadNames(db *sql.DB) ([]string, error) {
	rows, err := db.Query("SELECT name FROM users")
	if err != nil {
		return nil, fmt.Errorf("querying users: %w", err)
	}
	defer rows.Close()

	var names []string
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, fmt.Errorf("scanning row: %w", err)
		}
		names = append(names, name)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterating rows: %w", err)
	}
	return names, nil
}
```

**Why it works / Explanation:** Every error-returning call is checked at the point it happens, and `rows.Err()` is checked after the loop specifically because `Next()` returning `false` is ambiguous between "done" and "failed" — only `Err()` disambiguates the two.

**Design principle:** Treat `_` for an error as a deliberate, rare decision that should be obvious and justified in context — not a default way to keep a line short.

---

## 2. Comparing errors with == instead of errors.Is

**The Problem:** Sentinel error comparison with `==` only works if the exact same error value is returned unmodified. The moment any layer in the call chain wraps that error (even for something as reasonable as adding context with `fmt.Errorf("...: %w", err)`), a plain `==` check breaks silently — it just always evaluates to `false`.

**❌ Bad**
```go
var ErrNotFound = errors.New("not found")

func findUser(id int) error {
	return fmt.Errorf("findUser %d: %w", id, ErrNotFound)
}

func main() {
	err := findUser(42)
	if err == ErrNotFound { // BUG: always false once wrapped
		fmt.Println("not found")
	} else {
		fmt.Println("unknown error:", err) // runs, incorrectly
	}
}
```

**Why it's wrong:**
- `err` here is a `*fmt.wrapError` value, not `ErrNotFound` itself — `==` compares them as different values even though `ErrNotFound` is right there in the chain, reachable via `Unwrap()`.
- The bug is invisible in the error message (it still prints something reasonable-looking) and only manifests as the wrong branch of application logic running — often noticed only when a "not found" is mishandled as a 500 instead of a 404.

**✅ Good**
```go
func main() {
	err := findUser(42)
	if errors.Is(err, ErrNotFound) {
		fmt.Println("not found")
	} else {
		fmt.Println("unknown error:", err)
	}
}
```

**Why it works / Explanation:** `errors.Is` walks the chain of wrapped errors via each error's `Unwrap()` method, comparing against the target at every level — so it finds `ErrNotFound` no matter how many layers of `%w` wrapping sit on top of it.

**Design principle:** Never compare errors with `==` unless you control every layer between the point of creation and the point of comparison and can guarantee no wrapping ever happens; default to `errors.Is`/`errors.As`.

---

## 3. Losing error context with %v instead of %w

**The Problem:** `fmt.Errorf` with `%v` and `%w` produce messages that can look byte-for-byte identical, but only `%w` makes the wrapped error retrievable via `Unwrap()`. Using `%v` "for the error part" of a message quietly breaks every `errors.Is`/`errors.As` check further up the call stack.

**❌ Bad**
```go
func loadConfig(path string) error {
	_, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("loading config from %s: %v", path, err) // BUG: %v drops the chain
	}
	return nil
}

func main() {
	err := loadConfig("/etc/app/config.yaml")
	if errors.Is(err, os.ErrNotExist) {
		fmt.Println("config file missing, using defaults")
	} else if err != nil {
		log.Fatal(err)
	}
}
```

**Why it's wrong:**
- `errors.Is(err, os.ErrNotExist)` returns `false` even when the underlying cause genuinely is a missing file, because `%v` stringifies the original error into the message text instead of preserving it as something `Unwrap()` can return.
- The printed error message is identical either way ("loading config from ...: open ...: no such file or directory"), so this bug is completely invisible by inspection — it only surfaces when the `errors.Is` branch silently fails to trigger and the wrong code path (`log.Fatal` instead of falling back to defaults) runs in production.

**✅ Good**
```go
func loadConfig(path string) error {
	_, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("loading config from %s: %w", path, err)
	}
	return nil
}
```

**Why it works / Explanation:** `%w` tells `fmt.Errorf` to implement `Unwrap() error` on the returned error, returning the original `err`. `errors.Is`/`errors.As` can then walk down to `os.ErrNotExist` no matter how many wrapping layers are in between.

**Design principle:** Use `%w` whenever the error you're wrapping might need to be inspected programmatically later — which, in practice, is almost always; reserve `%v` for values that genuinely aren't errors.

---

## 4. Using panic for expected, recoverable conditions

**The Problem:** Conditions like "user not found" or "invalid input" are normal, expected outcomes of business logic — they should be represented as `error` values, not as panics. Panicking for them forces every caller to either add `recover()` gymnastics or risk crashing the process for something that isn't exceptional at all.

**❌ Bad**
```go
func GetUser(id int, users map[int]string) string {
	name, ok := users[id]
	if !ok {
		panic(fmt.Sprintf("user %d not found", id)) // BUG: expected condition treated as fatal
	}
	return name
}

func handler(w http.ResponseWriter, r *http.Request, userStore map[int]string) {
	name := GetUser(999, userStore) // panics — crashes this request's goroutine
	fmt.Fprintln(w, name)
}
```

**Why it's wrong:**
- "Not found" is a completely ordinary, expected outcome for a lookup — using `panic` for it means every caller must wrap calls in `recover()` just to handle a case that a plain `if err != nil` would cover far more clearly.
- Without a `recover()` somewhere in this goroutine's call stack, the panic crashes the entire process (see the panic-and-recover reference for why a single unhandled panic can take down unrelated in-flight work too).

**✅ Good**
```go
var ErrUserNotFound = errors.New("user not found")

func GetUser(id int, users map[int]string) (string, error) {
	name, ok := users[id]
	if !ok {
		return "", fmt.Errorf("user %d: %w", id, ErrUserNotFound)
	}
	return name, nil
}

func handler(w http.ResponseWriter, r *http.Request, userStore map[int]string) {
	name, err := GetUser(999, userStore)
	if errors.Is(err, ErrUserNotFound) {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	fmt.Fprintln(w, name)
}
```

**Why it works / Explanation:** The caller now has a normal, explicit decision point instead of an unwind to catch. Contrast this with `panic`'s correct use: truly unrecoverable programmer errors and invariant violations, e.g. `panic("unreachable")` in a `switch` default that validated input should never actually reach.

**Design principle:** Reserve `panic` for "this should be impossible if the code is correct"; use `error` for anything that can legitimately happen as part of normal operation.

---

## 5. Sentinel errors vs custom error types

**The Problem:** A package-level sentinel error (`var ErrNotFound = errors.New(...)`) works well for simple equality-style checks, but it can't carry per-call dynamic context — like *which* ID wasn't found. Callers that need that information have to parse it back out of a string, which is fragile and easy to get wrong.

**❌ Bad**
```go
var ErrNotFound = errors.New("not found")

func userExists(id int) bool { return false } // stand-in for a real lookup

func FindUser(id int) error {
	if !userExists(id) {
		return ErrNotFound // BUG: caller can't recover which ID was missing
	}
	return nil
}
```

**Why it's wrong:**
- `errors.Is(err, ErrNotFound)` tells the caller *that* something wasn't found, but nothing about *what* — any code that wants to log or display the missing ID has no structured way to get it back.
- Extending this later to carry more detail forces a breaking change to the sentinel's usage everywhere it's checked, since a plain `errors.New` value has no fields to add to.

**✅ Good**
```go
type NotFoundError struct {
	Resource string
	ID       int
}

func (e *NotFoundError) Error() string {
	return fmt.Sprintf("%s %d not found", e.Resource, e.ID)
}

func userExists(id int) bool { return false } // stand-in for a real lookup

func FindUser(id int) error {
	if !userExists(id) {
		return &NotFoundError{Resource: "user", ID: id}
	}
	return nil
}

func main() {
	err := FindUser(42)
	var nf *NotFoundError
	if errors.As(err, &nf) {
		fmt.Printf("could not find %s %d\n", nf.Resource, nf.ID)
	}
}
```

**Why it works / Explanation:** `errors.As` matches by concrete type instead of by value equality, so it works through any amount of `%w` wrapping, and the matched `*NotFoundError` gives the caller structured access to `Resource` and `ID` instead of an opaque static message. Add an `Unwrap() error` method too if the type ever needs to wrap an underlying cause.

**Design principle:** Use a sentinel when callers only need to ask "was it this specific error," and a custom type implementing the `error` interface (plus `Unwrap()` where relevant) when callers need dynamic data out of the error.

---

## 6. Allocating a new error every call for a static condition

**The Problem:** `fmt.Errorf` (even without any `%w`/`%v` verbs referencing dynamic data) always builds and allocates a new error value on every call. When the condition and message never actually vary, that allocation is pure overhead repeated on every invocation in a hot path.

**❌ Bad**
```go
func validate(age int) error {
	if age < 0 {
		return fmt.Errorf("age cannot be negative") // BUG: new allocation every call, same message every time
	}
	return nil
}
```

**Why it's wrong:**
- `fmt.Errorf` runs the message through the `fmt` formatting machinery and allocates a new error value on every single call, even though the message never changes — in a validator called millions of times per second, this is measurable, avoidable allocation churn.
- It also produces a fresh error value each time, which forces callers to use `errors.Is`/message comparison rather than identity comparison, when a single shared instance would let both work.

**✅ Good**
```go
var ErrNegativeAge = errors.New("age cannot be negative")

func validate(age int) error {
	if age < 0 {
		return ErrNegativeAge // reuses one package-level allocation
	}
	return nil
}
```

**Why it works / Explanation:** `ErrNegativeAge` is allocated exactly once, at package initialization, and every call site returns the same value. Callers can compare with `errors.Is` (or even `==`, though `errors.Is` remains the safer default) without any wrapping penalty on the hot path.

**Design principle:** Only pay for `fmt.Errorf`'s formatting and allocation when the message actually needs per-call dynamic data; reuse a sentinel for fixed, static conditions.

---

## 7. Error string style violations

**The Problem:** Go convention is that error strings are lowercase and don't end in punctuation, because they're routinely embedded inside larger, wrapped messages — capitalization and trailing punctuation in the middle of a sentence look broken, even though the code compiles and runs fine either way.

**❌ Bad**
```go
func openFile(path string) error {
	_, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("Could not open file: %s.", err) // BUG: capitalized, trailing period
	}
	return nil
}
```

**Why it's wrong:**
- When this error is wrapped again further up (`fmt.Errorf("processing request: %w", err)`), the result reads as `"processing request: Could not open file: os.Open failed."` — an awkward capital letter and a stray period mid-sentence.
- `go vet` and `staticcheck` (rule `ST1005`) both flag this pattern, so it tends to show up as noise in CI/lint output that either gets fixed reflexively or, worse, silenced with a blanket lint-ignore that hides other real issues too.

**✅ Good**
```go
func openFile(path string) error {
	_, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("could not open file: %w", err)
	}
	return nil
}
```

**Why it works / Explanation:** A lowercase, punctuation-free message composes cleanly no matter how many layers of `fmt.Errorf("...: %w", err)` wrap it later — every layer just becomes another lowercase clause in the eventual `": "`-joined breadcrumb trail.

**Design principle:** Write error strings as a lowercase clause meant to be embedded, not a standalone sentence; let `go vet`/`staticcheck` catch regressions.

---

## 8. Returning a typed nil pointer through the error interface

**The Problem:** A function with a declared `error` return type that returns a `nil` pointer of some concrete error type (instead of the literal untyped `nil`) produces a non-nil `error` interface value. This is the same typed-nil-interface trap covered for interfaces generally, but it's especially common — and especially damaging — in functions that return `error`, since every caller's `if err != nil` check is exactly the code path that gets fooled.

**❌ Bad**
```go
type MyErr struct{ msg string }

func (e *MyErr) Error() string { return e.msg }

func doWork(fail bool) error {
	var e *MyErr // nil pointer of concrete type *MyErr
	if fail {
		e = &MyErr{msg: "work failed"}
	}
	return e // BUG: always a non-nil `error` interface, even when e is nil
}

func main() {
	err := doWork(false)
	if err != nil {
		fmt.Println("got an error:", err) // prints, even though nothing failed
	}
}
```

**Why it's wrong:**
- An `error` interface value is `nil` only when *both* its type and value are nil. Here the type is always `*MyErr` (even when the pointer's value is nil), so `err != nil` is always `true`, regardless of whether `doWork` actually failed.
- This is one of the most common real-world incidents caused by the typed-nil trap, because it hits the exact idiom (`if err != nil`) that almost every piece of Go code relies on to detect failure — the bug hides in plain sight inside "obviously correct" looking code.

**✅ Good**
```go
func doWork(fail bool) error {
	if fail {
		return &MyErr{msg: "work failed"}
	}
	return nil // explicit untyped nil — a truly nil interface
}
```

**Why it works / Explanation:** Returning the literal `nil` directly, instead of a variable that happens to hold a nil pointer of a concrete type, means the returned `error` interface has no type and no value — genuinely `nil`, and `err != nil` behaves as every caller expects.

**Design principle:** Never let a `nil`-valued concrete pointer flow into an `error`-typed return; return literal `nil` explicitly whenever there is no error, and be suspicious of any `var e *SomeErrType` followed later by a bare `return e`.

---

## 9. Errors swallowed inside goroutines

**The Problem:** An error returned from a function invoked via `go func() { ... }()` has nowhere to go — the goroutine's return value, if any, is simply discarded, and there is no implicit channel back to the caller the way a normal function call has.

**❌ Bad**
```go
func process(item string) error { return nil }

func processAll(items []string) {
	for _, item := range items {
		go func(it string) {
			if err := process(it); err != nil {
				return // BUG: error vanishes — nothing is listening
			}
		}(item)
	}
}
```

**Why it's wrong:**
- If `process` fails for some items, `processAll`'s caller has absolutely no way to know — no return value, no log, nothing. The failure is complete and silent.
- Under load, this tends to surface only indirectly, days later, as "some records are missing" reports with no error logs anywhere pointing at the cause.

**✅ Good**
```go
func process(item string) error { return nil }

func processAll(ctx context.Context, items []string) error {
	g, ctx := errgroup.WithContext(ctx)
	for _, item := range items {
		item := item
		g.Go(func() error {
			return process(item)
		})
	}
	return g.Wait() // first non-nil error is returned; the group also cancels ctx for the rest
}
```

**Why it works / Explanation:** `errgroup.Group` (from `golang.org/x/sync/errgroup`) gives each goroutine a proper channel back to the caller: `g.Wait()` blocks until all goroutines finish and returns the first non-nil error. A plain channel of `error` values, or explicit structured logging with enough context to act on, are valid alternatives when you specifically want fire-and-forget semantics with visibility rather than a hard failure.

**Design principle:** Never launch a goroutine that can fail without an explicit path for that failure to reach something that can observe it — a channel, an `errgroup`, or at minimum a log line with enough context to investigate.

---

## 10. Overly generic error messages

**The Problem:** An error like `"failed"` is technically an error, but it carries none of the information a future reader (often you, at 2am, in production) needs to know what operation was being attempted or with what inputs.

**❌ Bad**
```go
func fetchUser(id int) (*User, error) {
	resp, err := http.Get(fmt.Sprintf("https://api.example.com/users/%d", id))
	if err != nil {
		return nil, errors.New("failed") // BUG: no idea what failed or why
	}
	defer resp.Body.Close()

	var user User
	if err := json.NewDecoder(resp.Body).Decode(&user); err != nil {
		return nil, errors.New("failed") // BUG: same generic message for a totally different failure
	}
	return &user, nil
}

type User struct {
	ID   int
	Name string
}
```

**Why it's wrong:**
- Two completely different failure modes — a failed HTTP request and a failed JSON decode — produce the exact same message, so logs give zero information about which one actually happened, let alone for which user ID.
- As this error propagates up through several more layers, each of which might also just say `"failed"` or similarly vague text, the final message reaching an alert or log line is functionally useless for debugging.

**✅ Good**
```go
func fetchUser(id int) (*User, error) {
	resp, err := http.Get(fmt.Sprintf("https://api.example.com/users/%d", id))
	if err != nil {
		return nil, fmt.Errorf("fetching user %d: %w", id, err)
	}
	defer resp.Body.Close()

	var user User
	if err := json.NewDecoder(resp.Body).Decode(&user); err != nil {
		return nil, fmt.Errorf("fetching user %d: decoding response: %w", id, err)
	}
	return &user, nil
}
```

**Why it works / Explanation:** Each wrapping layer adds the specific operation and arguments relevant at that point (`fetching user %d`), while `%w` preserves the underlying cause. By the time the error reaches a top-level log line, it reads as a full breadcrumb trail: what was being attempted, with what input, and exactly what failed underneath.

**Design principle:** Every `fmt.Errorf` wrap should add information a reader doesn't already have — the operation and the arguments involved — not just restate that "an error happened."

---

## Key Takeaways
- Never discard an error with `_`; in particular, always check `rows.Err()` after a `for rows.Next()` loop.
- Use `errors.Is`, not `==`, to compare against sentinel errors — wrapping breaks direct equality.
- Wrap with `%w`, not `%v`, whenever the wrapped error might need to be unwrapped later by `errors.Is`/`errors.As`.
- Use `error` for expected, recoverable conditions; reserve `panic` for invariant violations a correct program should never hit.
- Use sentinel errors for simple identity checks, custom error types (with `Unwrap()`) when callers need dynamic context.
- Reuse a package-level sentinel error instead of calling `fmt.Errorf` repeatedly for a static, non-dynamic condition in hot paths.
- Keep error strings lowercase and free of trailing punctuation so they compose cleanly when wrapped.
- Never return a typed-nil concrete pointer through an `error`-typed return value — return literal `nil` explicitly.
- Give every error-returning goroutine an explicit path back to something that observes the error — a channel, `errgroup`, or logging.
- Wrap errors with the specific operation and arguments involved, not generic text like `"failed"`.

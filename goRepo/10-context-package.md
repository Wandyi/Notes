# The context Package: Production Pitfalls

Go's `context` package is the standard mechanism for carrying cancellation signals, deadlines, and request-scoped metadata across API boundaries and goroutines. Misusing it is one of the most common sources of production incidents in Go services: goroutine leaks that slowly exhaust memory, requests that ignore timeouts and keep running long after a client has given up, and subtle bugs from context values colliding or disappearing. Because `context` misuse rarely causes a compile error and often "works" in casual testing, these mistakes tend to ship and only show up under real load or after a refactor.

## 1. Storing context.Context in a struct field

**The Problem:** It's tempting to save a `ctx` on a struct once (e.g. in a constructor) so you don't have to thread it through every method. But a struct is typically longer-lived than any single request or operation, so the stored context goes stale, gets reused across unrelated calls, or — if it was a request context — ends up cancelled for every call made after the original request finished.

**❌ Bad**
```go
type Service struct {
	ctx context.Context // BUG: context stored on the struct
	db  *sql.DB
}

func NewService(ctx context.Context, db *sql.DB) *Service {
	return &Service{ctx: ctx, db: db}
}

func (s *Service) GetUser(id int) (string, error) {
	var name string
	row := s.db.QueryRowContext(s.ctx, "SELECT name FROM users WHERE id = ?", id)
	err := row.Scan(&name)
	return name, err
}
```

**Why it's wrong:**
- If `s.ctx` came from an HTTP request, it's cancelled the moment that request finishes — every later call to `GetUser` on this long-lived `Service` fails immediately with `context canceled`, even for completely unrelated requests.
- If `s.ctx` is `context.Background()` set once at startup instead, every call loses the ability to carry a per-request deadline or trace ID at all, defeating the point of using `context` in the first place.

**✅ Good**
```go
type Service struct {
	db *sql.DB
}

func NewService(db *sql.DB) *Service {
	return &Service{db: db}
}

func (s *Service) GetUser(ctx context.Context, id int) (string, error) {
	var name string
	row := s.db.QueryRowContext(ctx, "SELECT name FROM users WHERE id = ?", id)
	err := row.Scan(&name)
	return name, err
}
```

**Why it works / Explanation:** Passing `ctx` explicitly into every method that does I/O or can block means each call carries its own caller-appropriate deadline, cancellation signal, and trace metadata. The struct only stores what genuinely outlives a single call (the `*sql.DB` connection pool).

**Design principle:** "context.Context should be the first parameter of a function, named `ctx`" — one of the most consistently followed idioms in the Go standard library, precisely because it makes the lifetime of the context obvious at every call site.

---

## 2. Using context.WithValue for required parameters

**The Problem:** `context.Value` is convenient — it lets you avoid touching every function signature between the point a value is known and the point it's needed. But when that value is something a function cannot correctly operate without (like the ID of the user making the request), hiding it in the context turns a compile-time-checked dependency into a runtime maybe-there-maybe-not lookup.

**❌ Bad**
```go
type ctxKey string

func CreateOrder(ctx context.Context, itemID int) error {
	userID, ok := ctx.Value(ctxKey("userID")).(int)
	if !ok {
		return errors.New("userID missing from context") // BUG: required input smuggled through ctx
	}
	return insertOrder(userID, itemID)
}

func handler(w http.ResponseWriter, r *http.Request) {
	ctx := context.WithValue(r.Context(), ctxKey("userID"), 42)
	if err := CreateOrder(ctx, 7); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}
```

**Why it's wrong:**
- The function signature no longer tells the truth about what `CreateOrder` needs — a caller can pass any `ctx` and only discover the missing dependency at runtime, in production, instead of at compile time.
- Every caller (including every test) has to know the magic key and type to set up the context correctly; the compiler gives zero help, and a typo in the key silently produces the "missing" error path.

**✅ Good**
```go
func CreateOrder(ctx context.Context, userID, itemID int) error {
	return insertOrder(userID, itemID)
}

func handler(w http.ResponseWriter, r *http.Request) {
	userID := 42 // e.g. extracted by auth middleware earlier in the chain
	if err := CreateOrder(r.Context(), userID, 7); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}
```

**Why it works / Explanation:** `userID` is now a normal, required, compiler-checked argument. `ctx` is still passed through for cancellation, but it no longer pretends to be a dependency-injection container. `context.Value` should be reserved for optional, cross-cutting data — trace IDs, deadlines-adjacent metadata — that a function can still behave correctly without.

**Design principle:** Explicit is better than implicit. If a caller *must* supply something for the function to work, it belongs in the signature, not in the context bag.

---

## 3. Plain string keys causing collisions

**The Problem:** `context.WithValue` keys are compared with `==`. Two unrelated packages that both use a plain `string` (or any comparable built-in type) as a key can accidentally pick the exact same key, and one will silently clobber or read the other's value.

**❌ Bad**
```go
// package auth
func WithUser(ctx context.Context, user string) context.Context {
	return context.WithValue(ctx, "user", user) // BUG: plain string key
}

// package audit — written by a different team, unaware of auth's key choice
func WithActor(ctx context.Context, actor string) context.Context {
	return context.WithValue(ctx, "user", actor) // BUG: same key, different package
}
```

**Why it's wrong:**
- Both packages use the identical key value `"user"` of identical type `string`; whichever `WithValue` call happens last wins, and the earlier value becomes permanently unreachable through that key.
- The collision is invisible at compile time and often invisible in code review too, since the two packages may never be viewed side by side — it only surfaces as "wrong user showing up in audit logs" weeks later.

**✅ Good**
```go
package auth

type ctxKey int

const userKey ctxKey = 0

func WithUser(ctx context.Context, user string) context.Context {
	return context.WithValue(ctx, userKey, user)
}

func UserFrom(ctx context.Context) (string, bool) {
	u, ok := ctx.Value(userKey).(string)
	return u, ok
}
```

**Why it works / Explanation:** `ctxKey` is an unexported type defined inside `package auth`. No other package can construct a value of that exact type (even another package writing `type ctxKey int` gets a *different* type, since Go types are identified by package + name), so `context.Value` lookups can never collide across package boundaries, even by accident.

**Design principle:** Use an unexported, package-private key type for every `context.WithValue` call — never a bare `string`, `int`, or other type another package could plausibly also produce.

---

## 4. Forgetting to call cancel

**The Problem:** `context.WithCancel`, `WithTimeout`, and `WithDeadline` all return a `cancel` function alongside the new context. That function releases resources associated with the context (an internal timer, and the parent-child bookkeeping link) — and it must be called even if the context's own deadline will eventually fire on its own.

**❌ Bad**
```go
func processAll(urls []string) {
	for _, u := range urls {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		// BUG: `cancel` is never called
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			log.Println(err)
			continue
		}
		resp.Body.Close()
	}
}
```

**Why it's wrong:**
- Each iteration starts an internal timer that is only released when `cancel` runs or the timeout naturally elapses; under a burst of many URLs, live timers and their associated context nodes pile up in memory until each one's 2 seconds passes on its own.
- If this pattern is used with `context.WithCancel` (no built-in timer) instead, the resources are held for as long as the parent context stays alive — which, chained off `context.Background()`, means for the lifetime of the process.

**✅ Good**
```go
func processAll(urls []string) {
	for _, u := range urls {
		fetchOne(u)
	}
}

func fetchOne(u string) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel() // released as soon as fetchOne returns, every iteration
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Println(err)
		return
	}
	defer resp.Body.Close()
}
```

**Why it works / Explanation:** Moving the per-URL work into its own function means `defer cancel()` fires at the end of *that* call rather than piling up for the lifetime of the outer loop (deferring directly in the loop body would trade this bug for the defer-in-a-loop pitfall instead). Calling `cancel()` promptly frees the timer immediately instead of waiting for it to fire naturally.

**Design principle:** Always pair `WithCancel`/`WithTimeout`/`WithDeadline` with an immediate `defer cancel()` in the function that owns the derived context; if that creation happens in a loop, scope it to a per-iteration function so the defer actually runs every iteration.

---

## 5. Ignoring ctx.Done() in long-running work

**The Problem:** Passing a context into a function does nothing by itself — a function that never checks `ctx.Done()` or `ctx.Err()` will run to completion (or block forever) regardless of what deadline or cancellation the caller attached.

**❌ Bad**
```go
func sumPrimes(ctx context.Context, limit int) int {
	total := 0
	for i := 2; i < limit; i++ {
		// BUG: no cancellation check — runs to completion no matter what
		if isPrime(i) {
			total += i
		}
	}
	return total
}

func isPrime(n int) bool {
	if n < 2 {
		return false
	}
	for d := 2; d*d <= n; d++ {
		if n%d == 0 {
			return false
		}
	}
	return true
}
```

**Why it's wrong:**
- A caller that wraps this in `context.WithTimeout(ctx, 100*time.Millisecond)` gets no protection at all: the loop keeps burning CPU well past the deadline, because nothing inside ever looks at `ctx`.
- For blocking operations (channel receives, waiting on a condition) instead of CPU loops, the effect is worse — the goroutine can block forever, leaking for the life of the process even after the caller has moved on.

**✅ Good**
```go
func sumPrimes(ctx context.Context, limit int) (int, error) {
	total := 0
	for i := 2; i < limit; i++ {
		select {
		case <-ctx.Done():
			return 0, ctx.Err()
		default:
		}
		if isPrime(i) {
			total += i
		}
	}
	return total, nil
}
```

**Why it works / Explanation:** Checking `ctx.Done()` on every iteration (or before every blocking step) means the function actually honors the caller's deadline or explicit cancellation, returning `ctx.Err()` (`context.DeadlineExceeded` or `context.Canceled`) promptly instead of running unchecked.

**Design principle:** Accepting a `ctx` parameter is a contract to *respect* it, not just to have it available — any loop or blocking call inside a cancellable operation needs an explicit cancellation check.

---

## 6. Library code manufacturing its own root context

**The Problem:** Exported functions that do I/O or long-running work should accept `ctx` from their caller. Calling `context.Background()` (or `context.TODO()`) deep inside a library function instead creates a context the caller has no way to cancel, time out, or attach trace information to.

**❌ Bad**
```go
package cache

func (c *Cache) Refresh() error {
	ctx := context.Background() // BUG: caller has no say over cancellation or deadlines
	return c.fetchFromUpstream(ctx)
}
```

**Why it's wrong:**
- A caller that wants `Refresh` to respect an overall request deadline, or to be cancellable when the application is shutting down, simply cannot — the library silently ignores anything the caller might have wanted to propagate.
- Any tracing or request-scoped metadata the caller attached to its own context (trace IDs, deadlines) is lost the moment execution crosses into this function.

**✅ Good**
```go
package cache

func (c *Cache) Refresh(ctx context.Context) error {
	return c.fetchFromUpstream(ctx)
}
```

**Why it works / Explanation:** The caller decides the lifetime and cancellation behavior of the work; the library just propagates whatever `ctx` it's handed. `context.Background()` should really only appear at true program roots — `main`, top-level test setup, or a signal handler wiring up graceful shutdown — never inside a reusable library function.

**Design principle:** Any exported function that can block, do network/disk I/O, or run for a nontrivial amount of time should accept `ctx context.Context` as its first parameter — full stop.

---

## 7. Passing nil instead of a context

**The Problem:** `context.Context` is an interface, and `nil` satisfies it at compile time, so `doWork(nil)` compiles cleanly. It only fails once something inside actually calls a method on that nil interface value.

**❌ Bad**
```go
func doWork(ctx context.Context) {
	select {
	case <-ctx.Done(): // BUG: panics here — nil interface has no concrete type to dispatch to
		return
	default:
	}
	// ... do work
}

func main() {
	doWork(nil) // compiles fine; panics at runtime with a nil pointer dereference
}
```

**Why it's wrong:**
- `ctx.Done()` on a truly nil `context.Context` panics with `invalid memory address or nil pointer dereference` — there's no concrete type behind the interface for the method call to dispatch to.
- The panic happens far from the call site that passed `nil`, often deep inside a helper or third-party library, making the root cause confusing to track down from the stack trace alone.

**✅ Good**
```go
func doWork(ctx context.Context) {
	select {
	case <-ctx.Done():
		return
	default:
	}
}

func main() {
	doWork(context.Background()) // the true root of the call tree
}

// Mid-refactor, before ctx has been threaded all the way through yet:
func legacyEntryPoint() {
	ctx := context.TODO() // documents "a real context belongs here eventually"
	doWork(ctx)
}
```

**Why it works / Explanation:** `context.Background()` is the correct non-nil root context for `main`, init, and tests. `context.TODO()` is functionally identical to `Background()` but documents that the surrounding code hasn't been wired up with a real context yet — useful during incremental refactors, and greppable later.

**Design principle:** Never pass `nil` where a `context.Context` is expected; use `context.TODO()` as an explicit, self-documenting placeholder instead.

---

## 8. Redundant nested timeouts that silently do nothing

**The Problem:** A child context derived with `WithTimeout`/`WithDeadline` can only ever have an *earlier or equal* effective deadline than its parent — never a later one. Developers who add a "generous" inner timeout without checking the outer one often assume it controls the operation, when in fact the outer deadline (set upstream) is what actually fires first.

**❌ Bad**
```go
func handler(ctx context.Context) error {
	// Outer deadline: 2s, set by middleware upstream via
	// context.WithTimeout(r.Context(), 2*time.Second)
	return fetchUserData(ctx)
}

func fetchUserData(ctx context.Context) error {
	// BUG: developer assumes this grants 10 full seconds — it can't.
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	return queryDB(ctx)
}
```

**Why it's wrong:**
- `ctx.Done()` on the derived context fires at `min(parent deadline, own deadline)`. If the parent already has a 2-second deadline, the "10 second" timeout set here is pure decoration — the operation is still cut off at 2 seconds.
- Whoever reads `fetchUserData` in isolation reasonably concludes it has a 10-second budget, and will misdiagnose timeouts as some other bug, because the code "clearly" allows more time.

**✅ Good**
```go
func handler(ctx context.Context) error {
	// Set the one meaningful deadline where the budget is actually known.
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	return fetchUserData(ctx)
}

func fetchUserData(ctx context.Context) error {
	// No redundant timeout here — pass ctx straight through.
	return queryDB(ctx)
}

func queryDB(ctx context.Context) error {
	if dl, ok := ctx.Deadline(); ok {
		log.Printf("query must finish by %s", dl)
	}
	return nil // ... run the query using ctx
}
```

**Why it works / Explanation:** Deadlines compose by intersection, shrinking only, down the call chain. Setting the meaningful timeout once — at the point that actually knows the real budget — and simply propagating `ctx` afterward avoids the false impression that an inner function controls timing it has no real influence over.

**Design principle:** A context's effective deadline is always the *earliest* one anywhere in its ancestry; don't add inner timeouts that assume otherwise, and use `ctx.Deadline()` to inspect the real, already-composed value when it matters.

---

## 9. Long chains of context.WithValue hurting lookup cost

**The Problem:** Each call to `context.WithValue` allocates a new wrapper that links back to its parent. `ctx.Value(key)` walks that chain backwards, comparing keys, until it finds a match or reaches the root. Wrapping many individual values one at a time — especially in a loop — builds a long chain that makes every subsequent lookup, and every future `WithValue` call, more expensive than it needs to be.

**❌ Bad**
```go
type ctxKey string

func enrichContext(ctx context.Context, tags map[string]string) context.Context {
	for k, v := range tags {
		ctx = context.WithValue(ctx, ctxKey(k), v) // BUG: one new wrapper layer per tag
	}
	return ctx
}
```

**Why it's wrong:**
- With dozens of tags, this builds a chain dozens of layers deep; every later `ctx.Value(someKey)` call anywhere downstream has to walk backwards through all of them before it finds a match (or gives up at the root), on every single lookup.
- Every iteration is a separate heap allocation for the wrapper struct — in a hot request path called thousands of times a second, this adds measurable, easy-to-miss allocation churn.

**✅ Good**
```go
type tagsKey struct{}

func enrichContext(ctx context.Context, tags map[string]string) context.Context {
	return context.WithValue(ctx, tagsKey{}, tags) // one wrapper layer for the whole set
}

func TagsFrom(ctx context.Context) map[string]string {
	tags, _ := ctx.Value(tagsKey{}).(map[string]string)
	return tags
}
```

**Why it works / Explanation:** Bundling related request-scoped metadata into a single value behind a single key keeps the context chain shallow — one allocation, one lookup step, regardless of how many individual fields are inside the bundle.

**Design principle:** Prefer one `context.WithValue` call carrying a small struct or map of related fields over one call per field; keep the value chain as short as the data's actual shape allows.

---

## Key Takeaways
- Never store `context.Context` on a struct; pass it explicitly as the first parameter of every method that needs it.
- Don't smuggle required parameters through `context.Value` — only optional, cross-cutting metadata belongs there.
- Always use an unexported custom key type with `context.WithValue`, never a plain `string` or other collision-prone built-in type.
- Always pair `WithCancel`/`WithTimeout`/`WithDeadline` with an immediate `defer cancel()`.
- Check `ctx.Done()`/`ctx.Err()` inside any loop or blocking operation you want to actually be cancellable.
- Exported library functions doing I/O should accept `ctx` from the caller, never manufacture their own via `context.Background()`.
- Never pass `nil` as a context; use `context.TODO()` as an explicit placeholder during refactors.
- Nested timeouts can only shrink an ancestor's deadline, never extend it — set the meaningful one where the real budget is known.
- Bundle related context values into one `WithValue` call instead of chaining many, to keep lookups and allocations cheap.

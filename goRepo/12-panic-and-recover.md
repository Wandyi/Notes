# Panic and Recover: Production Pitfalls

`panic` and `recover` are Go's mechanism for truly exceptional, unrecoverable conditions — not a general-purpose exception system for ordinary error handling. Treating them as interchangeable with `error` leads to obscured control flow, and misunderstanding exactly how `recover` interacts with goroutines and stack unwinding leads to servers that crash under load, panics that vanish without a trace, and cleanup code that never runs. This file covers the specific mechanics that trip people up and the patterns that keep a single unexpected panic from taking down an entire process.

## 1. Using panic/recover as general control flow

**The Problem:** `panic` unwinds the stack, running every deferred function along the way, until something calls `recover` or the goroutine (and often the whole process) dies. That's expensive and hard to follow compared to a normal `if err != nil` return — and it's the wrong tool for conditions that are simply expected outcomes, like a validation failure.

**❌ Bad**
```go
type User struct {
	Name string
	Age  int
}

func validateAge(age int) {
	if age < 0 {
		panic("invalid age") // BUG: expected validation failure treated as fatal
	}
}

func createUser(name string, age int) (u *User, err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("createUser: %v", r) // recovering just to turn it back into an error
		}
	}()
	validateAge(age)
	return &User{Name: name, Age: age}, nil
}
```

**Why it's wrong:**
- Every caller of `validateAge` now needs a matching `recover()` somewhere in the same goroutine, just to get back to the ordinary `error` handling this whole scheme was avoiding.
- Panicking unwinds through every stack frame between `validateAge` and the `recover()`, running all of their deferred functions along the way — for a plain validation check, that's a lot of unnecessary work and a lot of unrelated code paths implicitly coupled together.

**✅ Good**
```go
func validateAge(age int) error {
	if age < 0 {
		return errors.New("invalid age")
	}
	return nil
}

func createUser(name string, age int) (*User, error) {
	if err := validateAge(age); err != nil {
		return nil, fmt.Errorf("createUser: %w", err)
	}
	return &User{Name: name, Age: age}, nil
}
```

**Why it works / Explanation:** The failure path is a plain, visible `if err != nil` — no unwinding, no `recover()`, and no risk of crashing a goroutine that doesn't happen to have a matching recovery in its call stack.

**Design principle:** Reserve `panic` for conditions that indicate a bug in the program itself (an invariant violation), not for outcomes — like invalid input — that are a normal part of the function's contract.

---

## 2. recover() only works within the same goroutine

**The Problem:** A `defer`/`recover()` pair only catches panics that occur in the same goroutine's call stack. Spawning a new goroutine with `go` starts an independent stack — a panic inside it is completely invisible to any `recover()` sitting in the goroutine that spawned it.

**❌ Bad**
```go
func safeCall(fn func()) {
	defer func() {
		if r := recover(); r != nil {
			log.Println("recovered:", r)
		}
	}()
	go fn() // BUG: fn panics in a different goroutine; this recover cannot see it
}

func main() {
	safeCall(func() {
		var m map[string]int
		m["x"] = 1 // panics: assignment to entry in nil map
	})
	time.Sleep(100 * time.Millisecond) // process crashes before this even matters
}
```

**Why it's wrong:**
- `safeCall`'s `defer`/`recover()` lives on `main`'s (or its caller's) stack; the panic inside the spawned goroutine happens on an entirely separate stack that this `recover()` never even sees.
- Since nothing recovers the panic where it actually happens, the Go runtime prints it and terminates the *entire process* — every other goroutine, including any unrelated in-flight work, dies with it.

**✅ Good**
```go
func safeGo(fn func()) {
	go func() {
		defer func() {
			if r := recover(); r != nil {
				log.Println("recovered in goroutine:", r)
			}
		}()
		fn()
	}()
}

func main() {
	safeGo(func() {
		var m map[string]int
		m["x"] = 1
	})
	time.Sleep(100 * time.Millisecond)
	fmt.Println("main survived")
}
```

**Why it works / Explanation:** The `defer`/`recover()` now lives directly inside the goroutine that runs `fn`, on the same stack where the panic actually happens, so it can catch it before the runtime treats it as fatal.

**Design principle:** `recover` is stack-local, not global — every goroutine that can panic and should survive doing so needs its own top-level `defer`/`recover()`, full stop.

---

## 3. Unconditional recover swallowing everything

**The Problem:** Calling `recover()` and doing nothing else with the result "catches" a panic in the sense that the process doesn't crash, but it also throws away the one piece of information (what actually panicked, and where) that would let anyone notice and fix the underlying bug.

**❌ Bad**
```go
func recoverMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			recover() // BUG: swallows everything, no logging, no distinction
		}()
		next.ServeHTTP(w, r)
	})
}
```

**Why it's wrong:**
- A real bug — a nil map write, an out-of-range index, a genuine nil pointer dereference — gets silently eaten instead of surfacing in logs, staging, or alerts where someone could actually fix it.
- The client typically gets a broken or empty response (since the handler stopped partway through writing it) with no indication anything went wrong server-side, and there's no log entry pointing anyone at the cause.

**✅ Good**
```go
func recoverMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				log.Printf("panic handling %s %s: %v\n%s", r.Method, r.URL.Path, rec, debug.Stack())
				http.Error(w, "internal server error", http.StatusInternalServerError)
			}
		}()
		next.ServeHTTP(w, r)
	})
}
```

**Why it works / Explanation:** The recovered value and a stack trace are logged with request context before responding, so the process survives *and* the bug is visible and actionable — the middleware still keeps the server up, but it no longer hides the problem.

**Design principle:** Recovering from a panic should make the failure loud in your observability stack, not quiet — silence is for expected conditions, and a panic, by definition, wasn't one.

---

## 4. Calling recover() outside of a defer

**The Problem:** `recover()` only has an effect when it is called directly by a function that was deferred, during an active panic. Calling it anywhere else — in normal, non-deferred control flow — always returns `nil` and does absolutely nothing, even if a panic is in flight elsewhere in a way that makes this look plausible.

**❌ Bad**
```go
func riskyOperation() {
	panic("boom")
}

func doSomething() {
	r := recover() // BUG: not inside a defer — always returns nil, does nothing
	if r != nil {
		log.Println("recovered:", r)
	}
	riskyOperation()
}
```

**Why it's wrong:**
- At the point `recover()` is called here, no panic is in progress (this is normal, top-to-bottom execution, not stack unwinding), so `recover()` immediately returns `nil` and the `if` block never runs — this code has zero effect on the panic that `riskyOperation()` is about to raise.
- `riskyOperation()`'s panic then propagates completely uncaught, since the "recovery" that looked like it was in place never actually could have caught anything.

**✅ Good**
```go
func doSomething() {
	defer func() {
		if r := recover(); r != nil { // correct: inside a defer, checked for non-nil
			log.Println("recovered:", r)
		}
	}()
	riskyOperation()
}
```

**Why it works / Explanation:** `recover()` is called from within a deferred closure, which is the only context where it can actually intercept an in-progress panic; checking `r != nil` correctly distinguishes "a panic happened" from "no panic happened," which a bare unconditional call cannot do on its own.

**Design principle:** `recover()` is only meaningful directly inside a deferred function; always write it as `if r := recover(); r != nil { ... }` inside a `defer func() { ... }()`, never as a standalone statement in normal flow.

---

## 5. A second panic during unwinding clobbers the first

**The Problem:** If a deferred function itself panics while an earlier panic is still being processed, the new panic becomes the "active" one. Any `recover()` further up the stack sees only the most recent panic value — the original one is lost from the program's perspective (it may still appear in a crash dump, but only if nothing ever recovers).

**❌ Bad**
```go
func process() (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("process panicked: %v", r) // BUG: may report the wrong panic
		}
	}()

	defer func() {
		var m map[string]int
		m["k"] = 1 // panics while "original failure" is still unwinding — overwrites it
	}()

	panic("original failure")
}
```

**Why it's wrong:**
- Execution order is LIFO: the second `defer` (the one writing to a nil map) runs first, panics with `"assignment to entry in nil map"`, and that new panic replaces `"original failure"` as the one actively propagating.
- The first `defer`'s `recover()` now returns the nil-map panic instead of the real root cause — `err` ends up describing a completely different failure than the one that actually started the unwind, making the eventual log misleading.

**✅ Good**
```go
func process() (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("process panicked: %v", r)
		}
	}()

	defer safeCleanup() // contains its own panic — can't clobber the one above

	panic("original failure")
}

func safeCleanup() {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("cleanup panicked (suppressed, original panic preserved): %v", r)
		}
	}()
	var m map[string]int
	m["k"] = 1 // panics; recovered locally inside safeCleanup, never escapes it
}
```

**Why it works / Explanation:** `safeCleanup` has its own `defer`/`recover()`, so any panic that happens inside it is fully contained and never propagates out to interfere with the outer unwind — the original `"original failure"` panic is still the active one by the time the outer `defer` runs its `recover()`.

**Design principle:** Any cleanup step that runs during a `defer` and could itself fail needs its own local recovery, so it can never overwrite a panic that's already in flight.

---

## 6. Not restoring invariants after recover

**The Problem:** Recovering from a panic stops the crash, but it does nothing on its own to undo whatever partial work was in progress — a held lock, an open transaction, a half-written file. Code that recovers and then just returns an error, without explicit cleanup, leaves the program's state inconsistent.

**❌ Bad**
```go
func mustDebit(tx *sql.Tx, account string, amount int)  { /* panics if insufficient funds */ }
func mustCredit(tx *sql.Tx, account string, amount int) { /* ... */ }

func transferFunds(db *sql.DB, from, to string, amount int) (err error) {
	tx, err := db.Begin()
	if err != nil {
		return err
	}

	defer func() {
		if r := recover(); r != nil {
			log.Printf("recovered during transfer: %v", r)
			err = fmt.Errorf("transfer failed: %v", r)
			// BUG: tx is never rolled back or committed here
		}
	}()

	mustDebit(tx, from, amount) // panics on insufficient funds
	mustCredit(tx, to, amount)
	return tx.Commit()
}
```

**Why it's wrong:**
- When `mustDebit` panics, the recovery logs the error and sets `err`, but the transaction `tx` is left neither committed nor rolled back — the underlying connection stays checked out with an open transaction, potentially holding row/table locks until it eventually times out or the connection pool forcibly reclaims it.
- Any debit that already happened before the panic is left in limbo from the database's point of view — not committed, but also not explicitly undone by this code, relying entirely on driver/connection cleanup behavior that isn't guaranteed to happen promptly.

**✅ Good**
```go
func transferFunds(db *sql.DB, from, to string, amount int) (err error) {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("transfer failed: %v", r)
		}
		if err != nil {
			tx.Rollback() // always restore invariants, panic or not
		} else {
			err = tx.Commit()
		}
	}()

	mustDebit(tx, from, amount)
	mustCredit(tx, to, amount)
	return nil
}
```

**Why it works / Explanation:** The single deferred function handles both the panic recovery and the transaction's final state, so no matter whether the function returns normally, returns an error, or panics partway through, `tx` is always either rolled back or committed before `transferFunds` actually returns.

**Design principle:** Cleanup that must happen "no matter what" belongs in a `defer` that runs regardless of whether a panic occurred — recovering from a panic is not a substitute for that cleanup, it just buys you the chance to still perform it.

---

## 7. Missing panic-recovery middleware in an HTTP server

**The Problem:** Go's `net/http` server does recover panics that happen directly in a handler's own goroutine — but by abruptly closing the connection with no response body and a bare line in the server log, not a clean response. And that built-in recovery covers *only* the handler's goroutine: a panic in a goroutine the handler spawns itself is invisible to it and brings down the entire process.

**❌ Bad**
```go
func userHandler(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("id")
	parts := strings.Split(id, ",")
	fmt.Fprintln(w, "third id:", parts[2]) // panics: index out of range if fewer than 3 parts

	go func() {
		auditLog(parts[2]) // BUG: same bad index, but now inside a spawned goroutine —
	}() // net/http's built-in recovery can't catch this one at all
}

func auditLog(id string) { /* ... */ }

func main() {
	http.HandleFunc("/user", userHandler)
	log.Fatal(http.ListenAndServe(":8080", nil)) // no custom recover middleware
}
```

**Why it's wrong:**
- The first panic (in the request-handling goroutine) is recovered by `net/http` internally, but the client just gets an abruptly closed connection and no proper 500 — a poor experience with no useful diagnostic sent anywhere structured.
- The second panic, inside the `go func() { ... }()`, is not covered by `net/http`'s recovery at all — it crashes the whole process, taking down every other in-flight request on every other connection, not just this one.

**✅ Good**
```go
func recoverMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				log.Printf("panic serving %s: %v\n%s", r.URL.Path, rec, debug.Stack())
				http.Error(w, "internal server error", http.StatusInternalServerError)
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/user", userHandler)
	log.Fatal(http.ListenAndServe(":8080", recoverMiddleware(mux)))
	// NOTE: goroutines spawned inside handlers still need their own
	// recover — this middleware only covers the request-handling goroutine.
}
```

**Why it works / Explanation:** The middleware gives every request a clean, logged 500 response instead of an abrupt disconnect, and makes the recovery behavior explicit and controllable rather than relying on the standard library's undocumented-feeling default. It's still important to remember the caveat in the comment: any goroutine spawned from inside a handler needs its own `defer`/`recover()` too (see section 2).

**Design principle:** Install your own top-level recover-and-respond middleware rather than relying on `net/http`'s internal recovery — you control the response, the logging, and the coverage of spawned goroutines that the standard library's default cannot reach.

---

## 8. Re-panicking without preserving the stack trace

**The Problem:** `recover()` gives you the panic *value*, but by the time you've recovered it, the runtime's own record of exactly where the original panic occurred is already unwinding away. Re-panicking with a bare `panic(r)` loses that original location information for whoever reads the eventual crash log.

**❌ Bad**
```go
func initPlugin(name string) { /* ... */ }

func loadPlugin(name string) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("plugin %s failed: %v", name, r) // BUG: message only, no stack captured here
			panic(r)                                    // the original call-site stack trace is already gone
		}
	}()
	initPlugin(name)
}
```

**Why it's wrong:**
- The log line records only the panic's message, not where it happened; if this re-panic eventually crashes the process, the crash dump reflects the stack at the point of *this* `panic(r)` call, not the original failure site inside `initPlugin`.
- Anyone debugging from the logs alone has the panic message but no path back to the actual line that triggered it — exactly the information a stack trace exists to provide.

**✅ Good**
```go
func loadPlugin(name string) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("plugin %s failed: %v\n%s", name, r, debug.Stack()) // capture stack now, closest to the original panic
			panic(r)
		}
	}()
	initPlugin(name)
}
```

**Why it works / Explanation:** `debug.Stack()` is called immediately inside the `recover()` block, which is as close to the original panic site as the recovering code can get — capturing it here, before any further unwinding, gives the log the most accurate trace available.

**Design principle:** If you log a recovered panic at all, capture `debug.Stack()` at the point of recovery rather than relying on whatever the eventual, further-unwound crash happens to report.

---

## 9. Panicking with inconsistent, non-error values

**The Problem:** `recover()` returns a value of type `any` — Go doesn't require (or enforce) that panic values implement `error`. Recovery code that blindly type-asserts the recovered value to `error` breaks the moment something panics with a plain string or other type instead.

**❌ Bad**
```go
func mustPositive(n int) int {
	if n < 0 {
		panic("n must be positive") // BUG: panics with a bare string
	}
	return n
}

func safeCall() (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = r.(error) // BUG: panics again — r is a string, not an error
		}
	}()
	mustPositive(-1)
	return nil
}
```

**Why it's wrong:**
- `mustPositive` panics with a `string`, but the recovery code assumes every panic value implements `error` and asserts it directly with `r.(error)` — a single-value type assertion that panics if it fails.
- Instead of gracefully turning the panic into a returned `error`, `safeCall` panics a second time during its own recovery, crashing exactly the code path that existed to prevent a crash.

**✅ Good**
```go
func mustPositive(n int) int {
	if n < 0 {
		panic(fmt.Errorf("n must be positive, got %d", n)) // panic with an error value
	}
	return n
}

func safeCall() (err error) {
	defer func() {
		if r := recover(); r != nil {
			if e, ok := r.(error); ok {
				err = e
			} else {
				err = fmt.Errorf("panic: %v", r) // handles non-error panic values too
			}
		}
	}()
	mustPositive(-1)
	return nil
}
```

**Why it works / Explanation:** Panicking with an `error` value makes it natural for recovery code to preserve it directly; using the two-value form of the type assertion (`r.(error)` with `ok`) or a type switch means an unexpected panic value type is handled gracefully instead of crashing the recovery path itself.

**Design principle:** Prefer panicking with an `error` value for anything that might later be recovered and converted back into one, and always guard type assertions on a recovered value with the two-value form rather than assuming a specific type.

---

## Key Takeaways
- Use `error` for expected conditions and reserve `panic` for invariant violations — don't use panic/recover as a substitute for normal control flow.
- `recover()` only catches panics in the same goroutine; every goroutine you spawn needs its own `defer`/`recover()` if it should survive one.
- Never call `recover()` unconditionally without logging or inspecting what was recovered — silent swallowing hides real bugs.
- `recover()` only has an effect when called directly inside a deferred function during an active panic; always check `if r := recover(); r != nil`.
- A panic during the unwinding of another panic replaces it — give risky cleanup steps their own local recovery so they can't clobber the original.
- Recovering from a panic doesn't undo partial work by itself; always restore invariants (rollback transactions, release locks) via defer, panic or not.
- Install explicit panic-recovery middleware in HTTP servers — the standard library's default recovery doesn't cover goroutines you spawn, and gives clients no clean response.
- Capture `debug.Stack()` at the point of recovery before re-panicking, since the original stack trace context degrades the further the panic propagates.
- Panic with `error` values where possible, and always use the two-value type assertion (or a type switch) when inspecting a recovered value.

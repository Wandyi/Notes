# The defer Statement: Production Pitfalls

`defer` is Go's mechanism for guaranteeing cleanup code runs when a function returns, regardless of which return path was taken. It's simple in principle, but its precise evaluation-timing and ordering rules are a frequent source of subtle production bugs — stale captured values, resources held open far longer than intended, and silently discarded errors from cleanup calls. This file covers the mechanics that matter and the patterns that avoid the common traps.

## 1. Deferred arguments are evaluated immediately, not at execution time

**The Problem:** The arguments to a deferred function call are evaluated the moment the `defer` statement runs, not when the deferred call actually executes at the end of the function. Code that expects a deferred call to see a variable's *final* value, based on its value where the `defer` line is written, gets a stale snapshot instead.

**❌ Bad**
```go
func step1() error { return nil }
func step2() error { return errors.New("step2 failed") }

func doWork() error {
	var err error
	defer log.Println("doWork finished, err:", err) // BUG: err is nil right now — captured immediately
	err = step1()
	if err != nil {
		return err
	}
	err = step2()
	return err
}
```

**Why it's wrong:**
- At the moment the `defer` statement runs, `err` is still `nil` (just declared). That `nil` is evaluated and captured as the argument to `log.Println` right then — the deferred call is already "loaded" with `nil` before `step1` or `step2` ever run.
- No matter what `err` ends up being when `doWork` actually returns, the log line always prints `"doWork finished, err: <nil>"`, silently hiding every real failure from this particular log statement.

**✅ Good**
```go
func doWork() error {
	var err error
	defer func() {
		log.Println("doWork finished, err:", err) // reads err at execution time, via closure
	}()
	err = step1()
	if err != nil {
		return err
	}
	err = step2()
	return err
}
```

**Why it works / Explanation:** Wrapping the call in a closure defers evaluating `err` until the closure actually runs — and because closures capture variables by reference, not by value, `err` is read fresh at that point, correctly reflecting whatever it was set to just before `doWork` returned.

**Design principle:** If a deferred call needs to observe a value as of *return time* rather than *defer time*, wrap it in `defer func() { ... }()` so the read happens inside the closure body, not in the argument list of the `defer` statement itself.

---

## 2. defer inside a loop accumulating unreleased resources

**The Problem:** A `defer` always runs when the *enclosing function* returns — not when the current loop iteration ends. `defer`ring a `Close()` call for a resource opened inside a loop means every single one of those resources stays open until the whole function returns, not just until that iteration finishes with it.

**❌ Bad**
```go
func concatFiles(paths []string) (string, error) {
	var sb strings.Builder
	for _, p := range paths {
		f, err := os.Open(p)
		if err != nil {
			return "", err
		}
		defer f.Close() // BUG: none of these run until concatFiles itself returns
		if _, err := io.Copy(&sb, f); err != nil {
			return "", err
		}
	}
	return sb.String(), nil
}
```

**Why it's wrong:**
- With thousands of paths, every single opened `*os.File` stays open — holding a file descriptor — for the entire remainder of the loop, not just until its own contents are copied.
- On a system with a limited number of file descriptors per process, this can exhaust the limit and start failing `os.Open` calls partway through, well before `concatFiles` would otherwise have finished.

**✅ Good**
```go
func concatFiles(paths []string) (string, error) {
	var sb strings.Builder
	for _, p := range paths {
		if err := appendFile(&sb, p); err != nil {
			return "", err
		}
	}
	return sb.String(), nil
}

func appendFile(sb *strings.Builder, path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close() // released as soon as appendFile returns, every iteration
	_, err = io.Copy(sb, f)
	return err
}
```

**Why it works / Explanation:** Moving the open-use-close sequence into its own function means the `defer` fires when *that* function returns — once per iteration — instead of accumulating for the lifetime of the outer loop.

**Design principle:** Never `defer` a resource release inside a loop body directly; either wrap the body in its own function so the defer fires per-iteration, or call the release explicitly without `defer` when you specifically need it to happen before the next iteration.

---

## 3. Deferred Close() errors being silently discarded

**The Problem:** `defer file.Close()` looks harmless, but `Close()` on a writable file can genuinely fail — some filesystems only report a failed final flush (e.g. running out of disk space) at `Close()` time. A bare `defer f.Close()` throws that returned error away completely.

**❌ Bad**
```go
func writeReport(path string, data []byte) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close() // BUG: a failing flush-on-close error is silently dropped

	_, err = f.Write(data)
	return err
}
```

**Why it's wrong:**
- If `f.Write` succeeds (writing to the OS's page cache) but the actual flush to disk fails at `Close()` time — a real possibility under disk pressure — `writeReport` still returns `nil`, reporting success for data that was never durably written.
- Callers that treat a `nil` error from `writeReport` as "the report is safely on disk" are simply wrong in exactly the cases where it matters most.

**✅ Good**
```go
func writeReport(path string, data []byte) (err error) {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer func() {
		if cerr := f.Close(); cerr != nil && err == nil {
			err = fmt.Errorf("closing report file: %w", cerr) // surfaced via the named return
		}
	}()

	_, err = f.Write(data)
	return err
}
```

**Why it works / Explanation:** Because the return value `err` is named, the deferred closure can assign to it directly. It only overwrites `err` with the close error when there wasn't already a different failure (`err == nil`), so a write failure still takes priority over a close failure, while a close-only failure is no longer silently swallowed.

**Design principle:** For any writable resource where `Close()` can meaningfully fail, capture that error explicitly via a named return and a deferred closure — don't rely on a bare `defer f.Close()`.

---

## 4. Order of multiple defer statements — LIFO, not FIFO

**The Problem:** When a function has multiple `defer` statements, they run in last-in-first-out order: the most recently deferred call executes first. This surprises people who read the statements top-to-bottom and expect them to fire in that same order, and it matters a great deal when defers have ordering-sensitive side effects, like releasing a lock versus logging.

**❌ Bad**
```go
func doWork() {}

func logCompletion() {
	// e.g. ships a log line over the network — can be slow
	fmt.Println("request completed")
}

func handleRequest(mu *sync.Mutex) {
	mu.Lock()
	defer mu.Unlock()     // BUG: deferred FIRST, so LIFO runs it LAST
	defer logCompletion() // deferred SECOND, so LIFO runs it FIRST — while the lock is still held

	doWork()
}
```

**Why it's wrong:**
- LIFO order means `logCompletion()` runs *before* `mu.Unlock()`, even though it was written second — the mutex stays locked for the entire duration of `logCompletion()` too, despite the apparent intent of unlocking promptly after `doWork()`.
- If `logCompletion()` is slow (shipping a log line over the network, writing to disk), every other goroutine blocked waiting on `mu` is held up for that much longer — a self-inflicted contention problem that isn't obvious from reading the two `defer` lines in the order they're written.

**✅ Good**
```go
func handleRequest(mu *sync.Mutex) {
	mu.Lock()
	defer logCompletion() // deferred FIRST, runs LAST — after the lock is released
	defer mu.Unlock()     // deferred SECOND, runs FIRST — releases the lock promptly

	doWork()
}
```

**Why it works / Explanation:** Simply swapping the order the two statements are written in changes their execution order because of LIFO: `mu.Unlock()` now runs immediately after `doWork()` finishes, and `logCompletion()` runs afterward, outside the critical section.

**Design principle:** Read a function's `defer` statements from the bottom up to know their true execution order; when relative ordering matters, that's what determines which `defer` needs to be written last.

---

## 5. Overhead of defer in hot paths

**The Problem:** Before Go 1.14, every `defer` allocated and linked a bookkeeping record onto the goroutine's defer chain, which was measurably slower than a direct call. Some codebases responded by avoiding `defer` entirely in anything remotely performance-sensitive — manually duplicating unlock/close calls on every return path — even though this trades away safety and readability for a performance problem that, in the common case, no longer exists.

**❌ Bad**
```go
func readValue(mu *sync.Mutex, m map[string]int, key string) (int, bool) {
	mu.Lock()
	v, ok := m[key]
	if !ok {
		mu.Unlock() // BUG: easy to forget on this path as the function grows more branches
		return 0, false
	}
	mu.Unlock()
	return v, true
}
```

**Why it's wrong:**
- Manually calling `mu.Unlock()` before every `return` is a maintenance hazard: add one more branch or early return later, forget the matching `Unlock()`, and every goroutine that subsequently tries to acquire `mu` deadlocks permanently.
- The "defer is too slow" justification for writing it this way no longer holds for an ordinary function like this on Go 1.14+: it has a small, fixed number of defers and no loop, so it's exactly the case the compiler optimizes.

**✅ Good**
```go
func readValue(mu *sync.Mutex, m map[string]int, key string) (int, bool) {
	mu.Lock()
	defer mu.Unlock() // cheap: open-coded defer inlines this at compile time (Go 1.14+)
	v, ok := m[key]
	return v, ok
}
```

**Why it works / Explanation:** Go 1.14 introduced open-coded defers, which inline the deferred call directly at each return site for the common case — a function with a small, fixed number of `defer` statements and none of them inside a loop — making it close to as cheap as a direct call. Functions with more than eight defers, or a `defer` executed inside a loop, still fall back to the older, heap-allocated defer chain, which is the specific case worth being mindful of.

**Design principle:** Don't avoid `defer` for "performance" in ordinary code without measuring — it's cheap for the common case today. Reserve restructuring for a profiled hot loop that calls a function with a `defer` millions of times per second, not for every function that happens to touch a mutex or a file.

---

## 6. Modifying a named return value from a defer — and getting it wrong

**The Problem:** A deferred function can modify the value a function returns, but only if the return value is *named*. Writing to a variable that merely shares a name with logic inside the function, when the actual return values are unnamed, has no effect on what the caller receives.

**❌ Bad**
```go
type User struct{ ID int }

func insert(u *User) error      { return nil }
func notifyAudit(u *User) error { return errors.New("audit service unreachable") }

func saveUser(u *User) error { // BUG: return value is unnamed
	err := insert(u)
	defer func() {
		if cerr := notifyAudit(u); cerr != nil {
			err = cerr // BUG: only reassigns the local `err`, not the value already returned
		}
	}()
	return err
}
```

**Why it's wrong:**
- `return err` copies the current value of `err` into the function's return slot *at the moment the `return` statement executes*. Because the return value is unnamed, there is no way for code running afterward (the deferred closure) to reach back and change what's already been placed there.
- The deferred closure's `err = cerr` reassigns the local variable `err`, which looks like it should matter, but the caller has already received whatever value was returned before the defer even ran — so audit-notification failures are silently lost even though the code visually appears to handle them.

**✅ Good**
```go
func saveUser(u *User) (err error) { // return value now named
	err = insert(u)
	defer func() {
		if cerr := notifyAudit(u); cerr != nil {
			err = cerr // now correctly overwrites the actual return value
		}
	}()
	return err
}
```

**Why it works / Explanation:** With `err` declared as the function's named return value, `err` inside the deferred closure refers to the exact same storage location the caller reads from — assigning to it after the `return` statement has already executed still changes what the caller ultimately receives.

**Design principle:** A deferred function can only affect the caller's result if it writes to a *named* return value; if you intend for a defer to alter what's returned, the signature must name that return, not just reuse a similarly-named local variable.

---

## 7. defer never runs if the process exits via os.Exit or log.Fatal

**The Problem:** `os.Exit` terminates the process immediately, without running any pending `defer` calls anywhere in the program. Since `log.Fatal` (and `log.Fatalf`/`log.Fatalln`) calls `os.Exit(1)` internally after logging, calling it from a function that has cleanup deferred means that cleanup silently never happens.

**❌ Bad**
```go
func main() {
	f, err := os.Create("/tmp/output.log")
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close() // BUG: never runs if log.Fatal below is reached

	if err := process(f); err != nil {
		log.Fatal(err) // calls os.Exit(1) internally — skips every pending defer in the program
	}
}

func process(f *os.File) error { return errors.New("processing failed") }
```

**Why it's wrong:**
- When `process` fails, `log.Fatal` logs the error and immediately calls `os.Exit(1)` — the `defer f.Close()` registered earlier in `main` never runs, so any buffered writes to `f` may never be flushed to disk.
- This isn't specific to files — *any* deferred cleanup anywhere in the call stack (unlocking mutexes, closing database connections, flushing metrics) is skipped the moment anything calls `os.Exit`, directly or via `log.Fatal`.

**✅ Good**
```go
func main() {
	if err := run(); err != nil {
		log.Println(err) // log only; run's own defers have already completed
		os.Exit(1)        // exit after cleanup has already happened
	}
}

func run() error {
	f, err := os.Create("/tmp/output.log")
	if err != nil {
		return err
	}
	defer f.Close() // runs normally, because run() returns instead of exiting the process directly

	return process(f)
}
```

**Why it works / Explanation:** All the resource-owning logic lives in `run()`, which returns an `error` instead of ever calling `os.Exit` or `log.Fatal` itself — so its `defer f.Close()` always executes before `run` returns. `main` only calls `os.Exit` after `run` has already fully unwound and every one of its defers has run.

**Design principle:** Keep `os.Exit` (and anything that wraps it, like `log.Fatal`) out of any function that owns resources needing cleanup; confine it to `main`, called only after all defer-bearing code has already returned normally.

---

## 8. defer binding to a stale receiver after reassignment

**The Problem:** `defer resp.Body.Close()` evaluates the method's receiver — `resp.Body` — at the moment the `defer` statement runs, exactly like a regular argument. If the variable `resp` is later reassigned (a common pattern in retry logic), the already-registered defer still targets whatever `resp.Body` was at defer-time, not the new one.

**❌ Bad**
```go
func fetchWithRetry(url string) ([]byte, error) {
	resp, err := http.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close() // binds to THIS resp's Body right now

	if resp.StatusCode == http.StatusServiceUnavailable {
		resp, err = http.Get(url) // BUG: resp is reassigned, but the defer above still targets the OLD resp.Body
		if err != nil {
			return nil, err
		}
	}
	return io.ReadAll(resp.Body)
}
```

**Why it's wrong:**
- `defer resp.Body.Close()` captured the *original* response's body at the point it ran; reassigning `resp` on retry changes what `resp.Body` refers to for the rest of the function, but the defer keeps its original binding.
- The function ends up reading from the *new* response body but only ever closing the *old* one — leaking the new response's underlying connection (it's never returned to the pool), while the retry's replaced original response goes uncle closed by this code entirely.

**✅ Good**
```go
func fetchWithRetry(url string) ([]byte, error) {
	resp, err := doGetWithRetry(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close() // bound to the FINAL resp — retry already happened before this defer exists
	return io.ReadAll(resp.Body)
}

func doGetWithRetry(url string) (*http.Response, error) {
	resp, err := http.Get(url)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusServiceUnavailable {
		resp.Body.Close() // close the stale one explicitly before replacing resp
		return http.Get(url)
	}
	return resp, nil
}
```

**Why it works / Explanation:** The retry logic is isolated inside `doGetWithRetry`, which explicitly closes any response it's about to discard before replacing it. By the time `fetchWithRetry` registers its `defer`, `resp` is already the final response — there's no later reassignment left to go stale.

**Design principle:** Never `defer` a call bound to a variable that might still be reassigned later in the same function; either close intermediate values explicitly before reassigning, or isolate the reassignment logic in its own function so the defer is registered only after the variable's final value is settled.

---

## 9. defer in recursive functions holding resources for the whole call depth

**The Problem:** In a recursive function, a `defer` registered before the recursive call doesn't run until *that specific invocation* returns — which, for the outermost call, is only after every deeper recursive call has already finished. A resource opened and deferred at each level stays open for as long as its entire remaining subtree of recursive calls takes to complete, not just for the duration of the work at that level.

**❌ Bad**
```go
func walkAndCount(dir string) (int, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0, err
	}
	f, err := os.Open(dir) // pretend this is needed for a per-directory metadata read
	if err != nil {
		return 0, err
	}
	defer f.Close() // BUG: doesn't close until THIS call's entire subtree finishes recursing

	count := 0
	for _, e := range entries {
		if e.IsDir() {
			sub, err := walkAndCount(filepath.Join(dir, e.Name()))
			if err != nil {
				return 0, err
			}
			count += sub // every directory handle from root to the deepest leaf is still open right now
		} else {
			count++
		}
	}
	return count, nil
}
```

**Why it's wrong:**
- `walkAndCount` recurses into subdirectories *before* its own `defer f.Close()` runs, so every directory handle opened anywhere along the current recursion path — from the root call down to whichever leaf is currently being processed — is open simultaneously.
- For a deeply nested tree (a large `node_modules`-style directory structure), this can hold hundreds or thousands of file descriptors open at once, even though only one directory's contents are actually being used at any given instant.

**✅ Good**
```go
func walkAndCount(dir string) (int, error) {
	count := 0
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0, err
	}
	if err := touchDirMetadata(dir); err != nil { // handle opened and closed within its own call
		return 0, err
	}

	for _, e := range entries {
		if e.IsDir() {
			sub, err := walkAndCount(filepath.Join(dir, e.Name()))
			if err != nil {
				return 0, err
			}
			count += sub
		} else {
			count++
		}
	}
	return count, nil
}

func touchDirMetadata(dir string) error {
	f, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer f.Close() // closes before walkAndCount ever recurses into subdirectories
	// ... read whatever metadata is needed here
	return nil
}
```

**Why it works / Explanation:** The resource-owning step is extracted into `touchDirMetadata`, which opens, uses, and closes its handle — all before `walkAndCount` reaches the recursive call. Each level's handle is closed before descending into the next, bounding the number of concurrently open handles instead of letting it grow with recursion depth.

**Design principle:** In recursive functions, close any per-call resource before recursing deeper — isolate the resource's use in its own function if needed — so open handles don't accumulate for the entire depth of the recursion.

---

## Key Takeaways
- Deferred function arguments are evaluated at the `defer` statement, not at execution time — use a closure to capture a variable's final value instead.
- Don't `defer` resource cleanup directly inside a loop body — it won't run until the whole function returns; wrap the body in its own function instead.
- A bare `defer file.Close()` silently discards any error `Close()` returns — capture it via a named return and a deferred closure when it matters.
- Multiple `defer` statements run LIFO (last-in-first-out), not in the order they're written — order them deliberately when relative ordering matters.
- Go 1.14's open-coded defers make `defer` cheap for ordinary functions — don't hand-roll manual cleanup on every return path to "avoid the cost."
- A deferred function can only modify a function's actual return value if that return value is named.
- `os.Exit` (and `log.Fatal`, which calls it) skips every pending `defer` in the program — keep it out of functions that own resources needing cleanup.
- `defer` evaluates its receiver at defer-time — reassigning the underlying variable afterward leaves the defer bound to the stale value.
- In recursive functions, close per-call resources before recursing deeper, or handles accumulate for the entire depth of the recursion.

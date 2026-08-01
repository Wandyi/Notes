# Go Production Pitfalls, Gotchas & Best Practices

A comprehensive, topic-wise reference of mistakes, bad design practices, and gotchas that show up in
real production Go codebases — each with a **bad** example, an explanation of *why* it's dangerous,
a **corrected/idiomatic** example, and (where relevant) the design principle or pattern that fixes it
for good.

This is meant to be read topic-by-topic, or used as a checklist during code review.

## How each file is organized

Every topic file follows the same structure for every gotcha discussed:

1. **The Problem** — what goes wrong and why it's tempting/easy to write it this way.
2. **❌ Bad** — a realistic, minimal repro of the mistake.
3. **Why it's wrong** — concrete failure modes: panics, races, leaks, perf cliffs, silent data corruption.
4. **✅ Good** — the idiomatic fix.
5. **Design principle** — the underlying principle/pattern (SOLID, Go proverbs, GoF patterns adapted to Go, etc.) so the fix generalizes beyond the one example.

## Tooling you should already have wired into CI

Many of the issues below are mechanically detectable — don't rely on code review alone:

- `go vet` — catches copylocks, printf format mismatches, struct tag issues, unreachable code.
- `staticcheck` / `golangci-lint` — catches unused code, ineffective assignments, common anti-patterns.
- `go build -race` / `go test -race` — catches data races (run in CI, not just locally).
- `go test -bench . -benchmem` — catches allocation regressions.
- `net/http/pprof` + `go tool pprof` — catches CPU/memory hotspots.
- `go tool trace` — catches goroutine scheduling/blocking issues invisible in pprof.

## Topics

### Core data structures
- [01. Slices](01-slices.md) — nil vs empty, append aliasing, memory leaks via sub-slicing, capacity growth
- [02. Arrays](02-arrays.md) — value semantics, copying costs, fixed size as part of the type
- [03. Maps](03-maps.md) — nil map panics, iteration order, concurrent access, comparability of keys

### Types & abstraction
- [04. Structs & Embedding](04-structs-and-embedding.md) — receiver consistency, embedding ambiguity, copying locks, field alignment
- [05. Interfaces](05-interfaces.md) — typed-nil trap, interface pollution, empty `interface{}`, type assertions
- [06. Pointers & Memory](06-pointers-and-memory.md) — escape analysis, loop-variable capture, indirection cost

### Concurrency
- [07. Goroutines & Concurrency](07-goroutines-and-concurrency.md) — leaks, unbounded spawning, panics crashing the process
- [08. Channels](08-channels.md) — deadlocks, double-close, nil channels, ownership rules
- [09. The `sync` Package](09-sync-package.md) — copying mutexes, `WaitGroup` misuse, `sync.Once`, atomics

### Control flow & errors
- [10. The `context` Package](10-context-package.md) — misuse of `context.Value`, leaks, cancellation
- [11. Error Handling](11-error-handling.md) — swallowed errors, `==` vs `errors.Is`, sentinel vs typed errors
- [12. Panic & Recover](12-panic-and-recover.md) — panic-as-control-flow, goroutine recovery scope, defer ordering
- [13. Defer](13-defer.md) — argument evaluation timing, defer-in-loop, ignored `Close()` errors

### Runtime & performance
- [14. Garbage Collection & Memory](14-garbage-collection-and-memory.md) — leaks, GOGC/GOMEMLIMIT, finalizers, sync.Pool
- [15. Profiling & Benchmarking](15-profiling-and-benchmarking.md) — pprof usage, correct benchmark writing
- [16. Performance Optimization](16-performance-optimization.md) — allocation avoidance, false sharing, preallocation

### Language features
- [17. Generics](17-generics.md) — overuse, constraints, performance trade-offs
- [18. Reflection](18-reflection.md) — cost, panics, `reflect.DeepEqual` pitfalls
- [19. Strings, Runes & Bytes](19-strings-runes-bytes.md) — UTF-8 indexing, conversion costs, EqualFold
- [26. Miscellaneous Gotchas](26-common-gotchas-misc.md) — iota, init(), shadowing, integer overflow, os.Exit

### Ecosystem
- [20. JSON & Serialization](20-json-and-serialization.md) — struct tags, unexported fields, number precision
- [21. Time & Timezones](21-time-and-timezones.md) — monotonic clock, timer leaks, layout strings
- [22. Testing Practices](22-testing-practices.md) — table tests, t.Parallel races, flaky tests

### Architecture
- [23. Logging & Observability](23-logging-and-observability.md) — structured logging, sensitive data, correlation IDs
- [24. Design Patterns in Go](24-design-patterns-in-go.md) — functional options, strategy, decorator, DI, singleton
- [25. Package Design & Project Structure](25-package-design-and-project-structure.md) — naming, boundaries, internal/, API surface

## Suggested reading order

If you're onboarding a team or doing a focused review pass, this order tends to surface the highest-impact issues first:

1. Slices/Maps/Structs/Interfaces (data modeling bugs are the most common source of prod incidents)
2. Goroutines/Channels/sync (concurrency bugs are the hardest to debug after the fact)
3. Error handling/Panic-Recover (determines how gracefully failures degrade)
4. GC/Profiling/Performance (only after correctness is nailed down)
5. Everything else, as needed

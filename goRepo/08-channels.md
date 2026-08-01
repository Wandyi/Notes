# Channels

Channels are Go's primary tool for coordinating goroutines, but their rules — who sends, who closes, what blocks versus what panics — are easy to get backwards under deadline pressure, and the failure modes range from an instant, loud `fatal error: deadlock` to a goroutine that hangs silently for the life of the process. This file covers the channel mistakes that show up repeatedly in production Go code: ownership violations that panic, missing receivers that deadlock, and channel machinery reached for when a simpler tool would have done the job.

## 1. Deadlock on an Unbuffered Channel With No Receiver

**The Problem:** A send on an unbuffered channel blocks until another goroutine is ready to receive. If no such goroutine exists — because it was never started, or already exited — the send blocks forever, and if that happens on the only goroutine left running, the Go runtime detects it and crashes the process outright.

**❌ Bad**
```go
func main() {
	ch := make(chan int)
	ch <- 42 // BUG: unbuffered send with no goroutine anywhere ready to receive
	fmt.Println(<-ch)
}
```

**Why it's wrong:**
- `ch <- 42` blocks immediately because `ch` is unbuffered and nothing is receiving from it yet — and nothing ever will be, since the very next line (the receive) never gets reached.
- Because this is the only goroutine running, the Go runtime detects that literally every goroutine is asleep waiting on something that can never happen, and terminates the program with `fatal error: all goroutines are asleep - deadlock!` — a hard crash, not a hang you can attach a debugger to later.

**✅ Good**
```go
func main() {
	ch := make(chan int)
	go func() {
		fmt.Println(<-ch) // a goroutine is ready to receive before the send happens
	}()
	ch <- 42
	time.Sleep(10 * time.Millisecond) // just for the example; use a WaitGroup in real code
}
```

**Why it works / Explanation:** Starting the receiver goroutine before the send gives `ch <- 42` a partner to synchronize with, so the send can complete. The other common fix is to size the channel's buffer to the number of values you know will be sent before anyone drains it (`make(chan int, 1)` here) — a buffered channel absorbs sends up to its capacity without requiring a simultaneous receiver. Choose between them based on whether you need a receiver to actually be running (unbuffered, for synchronization) or just need sends to not block up to a known count (buffered).

**Design principle:** Every channel send needs a plan for who receives it and when — "unbuffered" means "rendezvous required," not "fire and forget."

---

## 2. Closing a Channel Twice

**The Problem:** `close(ch)` on an already-closed channel panics immediately with `close of closed channel`. This typically happens when more than one goroutine believes it's responsible for closing the same channel — most often in fan-in code where several producer goroutines all feed the same output channel.

**❌ Bad**
```go
func fanIn(sources ...<-chan int) <-chan int {
	out := make(chan int)
	var wg sync.WaitGroup
	for _, src := range sources {
		wg.Add(1)
		go func(src <-chan int) {
			defer wg.Done()
			for v := range src {
				out <- v
			}
			close(out) // BUG: every producer goroutine closes the same shared channel
		}(src)
	}
	wg.Wait()
	return out
}
```

**Why it's wrong:**
- The first producer to finish its `src` closes `out`. The second producer to finish then calls `close(out)` again on an already-closed channel and panics with `close of closed channel`, crashing whichever goroutine reaches it (and, per gotcha #5 in the goroutines file, potentially the whole process if unrecovered).
- Even if only one producer reaches the `range` loop's end at a time by luck in testing, this is a genuine race — the number of producers that "win" the close varies run to run, making it a flaky, hard-to-reproduce crash in production.

**✅ Good**
```go
func fanIn(sources ...<-chan int) <-chan int {
	out := make(chan int)
	var wg sync.WaitGroup
	for _, src := range sources {
		wg.Add(1)
		go func(src <-chan int) {
			defer wg.Done()
			for v := range src {
				out <- v
			}
		}(src)
	}
	go func() {
		wg.Wait()   // wait for every producer to finish draining its source
		close(out)  // exactly one owner closes, exactly once
	}()
	return out
}
```

**Why it works / Explanation:** Closing responsibility moves to a single dedicated goroutine that waits on a `sync.WaitGroup` for all producers to finish, then closes `out` exactly once. No producer goroutine touches `close` directly anymore, so there's no race over who gets to close it. If you truly need multiple independent call sites to be allowed to attempt a close safely, `sync.Once` around the close call is the standard guard — but designing a single owner is almost always the cleaner fix.

**Design principle:** Exactly one goroutine should ever be responsible for closing a given channel — make that ownership explicit in the code structure, not implicit in hoped-for timing.

---

## 3. Sending on a Closed Channel

**The Problem:** A send on a closed channel panics immediately with `send on closed channel` — unlike a receive, which returns the zero value cleanly. This bites when a channel is closed based on some external signal while a sender might still be mid-loop trying to send on it.

**❌ Bad**
```go
func worker(ch chan int, done <-chan struct{}) {
	go func() {
		<-done
		close(ch) // a separate goroutine closes ch based on a "done" signal
	}()

	for i := 0; i < 5; i++ {
		ch <- i // BUG: if done fires mid-loop, this send can race the close above and panic
	}
}
```

**Why it's wrong:**
- There is no ordering guarantee between the `close(ch)` in the spawned goroutine and the `ch <- i` sends in the loop — if `done` fires while the loop still has iterations left, `ch <- i` can execute after `ch` has already been closed, panicking with `send on closed channel`.
- This is a genuine race, not a deterministic bug, so it can pass every test run and then crash in production the first time the timing lines up differently.

**✅ Good**
```go
func worker(ctx context.Context, ch chan int) {
	defer close(ch) // only the sender closes ch, and only after it's done sending
	for i := 0; i < 5; i++ {
		select {
		case ch <- i:
		case <-ctx.Done():
			return // stop sending; the deferred close still runs
		}
	}
}
```

**Why it works / Explanation:** The channel is closed only by the same goroutine that sends on it, and only via `defer` after that goroutine has genuinely finished sending (either by completing the loop or by bailing out early on cancellation) — so there is never another goroutine racing to close `ch` out from under an in-flight send. Cancellation is now expressed through `ctx.Done()`, a separate signal that tells the sender to *stop sending*, rather than something external reaching in and closing the sender's own channel for it.

**Design principle:** Never close a channel from the receiving side, and never let a "stop" signal closed by one goroutine be the same channel another goroutine sends data on — keep control signals and data channels separate.

---

## 4. Nil Channel Operations Block Forever

**The Problem:** Sending or receiving on a `nil` channel doesn't panic — it blocks forever. This happens by accident when a struct field of channel type is never initialized with `make`, and it's used deliberately inside `select` to disable a case.

**❌ Bad**
```go
type Broadcaster struct {
	updates chan string // BUG: zero value of a channel is nil; never initialized with make()
}

func (b *Broadcaster) Notify(msg string) {
	b.updates <- msg // blocks forever: sending on a nil channel never proceeds, no panic, no error
}
```

**Why it's wrong:**
- `Broadcaster{}` (or a struct literal that never sets `updates`) leaves `updates` as its zero value, `nil`. Calling `Notify` on it doesn't crash — it hangs the calling goroutine indefinitely with zero indication of why, since there's no panic or error message to point at the bug.
- This is a particularly nasty class of bug because "forgot to call `make`" produces a hang, not a compile error or an obvious runtime failure — the fix is usually a constructor that guarantees initialization.

**✅ Good — the accidental case, fixed with a constructor**
```go
type Broadcaster struct {
	updates chan string
}

func NewBroadcaster() *Broadcaster {
	return &Broadcaster{updates: make(chan string)} // guarantees the channel is never nil
}
```

**✅ Good — the intentional case: disabling a `select` case**
```go
func merge(a, b <-chan int) {
	for a != nil || b != nil {
		select {
		case v, ok := <-a:
			if !ok {
				a = nil // nil-ing this out disables the case: select will never pick it again
				continue
			}
			fmt.Println("a:", v)
		case v, ok := <-b:
			if !ok {
				b = nil
				continue
			}
			fmt.Println("b:", v)
		}
	}
}
```

**Why it works / Explanation:** For the accidental case, forcing construction through `NewBroadcaster` removes the possibility of an uninitialized channel field entirely. For the intentional case, `select` never selects a `case` whose channel is `nil` — it simply treats that case as permanently not-ready, which is exactly the behavior you want once a source channel is drained and closed: setting it to `nil` cleanly removes it from consideration without an `if` branch inside every iteration of the `select`.

**Design principle:** A `nil` channel blocks by design, not by error — use constructors to prevent it from happening accidentally on struct fields, and use it deliberately in `select` to turn off a case.

---

## 5. Channel Ownership: Only the Sender Closes

**The Problem:** Go's convention is that whichever goroutine owns writing to a channel is the only one that ever closes it — receivers should never close a channel they read from. Violating this ownership rule is the root cause behind both "close of closed channel" and "send on closed channel" panics.

**❌ Bad — receiver closes a channel it doesn't own**
```go
func consume(jobs chan int) {
	for job := range jobs {
		fmt.Println(job)
		if job == -1 {
			close(jobs) // BUG: consumer closing a channel the producer still owns and may still send on
		}
	}
}
```

**Why it's wrong:**
- The consumer has no way to know whether the producer intends to send more values after `-1`. If it does, that later send panics with `send on closed channel` — the consumer's decision to close reached into the producer's responsibility and broke it.
- More generally, once you allow "whoever feels like it" to close a channel, you've lost any way to reason locally about whether a given `close` call is safe — safety now depends on global program behavior.

**✅ Good — fan-out with a single producer, multiple consumers, correct ownership**
```go
func produce(n int) <-chan int {
	jobs := make(chan int)
	go func() {
		defer close(jobs) // the producer is the sole owner and the sole closer
		for i := 0; i < n; i++ {
			jobs <- i
		}
	}()
	return jobs
}

func fanOut(jobs <-chan int, numWorkers int) {
	var wg sync.WaitGroup
	for i := 0; i < numWorkers; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for job := range jobs { // exits cleanly once the producer closes jobs
				fmt.Printf("worker %d processing job %d\n", id, job)
			}
		}(i)
	}
	wg.Wait()
}
```

**Why it works / Explanation:** `produce` is the only place that ever calls `close(jobs)`, and it does so via `defer` right where the sends happen, guaranteeing it runs after the last send and exactly once. None of the `fanOut` worker goroutines close anything — they just `range` until the channel is closed and exit. With N consumers reading from one channel, this is the only safe arrangement: closing must happen exactly once, by the producer, after it's genuinely done sending.

**Design principle:** Ownership of "when to close" belongs to the sender, full stop — receivers signal that they're done consuming through other means (canceling a context, returning from a loop), never by closing the channel they read from.

---

## 6. Reaching for a Channel When a Mutex Would Do

**The Problem:** Channels can implement mutual exclusion (a goroutine "owns" some state and serializes access to it via channel operations), but for a simple shared counter or flag, this is more code, an extra always-running goroutine, and slower than just using a `sync.Mutex`. Channels earn their complexity for pipelines, signaling, and ownership transfer — not for plain mutual exclusion.

**❌ Bad — a whole goroutine and two channels just to guard one int**
```go
type Counter struct {
	incrCh chan struct{}
	valCh  chan int
}

func NewCounter() *Counter {
	c := &Counter{incrCh: make(chan struct{}), valCh: make(chan int)}
	go func() { // BUG: this monitor goroutine exists solely to serialize access to `value`
		value := 0
		for {
			select {
			case <-c.incrCh:
				value++
			case c.valCh <- value:
			}
		}
	}()
	return c
}

func (c *Counter) Incr()      { c.incrCh <- struct{}{} }
func (c *Counter) Value() int { return <-c.valCh }
```

**Why it's wrong:**
- This works correctly, but it costs a permanently-running background goroutine, two channels, and a `select` loop — all to protect a single `int` that a three-line mutex-based type would protect just as correctly.
- Channel operations involve more scheduling overhead than a mutex `Lock`/`Unlock` pair (which can often stay entirely in userspace without a syscall or goroutine handoff), so this version is measurably slower under contention for no benefit.

**✅ Good**
```go
type Counter struct {
	mu    sync.Mutex
	value int
}

func (c *Counter) Incr() {
	c.mu.Lock()
	c.value++
	c.mu.Unlock()
}

func (c *Counter) Value() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.value
}
```

**Why it works / Explanation:** A `sync.Mutex` protecting a plain field is simpler to read, has no background goroutine to leak or crash, and is faster for straightforward mutual exclusion. Channels genuinely earn their complexity when the problem is actually about *communication* rather than exclusion: pipelines where values flow through a sequence of transformation stages, signaling completion or cancellation (`close(done)`), or transferring ownership of a value from one goroutine to another so only one of them ever touches it at a time. A simple "protect this counter" problem is none of those.

**Design principle:** Use a mutex for mutual exclusion over shared state, and a channel for communication or ownership transfer between goroutines — picking the wrong one for the job adds complexity without adding safety.

---

## 7. Skipping Directional Channel Types in Function Signatures

**The Problem:** Function parameters typed as plain `chan T` compile fine even when the function should only ever send or only ever receive — Go offers `chan<- T` (send-only) and `<-chan T` (receive-only) specifically to let the compiler enforce that boundary, and skipping them throws away free correctness checking.

**❌ Bad**
```go
func producer(ch chan int) { // BUG: bidirectional type lets callers misuse ch either way
	defer close(ch)
	for i := 0; i < 5; i++ {
		ch <- i
	}
}

func consumer(ch chan int) {
	// nothing in the type system stops this function from also sending on ch by mistake
	for v := range ch {
		fmt.Println(v)
	}
}
```

**Why it's wrong:**
- With `chan int`, a future edit to `consumer` that accidentally adds `ch <- 0` somewhere (a copy-paste mistake, a merge conflict) compiles without complaint — the compiler had no way to flag it, because the type didn't say "this function only receives."
- Bugs like "the consumer also closes or sends on a channel it should only read from" (see gotchas #2 and #3 above) become much easier to introduce when the function signatures don't restrict what's even possible to write.

**✅ Good**
```go
func producer(ch chan<- int) { // send-only: the compiler rejects any receive on ch here
	defer close(ch)
	for i := 0; i < 5; i++ {
		ch <- i
	}
}

func consumer(ch <-chan int) { // receive-only: the compiler rejects any send on ch here
	for v := range ch {
		fmt.Println(v)
	}
}

func main() {
	ch := make(chan int) // bidirectional at creation
	go producer(ch)      // implicitly converted to chan<- int at the call site
	consumer(ch)          // implicitly converted to <-chan int at the call site
}
```

**Why it works / Explanation:** `make(chan int)` still produces a bidirectional channel, but Go implicitly narrows it to `chan<- int` or `<-chan int` when it's passed to a function expecting that directional type. From that point on, inside `producer`, trying to `<-ch` is a compile error, and inside `consumer`, trying `ch <- v` is a compile error — the type signature documents and enforces each function's role at compile time, for free.

**Design principle:** Say what a function is allowed to do with a channel in its type signature — directional channel types turn a convention ("this function should only send") into a compiler-checked guarantee.

---

## 8. `select` Blocking Forever vs. `select` With `default` Busy-Looping

**The Problem:** A `select` with no `default` blocks until some case is ready — correct for event-driven code, but a bug if you actually needed a non-blocking check. A `select` with `default` never blocks — if used inside a tight loop with nothing to rate-limit it, it spins continuously, checking "is anything ready?" as fast as the CPU allows and burning an entire core for no work done.

**❌ Bad — unintentional busy-loop**
```go
func pollForUpdate(updates <-chan Update) {
	for {
		select {
		case u := <-updates:
			handle(u)
		default:
			// BUG: nothing ready right now, but the loop immediately spins and checks again,
			// burning 100% of a CPU core doing nothing between real updates
		}
	}
}
```

**Why it's wrong:**
- The `default` case makes every loop iteration non-blocking, so when `updates` has nothing to offer, the `for` loop just spins back around and checks again instantly — thousands or millions of times per second — pegging a CPU core at 100% with zero useful work.
- In production this shows up as one goroutine (and its underlying OS thread) permanently maxed out, starving other goroutines of scheduler time and inflating cloud compute costs for a component that's supposed to be idle most of the time.

**✅ Good — blocking select for genuinely event-driven code**
```go
func pollForUpdate(updates <-chan Update) {
	for u := range updates { // blocks until a value arrives or the channel closes; no spinning
		handle(u)
	}
}
```

**✅ Good — bounded-rate polling with `time.Ticker`**
```go
func pollForUpdate(updates <-chan Update) {
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case u := <-updates:
			handle(u)
		case <-ticker.C:
			checkForWork() // runs at most 10 times/sec instead of continuously
		}
	}
}
```

**Why it works / Explanation:** When there's truly nothing else to do while waiting, a blocking `select` (or a plain `range` over a channel) puts the goroutine to sleep until something happens — zero CPU spent waiting. When you genuinely need periodic polling alongside event handling, `time.Ticker` bounds how often the "nothing happened, check anyway" path runs, trading a small fixed latency for eliminating the busy-loop entirely. `default` in a `select` is for the rare case where you need a truly non-blocking check *once*, not as the body of an unthrottled loop.

**Design principle:** Let goroutines sleep when they have nothing to do — block on channel operations for event-driven code, and rate-limit any intentional polling with a ticker instead of a bare loop.

---

## 9. Guessing Buffered Channel Sizes

**The Problem:** Picking a buffer size because "it worked in local testing" bakes in an assumption about the relative speed of producers and consumers that production traffic won't necessarily honor. Under a different load pattern, the same buffer size can turn into unexpected blocking, backpressure, or in constrained scenarios, a deadlock.

**❌ Bad**
```go
func startPipeline(items []Item) <-chan Result {
	results := make(chan Result, 10) // BUG: 10 was picked arbitrarily, based on a quick local test
	for _, item := range items {
		item := item
		go func() {
			results <- compute(item) // fine with a handful of fast items; not fine at scale
		}()
	}
	return results
}
```

**Why it's wrong:**
- With a small test input and a fast consumer, a buffer of 10 never fills, so the code "works." Under real load — many more items, a slower downstream consumer (e.g. writing each result to a database) — the buffer fills up, and every producer beyond the first 10 blocks on `results <-`, backing up an unbounded number of goroutines waiting to send.
- The buffer size was never actually derived from the real producer/consumer rate mismatch, so there's no principled reason to expect it to hold up as traffic patterns shift — it's a magic number that happened to pass whatever test was run against it.

**✅ Good**
```go
func startPipeline(ctx context.Context, items []Item) <-chan Result {
	// Unbuffered: producer and consumer progress in lockstep, so backpressure is
	// immediate and visible instead of hidden behind an arbitrarily sized buffer.
	results := make(chan Result)
	var wg sync.WaitGroup
	for _, item := range items {
		item := item
		wg.Add(1)
		go func() {
			defer wg.Done()
			select {
			case results <- compute(item):
			case <-ctx.Done():
			}
		}()
	}
	go func() {
		wg.Wait()
		close(results)
	}()
	return results
}
```

**Why it works / Explanation:** Going unbuffered removes the illusion of slack — if the consumer falls behind, producers block immediately and visibly (and can bail out via `ctx.Done()`), rather than silently queuing up behind a buffer sized for a load pattern that no longer applies. When a buffer genuinely is the right call — smoothing out bursty producer rates against a steadier consumer — size it from a measured or expected rate mismatch (e.g., "the consumer processes N items/sec slower than peak producer rate, so buffer M seconds of that gap"), and document the reasoning in a comment, rather than picking a round number that happened to pass a test.

**Design principle:** A buffer size is a capacity-planning decision, not a default — either derive it from real throughput numbers, or use an unbuffered channel so backpressure is explicit rather than hidden.

---

## 10. `range` Over a Channel That's Never Closed

**The Problem:** `for v := range ch` blocks waiting for the next value until `ch` is closed — if the producer has any code path that returns without closing the channel (an early return on error, a panic recovered elsewhere), the consumer's `range` loop hangs forever on that path, with no error and no indication of why.

**❌ Bad**
```go
func produce(items []Item) <-chan Item {
	out := make(chan Item)
	go func() {
		for _, item := range items {
			if item.Invalid() {
				return // BUG: early return skips close(out) entirely
			}
			out <- item
		}
		close(out) // only reached on the "no invalid items" happy path
	}()
	return out
}

func consume(items []Item) {
	for item := range produce(items) { // hangs forever if produce ever hit the early return
		fmt.Println(item)
	}
}
```

**Why it's wrong:**
- If any item in `items` is invalid, the producer goroutine returns without ever calling `close(out)`. The consumer's `range` loop is still waiting for the next value (or a close) that will never come — the consumer goroutine hangs permanently, and so does anything waiting on it to finish.
- There's no crash and no log line pointing at the cause — this manifests as a request or job that simply never completes, which is one of the hardest failure modes to diagnose after the fact.

**✅ Good**
```go
func produce(items []Item) <-chan Item {
	out := make(chan Item)
	go func() {
		defer close(out) // runs on every exit path: happy path, early return, or recovered panic
		for _, item := range items {
			if item.Invalid() {
				return
			}
			out <- item
		}
	}()
	return out
}
```

**Why it works / Explanation:** `defer close(out)` is registered once, at the top of the goroutine, so it runs no matter which `return` statement the function actually hits (or even if a `recover()` further up catches a panic first) — there is no longer any code path that leaves `out` un-closed. This is the same principle as "always pair `Lock` with `defer Unlock`": tie the cleanup action to the function's exit via `defer` so it can't be accidentally skipped by a future edit that adds a new early return.

**Design principle:** A channel a consumer ranges over must be closed on every exit path of the producer — use `defer close(ch)` right where the channel is created so no future control-flow change can bypass it.

---

## Key Takeaways
- Unbuffered channel sends need a ready receiver, or they deadlock the whole program with a `fatal error`, not just the sending goroutine.
- Only one goroutine should ever call `close` on a given channel — coordinate multi-producer closes through a `WaitGroup` and a dedicated closer, not every producer independently.
- Never close a data channel from the receiving side, and keep "stop" signals separate from the channel senders are actively writing to, or a send can race a close and panic.
- A `nil` channel blocks forever on send or receive — guard against it accidentally via constructors, and use it deliberately to disable a `select` case.
- Establish single-owner-closes as the hard rule for channel ownership, especially in fan-out/fan-in code with multiple goroutines touching the same channel.
- Reach for a `sync.Mutex` for plain mutual exclusion; reserve channels for pipelines, signaling, and ownership transfer where they actually earn their overhead.
- Use directional channel types (`chan<-`, `<-chan`) in function signatures so the compiler enforces who's allowed to send and who's allowed to receive.
- Use a blocking `select` for event-driven code and a `time.Ticker` for polling — a `default` case inside an unthrottled loop turns into a CPU-burning busy-loop.
- Derive buffered channel sizes from real producer/consumer rate mismatches, or default to unbuffered so backpressure is explicit instead of a surprise under production load.
- Always `defer close(ch)` in the producer so every exit path closes the channel — otherwise a `range` consumer can hang forever on an error path that skipped the close.

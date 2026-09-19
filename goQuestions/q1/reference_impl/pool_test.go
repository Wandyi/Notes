package kafkaworker

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// --- fakes -----------------------------------------------------------------

type fakeFetcher struct {
	mu      sync.Mutex
	batches [][]Record
	polls   atomic.Int64
}

func (f *fakeFetcher) Poll(ctx context.Context) ([]Record, error) {
	f.polls.Add(1)
	f.mu.Lock()
	if len(f.batches) > 0 {
		b := f.batches[0]
		f.batches = f.batches[1:]
		f.mu.Unlock()
		return b, nil
	}
	f.mu.Unlock()
	<-ctx.Done() // no more data: block like a real long-poll
	return nil, ctx.Err()
}

func (f *fakeFetcher) remaining() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.batches)
}

// checkingCommitter fails the test if a commit ever acknowledges an offset
// whose predecessors have not all completed. That is the at-least-once
// safety property, asserted on every commit rather than at the end.
type checkingCommitter struct {
	t         *testing.T
	completed *completionSet
	mu        sync.Mutex
	last      map[TopicPartition]int64
}

func (c *checkingCommitter) Commit(_ context.Context, offsets map[TopicPartition]int64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.last == nil {
		c.last = map[TopicPartition]int64{}
	}
	for tp, off := range offsets {
		if off < c.last[tp] {
			c.t.Errorf("commit went backwards on %v: %d then %d", tp, c.last[tp], off)
		}
		c.last[tp] = off
		for o := int64(0); o < off; o++ {
			if !c.completed.done(tp, o) {
				c.t.Errorf("committed %d on %v while offset %d was still in flight", off, tp, o)
			}
		}
	}
	return nil
}

type completionSet struct {
	mu sync.Mutex
	m  map[TopicPartition]map[int64]int // offset -> delivery count
}

func newCompletionSet() *completionSet {
	return &completionSet{m: map[TopicPartition]map[int64]int{}}
}

func (s *completionSet) mark(r Record) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.m[r.TP()] == nil {
		s.m[r.TP()] = map[int64]int{}
	}
	s.m[r.TP()][r.Offset]++
}

func (s *completionSet) done(tp TopicPartition, off int64) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.m[tp][off] > 0
}

func (s *completionSet) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, offs := range s.m {
		n += len(offs)
	}
	return n
}

func makeBatches(partitions int, perPartition int, batchSize int) ([][]Record, []TopicPartition) {
	var all []Record
	var tps []TopicPartition
	for p := range partitions {
		tp := TopicPartition{Topic: "orders", Partition: int32(p)}
		tps = append(tps, tp)
		for o := range perPartition {
			all = append(all, Record{
				Topic:     tp.Topic,
				Partition: tp.Partition,
				Offset:    int64(o),
				Key:       fmt.Appendf(nil, "key-%d", o%7),
				Value:     fmt.Appendf(nil, "payload-%d-%d", p, o),
			})
		}
	}
	// Interleave partitions the way a real fetch response does, while keeping
	// each partition's offsets ascending.
	var batches [][]Record
	for i := 0; i < len(all); i += batchSize {
		batches = append(batches, all[i:min(i+batchSize, len(all))])
	}
	return batches, tps
}

// --- tests -----------------------------------------------------------------

// The core safety test: 4 partitions, out-of-order completion, random
// latency, a failing dependency, and a commit ticker running concurrently.
// Every commit is checked against the completion set.
func TestAtLeastOnceUnderConcurrentCompletion(t *testing.T) {
	batches, tps := makeBatches(4, 250, 17)
	completed := newCompletionSet()
	fetcher := &fakeFetcher{batches: batches}
	committer := &checkingCommitter{t: t, completed: completed}

	tracker := NewOffsetTracker()
	tracker.Assign(tps...)

	var handled atomic.Int64
	handler := HandlerFunc(func(ctx context.Context, r Record) error {
		// Jittered latency guarantees out-of-order completion.
		select {
		case <-time.After(time.Duration(rand.IntN(400)) * time.Microsecond):
		case <-ctx.Done():
			return ctx.Err()
		}
		handled.Add(1)
		if rand.IntN(20) == 0 {
			return errors.New("transient downstream error")
		}
		completed.mark(r)
		return nil
	})

	metrics := &CountingMetrics{}
	pool := NewPool(PoolConfig{
		Workers:        16,
		QueueDepth:     8,
		HandlerTimeout: time.Second,
		MaxRetries:     5,
		Backoff:        func(int) time.Duration { return time.Millisecond },
		Handler:        handler,
		Tracker:        tracker,
		Metrics:        metrics,
		Limiter: NewAdaptiveLimiter(LimiterConfig{
			Start: 16, Max: 16, Min: 2, LatencyBudget: 50 * time.Millisecond,
		}),
	})

	consumer, err := NewConsumer(ConsumerConfig{
		Fetcher:        fetcher,
		Committer:      committer,
		Pool:           pool,
		Tracker:        tracker,
		Metrics:        metrics,
		CommitInterval: 2 * time.Millisecond,
		DrainTimeout:   10 * time.Second,
		OnError:        func(err error) { t.Logf("consumer: %v", err) },
	})
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- consumer.Run(ctx) }()

	// Let it consume everything, then ask for a graceful stop. Waiting on the
	// completion count (not just an empty fetch queue) avoids racing the
	// window between the last Poll and the first Track of that batch.
	waitFor(t, 10*time.Second, func() bool {
		return fetcher.remaining() == 0 && completed.count() == 4*250 && tracker.InFlight() == 0
	})
	cancel()

	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Run did not return after cancellation")
	}

	if got, want := completed.count(), 4*250; got != want {
		t.Fatalf("completed %d distinct records, want %d", got, want)
	}
	if n := tracker.InFlight(); n != 0 {
		t.Fatalf("in-flight = %d after shutdown, want 0", n)
	}
	for _, tp := range tps {
		if got := committer.last[tp]; got != 250 {
			t.Errorf("final committed offset on %v = %d, want 250", tp, got)
		}
	}
	if metrics.BusyNanos.Load() == 0 {
		t.Error("worker busy time was never recorded")
	}
	if handled.Load() < int64(4*250) {
		t.Errorf("handler invoked %d times, want at least %d", handled.Load(), 4*250)
	}
}

// Backpressure: the pool must never buffer more than QueueDepth records, and
// the fetch loop must block rather than accumulate an unbounded backlog.
func TestQueueDepthIsBounded(t *testing.T) {
	const depth = 4
	release := make(chan struct{})
	var peak atomic.Int64

	pool := NewPool(PoolConfig{
		Workers:        2,
		QueueDepth:     depth,
		HandlerTimeout: 5 * time.Second,
		Handler: HandlerFunc(func(ctx context.Context, _ Record) error {
			select {
			case <-release:
			case <-ctx.Done():
				return ctx.Err()
			}
			return nil
		}),
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pool.Start(ctx)

	// Two workers block immediately; the queue then holds at most `depth`.
	submitted := 0
	for range 100 {
		if !pool.TrySubmit(Record{Topic: "t", Offset: int64(submitted)}) {
			break
		}
		submitted++
		if d := int64(pool.QueueDepth()); d > peak.Load() {
			peak.Store(d)
		}
	}

	if peak.Load() > depth {
		t.Fatalf("queue depth reached %d, want <= %d", peak.Load(), depth)
	}
	if submitted > depth+2 {
		t.Fatalf("accepted %d records with depth %d and 2 workers", submitted, depth)
	}

	close(release)
	pool.Close()
}

// Ordered mode must serialise records that share a key, while still running
// different keys in parallel.
func TestOrderedModePreservesPerKeyOrder(t *testing.T) {
	var mu sync.Mutex
	seen := map[string][]int64{}

	pool := NewPool(PoolConfig{
		Workers:        8,
		QueueDepth:     16,
		Ordered:        true,
		HandlerTimeout: time.Second,
		Handler: HandlerFunc(func(_ context.Context, r Record) error {
			time.Sleep(time.Duration(rand.IntN(200)) * time.Microsecond)
			mu.Lock()
			seen[string(r.Key)] = append(seen[string(r.Key)], r.Offset)
			mu.Unlock()
			return nil
		}),
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pool.Start(ctx)

	for off := range int64(400) {
		r := Record{
			Topic:  "t",
			Offset: off,
			Key:    fmt.Appendf(nil, "k%d", off%5),
		}
		if err := pool.Submit(ctx, r); err != nil {
			t.Fatal(err)
		}
	}
	pool.Close()

	for key, offs := range seen {
		for i := 1; i < len(offs); i++ {
			if offs[i] < offs[i-1] {
				t.Fatalf("key %s processed out of order: %v", key, offs)
			}
		}
	}
	if len(seen) != 5 {
		t.Fatalf("saw %d keys, want 5", len(seen))
	}
}

// A record that exhausts its retries with no dead-letter sink must NOT be
// acked: the watermark freezes at the poison pill instead of losing it.
func TestPoisonPillStallsWatermarkByDefault(t *testing.T) {
	tracker := NewOffsetTracker()
	tracker.Assign(tp0)

	pool := NewPool(PoolConfig{
		Workers:        1,
		QueueDepth:     4,
		MaxRetries:     0,
		HandlerTimeout: time.Second,
		Tracker:        tracker,
		Handler: HandlerFunc(func(_ context.Context, r Record) error {
			if r.Offset == 1 {
				return fmt.Errorf("bad payload: %w", ErrFatal)
			}
			return nil
		}),
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pool.Start(ctx)

	for off := int64(0); off < 3; off++ {
		tracker.Track(rec(off))
		if err := pool.Submit(ctx, rec(off)); err != nil {
			t.Fatal(err)
		}
	}
	pool.Close()

	if got := tracker.Committable()[tp0]; got != 1 {
		t.Fatalf("watermark = %d, want 1 (frozen at the poison pill)", got)
	}
}

// The same record with a dead-letter sink must be acked so the partition
// keeps moving.
func TestDeadLetterUnblocksWatermark(t *testing.T) {
	tracker := NewOffsetTracker()
	tracker.Assign(tp0)
	var dlq atomic.Int64

	pool := NewPool(PoolConfig{
		Workers:        1,
		QueueDepth:     4,
		MaxRetries:     0,
		HandlerTimeout: time.Second,
		Tracker:        tracker,
		DeadLetter: DeadLetterFunc(func(context.Context, Record, error) error {
			dlq.Add(1)
			return nil
		}),
		Handler: HandlerFunc(func(_ context.Context, r Record) error {
			if r.Offset == 1 {
				return fmt.Errorf("bad payload: %w", ErrFatal)
			}
			return nil
		}),
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pool.Start(ctx)

	for off := int64(0); off < 3; off++ {
		tracker.Track(rec(off))
		if err := pool.Submit(ctx, rec(off)); err != nil {
			t.Fatal(err)
		}
	}
	pool.Close()

	if got := dlq.Load(); got != 1 {
		t.Fatalf("dead-lettered %d records, want 1", got)
	}
	if got := tracker.Committable()[tp0]; got != 3 {
		t.Fatalf("watermark = %d, want 3", got)
	}
}

// ErrFatal must skip the retry loop entirely.
func TestFatalErrorSkipsRetries(t *testing.T) {
	var attempts atomic.Int64
	pool := NewPool(PoolConfig{
		Workers:         1,
		QueueDepth:      1,
		MaxRetries:      10,
		HandlerTimeout:  time.Second,
		DropOnExhausted: true,
		Handler: HandlerFunc(func(context.Context, Record) error {
			attempts.Add(1)
			return fmt.Errorf("schema mismatch: %w", ErrFatal)
		}),
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pool.Start(ctx)
	if err := pool.Submit(ctx, Record{Topic: "t"}); err != nil {
		t.Fatal(err)
	}
	pool.Close()

	if got := attempts.Load(); got != 1 {
		t.Fatalf("handler called %d times, want 1", got)
	}
}

// Retries must be interruptible: shutdown cannot wait out the backoff.
func TestBackoffIsCancellable(t *testing.T) {
	pool := NewPool(PoolConfig{
		Workers:        1,
		QueueDepth:     1,
		MaxRetries:     100,
		HandlerTimeout: time.Second,
		Backoff:        func(int) time.Duration { return time.Hour },
		Handler: HandlerFunc(func(context.Context, Record) error {
			return errors.New("always fails")
		}),
	})
	ctx, cancel := context.WithCancel(context.Background())
	pool.Start(ctx)
	if err := pool.Submit(ctx, Record{Topic: "t"}); err != nil {
		t.Fatal(err)
	}

	time.Sleep(20 * time.Millisecond) // let it enter the backoff sleep
	cancel()

	closed := make(chan struct{})
	go func() { pool.Close(); close(closed) }()
	select {
	case <-closed:
	case <-time.After(2 * time.Second):
		t.Fatal("Close blocked on a backoff sleep instead of honouring cancellation")
	}
}

func waitFor(t *testing.T, timeout time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("condition not met before timeout")
}

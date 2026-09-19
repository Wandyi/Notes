package kafkaworker

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestLimiterBoundsConcurrency(t *testing.T) {
	const limit = 4
	l := NewAdaptiveLimiter(LimiterConfig{Max: limit, Min: limit}) // pinned
	var cur, peak atomic.Int64
	var wg sync.WaitGroup

	for range 64 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := l.Acquire(context.Background()); err != nil {
				t.Error(err)
				return
			}
			n := cur.Add(1)
			for {
				old := peak.Load()
				if n <= old || peak.CompareAndSwap(old, n) {
					break
				}
			}
			time.Sleep(time.Millisecond)
			cur.Add(-1)
			l.Release(Outcome{Latency: time.Millisecond})
		}()
	}
	wg.Wait()

	if got := peak.Load(); got > limit {
		t.Fatalf("peak concurrency = %d, want <= %d", got, limit)
	}
	if got := l.InFlight(); got != 0 {
		t.Fatalf("leaked %d permits", got)
	}
}

func TestLimiterHalvesOnFailure(t *testing.T) {
	l := NewAdaptiveLimiter(LimiterConfig{Start: 16, Max: 16, Min: 2})
	boom := errors.New("downstream timeout")

	if err := l.Acquire(context.Background()); err != nil {
		t.Fatal(err)
	}
	l.Release(Outcome{Err: boom})
	if got := l.Limit(); got != 8 {
		t.Fatalf("limit = %d, want 8 after one failure", got)
	}

	for range 5 {
		if err := l.Acquire(context.Background()); err != nil {
			t.Fatal(err)
		}
		l.Release(Outcome{Err: boom})
	}
	if got := l.Limit(); got != 2 {
		t.Fatalf("limit = %d, want the floor of 2", got)
	}
}

func TestLimiterTreatsSlowSuccessAsCongestion(t *testing.T) {
	l := NewAdaptiveLimiter(LimiterConfig{Start: 8, Max: 8, Min: 1, LatencyBudget: 100 * time.Millisecond})
	if err := l.Acquire(context.Background()); err != nil {
		t.Fatal(err)
	}
	l.Release(Outcome{Latency: 250 * time.Millisecond}) // succeeded, but slow
	if got := l.Limit(); got != 4 {
		t.Fatalf("limit = %d, want 4 — a slow success is still a congestion signal", got)
	}
}

func TestLimiterRecoversAdditively(t *testing.T) {
	l := NewAdaptiveLimiter(LimiterConfig{Start: 2, Max: 8, Min: 1})
	for range 2 { // limit successes at limit=2 → one increase
		_ = l.Acquire(context.Background())
		l.Release(Outcome{Latency: time.Millisecond})
	}
	if got := l.Limit(); got != 3 {
		t.Fatalf("limit = %d, want 3 after 2 clean releases at limit 2", got)
	}
}

func TestLimiterAcquireHonoursContext(t *testing.T) {
	l := NewAdaptiveLimiter(LimiterConfig{Max: 1, Min: 1})
	if err := l.Acquire(context.Background()); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if err := l.Acquire(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Acquire err = %v, want DeadlineExceeded", err)
	}

	// The abandoned waiter must not have consumed a permit.
	l.Release(Outcome{Latency: time.Millisecond})
	if got := l.InFlight(); got != 0 {
		t.Fatalf("in-flight = %d after cancelled waiter, want 0", got)
	}
	if err := l.Acquire(context.Background()); err != nil {
		t.Fatalf("limiter deadlocked after a cancelled waiter: %v", err)
	}
}

// Shrinking the limit while permits are outstanding must not hand out extra
// permits or drop waiters.
func TestLimiterShrinkWithWaiters(t *testing.T) {
	l := NewAdaptiveLimiter(LimiterConfig{Start: 4, Max: 4, Min: 1})
	for range 4 {
		if err := l.Acquire(context.Background()); err != nil {
			t.Fatal(err)
		}
	}

	acquired := make(chan struct{}, 4)
	for range 4 {
		go func() {
			if err := l.Acquire(context.Background()); err == nil {
				acquired <- struct{}{}
			}
		}()
	}

	// Release all four with failures: limit collapses 4→2→1→1→1.
	for range 4 {
		l.Release(Outcome{Err: errors.New("boom")})
	}

	deadline := time.After(2 * time.Second)
	got := 0
	for got < 1 {
		select {
		case <-acquired:
			got++
		case <-deadline:
			t.Fatal("no waiter was granted a permit after releases")
		}
	}
	if l.InFlight() > 4 {
		t.Fatalf("in-flight = %d exceeds the pre-shrink ceiling", l.InFlight())
	}
}

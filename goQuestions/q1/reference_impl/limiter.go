package kafkaworker

import (
	"container/list"
	"context"
	"sync"
	"time"
)

// AdaptiveLimiter is a resizable counting semaphore that guards calls to a
// downstream dependency (database, HTTP API, gRPC service).
//
// A fixed worker count bounds concurrency, but it does not bound *pressure on
// the dependency*: if the database slows from 5ms to 500ms, N workers happily
// pile N concurrent slow queries onto it and make it worse. The limiter closes
// that loop with AIMD (additive increase, multiplicative decrease), the same
// control law TCP congestion control uses:
//
//   - every `limit` consecutive successes: limit++      (probe for headroom)
//   - any failure or over-latency response: limit /= 2  (back off fast)
//
// Workers that cannot acquire a permit block, the in-memory queue fills,
// the fetch loop stops polling, and backpressure propagates all the way to
// the broker as consumer lag rather than as an OOM or a downstream outage.
type AdaptiveLimiter struct {
	mu       sync.Mutex
	limit    int
	inFlight int
	waiters  *list.List // of *permitWaiter, FIFO

	min, max int
	// latencyBudget is the response time above which a successful call is
	// still treated as a congestion signal. Zero disables latency-based
	// backoff.
	latencyBudget time.Duration
	successStreak int

	// limitChanged is set by adjustLocked so onChange can fire outside the
	// mutex, where user code cannot deadlock the limiter.
	limitChanged bool
	onChange     func(limit int)
}

type permitWaiter struct {
	ready   chan struct{}
	granted bool
}

// LimiterConfig configures an AdaptiveLimiter.
type LimiterConfig struct {
	// Start is the initial permit count. Defaults to Max.
	Start int
	// Min is the floor. The limiter never drops below it, so a fully
	// degraded dependency still receives trickle traffic to probe recovery.
	// Defaults to 1.
	Min int
	// Max is the ceiling, normally the worker count.
	Max int
	// LatencyBudget treats slow-but-successful calls as congestion.
	LatencyBudget time.Duration
	// OnChange, if set, is called whenever the limit moves. Use it to export
	// the limit as a gauge.
	OnChange func(limit int)
}

// NewAdaptiveLimiter builds a limiter from cfg.
func NewAdaptiveLimiter(cfg LimiterConfig) *AdaptiveLimiter {
	if cfg.Max < 1 {
		cfg.Max = 1
	}
	if cfg.Min < 1 {
		cfg.Min = 1
	}
	if cfg.Min > cfg.Max {
		cfg.Min = cfg.Max
	}
	if cfg.Start <= 0 || cfg.Start > cfg.Max {
		cfg.Start = cfg.Max
	}
	if cfg.Start < cfg.Min {
		cfg.Start = cfg.Min
	}
	return &AdaptiveLimiter{
		limit:         cfg.Start,
		waiters:       list.New(),
		min:           cfg.Min,
		max:           cfg.Max,
		latencyBudget: cfg.LatencyBudget,
		onChange:      cfg.OnChange,
	}
}

// Acquire blocks until a permit is available or ctx is done. On success the
// caller must eventually call Release exactly once.
func (l *AdaptiveLimiter) Acquire(ctx context.Context) error {
	l.mu.Lock()
	if l.inFlight < l.limit {
		l.inFlight++
		l.mu.Unlock()
		return nil
	}
	w := &permitWaiter{ready: make(chan struct{})}
	elem := l.waiters.PushBack(w)
	l.mu.Unlock()

	select {
	case <-w.ready:
		// The releasing goroutine transferred its permit to us; inFlight
		// was never decremented, so there is nothing to increment here.
		return nil
	case <-ctx.Done():
		l.mu.Lock()
		if w.granted {
			// Raced with a grant: we own a permit we no longer want.
			l.mu.Unlock()
			l.Release()
			return ctx.Err()
		}
		l.waiters.Remove(elem)
		l.mu.Unlock()
		return ctx.Err()
	}
}

// Release returns a permit and feeds the AIMD controller. Pass the observed
// call latency and whether the call succeeded.
func (l *AdaptiveLimiter) Release(outcome ...Outcome) {
	l.mu.Lock()
	if len(outcome) > 0 {
		l.adjustLocked(outcome[0])
	}
	l.releaseLocked()
	changed := l.limitChanged
	l.limitChanged = false
	newLimit := l.limit
	l.mu.Unlock()
	if changed && l.onChange != nil {
		l.onChange(newLimit)
	}
}

// Outcome is the result of one downstream call, fed back into the controller.
type Outcome struct {
	Latency time.Duration
	Err     error
}

func (l *AdaptiveLimiter) releaseLocked() {
	// Hand the permit straight to the oldest waiter when the (possibly just
	// shrunk) limit still has room for it. That keeps FIFO fairness and
	// avoids a thundering herd of wakeups racing for one permit.
	if l.waiters.Len() > 0 && l.inFlight <= l.limit {
		elem := l.waiters.Front()
		l.waiters.Remove(elem)
		w := elem.Value.(*permitWaiter)
		w.granted = true
		close(w.ready)
		return
	}
	l.inFlight--
}

func (l *AdaptiveLimiter) adjustLocked(o Outcome) {
	old := l.limit
	congested := o.Err != nil ||
		(l.latencyBudget > 0 && o.Latency > l.latencyBudget)

	if congested {
		l.successStreak = 0
		l.limit = max(l.min, l.limit/2)
	} else {
		l.successStreak++
		if l.successStreak >= l.limit {
			l.successStreak = 0
			l.limit = min(l.max, l.limit+1)
		}
	}
	if l.limit != old {
		l.limitChanged = true
	}
}

// Limit returns the current permit ceiling.
func (l *AdaptiveLimiter) Limit() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.limit
}

// InFlight returns the number of outstanding permits.
func (l *AdaptiveLimiter) InFlight() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.inFlight
}

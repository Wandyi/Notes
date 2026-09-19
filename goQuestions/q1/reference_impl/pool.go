package kafkaworker

import (
	"context"
	"errors"
	"hash/fnv"
	"math/rand/v2"
	"sync"
	"sync/atomic"
	"time"
)

// ErrFatal marks a handler error as non-retryable (malformed payload, schema
// violation, business-rule rejection). Wrap it to skip the retry loop and go
// straight to the dead-letter path:
//
//	return fmt.Errorf("decode order: %w", ErrFatal)
var ErrFatal = errors.New("fatal: record is not retryable")

// PoolConfig configures a Pool.
type PoolConfig struct {
	// Workers is the hard upper bound on concurrent handler invocations.
	Workers int

	// QueueDepth is the buffer capacity per queue. It is the shock absorber
	// between a bursty fetch loop and steady workers. Keep it small: a deep
	// queue only converts backpressure into latency and into records that
	// must be reprocessed after a crash.
	QueueDepth int

	// Ordered routes records with the same key to the same single-threaded
	// queue, preserving per-key order at the cost of head-of-line blocking
	// within a key. Leave false when the handler is commutative.
	Ordered bool

	// HandlerTimeout bounds one handler invocation. It must be well below
	// max.poll.interval.ms, or a slow handler triggers a rebalance.
	HandlerTimeout time.Duration

	// MaxRetries is the number of retries after the first attempt.
	MaxRetries int

	// Backoff returns the delay before attempt n (1-based). Defaults to
	// exponential backoff with full jitter, capped at 30s.
	Backoff func(attempt int) time.Duration

	// DropOnExhausted decides what happens to a record that exhausted its
	// retries when no DeadLetter sink is configured, or when the sink itself
	// fails: true acks it (data loss, throughput preserved), false leaves it
	// un-acked so the commit watermark freezes and the partition stops
	// making progress until an operator intervenes. Default false — a stuck
	// partition is loud, silent data loss is not.
	DropOnExhausted bool

	Handler    Handler
	DeadLetter DeadLetter
	Limiter    *AdaptiveLimiter
	Tracker    *OffsetTracker
	Metrics    Metrics
}

// Pool is a bounded set of worker goroutines that consume from in-memory
// queues and invoke the handler.
type Pool struct {
	cfg       PoolConfig
	queues    []chan Record
	perQ      int
	wg        sync.WaitGroup
	queued    atomic.Int64
	rr        atomic.Uint64
	closeOnce sync.Once
}

// NewPool validates cfg and constructs the pool. Call Start to launch workers.
func NewPool(cfg PoolConfig) *Pool {
	if cfg.Workers < 1 {
		cfg.Workers = 1
	}
	if cfg.QueueDepth < 0 {
		cfg.QueueDepth = 0
	}
	if cfg.HandlerTimeout <= 0 {
		cfg.HandlerTimeout = 30 * time.Second
	}
	if cfg.Backoff == nil {
		cfg.Backoff = ExponentialBackoff(50*time.Millisecond, 30*time.Second)
	}
	if cfg.Metrics == nil {
		cfg.Metrics = NopMetrics{}
	}
	if cfg.Tracker == nil {
		cfg.Tracker = NewOffsetTracker()
	}

	p := &Pool{cfg: cfg}
	if cfg.Ordered {
		// One queue per worker: a queue is a serialisation domain, so each
		// must have exactly one consumer.
		p.queues = make([]chan Record, cfg.Workers)
		p.perQ = 1
	} else {
		// One shared queue: work-stealing for free, no key affinity.
		p.queues = make([]chan Record, 1)
		p.perQ = cfg.Workers
	}
	for i := range p.queues {
		p.queues[i] = make(chan Record, cfg.QueueDepth)
	}
	return p
}

// Start launches the workers. Cancelling ctx is an *abort*: in-flight
// handlers are cancelled and un-acked records are left for redelivery. For a
// clean stop, call Close (which drains) and cancel ctx only afterwards, or as
// a deadline backstop.
func (p *Pool) Start(ctx context.Context) {
	for i := range p.queues {
		for range p.perQ {
			p.wg.Add(1)
			go p.worker(ctx, p.queues[i])
		}
	}
}

// Submit hands a record to a worker queue, blocking while the queue is full.
// That block is the backpressure signal: the caller (the fetch loop) stops
// polling, the client stops fetching, and the backlog stays on the broker
// where it is durable and observable as consumer lag.
//
// The record must already be registered with the tracker via Track.
func (p *Pool) Submit(ctx context.Context, r Record) error {
	q := p.queues[p.shardFor(r)]
	select {
	case q <- r:
		p.cfg.Metrics.SetQueueDepth(int(p.queued.Add(1)))
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// TrySubmit is the non-blocking variant, useful when the caller would rather
// pause the partition than block the fetch loop.
func (p *Pool) TrySubmit(r Record) bool {
	q := p.queues[p.shardFor(r)]
	select {
	case q <- r:
		p.cfg.Metrics.SetQueueDepth(int(p.queued.Add(1)))
		return true
	default:
		return false
	}
}

// Close stops accepting work and waits for every buffered record to reach a
// terminal outcome. It is idempotent and safe to call once from the shutdown
// path only after the fetch loop has stopped calling Submit — sending on a
// closed channel panics.
func (p *Pool) Close() {
	p.closeOnce.Do(func() {
		for _, q := range p.queues {
			close(q)
		}
	})
	p.wg.Wait()
}

// QueueDepth returns the number of records buffered across all queues.
func (p *Pool) QueueDepth() int { return int(p.queued.Load()) }

func (p *Pool) shardFor(r Record) int {
	if len(p.queues) == 1 {
		return 0
	}
	if len(r.Key) == 0 {
		// No key means no ordering contract; spread round-robin so a null-key
		// burst does not pin one worker.
		return int(p.rr.Add(1) % uint64(len(p.queues)))
	}
	h := fnv.New32a()
	_, _ = h.Write(r.Key)
	return int(h.Sum32() % uint32(len(p.queues)))
}

func (p *Pool) worker(ctx context.Context, q <-chan Record) {
	defer p.wg.Done()
	for r := range q {
		p.cfg.Metrics.SetQueueDepth(int(p.queued.Add(-1)))
		if ctx.Err() != nil {
			// Aborting. Leave the record un-acked: the watermark holds and
			// the broker redelivers it. Keep draining the channel so the
			// range loop terminates instead of leaking this goroutine.
			continue
		}
		p.process(ctx, r)
	}
}

// process runs one record to a terminal outcome: acked (success or dead
// lettered), or deliberately left un-acked.
func (p *Pool) process(ctx context.Context, r Record) {
	start := time.Now()
	var lastErr error

	for attempt := 0; ; attempt++ {
		if attempt > 0 {
			p.cfg.Metrics.ObserveRetry()
			if !sleepCtx(ctx, p.cfg.Backoff(attempt)) {
				return // shutting down; no ack, redelivery will retry
			}
		}

		err := p.invoke(ctx, r)
		if err == nil {
			p.cfg.Metrics.ObserveProcess(time.Since(start), "ok")
			p.cfg.Tracker.Ack(r)
			return
		}
		lastErr = err

		// A cancelled root context means shutdown, not a bad record. Do not
		// burn a retry and do not dead-letter — leave it for redelivery.
		if ctx.Err() != nil {
			return
		}
		if errors.Is(err, ErrFatal) || attempt >= p.cfg.MaxRetries {
			break
		}
	}

	p.deadLetter(ctx, r, lastErr, start)
}

func (p *Pool) deadLetter(ctx context.Context, r Record, cause error, start time.Time) {
	if p.cfg.DeadLetter != nil {
		if err := p.cfg.DeadLetter.Publish(ctx, r, cause); err == nil {
			p.cfg.Metrics.ObserveProcess(time.Since(start), "retry_exhausted")
			p.cfg.Tracker.Ack(r)
			return
		}
	}
	if p.cfg.DropOnExhausted {
		p.cfg.Metrics.ObserveProcess(time.Since(start), "dropped")
		p.cfg.Tracker.Ack(r)
		return
	}
	// No ack. The partition's commit watermark stops at this offset; lag
	// climbs and pages someone. That is the intended failure mode.
	p.cfg.Metrics.ObserveProcess(time.Since(start), "stalled")
}

// invoke performs one handler attempt under the downstream permit and the
// per-record timeout.
func (p *Pool) invoke(ctx context.Context, r Record) error {
	if p.cfg.Limiter != nil {
		waitStart := time.Now()
		if err := p.cfg.Limiter.Acquire(ctx); err != nil {
			return err
		}
		p.cfg.Metrics.ObserveBlockedOnLimiter(time.Since(waitStart))
	}

	hctx, cancel := context.WithTimeout(ctx, p.cfg.HandlerTimeout)
	defer cancel()

	callStart := time.Now()
	err := p.cfg.Handler.Handle(hctx, r)
	elapsed := time.Since(callStart)

	p.cfg.Metrics.AddWorkerBusy(elapsed)
	if p.cfg.Limiter != nil {
		p.cfg.Limiter.Release(Outcome{Latency: elapsed, Err: err})
		p.cfg.Metrics.SetConcurrencyLimit(p.cfg.Limiter.Limit())
	}
	return err
}

// ExponentialBackoff returns a backoff function with full jitter, which
// de-synchronises retries so a recovering dependency is not hit by a
// thundering herd of every worker retrying on the same schedule.
func ExponentialBackoff(base, cap time.Duration) func(attempt int) time.Duration {
	return func(attempt int) time.Duration {
		if attempt < 1 {
			attempt = 1
		}
		d := base
		for range attempt - 1 {
			d *= 2
			if d >= cap {
				d = cap
				break
			}
		}
		return time.Duration(rand.Int64N(int64(d) + 1))
	}
}

// sleepCtx waits for d and reports whether the wait completed. A plain
// time.Sleep here would make shutdown wait for the longest backoff.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return ctx.Err() == nil
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return true
	case <-ctx.Done():
		return false
	}
}

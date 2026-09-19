package kafkaworker

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"
)

// ConsumerConfig configures a Consumer.
type ConsumerConfig struct {
	Fetcher   Fetcher
	Committer Committer
	Pool      *Pool
	Tracker   *OffsetTracker
	Metrics   Metrics

	// CommitInterval is how often completed offsets are flushed to the
	// broker. It is a pure durability/throughput knob: shorter means fewer
	// duplicates after a crash, more commit RPCs.
	CommitInterval time.Duration

	// CommitTimeout bounds one OffsetCommit RPC.
	CommitTimeout time.Duration

	// DrainTimeout is how long a graceful shutdown waits for in-flight
	// records before cancelling them. Anything still running is abandoned
	// un-acked and redelivered after restart.
	DrainTimeout time.Duration

	// OnError receives non-fatal errors (commit failures, poll retries).
	OnError func(err error)
}

// Consumer wires the fetch loop, the worker pool and the commit loop
// together. It owns exactly three long-lived goroutines plus the pool's
// workers, which makes the shutdown order explicit and auditable.
type Consumer struct {
	cfg ConsumerConfig
}

// NewConsumer validates cfg and returns a Consumer.
func NewConsumer(cfg ConsumerConfig) (*Consumer, error) {
	if cfg.Fetcher == nil || cfg.Committer == nil || cfg.Pool == nil {
		return nil, errors.New("kafkaworker: Fetcher, Committer and Pool are required")
	}
	if cfg.Tracker == nil {
		cfg.Tracker = cfg.Pool.cfg.Tracker
	}
	if cfg.Metrics == nil {
		cfg.Metrics = NopMetrics{}
	}
	if cfg.CommitInterval <= 0 {
		cfg.CommitInterval = 5 * time.Second
	}
	if cfg.CommitTimeout <= 0 {
		cfg.CommitTimeout = 10 * time.Second
	}
	if cfg.DrainTimeout <= 0 {
		cfg.DrainTimeout = 30 * time.Second
	}
	if cfg.OnError == nil {
		cfg.OnError = func(error) {}
	}
	return &Consumer{cfg: cfg}, nil
}

// Run blocks until ctx is cancelled or the fetcher returns a fatal error,
// then shuts down in the only order that preserves at-least-once:
//
//	stop fetching → drain the pool → commit what completed → return
//
// Committing before draining would acknowledge records that are still
// running. Draining without a deadline would let one stuck handler hold the
// process past the orchestrator's termination grace period, turning a clean
// stop into a SIGKILL.
func (c *Consumer) Run(ctx context.Context) error {
	// Workers live under their own context so "stop fetching" and "abort
	// in-flight work" are separate decisions.
	workCtx, abort := context.WithCancel(context.WithoutCancel(ctx))
	defer abort()

	c.cfg.Pool.Start(workCtx)

	commitDone := make(chan struct{})
	var commitOnce sync.Once
	stopCommits := func() { commitOnce.Do(func() { close(commitDone) }) }
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		c.commitLoop(workCtx, commitDone)
	}()

	fetchErr := c.fetchLoop(ctx)

	// 1. Fetching has stopped; no further Submit calls can happen, so the
	//    pool's queues can be closed safely.
	drained := make(chan struct{})
	go func() {
		c.cfg.Pool.Close()
		close(drained)
	}()

	select {
	case <-drained:
	case <-time.After(c.cfg.DrainTimeout):
		c.cfg.OnError(fmt.Errorf("drain timeout after %s, aborting %d in-flight records",
			c.cfg.DrainTimeout, c.cfg.Tracker.InFlight()))
		abort()
		<-drained
	}

	// 2. Every record has reached a terminal outcome (or was abandoned
	//    un-acked). The watermark is now final and safe to commit.
	stopCommits()
	wg.Wait()

	// 3. Final commit on a context detached from the cancelled ctx —
	//    otherwise the most valuable commit of the process's life is
	//    guaranteed to fail with context.Canceled.
	finalCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), c.cfg.CommitTimeout)
	defer cancel()
	if err := c.commitOnce(finalCtx); err != nil {
		c.cfg.OnError(fmt.Errorf("final commit: %w", err))
	}

	if fetchErr != nil && !errors.Is(fetchErr, context.Canceled) {
		return fetchErr
	}
	return nil
}

// fetchLoop is the single goroutine that polls the broker and dispatches.
// Keeping it single-threaded is what makes per-partition offset ordering
// trivially correct: Track is called in ascending offset order by
// construction, with no lock held across the dispatch.
func (c *Consumer) fetchLoop(ctx context.Context) error {
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		records, err := c.cfg.Fetcher.Poll(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return fmt.Errorf("poll: %w", err)
		}
		for _, r := range records {
			// Track before Submit: a record must be visible to the tracker
			// before any worker can ack it.
			if !c.cfg.Tracker.Track(r) {
				continue // partition revoked between fetch and dispatch
			}
			if err := c.cfg.Pool.Submit(ctx, r); err != nil {
				// Shutting down. The record stays un-acked, so the watermark
				// stops here and the broker redelivers it.
				return err
			}
			c.cfg.Metrics.SetInFlight(c.cfg.Tracker.InFlight())
		}
	}
}

func (c *Consumer) commitLoop(ctx context.Context, done <-chan struct{}) {
	t := time.NewTicker(c.cfg.CommitInterval)
	defer t.Stop()
	for {
		select {
		case <-done:
			return
		case <-ctx.Done():
			return
		case <-t.C:
			cctx, cancel := context.WithTimeout(ctx, c.cfg.CommitTimeout)
			err := c.commitOnce(cctx)
			cancel()
			if err != nil {
				// Offset commits are idempotent absolute values, so a failed
				// commit needs no special recovery: the next tick sends the
				// same-or-higher watermark. Worst case is extra duplicates
				// after a crash, which at-least-once already tolerates.
				c.cfg.OnError(fmt.Errorf("commit: %w", err))
			}
		}
	}
}

func (c *Consumer) commitOnce(ctx context.Context) error {
	offsets := c.cfg.Tracker.Committable()
	if len(offsets) == 0 {
		return nil
	}
	start := time.Now()
	err := c.cfg.Committer.Commit(ctx, offsets)
	c.cfg.Metrics.ObserveCommit(time.Since(start), err)
	if err != nil {
		return err
	}
	c.cfg.Tracker.Commit(offsets)
	c.cfg.Metrics.SetInFlight(c.cfg.Tracker.InFlight())
	return nil
}

// OnPartitionsAssigned must be wired to the client's rebalance callback.
func (c *Consumer) OnPartitionsAssigned(tps []TopicPartition) {
	c.cfg.Tracker.Assign(tps...)
}

// OnPartitionsRevoked must be wired to the client's rebalance callback and
// must block until it returns — that is the only window in which the old
// owner can still commit. The order is: stop dispatching (the client
// guarantees this by calling the callback from the poll goroutine), wait for
// in-flight records on those partitions, commit, then forget the partitions.
//
// Whatever does not drain inside ctx is simply reprocessed by the new owner.
// Duplicates are expected; lost records are not.
func (c *Consumer) OnPartitionsRevoked(ctx context.Context, tps []TopicPartition) {
	if err := c.cfg.Tracker.WaitDrained(ctx, tps...); err != nil {
		c.cfg.OnError(fmt.Errorf("revoke drain incomplete for %v: %w", tps, err))
	}
	cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), c.cfg.CommitTimeout)
	defer cancel()
	if err := c.commitOnce(cctx); err != nil {
		c.cfg.OnError(fmt.Errorf("revoke commit: %w", err))
	}
	c.cfg.Tracker.Revoke(tps...)
}

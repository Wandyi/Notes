package bus

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/vaibhav/metricsprocessor/pkg/model"
	"github.com/vaibhav/metricsprocessor/pkg/pipeline"
)

// InProc is a partitioned in-process bus. It is the default binding for local
// development, tests, and single-binary deployments, and it implements exactly
// the contract a real broker must satisfy -- one goroutine per partition,
// strict FIFO within a partition, at-least-once with bounded retry -- so code
// written against it does not change when Kafka is swapped in underneath.
type InProc struct {
	parts   []chan Envelope
	log     *slog.Logger
	closed  atomic.Bool
	wg      sync.WaitGroup
	maxTry  int
	backoff time.Duration
	dlq     func(Envelope, error)

	published atomic.Uint64
	delivered atomic.Uint64
	retried   atomic.Uint64
	dead      atomic.Uint64
}

// InProcOptions configures the in-process bus.
type InProcOptions struct {
	Partitions   int
	BufferSize   int
	MaxAttempts  int
	RetryBackoff time.Duration
	Logger       *slog.Logger
	// DeadLetter receives envelopes that exhausted their attempts. Without a sink
	// here, "at-least-once" quietly becomes "at-most-once" for poisoned data.
	DeadLetter func(Envelope, error)
}

// NewInProc builds an in-process bus.
func NewInProc(o InProcOptions) *InProc {
	if o.Partitions <= 0 {
		o.Partitions = 8
	}
	if o.BufferSize <= 0 {
		o.BufferSize = 1024
	}
	if o.MaxAttempts <= 0 {
		o.MaxAttempts = 3
	}
	if o.RetryBackoff <= 0 {
		o.RetryBackoff = 25 * time.Millisecond
	}
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	b := &InProc{
		parts:   make([]chan Envelope, o.Partitions),
		log:     o.Logger,
		maxTry:  o.MaxAttempts,
		backoff: o.RetryBackoff,
		dlq:     o.DeadLetter,
	}
	for i := range b.parts {
		b.parts[i] = make(chan Envelope, o.BufferSize)
	}
	return b
}

func (b *InProc) Partitions() int { return len(b.parts) }

// Publish routes by key using the same partitioner the aggregator shards with, so
// a series' ordering domain is identical on both sides of the transport.
func (b *InProc) Publish(ctx context.Context, key string, pts []*model.Point) error {
	if b.closed.Load() {
		return ErrClosed
	}
	if len(pts) == 0 {
		return nil
	}
	part := pipeline.PartitionFor(key, len(b.parts))
	select {
	case b.parts[part] <- Envelope{Key: key, Points: pts}:
		b.published.Add(1)
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Subscribe starts one consumer per partition.
func (b *InProc) Subscribe(ctx context.Context, group string, h Handler) error {
	if b.closed.Load() {
		return ErrClosed
	}
	for i := range b.parts {
		b.wg.Add(1)
		go b.consume(ctx, group, i, h)
	}
	return nil
}

func (b *InProc) consume(ctx context.Context, group string, part int, h Handler) {
	defer b.wg.Done()
	log := b.log.With("component", "bus", "group", group, "partition", part)
	for {
		select {
		case <-ctx.Done():
			return
		case env, ok := <-b.parts[part]:
			if !ok {
				return
			}
			b.deliver(ctx, log, env, h)
		}
	}
}

// deliver retries in place. Retrying in place rather than re-queueing is what
// preserves order: a re-queued envelope would land behind envelopes that were
// published after it.
func (b *InProc) deliver(ctx context.Context, log *slog.Logger, env Envelope, h Handler) {
	var err error
	for attempt := 1; attempt <= b.maxTry; attempt++ {
		env.Attempt = attempt
		if err = h(ctx, env); err == nil {
			b.delivered.Add(1)
			return
		}
		if errors.Is(err, ErrDrop) || ctx.Err() != nil {
			break
		}
		b.retried.Add(1)
		select {
		case <-ctx.Done():
			return
		case <-time.After(b.backoff * time.Duration(attempt)):
		}
	}
	b.dead.Add(1)
	log.Warn("envelope dead-lettered", "key", env.Key, "points", len(env.Points), "error", err)
	if b.dlq != nil {
		b.dlq(env, err)
	}
}

// Close stops accepting publishes and waits for consumers to finish. Consumers
// exit on their subscription context, so callers cancel that first.
func (b *InProc) Close() error {
	b.closed.Store(true)
	b.wg.Wait()
	return nil
}

// Stats reports transport health.
func (b *InProc) Stats() map[string]uint64 {
	depth := uint64(0)
	for _, p := range b.parts {
		depth += uint64(len(p))
	}
	return map[string]uint64{
		"published":     b.published.Load(),
		"delivered":     b.delivered.Load(),
		"retried":       b.retried.Load(),
		"dead_lettered": b.dead.Load(),
		"queue_depth":   depth,
	}
}

// Drain blocks until every partition is empty or ctx expires. Used by the demo
// and by tests that need a quiescent point; not part of the Bus contract.
func (b *InProc) Drain(ctx context.Context) error {
	t := time.NewTicker(2 * time.Millisecond)
	defer t.Stop()
	for {
		empty := true
		for _, p := range b.parts {
			if len(p) > 0 {
				empty = false
				break
			}
		}
		if empty {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
		}
	}
}

// Package pipeline implements the real-time aggregation core: a sharded,
// channel-based fold that preserves per-series event order, closes windows on an
// event-time watermark, and reports partial failures alongside results rather
// than in place of them.
//
// # Topology
//
//	producers --Submit--> shard queue (chan) --> shard goroutine (fold) --+
//	                          ...                                          |--> results (chan)
//	                      shard queue (chan) --> shard goroutine (fold) --+
//
// One hop from producer to fold. The shard index is computed by the caller's
// goroutine, so there is no dispatcher goroutine in the middle adding a scheduling
// hop and a queue's worth of latency to every point.
//
// # Ownership rules
//
//  1. A shard's state is owned by its goroutine. Nothing else reads or writes it.
//  2. Shard inbound channels are never closed. With many concurrent senders there
//     is no safe moment to close them (Go's "only the sender closes" rule has no
//     single sender to appeal to), so shutdown is signalled out of band and the
//     workers drain instead.
//  3. The results channel has exactly one closer: the reaper goroutine, after
//     every shard has exited. That is the only channel close in the package.
package pipeline

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"time"

	"github.com/vaibhav/metricsprocessor/pkg/merr"
	"github.com/vaibhav/metricsprocessor/pkg/model"
)

// Pipeline is a running aggregation engine. It is safe for concurrent use by any
// number of producer goroutines.
type Pipeline struct {
	cfg    Config
	shards []*shard

	results chan Result
	quit    chan struct{}
	abandon chan struct{}
	done    chan struct{}

	wg        sync.WaitGroup
	closeOnce sync.Once
	closed    atomic.Bool

	quar *quarantineTable

	accepted     atomic.Uint64
	rejected     atomic.Uint64
	backpressure atomic.Uint64
	shed         atomic.Uint64
}

// New builds and starts a pipeline. Call Close to stop it. The caller must drain
// Results until it is closed; a stalled consumer is real backpressure and will
// eventually stall ingest, which is the intended behaviour.
func New(cfg Config) (*Pipeline, error) {
	if err := cfg.normalize(); err != nil {
		return nil, err
	}
	p := &Pipeline{
		cfg:     cfg,
		results: make(chan Result, cfg.ResultQueueSize),
		quit:    make(chan struct{}),
		abandon: make(chan struct{}),
		done:    make(chan struct{}),
		quar:    newQuarantineTable(),
		shards:  make([]*shard, cfg.Shards),
	}
	for i := range p.shards {
		p.shards[i] = newShard(i, cfg, p.results, p.quit, p.abandon, p.quar)
	}
	p.wg.Add(len(p.shards))
	for _, s := range p.shards {
		go s.run(&p.wg)
	}
	// The reaper is the single owner of the results channel's close. Having
	// exactly one closer, running only after every producer into the channel has
	// exited, is what makes "send on closed channel" structurally impossible.
	go func() {
		p.wg.Wait()
		close(p.results)
		close(p.done)
	}()
	return p, nil
}

// Results yields closed windows. It is closed once every shard has drained and
// flushed, which happens after Close returns from the shards' point of view.
func (p *Pipeline) Results() <-chan Result { return p.results }

// Submit hands one point to the shard that owns its series.
//
// The fast path is: validate, hash, one non-blocking channel send. Everything
// expensive -- blocking, timers, error construction -- lives on the slow path
// behind a taken branch, so the common case costs a few hundred nanoseconds and
// never allocates.
func (p *Pipeline) Submit(ctx context.Context, pt *model.Point) error {
	if p.closed.Load() {
		return merr.ErrPipelineClosed
	}
	if err := pt.Validate(); err != nil {
		p.rejected.Add(1)
		return &merr.Error{Code: merr.CodeValidation, Source: pt.Source, Err: err, At: time.Now()}
	}
	if p.cfg.MaxFutureSkew > 0 {
		if now := p.cfg.Now(); pt.EventTime.Sub(now) > p.cfg.MaxFutureSkew {
			p.rejected.Add(1)
			return &merr.Error{
				Code: merr.CodeValidation, Series: pt.SeriesKey(), Source: pt.Source,
				Msg: "event_time is too far in the future; refusing to advance the watermark", At: now,
			}
		}
	}
	if p.cfg.QuarantineErrorRate > 0 && p.quar.blocked(pt.Source, p.cfg.Now()) {
		p.shed.Add(1)
		return &merr.Error{Code: merr.CodeQuarantine, Source: pt.Source, Err: merr.ErrQuarantined}
	}

	sh := p.shards[PartitionFor(pt.SeriesKey(), len(p.shards))]

	select {
	case sh.in <- pt:
		p.accepted.Add(1)
		return nil
	default:
		// Queue full. What happens next is a policy decision, not an accident.
	}
	return p.submitSlow(ctx, sh, pt)
}

func (p *Pipeline) submitSlow(ctx context.Context, sh *shard, pt *model.Point) error {
	if p.cfg.Overflow == PolicyDropNewest {
		p.backpressure.Add(1)
		return &merr.Error{
			Code: merr.CodeBackpressure, Series: pt.SeriesKey(), Source: pt.Source,
			Msg: "shard queue full", Err: merr.ErrBackpressure, At: time.Now(),
		}
	}
	t := time.NewTimer(p.cfg.SubmitTimeout)
	defer t.Stop()
	select {
	case sh.in <- pt:
		p.accepted.Add(1)
		return nil
	case <-ctx.Done():
		p.backpressure.Add(1)
		return &merr.Error{Code: merr.CodeBackpressure, Series: pt.SeriesKey(), Source: pt.Source,
			Msg: "caller context cancelled while waiting for shard capacity", Err: ctx.Err()}
	case <-t.C:
		p.backpressure.Add(1)
		return &merr.Error{Code: merr.CodeBackpressure, Series: pt.SeriesKey(), Source: pt.Source,
			Msg: "submit timeout waiting for shard capacity", Err: merr.ErrBackpressure}
	case <-p.quit:
		return merr.ErrPipelineClosed
	}
}

// SubmitBatch submits every point and returns one MultiError describing whatever
// failed. It does not stop at the first failure -- a batch of a thousand points
// with three bad ones should aggregate nine hundred and ninety seven.
func (p *Pipeline) SubmitBatch(ctx context.Context, pts []*model.Point) error {
	var m merr.MultiError
	for _, pt := range pts {
		if err := p.Submit(ctx, pt); err != nil {
			var e *merr.Error
			if errors.As(err, &e) {
				m.Add(e)
			} else {
				m.Add(&merr.Error{Code: merr.CodeDownstream, Source: pt.Source, Err: err})
			}
			if errors.Is(err, merr.ErrPipelineClosed) {
				break
			}
		}
	}
	return m.ErrorOrNil()
}

// Close stops accepting points, drains the shard queues, flushes every open
// window as a partial result, and closes Results.
//
// Shutdown order matters and is deliberate:
//
//  1. reject new work    (so the queues can actually reach empty)
//  2. signal the shards  (they drain what is queued, then flush)
//  3. wait for the shards, then close Results (single owner, no races)
//
// The caller must keep reading Results while Close runs, otherwise the final
// flush has nowhere to go. If ctx expires first, delivery is abandoned and the
// number of lost windows is reported in Stats.DroppedResults.
func (p *Pipeline) Close(ctx context.Context) error {
	p.closeOnce.Do(func() {
		p.closed.Store(true)
		close(p.quit)
	})
	select {
	case <-p.done:
		return nil
	case <-ctx.Done():
		close(p.abandon) // let shards stop waiting on the results channel
		<-p.done
		return ctx.Err()
	}
}

// Stats returns a consistent-enough snapshot of pipeline health. Counters are
// read without a global barrier, so numbers may be a few operations apart from
// each other; that is the right trade for a metrics path that must not
// synchronize the fold to observe itself.
func (p *Pipeline) Stats() Stats {
	s := Stats{
		Accepted:      p.accepted.Load(),
		Rejected:      p.rejected.Load(),
		Backpressure:  p.backpressure.Load(),
		Shed:          p.shed.Load(),
		QueueCapacity: p.cfg.ShardQueueSize * len(p.shards),
		CollectedAt:   p.cfg.Now(),
	}
	for _, sh := range p.shards {
		s.Received += sh.stats.received.Load()
		s.Folded += sh.stats.folded.Load()
		s.Emitted += sh.stats.emitted.Load()
		s.Windows += sh.stats.windows.Load()
		s.Rejected += sh.stats.rejected.Load()
		s.Late += sh.stats.late.Load()
		s.Duplicates += sh.stats.duplicates.Load()
		s.Gaps += sh.stats.gaps.Load()
		s.Panics += sh.stats.panics.Load()
		s.Quarantines += sh.stats.quarantines.Load()
		s.DroppedResults += sh.stats.droppedResults.Load()
		s.OpenWindows += int(sh.stats.windowsOpen.Load())
		s.QueueDepth += len(sh.in)
	}
	return s
}

// Quarantined lists producers currently being shed, for the admin endpoint.
func (p *Pipeline) Quarantined() map[string]time.Time { return p.quar.list(p.cfg.Now()) }

// PartitionFor maps a series key onto one of n partitions.
//
// The hash is FNV-1a and is written out here rather than taken from hash/maphash
// on purpose: maphash is seeded randomly per process, so gateway and aggregator
// would disagree about which partition owns a series and the ordering guarantee
// would evaporate at the process boundary. Partitioning must be a pure function
// of the key, stable across processes, restarts, and releases.
func PartitionFor(key string, n int) int {
	if n <= 1 {
		return 0
	}
	const (
		offset64 = 14695981039346656037
		prime64  = 1099511628211
	)
	h := uint64(offset64)
	for i := 0; i < len(key); i++ {
		h ^= uint64(key[i])
		h *= prime64
	}
	return int(h % uint64(n))
}

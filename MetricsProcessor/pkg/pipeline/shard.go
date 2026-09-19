package pipeline

import (
	"fmt"
	"math/rand"
	"slices"
	"sort"
	"sync"
	"time"

	"github.com/vaibhav/metricsprocessor/pkg/merr"
	"github.com/vaibhav/metricsprocessor/pkg/model"
)

// CloseReason explains why a window was emitted. Consumers use it to decide how
// much to trust a window: a watermark close is complete by the pipeline's own
// definition, an idle close is a liveness fallback, and a shutdown close is
// explicitly partial.
type CloseReason string

const (
	ReasonWatermark CloseReason = "watermark"
	ReasonIdle      CloseReason = "idle"
	ReasonShutdown  CloseReason = "shutdown"
)

// Result is one closed window from one shard, with every partial failure the
// shard observed while building it.
//
// This type is the concrete answer to "return the aggregated metrics along with
// any errors encountered": results and errors are one value, delivered together,
// and a non-nil Err never means the Aggregates are invalid -- it means they are
// incomplete in the specific, enumerated ways the MultiError describes.
type Result struct {
	Shard       int               `json:"shard"`
	WindowStart time.Time         `json:"window_start"`
	WindowEnd   time.Time         `json:"window_end"`
	Aggregates  []model.Aggregate `json:"aggregates"`
	Err         *merr.MultiError  `json:"errors,omitempty"`
	Reason      CloseReason       `json:"reason"`
	ClosedAt    time.Time         `json:"closed_at"`
}

// shard is a single-goroutine fold worker. Everything reachable from a shard is
// owned by exactly one goroutine for the shard's whole lifetime, which is why the
// fold path contains no mutex, no atomic, and no channel other than its own inbox.
type shard struct {
	id   int
	cfg  Config
	in   chan *model.Point
	out  chan<- Result
	quit <-chan struct{}
	// abandon is closed when a graceful shutdown has exceeded its deadline; it is
	// the only thing that may cut a result delivery short.
	abandon <-chan struct{}

	errs    *merr.Collector
	streams map[string]*reorderBuffer
	windows map[int64]*windowState

	maxEventTime  time.Time
	watermark     time.Time
	lastClosedEnd time.Time
	lastPointAt   time.Time

	quar   *quarantineTable
	budget map[string]*budget

	stats   *shardCounters
	scratch []*model.Point
	rnd     *rand.Rand
}

func newShard(id int, cfg Config, out chan<- Result, quit, abandon <-chan struct{}, quar *quarantineTable) *shard {
	return &shard{
		id:      id,
		cfg:     cfg,
		in:      make(chan *model.Point, cfg.ShardQueueSize),
		out:     out,
		quit:    quit,
		abandon: abandon,
		errs:    merr.NewCollector(id, cfg.MaxErrorsPerWindow),
		streams: make(map[string]*reorderBuffer, 256),
		windows: make(map[int64]*windowState, 4),
		quar:    quar,
		budget:  make(map[string]*budget, 64),
		stats:   &shardCounters{},
		scratch: make([]*model.Point, 0, 64),
		rnd:     rand.New(rand.NewSource(int64(id)*2654435761 + 1)),
	}
}

// run is the shard's event loop.
//
// Three inputs, one goroutine: data, a maintenance tick, and shutdown. Because
// window closing happens inline on the data path -- the moment a point arrives
// that belongs to a later window -- the timer is only a liveness fallback for
// idle series, not the mechanism that drives emission. That is what keeps
// end-to-end latency at "as soon as the next window's first point lands" instead
// of "up to one tick".
func (s *shard) run(wg *sync.WaitGroup) {
	defer wg.Done()

	// Jitter the first tick so N shards do not all wake, allocate, and emit on
	// the same instant. Synchronized timers turn a smooth load into a sawtooth.
	jitter := time.Duration(s.rnd.Int63n(int64(s.cfg.MaintenanceInterval)))
	t := time.NewTimer(jitter)
	defer t.Stop()

	for {
		select {
		case p := <-s.in:
			s.ingest(p)
		case <-t.C:
			s.maintain(s.cfg.Now())
			t.Reset(s.cfg.MaintenanceInterval)
		case <-s.quit:
			s.shutdown()
			return
		}
	}
}

// shutdown drains whatever is already queued, then flushes every open window so
// that in-flight aggregation is reported rather than silently discarded.
func (s *shard) shutdown() {
	for {
		select {
		case p := <-s.in:
			s.ingest(p)
			continue
		default:
		}
		break
	}
	now := s.cfg.Now()
	// Release anything still parked in reorder buffers. Order is best effort at
	// this point; losing the data entirely would be worse than emitting it with
	// a Partial marker.
	for _, rb := range s.streams {
		s.scratch = s.scratch[:0]
		ready, gap := rb.drainAll(s.scratch)
		s.release(rb, ready, gap, now)
	}
	s.closeWindows(timeMax, ReasonShutdown, now)
}

// timeMax is a sentinel watermark that closes every open window.
var timeMax = time.Date(9999, time.December, 31, 23, 59, 59, 0, time.UTC)

// ---------------------------------------------------------------------------
// Ingest
// ---------------------------------------------------------------------------

func (s *shard) ingest(p *model.Point) {
	s.stats.received.Add(1)
	now := s.cfg.Now()
	s.lastPointAt = now

	// Sequence-free producers (Seq == 0) opt out of reordering: their arrival
	// order is taken as their event order. This keeps the fast path free for
	// producers that do not need the guarantee and cannot pay for the state.
	if s.cfg.ReorderDepth == 0 || p.Seq == 0 {
		s.fold(p, 0, now)
		return
	}

	key := p.StreamKey()
	rb := s.streams[key]
	if rb == nil {
		rb = newReorderBuffer(s.cfg.ReorderDepth, s.cfg.MaxReorderDelay)
		s.streams[key] = rb
	}

	s.scratch = s.scratch[:0]
	ready, res, gap := rb.push(p, now, s.scratch)
	s.scratch = ready

	if res == pushDuplicate {
		s.stats.duplicates.Add(1)
		s.errs.Add(&merr.Error{
			Code: merr.CodeDuplicate, Series: p.SeriesKey(), Source: p.Source,
			Msg: fmt.Sprintf("seq %d already folded", p.Seq), At: now,
		})
		s.recordOutcome(p.Source, false)
		return
	}
	if gap > 0 {
		s.stats.gaps.Add(gap)
		s.errs.Add(&merr.Error{
			Code: merr.CodeSequenceGap, Series: p.SeriesKey(), Source: p.Source,
			Msg: fmt.Sprintf("skipped %d sequence(s) after waiting for a missing point", gap), At: now,
		})
	}
	s.release(rb, ready, gap, now)
}

// release folds a batch of newly in-order points and collects the bookkeeping
// the reorder buffer accumulated while producing them. Every path that can
// release points -- arrival, maintenance tick, shutdown -- goes through here, so
// none of them can quietly skip the accounting.
func (s *shard) release(rb *reorderBuffer, ready []*model.Point, gap uint64, now time.Time) {
	for i, rp := range ready {
		g := uint64(0)
		if i == 0 {
			g = gap
		}
		s.fold(rp, g, now)
	}
	// Replays that were already parked when their predecessor arrived are only
	// discovered during the drain, so they are collected afterwards.
	d := rb.takeDupes()
	if d == 0 || len(ready) == 0 {
		return
	}
	s.stats.duplicates.Add(d)
	s.errs.Add(&merr.Error{
		Code: merr.CodeDuplicate, Series: ready[0].SeriesKey(), Source: ready[0].Source,
		Msg: fmt.Sprintf("%d replayed point(s) discarded while draining the reorder buffer", d), At: now,
	})
}

// fold applies one in-order point to its window.
func (s *shard) fold(p *model.Point, gap uint64, now time.Time) {
	// Advance event time and the watermark, then close everything the watermark
	// has passed. Doing this before folding means the point cannot leak into a
	// window it does not belong to, and a window closes on the very first point
	// of the next window rather than one tick later.
	if p.EventTime.After(s.maxEventTime) {
		s.maxEventTime = p.EventTime
		s.watermark = s.maxEventTime.Add(-s.cfg.AllowedLateness)
		s.closeWindows(s.watermark, ReasonWatermark, now)
	}

	start, end := model.WindowFor(p.EventTime, s.cfg.WindowSize)

	if !end.After(s.lastClosedEnd) {
		// The window this point belongs to has already been published. Admitting
		// it would mean retracting an emitted aggregate, which the downstream
		// contract does not allow. Count it, report it, keep going.
		s.stats.late.Add(1)
		s.errs.Add(&merr.Error{
			Code: merr.CodeLate, Series: p.SeriesKey(), Source: p.Source,
			Msg: fmt.Sprintf("event_time %s is behind closed window end %s",
				p.EventTime.UTC().Format(time.RFC3339Nano), s.lastClosedEnd.UTC().Format(time.RFC3339Nano)),
			At: now,
		})
		s.recordOutcome(p.Source, false)
		return
	}

	w := s.windows[start.UnixNano()]
	if w == nil {
		w = newWindowState(start, s.cfg.WindowSize)
		s.windows[start.UnixNano()] = w
		s.stats.windowsOpen.Add(1)
	}

	key := p.SeriesKey()
	st := w.series[key]
	if st == nil {
		st = newSeriesState(p)
		w.series[key] = st
	}
	if gap > 0 {
		st.gaps += gap
		st.partial = true
	}
	if p.EventTime.Before(s.watermark) {
		// Late, but its window was still open, so the data is not lost. Worth
		// surfacing per series because it predicts future data loss.
		st.late++
	}

	if !s.applyIsolated(st, p) {
		s.recordOutcome(p.Source, false)
		return
	}
	s.stats.folded.Add(1)
	s.recordOutcome(p.Source, true)
}

// applyIsolated runs the user transform and the fold with per-point failure
// containment. It reports whether the point was folded.
//
// The blast radius of a bad point is exactly that point. Without this, one
// malformed input in a transform takes down the shard goroutine and, with it, the
// in-memory aggregation state of every series that hashes to it -- turning a data
// problem into an availability problem.
func (s *shard) applyIsolated(st *seriesState, p *model.Point) (ok bool) {
	if !s.cfg.PanicIsolation {
		return s.applyRaw(st, p)
	}
	defer func() {
		if r := recover(); r != nil {
			ok = false
			st.partial = true // the fold may have been interrupted mid-update
			s.stats.panics.Add(1)
			s.errs.Add(&merr.Error{
				Code: merr.CodePanic, Series: p.SeriesKey(), Source: p.Source,
				Msg: fmt.Sprintf("fold panicked: %v", r), At: s.cfg.Now(),
			})
		}
	}()
	return s.applyRaw(st, p)
}

func (s *shard) applyRaw(st *seriesState, p *model.Point) bool {
	if s.cfg.Transform != nil {
		if err := s.cfg.Transform(p); err != nil {
			s.stats.rejected.Add(1)
			s.errs.Add(&merr.Error{
				Code: merr.CodeValidation, Series: p.SeriesKey(), Source: p.Source,
				Msg: "transform rejected point", Err: err, At: s.cfg.Now(),
			})
			return false
		}
	}
	st.apply(p)
	return true
}

// ---------------------------------------------------------------------------
// Window closing
// ---------------------------------------------------------------------------

// closeWindows emits every open window that ends at or before wm.
func (s *shard) closeWindows(wm time.Time, reason CloseReason, now time.Time) {
	if len(s.windows) == 0 {
		return
	}
	var due []int64
	for start, w := range s.windows {
		if !w.end.After(wm) {
			due = append(due, start)
		}
	}
	if len(due) == 0 {
		return
	}
	// Emit oldest first. Map iteration order is randomized, and a consumer that
	// sees window N+1 before window N would have to buffer and re-sort.
	slices.Sort(due)
	for _, start := range due {
		w := s.windows[start]
		delete(s.windows, start)
		s.stats.windowsOpen.Add(-1)
		if w.end.After(s.lastClosedEnd) {
			s.lastClosedEnd = w.end
		}
		s.emit(w, reason, now)
	}
}

func (s *shard) emit(w *windowState, reason CloseReason, now time.Time) {
	aggs := make([]model.Aggregate, 0, len(w.series))
	for key, st := range w.series {
		// A series whose every point was rejected leaves an empty shell behind
		// (the state is created before the fold can fail). Publishing it would
		// put zero-count rows in front of consumers; the fact that the series
		// existed and failed entirely is carried by the error report, which is
		// where it belongs.
		if st.count == 0 && !st.partial && st.gaps == 0 && st.late == 0 {
			continue
		}
		if reason == ReasonShutdown {
			st.partial = true
		}
		aggs = append(aggs, st.snapshot(key, w.start, w.end))
	}
	// Deterministic output ordering makes results diffable, snapshot-testable,
	// and cheap for a downstream merge that expects sorted input.
	sort.Slice(aggs, func(i, j int) bool { return aggs[i].SeriesKey < aggs[j].SeriesKey })

	s.stats.windows.Add(1)
	s.stats.emitted.Add(uint64(len(aggs)))

	res := Result{
		Shard:       s.id,
		WindowStart: w.start,
		WindowEnd:   w.end,
		Aggregates:  aggs,
		Err:         s.errs.Drain(),
		Reason:      reason,
		ClosedAt:    now,
	}

	// Backpressure from the publisher is real backpressure: block. Dropping a
	// closed window would lose the aggregation of millions of points, which is a
	// far worse trade than the one we make on the ingest path. The abandon
	// channel exists only so a shutdown deadline can still terminate.
	select {
	case s.out <- res:
	case <-s.abandon:
		s.stats.droppedResults.Add(1)
	}
}

// ---------------------------------------------------------------------------
// Maintenance
// ---------------------------------------------------------------------------

// maintain runs off the timer. It handles everything that must happen even when
// no data is arriving: releasing stalled reorder buffers, closing windows whose
// wall-clock time has passed, evicting idle stream state, and decaying error
// budgets.
func (s *shard) maintain(now time.Time) {
	// 1. Release streams that have been stalled too long behind a missing point.
	for _, rb := range s.streams {
		ready, gap := rb.flush(now)
		if len(ready) == 0 {
			continue
		}
		if gap > 0 {
			s.stats.gaps.Add(gap)
			s.errs.Add(&merr.Error{
				Code: merr.CodeSequenceGap, Series: ready[0].SeriesKey(), Source: ready[0].Source,
				Msg: fmt.Sprintf("skipped %d sequence(s) after %s stall", gap, s.cfg.MaxReorderDelay), At: now,
			})
		}
		s.release(rb, ready, gap, now)
	}

	// 2. Idle close. A series that stops emitting must not hold its last window
	// open forever. We use wall clock here rather than the event-time watermark
	// precisely because the watermark cannot advance without data.
	s.closeWindows(now.Add(-s.cfg.AllowedLateness), ReasonIdle, now)

	// 3. Evict reorder state for producers that have gone away, so that pod
	// churn does not grow the map without bound.
	if s.cfg.IdleStreamTTL > 0 {
		cutoff := now.Add(-s.cfg.IdleStreamTTL)
		for k, rb := range s.streams {
			if rb.pending() == 0 && rb.lastSeen.Before(cutoff) {
				delete(s.streams, k)
			}
		}
	}

	// 4. Decay error budgets and lift expired quarantines.
	s.decayBudgets()
}

// ---------------------------------------------------------------------------
// Error budget / quarantine
// ---------------------------------------------------------------------------

// budget is an exponentially decaying error rate for one producer, as seen by
// this shard.
type budget struct{ ok, bad float64 }

func (s *shard) recordOutcome(source string, good bool) {
	if s.cfg.QuarantineErrorRate <= 0 || source == "" {
		return
	}
	b := s.budget[source]
	if b == nil {
		b = &budget{}
		s.budget[source] = b
	}
	if good {
		b.ok++
	} else {
		b.bad++
	}
	total := b.ok + b.bad
	if total < float64(s.cfg.QuarantineMinSamples) {
		return
	}
	if b.bad/total > s.cfg.QuarantineErrorRate {
		until := s.cfg.Now().Add(s.cfg.QuarantineCooldown)
		if s.quar.trip(source, until) {
			s.stats.quarantines.Add(1)
			s.errs.Add(&merr.Error{
				Code: merr.CodeQuarantine, Source: source,
				Msg: fmt.Sprintf("error rate %.0f%% over %.0f samples; shedding until %s",
					100*b.bad/total, total, until.UTC().Format(time.RFC3339)),
				Err: merr.ErrQuarantined, At: s.cfg.Now(),
			})
		}
		// Reset so the source gets a clean evaluation window after cooldown.
		b.ok, b.bad = 0, 0
	}
}

func (s *shard) decayBudgets() {
	for src, b := range s.budget {
		b.ok *= 0.5
		b.bad *= 0.5
		if b.ok+b.bad < 0.01 {
			delete(s.budget, src)
		}
	}
}

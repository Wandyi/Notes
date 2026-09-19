package pipeline_test

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/vaibhav/metricsprocessor/pkg/merr"
	"github.com/vaibhav/metricsprocessor/pkg/model"
	"github.com/vaibhav/metricsprocessor/pkg/pipeline"
)

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func testConfig() pipeline.Config {
	cfg := pipeline.DefaultConfig()
	cfg.Shards = 4
	cfg.WindowSize = time.Hour // one window unless a test says otherwise
	cfg.AllowedLateness = 0
	cfg.MaintenanceInterval = 5 * time.Millisecond
	cfg.MaxReorderDelay = 20 * time.Millisecond
	cfg.Overflow = pipeline.PolicyBlock
	cfg.SubmitTimeout = 2 * time.Second
	cfg.QuarantineErrorRate = 0 // off unless a test enables it
	cfg.MaxFutureSkew = time.Hour
	return cfg
}

// windowBase returns the start of the current wall-clock hour: recent enough that
// an hour-wide window under test never closes on the idle path, and never in the
// future, so the skew guard does not fire.
func windowBase() time.Time { return time.Now().Truncate(time.Hour) }

func point(name string, kind model.Kind, v float64, at time.Time, source string, seq uint64) *model.Point {
	return &model.Point{
		Service: "checkout", Name: name, Kind: kind, Value: v,
		EventTime: at, Source: source, Seq: seq,
	}
}

func findAgg(t *testing.T, aggs []model.Aggregate, name string) model.Aggregate {
	t.Helper()
	for _, a := range aggs {
		if a.Name == name {
			return a
		}
	}
	t.Fatalf("no aggregate named %q in %d results", name, len(aggs))
	return model.Aggregate{}
}

// sink drains Results concurrently. Every live-pipeline test needs one, because
// a stalled consumer is designed to become backpressure.
type sink struct {
	mu   sync.Mutex
	res  []pipeline.Result
	done chan struct{}
}

func newSink(p *pipeline.Pipeline) *sink {
	s := &sink{done: make(chan struct{})}
	go func() {
		defer close(s.done)
		for r := range p.Results() {
			s.mu.Lock()
			s.res = append(s.res, r)
			s.mu.Unlock()
		}
	}()
	return s
}

func (s *sink) results() []pipeline.Result {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]pipeline.Result(nil), s.res...)
}

func (s *sink) aggregates() []model.Aggregate {
	var out []model.Aggregate
	for _, r := range s.results() {
		out = append(out, r.Aggregates...)
	}
	return out
}

func (s *sink) errs() *merr.MultiError {
	m := &merr.MultiError{}
	for _, r := range s.results() {
		m.Merge(r.Err)
	}
	return m
}

func (s *sink) wait(t *testing.T) {
	t.Helper()
	select {
	case <-s.done:
	case <-time.After(5 * time.Second):
		t.Fatal("results channel never closed")
	}
}

func closePipeline(t *testing.T, p *pipeline.Pipeline) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := p.Close(ctx); err != nil {
		t.Fatalf("close: %v", err)
	}
}

// eventually polls cond until it holds or the deadline passes.
func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// ---------------------------------------------------------------------------
// Ordering
// ---------------------------------------------------------------------------

// A cumulative counter that restarts (a redeployed pod) can only be interpreted
// correctly if its points are folded in order. This is the concrete reason the
// pipeline guarantees per-series ordering rather than treating it as a nicety.
//
// Values 10, 20, 5(reset), 15 -> true increase is (20-10) + 5 + (15-5) = 25.
func TestOrderingRestoredBySequenceNumbers(t *testing.T) {
	base := windowBase()
	cfg := testConfig()
	cfg.ReorderDepth = 2

	// Deliberately scrambled arrival order, correct sequence numbers.
	arrival := []struct {
		seq uint64
		val float64
		off time.Duration
	}{
		{3, 5, 2 * time.Second},
		{1, 10, 0},
		{2, 20, time.Second},
		{4, 15, 3 * time.Second},
	}
	var pts []*model.Point
	for _, a := range arrival {
		pts = append(pts, point("orders_total", model.KindCounter, a.val, base.Add(a.off), "pod-a", a.seq))
	}

	snap, err := pipeline.Collect(context.Background(), cfg, pts)
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	got := findAgg(t, snap.Aggregates, "orders_total")
	if got.Count != 4 {
		t.Fatalf("count = %d, want 4", got.Count)
	}
	if got.Delta != 25 {
		t.Fatalf("delta = %v, want 25 (ordering was not restored)", got.Delta)
	}
	if got.Resets != 1 {
		t.Fatalf("resets = %d, want 1", got.Resets)
	}
	if got.Last != 15 {
		t.Fatalf("last = %v, want 15", got.Last)
	}
	if snap.Err.Total() != 0 {
		t.Fatalf("unexpected errors: %v", snap.Err)
	}
}

// The control case. With reordering disabled the very same points produce a
// wrong answer -- which is what makes the guarantee worth its cost.
func TestUnorderedFoldProducesWrongCounterDelta(t *testing.T) {
	base := windowBase()
	cfg := testConfig()
	cfg.ReorderDepth = 0 // trust arrival order

	pts := []*model.Point{
		point("orders_total", model.KindCounter, 5, base.Add(2*time.Second), "pod-a", 3),
		point("orders_total", model.KindCounter, 10, base, "pod-a", 1),
		point("orders_total", model.KindCounter, 20, base.Add(time.Second), "pod-a", 2),
		point("orders_total", model.KindCounter, 15, base.Add(3*time.Second), "pod-a", 4),
	}
	snap, err := pipeline.Collect(context.Background(), cfg, pts)
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	got := findAgg(t, snap.Aggregates, "orders_total")
	if got.Delta == 25 {
		t.Fatal("expected the unordered fold to be wrong; ordering guarantee is not being exercised")
	}
	// The fold still reports what it saw rather than failing, and the
	// out-of-order observations are counted on the aggregate itself.
	if got.OutOfOrder == 0 {
		t.Fatal("out-of-order points were not reported on the aggregate")
	}
	if got.Last != 15 {
		t.Fatalf("last = %v, want 15 (last-write-wins is by event time, not arrival)", got.Last)
	}
}

func TestGaugeIsLastWriteWinsByEventTime(t *testing.T) {
	base := windowBase()
	cfg := testConfig()
	cfg.ReorderDepth = 0

	pts := []*model.Point{
		point("queue_depth", model.KindGauge, 7, base.Add(3*time.Second), "pod-a", 0),
		point("queue_depth", model.KindGauge, 3, base.Add(time.Second), "pod-a", 0),
		point("queue_depth", model.KindGauge, 5, base.Add(2*time.Second), "pod-a", 0),
	}
	snap, err := pipeline.Collect(context.Background(), cfg, pts)
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	got := findAgg(t, snap.Aggregates, "queue_depth")
	if got.Last != 7 {
		t.Fatalf("last = %v, want 7", got.Last)
	}
	if got.Min != 3 || got.Max != 7 || got.Count != 3 {
		t.Fatalf("min/max/count = %v/%v/%d, want 3/7/3", got.Min, got.Max, got.Count)
	}
}

// ---------------------------------------------------------------------------
// Partial failure
// ---------------------------------------------------------------------------

var errPoisoned = errors.New("poisoned point")

// One bad point must cost exactly one point. Not the shard, not the window, and
// certainly not the other series that happen to hash to the same worker.
func TestPartialFailuresDoNotStopAggregation(t *testing.T) {
	base := windowBase()
	cfg := testConfig()
	cfg.ReorderDepth = 0
	cfg.Shards = 1 // force every series onto one worker: worst case for blast radius
	cfg.Transform = func(p *model.Point) error {
		switch p.Value {
		case 666:
			panic("transform exploded")
		case -1:
			return errPoisoned
		}
		return nil
	}

	var pts []*model.Point
	for i := 0; i < 10; i++ {
		pts = append(pts, point("latency_ms", model.KindHistogram, float64(10+i), base.Add(time.Duration(i)*time.Second), "pod-a", 0))
	}
	pts = append(pts,
		point("latency_ms", model.KindHistogram, 666, base.Add(20*time.Second), "pod-a", 0),
		point("latency_ms", model.KindHistogram, -1, base.Add(21*time.Second), "pod-a", 0),
		point("other_series", model.KindGauge, 42, base.Add(22*time.Second), "pod-a", 0),
	)

	snap, err := pipeline.Collect(context.Background(), cfg, pts)
	if err != nil {
		t.Fatalf("collect returned a hard error: %v", err)
	}

	// The healthy points aggregated.
	lat := findAgg(t, snap.Aggregates, "latency_ms")
	if lat.Count != 10 {
		t.Fatalf("latency count = %d, want 10 (poisoned points must not be folded)", lat.Count)
	}
	// A series that merely shared the shard is untouched.
	other := findAgg(t, snap.Aggregates, "other_series")
	if other.Last != 42 {
		t.Fatalf("collateral damage: other_series last = %v, want 42", other.Last)
	}

	// And the failures came back with the results.
	if snap.Err == nil {
		t.Fatal("expected partial failures to be reported")
	}
	if n := snap.Err.Counts[merr.CodePanic]; n != 1 {
		t.Fatalf("panic count = %d, want 1", n)
	}
	if n := snap.Err.Counts[merr.CodeValidation]; n != 1 {
		t.Fatalf("validation count = %d, want 1", n)
	}
	if !errors.Is(snap.Err, errPoisoned) {
		t.Fatalf("the underlying cause should survive to the caller: %v", snap.Err)
	}
	if snap.Stats.Panics != 1 {
		t.Fatalf("stats.panics = %d, want 1", snap.Stats.Panics)
	}
}

func TestInvalidPointsAreRejectedWithoutAffectingOthers(t *testing.T) {
	base := windowBase()
	cfg := testConfig()
	cfg.ReorderDepth = 0

	pts := []*model.Point{
		point("good", model.KindGauge, 1, base, "pod-a", 0),
		{Service: "", Name: "no_service", Kind: model.KindGauge, EventTime: base, Source: "pod-a"},
		{Service: "checkout", Name: "", Kind: model.KindGauge, EventTime: base, Source: "pod-a"},
		point("good", model.KindGauge, 2, base.Add(time.Second), "pod-a", 0),
	}
	snap, err := pipeline.Collect(context.Background(), cfg, pts)
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	good := findAgg(t, snap.Aggregates, "good")
	if good.Count != 2 {
		t.Fatalf("count = %d, want 2", good.Count)
	}
	if got := snap.Err.Counts[merr.CodeValidation]; got != 2 {
		t.Fatalf("validation errors = %d, want 2", got)
	}
	if !errors.Is(snap.Err, model.ErrNoService) || !errors.Is(snap.Err, model.ErrNoName) {
		t.Fatalf("specific validation causes did not survive: %v", snap.Err)
	}
}

// ---------------------------------------------------------------------------
// Windowing, watermarks, lateness
// ---------------------------------------------------------------------------

// A window must close on the watermark -- the instant the first point of a later
// window arrives -- not on a timer. That is what keeps emission latency at
// roughly one inter-arrival gap instead of one tick.
func TestWindowClosesOnWatermarkNotOnTimer(t *testing.T) {
	base := time.Now().Truncate(time.Second)
	cfg := testConfig()
	cfg.WindowSize = time.Second
	cfg.ReorderDepth = 0
	cfg.MaintenanceInterval = time.Hour // prove the timer is not doing the work

	p, err := pipeline.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	sk := newSink(p)
	ctx := context.Background()

	if err := p.Submit(ctx, point("hits", model.KindCounter, 1, base.Add(100*time.Millisecond), "pod-a", 0)); err != nil {
		t.Fatal(err)
	}
	if got := len(sk.results()); got != 0 {
		t.Fatalf("window closed early: %d results", got)
	}

	start := time.Now()
	// One point in the next window advances the watermark past the first.
	if err := p.Submit(ctx, point("hits", model.KindCounter, 2, base.Add(1500*time.Millisecond), "pod-a", 0)); err != nil {
		t.Fatal(err)
	}
	eventually(t, "window to close on the watermark", func() bool { return len(sk.results()) > 0 })
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Fatalf("watermark close took %v; the timer must not be on the critical path", elapsed)
	}

	res := sk.results()[0]
	if res.Reason != pipeline.ReasonWatermark {
		t.Fatalf("close reason = %q, want watermark", res.Reason)
	}
	if !res.WindowStart.Equal(base) {
		t.Fatalf("window start = %v, want %v", res.WindowStart, base)
	}

	closePipeline(t, p)
	sk.wait(t)
}

func TestLateArrivalIsReportedNotSilentlyDropped(t *testing.T) {
	base := time.Now().Truncate(time.Second)
	cfg := testConfig()
	cfg.WindowSize = time.Second
	cfg.AllowedLateness = 0
	cfg.ReorderDepth = 0
	cfg.MaintenanceInterval = time.Hour

	p, err := pipeline.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	sk := newSink(p)
	ctx := context.Background()

	mustSubmit(t, p, ctx, point("hits", model.KindCounter, 1, base.Add(100*time.Millisecond), "pod-a", 0))
	mustSubmit(t, p, ctx, point("hits", model.KindCounter, 2, base.Add(3*time.Second), "pod-a", 0)) // closes window 0
	eventually(t, "first window", func() bool { return len(sk.results()) > 0 })

	// Now a straggler for the window that already shipped.
	mustSubmit(t, p, ctx, point("hits", model.KindCounter, 3, base.Add(200*time.Millisecond), "pod-a", 0))

	closePipeline(t, p)
	sk.wait(t)

	if got := sk.errs().Counts[merr.CodeLate]; got != 1 {
		t.Fatalf("late errors = %d, want 1", got)
	}
	if st := p.Stats(); st.Late != 1 {
		t.Fatalf("stats.late = %d, want 1", st.Late)
	}
	// The already-published window is never retracted.
	first := sk.results()[0]
	if c := first.Aggregates[0].Count; c != 1 {
		t.Fatalf("published window was mutated after the fact: count = %d, want 1", c)
	}
}

func TestPointsAreBucketedIntoAlignedWindows(t *testing.T) {
	base := time.Unix(0, 0).UTC().Add(3 * time.Hour) // deterministic, epoch-aligned
	cfg := testConfig()
	cfg.WindowSize = 10 * time.Second
	cfg.ReorderDepth = 0
	cfg.MaxFutureSkew = 0 // the test uses historical timestamps on purpose

	var pts []*model.Point
	for i := 0; i < 6; i++ { // 0s,5s,10s,15s,20s,25s -> three windows
		pts = append(pts, point("hits", model.KindCounter, float64(i), base.Add(time.Duration(i*5)*time.Second), "pod-a", 0))
	}
	snap, err := pipeline.Collect(context.Background(), cfg, pts)
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.Aggregates) != 3 {
		t.Fatalf("got %d windows, want 3", len(snap.Aggregates))
	}
	for i, a := range snap.Aggregates {
		want := base.Add(time.Duration(i*10) * time.Second)
		if !a.WindowStart.Equal(want) {
			t.Fatalf("window %d starts at %v, want %v", i, a.WindowStart, want)
		}
		if a.Count != 2 {
			t.Fatalf("window %d has %d points, want 2", i, a.Count)
		}
	}
}

func TestFutureSkewCannotForceCloseOtherWindows(t *testing.T) {
	cfg := testConfig()
	cfg.WindowSize = time.Second
	cfg.ReorderDepth = 0
	cfg.MaxFutureSkew = time.Minute

	p, err := pipeline.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	sk := newSink(p)

	err = p.Submit(context.Background(), point("hits", model.KindCounter, 1, time.Now().Add(48*time.Hour), "broken-clock", 0))
	if err == nil {
		t.Fatal("a point from a broken clock must be rejected, not allowed to drag the watermark")
	}
	var e *merr.Error
	if !errors.As(err, &e) || e.Code != merr.CodeValidation {
		t.Fatalf("got %v, want a validation error", err)
	}
	closePipeline(t, p)
	sk.wait(t)
}

// ---------------------------------------------------------------------------
// Concurrency
// ---------------------------------------------------------------------------

// Run under -race. Many producers, many series, one exact expected total.
func TestConcurrentProducersLoseNothing(t *testing.T) {
	base := windowBase()
	cfg := testConfig()
	cfg.ReorderDepth = 0
	cfg.Shards = 8
	cfg.ShardQueueSize = 64 // small on purpose, so blocking backpressure is exercised

	p, err := pipeline.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	sk := newSink(p)

	const (
		producers = 8
		perProd   = 500
		series    = 16
	)
	var wg sync.WaitGroup
	wg.Add(producers)
	for g := 0; g < producers; g++ {
		go func(g int) {
			defer wg.Done()
			ctx := context.Background()
			for i := 0; i < perProd; i++ {
				name := fmt.Sprintf("op_%d", i%series)
				pt := point(name, model.KindHistogram, 1, base.Add(time.Duration(i)*time.Millisecond), fmt.Sprintf("pod-%d", g), 0)
				if err := p.Submit(ctx, pt); err != nil {
					t.Errorf("submit: %v", err)
					return
				}
			}
		}(g)
	}
	wg.Wait()
	closePipeline(t, p)
	sk.wait(t)

	expected := uint64(producers * perProd)
	if total := totalCount(sk.aggregates()); total != expected {
		t.Fatalf("folded %d points, want %d", total, expected)
	}
	if st := p.Stats(); st.Folded != expected {
		t.Fatalf("stats.folded = %d, want %d", st.Folded, expected)
	}
}

func TestShardAssignmentIsStickyPerSeries(t *testing.T) {
	base := windowBase()
	cfg := testConfig()
	cfg.ReorderDepth = 0
	cfg.Shards = 8

	p, err := pipeline.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	sk := newSink(p)
	ctx := context.Background()
	for i := 0; i < 200; i++ {
		mustSubmit(t, p, ctx, point("sticky", model.KindCounter, float64(i), base.Add(time.Duration(i)*time.Millisecond), "pod-a", 0))
	}
	closePipeline(t, p)
	sk.wait(t)

	// One series must never be split across shards -- if it were, neither shard
	// would see the full ordered stream and both counter deltas would be wrong.
	shards := map[int]bool{}
	for _, r := range sk.results() {
		for _, a := range r.Aggregates {
			if a.Name == "sticky" {
				shards[r.Shard] = true
			}
		}
	}
	if len(shards) != 1 {
		t.Fatalf("series was folded on %d shards, want 1", len(shards))
	}
	if n := totalCount(sk.aggregates()); n != 200 {
		t.Fatalf("folded %d points, want 200", n)
	}
}

// ---------------------------------------------------------------------------
// Backpressure, quarantine, lifecycle
// ---------------------------------------------------------------------------

func TestDropNewestShedsInsteadOfBlocking(t *testing.T) {
	base := windowBase()
	cfg := testConfig()
	cfg.Shards = 1
	cfg.ShardQueueSize = 1
	cfg.ReorderDepth = 0
	cfg.Overflow = pipeline.PolicyDropNewest

	// Wedge the single shard inside a fold so the queue cannot drain.
	release := make(chan struct{})
	var once sync.Once
	cfg.Transform = func(p *model.Point) error {
		once.Do(func() { <-release })
		return nil
	}

	p, err := pipeline.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	sk := newSink(p)
	ctx := context.Background()

	var shed error
	for i := 0; i < 64 && shed == nil; i++ {
		shed = p.Submit(ctx, point("hits", model.KindCounter, 1, base, "pod-a", 0))
	}
	if shed == nil {
		t.Fatal("expected the queue to shed once full")
	}
	if !errors.Is(shed, merr.ErrBackpressure) {
		t.Fatalf("got %v, want a backpressure error", shed)
	}
	// Crucially, the producer was never made to wait.
	close(release)

	closePipeline(t, p)
	sk.wait(t)
	if st := p.Stats(); st.Backpressure == 0 {
		t.Fatal("shed points were not counted")
	}
}

func TestQuarantineShedsAPoisonedProducer(t *testing.T) {
	base := windowBase()
	cfg := testConfig()
	cfg.Shards = 1
	cfg.ReorderDepth = 0
	cfg.QuarantineErrorRate = 0.5
	cfg.QuarantineMinSamples = 20
	cfg.QuarantineCooldown = time.Minute
	cfg.Transform = func(p *model.Point) error {
		if p.Source == "bad-pod" {
			return errPoisoned
		}
		return nil
	}

	p, err := pipeline.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	sk := newSink(p)
	ctx := context.Background()

	var lastErr error
	for i := 0; i < 200; i++ {
		lastErr = p.Submit(ctx, point("hits", model.KindCounter, 1, base, "bad-pod", 0))
		if errors.Is(lastErr, merr.ErrQuarantined) {
			break
		}
		if i%20 == 19 {
			time.Sleep(5 * time.Millisecond) // let the shard catch up to the breaker
		}
	}
	if !errors.Is(lastErr, merr.ErrQuarantined) {
		t.Fatalf("producer was never shed; last error: %v", lastErr)
	}
	if q := p.Quarantined(); len(q) != 1 {
		t.Fatalf("quarantine list = %v, want one entry", q)
	}
	// A healthy producer is unaffected: the breaker is per source, not global.
	if err := p.Submit(ctx, point("hits", model.KindCounter, 1, base, "good-pod", 0)); err != nil {
		t.Fatalf("healthy producer was collaterally shed: %v", err)
	}

	closePipeline(t, p)
	sk.wait(t)
}

func TestShutdownFlushesOpenWindowsAsPartial(t *testing.T) {
	base := windowBase()
	cfg := testConfig()
	cfg.ReorderDepth = 0

	p, err := pipeline.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	sk := newSink(p)
	ctx := context.Background()
	for i := 0; i < 10; i++ {
		mustSubmit(t, p, ctx, point("hits", model.KindCounter, float64(i), base.Add(time.Duration(i)*time.Second), "pod-a", 0))
	}
	closePipeline(t, p)
	sk.wait(t)

	aggs := sk.aggregates()
	if len(aggs) != 1 {
		t.Fatalf("got %d aggregates, want 1", len(aggs))
	}
	if aggs[0].Count != 10 {
		t.Fatalf("in-flight window lost data: count = %d, want 10", aggs[0].Count)
	}
	if !aggs[0].Partial {
		t.Fatal("a window flushed at shutdown must be marked Partial")
	}
	if r := sk.results()[0].Reason; r != pipeline.ReasonShutdown {
		t.Fatalf("reason = %q, want shutdown", r)
	}
}

func TestCloseIsIdempotentAndRejectsLateSubmits(t *testing.T) {
	cfg := testConfig()
	p, err := pipeline.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	sk := newSink(p)
	closePipeline(t, p)
	closePipeline(t, p) // second call must be a no-op, not a double close panic
	sk.wait(t)

	err = p.Submit(context.Background(), point("hits", model.KindCounter, 1, windowBase(), "pod-a", 0))
	if !errors.Is(err, merr.ErrPipelineClosed) {
		t.Fatalf("got %v, want ErrPipelineClosed", err)
	}
}

func TestSubmitBatchReportsEveryFailure(t *testing.T) {
	base := windowBase()
	cfg := testConfig()
	cfg.ReorderDepth = 0

	p, err := pipeline.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	sk := newSink(p)

	batch := []*model.Point{
		point("ok", model.KindGauge, 1, base, "pod-a", 0),
		{Service: "s", Name: "n", Source: "pod-a"}, // no event time
		point("ok", model.KindGauge, 2, base, "pod-a", 0),
		{Service: "s", Source: "pod-a", EventTime: base}, // no name
	}
	err = p.SubmitBatch(context.Background(), batch)
	var m *merr.MultiError
	if !errors.As(err, &m) {
		t.Fatalf("got %T, want *merr.MultiError", err)
	}
	if m.Total() != 2 {
		t.Fatalf("reported %d failures, want 2 (batch must not stop at the first)", m.Total())
	}
	closePipeline(t, p)
	sk.wait(t)
	if total := totalCount(sk.aggregates()); total != 2 {
		t.Fatalf("folded %d valid points, want 2", total)
	}
}

func TestMergeAggregatesCombinesReplicaViews(t *testing.T) {
	base := windowBase().Truncate(time.Minute)
	mk := func(count uint64, sum, last float64, lastAt time.Time) model.Aggregate {
		return model.Aggregate{
			SeriesKey: "checkout\x1fhits", Name: "hits", WindowStart: base, WindowEnd: base.Add(time.Minute),
			Count: count, Sum: sum, Min: 1, Max: last, Last: last, LastEventTime: lastAt, Delta: sum,
		}
	}
	out := pipeline.MergeAggregates([]model.Aggregate{
		mk(3, 6, 3, base.Add(3*time.Second)),
		mk(2, 9, 5, base.Add(9*time.Second)),
	})
	if len(out) != 1 {
		t.Fatalf("got %d aggregates, want 1", len(out))
	}
	if out[0].Count != 5 || out[0].Sum != 15 {
		t.Fatalf("count/sum = %d/%v, want 5/15", out[0].Count, out[0].Sum)
	}
	if out[0].Last != 5 {
		t.Fatalf("last = %v, want 5 (resolved by event time)", out[0].Last)
	}
}

func TestHistogramQuantilesAreWithinRelativeError(t *testing.T) {
	s := model.NewSketch()
	for i := 1; i <= 1000; i++ {
		s.Add(float64(i))
	}
	for _, tc := range []struct{ q, want float64 }{{0.5, 500}, {0.9, 900}, {0.99, 990}} {
		got := s.Quantile(tc.q)
		if rel := (got - tc.want) / tc.want; rel > 0.03 || rel < -0.03 {
			t.Fatalf("p%v = %v, want ~%v (relative error %.3f)", tc.q*100, got, tc.want, rel)
		}
	}
	// Mergeability is what lets two replicas fold the same window independently.
	a, b := model.NewSketch(), model.NewSketch()
	for i := 1; i <= 500; i++ {
		a.Add(float64(i))
	}
	for i := 501; i <= 1000; i++ {
		b.Add(float64(i))
	}
	a.Merge(b)
	if got := a.Quantile(0.5); got < 480 || got > 520 {
		t.Fatalf("merged p50 = %v, want ~500", got)
	}
}

// ---------------------------------------------------------------------------
// helpers used above
// ---------------------------------------------------------------------------

func mustSubmit(t *testing.T, p *pipeline.Pipeline, ctx context.Context, pt *model.Point) {
	t.Helper()
	if err := p.Submit(ctx, pt); err != nil {
		t.Fatalf("submit: %v", err)
	}
}

func totalCount(aggs []model.Aggregate) uint64 {
	var n uint64
	for _, a := range aggs {
		n += a.Count
	}
	return n
}

// ---------------------------------------------------------------------------
// Benchmarks
// ---------------------------------------------------------------------------

func BenchmarkSubmit(b *testing.B) {
	cfg := pipeline.DefaultConfig()
	cfg.Shards = 8
	cfg.WindowSize = time.Hour
	cfg.ReorderDepth = 0
	cfg.ShardQueueSize = 1 << 16
	cfg.Overflow = pipeline.PolicyDropNewest

	p, _ := pipeline.New(cfg)
	go func() {
		for range p.Results() {
		}
	}()
	base := time.Now().Truncate(time.Hour).Add(90 * time.Second)
	names := make([]string, 64)
	for i := range names {
		names[i] = fmt.Sprintf("op_%d", i)
	}

	b.ReportAllocs()
	b.RunParallel(func(pb *testing.PB) {
		ctx := context.Background()
		i := 0
		for pb.Next() {
			pt := &model.Point{
				Service: "checkout", Name: names[i%len(names)], Kind: model.KindHistogram,
				Value: float64(i), EventTime: base, Source: "pod-a",
			}
			_ = p.Submit(ctx, pt)
			i++
		}
	})
	b.StopTimer()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = p.Close(ctx)
}

package pipeline

import (
	"errors"
	"fmt"
	"runtime"
	"time"

	"github.com/vaibhav/metricsprocessor/pkg/model"
)

// OverflowPolicy decides what a shard queue does when it is full. This is the
// single most consequential knob in the service, because it is where the
// latency-versus-completeness trade-off is actually made.
type OverflowPolicy uint8

const (
	// PolicyBlock applies backpressure to the producer up to SubmitTimeout. Use
	// it when every point matters more than tail latency (billing, SLO burn).
	PolicyBlock OverflowPolicy = iota
	// PolicyDropNewest rejects the incoming point immediately and reports it as
	// a backpressure error. Use it when the aggregator must never become the
	// thing that slows down the services it observes -- which is the usual
	// choice for telemetry: monitoring should degrade before production does.
	PolicyDropNewest
)

func (p OverflowPolicy) String() string {
	if p == PolicyDropNewest {
		return "drop_newest"
	}
	return "block"
}

// Config configures a Pipeline. The zero value is not usable; start from
// DefaultConfig and adjust.
type Config struct {
	// Shards is the number of independent fold workers. Each shard is a single
	// goroutine that owns its state exclusively, so there is no lock anywhere in
	// the fold path. Series are assigned to shards by a deterministic hash of the
	// series key, which is what preserves per-series ordering.
	Shards int

	// WindowSize is the tumbling window width. Windows are epoch-aligned so that
	// every shard, replica, and restart agrees on boundaries without coordination.
	WindowSize time.Duration

	// AllowedLateness is how far behind the maximum observed event time the
	// watermark trails. Larger values tolerate more skew between producers at the
	// cost of delaying every window emission by the same amount.
	AllowedLateness time.Duration

	// ReorderDepth is how many out-of-sequence points a single producer stream may
	// have buffered before the pipeline declares a gap and moves on. Zero disables
	// sequence-based reordering entirely (arrival order is then trusted).
	ReorderDepth int

	// MaxReorderDelay bounds how long a stalled stream waits for a missing
	// sequence number before the gap is declared. This is the hard ceiling on the
	// latency that reordering can add.
	MaxReorderDelay time.Duration

	// ShardQueueSize is the buffered capacity of each shard's inbound channel. It
	// absorbs bursts; it is not a durability mechanism.
	ShardQueueSize int

	// ResultQueueSize buffers closed windows on their way to the publisher.
	ResultQueueSize int

	// MaintenanceInterval is how often a shard wakes up with no traffic to close
	// windows that wall-clock time has passed, evict idle stream state, and decay
	// error budgets. It only affects idle or low-rate series; a busy series closes
	// its window the instant the first point of the next window arrives.
	MaintenanceInterval time.Duration

	// IdleStreamTTL is how long per-producer reorder state is retained without
	// traffic. Bounds memory against churning pod names.
	IdleStreamTTL time.Duration

	// MaxErrorsPerWindow caps the detailed errors retained per shard per window.
	// Beyond the cap only counts are kept.
	MaxErrorsPerWindow int

	// Overflow selects the backpressure policy. See OverflowPolicy.
	Overflow OverflowPolicy

	// SubmitTimeout bounds how long Submit blocks under PolicyBlock.
	SubmitTimeout time.Duration

	// PanicIsolation wraps each per-point fold in a recover so that one poisoned
	// point cannot take down a shard and the millions of healthy series it owns.
	// It costs a deferred call per point; leave it on unless a benchmark says
	// otherwise.
	PanicIsolation bool

	// QuarantineErrorRate is the fraction of a source's points that must fail
	// before the source is shed. Zero disables quarantine.
	QuarantineErrorRate float64
	// QuarantineMinSamples is the minimum observation count before the breaker
	// can trip, so a source's first bad point does not shed it.
	QuarantineMinSamples uint64
	// QuarantineCooldown is how long a shed source stays shed.
	QuarantineCooldown time.Duration

	// MaxFutureSkew rejects points whose event time is implausibly far ahead of
	// wall clock. Without this bound a single producer with a broken clock can
	// drag the watermark forward and force-close every open window on its shard,
	// silently truncating everyone else's data. Zero disables the check.
	MaxFutureSkew time.Duration

	// Transform, if set, runs on every point immediately before the fold. It is
	// the supported extension point for enrichment, unit conversion, and label
	// rewriting. It runs inside the per-point failure isolation, so a transform
	// that returns an error drops one point, and one that panics drops one point.
	Transform func(*model.Point) error

	// Now is injectable for tests. Defaults to time.Now.
	Now func() time.Time
}

// DefaultConfig returns settings tuned for a high-rate telemetry pipeline:
// second-scale freshness, tolerant of a couple of seconds of producer skew, and
// biased toward never blocking the services being observed.
func DefaultConfig() Config {
	return Config{
		Shards:               runtime.GOMAXPROCS(0),
		WindowSize:           10 * time.Second,
		AllowedLateness:      2 * time.Second,
		ReorderDepth:         64,
		MaxReorderDelay:      250 * time.Millisecond,
		ShardQueueSize:       4096,
		ResultQueueSize:      256,
		MaintenanceInterval:  200 * time.Millisecond,
		IdleStreamTTL:        5 * time.Minute,
		MaxErrorsPerWindow:   64,
		Overflow:             PolicyDropNewest,
		SubmitTimeout:        50 * time.Millisecond,
		PanicIsolation:       true,
		QuarantineErrorRate:  0.5,
		QuarantineMinSamples: 100,
		QuarantineCooldown:   30 * time.Second,
		Now:                  time.Now,
	}
}

// ErrBadConfig is returned by New for an unusable configuration.
var ErrBadConfig = errors.New("pipeline: invalid config")

func (c *Config) normalize() error {
	if c.Now == nil {
		c.Now = time.Now
	}
	if c.Shards <= 0 {
		c.Shards = runtime.GOMAXPROCS(0)
	}
	if c.WindowSize <= 0 {
		return fmt.Errorf("%w: WindowSize must be > 0", ErrBadConfig)
	}
	if c.AllowedLateness < 0 {
		return fmt.Errorf("%w: AllowedLateness must be >= 0", ErrBadConfig)
	}
	if c.ReorderDepth < 0 {
		return fmt.Errorf("%w: ReorderDepth must be >= 0", ErrBadConfig)
	}
	if c.QuarantineErrorRate < 0 || c.QuarantineErrorRate > 1 {
		return fmt.Errorf("%w: QuarantineErrorRate must be in [0,1]", ErrBadConfig)
	}
	if c.ShardQueueSize <= 0 {
		c.ShardQueueSize = 1024
	}
	if c.ResultQueueSize <= 0 {
		c.ResultQueueSize = 64
	}
	if c.MaintenanceInterval <= 0 {
		c.MaintenanceInterval = 200 * time.Millisecond
	}
	if c.IdleStreamTTL <= 0 {
		c.IdleStreamTTL = 5 * time.Minute
	}
	if c.MaxErrorsPerWindow < 0 {
		c.MaxErrorsPerWindow = 0
	}
	if c.MaxReorderDelay <= 0 {
		c.MaxReorderDelay = 250 * time.Millisecond
	}
	if c.SubmitTimeout <= 0 {
		c.SubmitTimeout = 50 * time.Millisecond
	}
	if c.QuarantineMinSamples == 0 {
		c.QuarantineMinSamples = 100
	}
	if c.QuarantineCooldown <= 0 {
		c.QuarantineCooldown = 30 * time.Second
	}
	return nil
}

package kafkaworker

import (
	"sync/atomic"
	"time"
)

// Metrics is the observability seam. The pool calls it on hot paths, so
// implementations must be cheap and non-blocking — a Prometheus counter or
// gauge qualifies, an HTTP call does not.
//
// Worker utilisation is deliberately exported as a *counter of busy seconds*
// rather than a percentage gauge. A gauge sampled every 15s misses bursts;
// rate(worker_busy_seconds_total[1m]) / worker_count reconstructs utilisation
// at any resolution and survives restarts.
type Metrics interface {
	// SetQueueDepth reports the number of records buffered in the pool.
	SetQueueDepth(n int)
	// SetInFlight reports records dispatched but not yet acked.
	SetInFlight(n int)
	// SetConcurrencyLimit reports the adaptive limiter's current ceiling.
	SetConcurrencyLimit(n int)
	// AddWorkerBusy accumulates time a worker spent inside a handler.
	AddWorkerBusy(d time.Duration)
	// ObserveProcess records one terminal record outcome.
	// result is one of "ok", "retry_exhausted", "dropped".
	ObserveProcess(d time.Duration, result string)
	// ObserveRetry records one retry attempt.
	ObserveRetry()
	// ObserveBlockedOnLimiter accumulates time workers spent waiting for a
	// downstream permit. Non-zero and rising is the signal that the
	// dependency, not the pool, is the bottleneck.
	ObserveBlockedOnLimiter(d time.Duration)
	// ObserveCommit records an offset-commit attempt.
	ObserveCommit(d time.Duration, err error)
}

// NopMetrics discards everything.
type NopMetrics struct{}

func (NopMetrics) SetQueueDepth(int)                     {}
func (NopMetrics) SetInFlight(int)                       {}
func (NopMetrics) SetConcurrencyLimit(int)               {}
func (NopMetrics) AddWorkerBusy(time.Duration)           {}
func (NopMetrics) ObserveProcess(time.Duration, string)  {}
func (NopMetrics) ObserveRetry()                         {}
func (NopMetrics) ObserveBlockedOnLimiter(time.Duration) {}
func (NopMetrics) ObserveCommit(time.Duration, error)    {}

// CountingMetrics is a lock-free in-memory Metrics used by the tests and by
// the /debug endpoints of the demo. Production wires Prometheus instead.
type CountingMetrics struct {
	QueueDepth  atomic.Int64
	InFlight    atomic.Int64
	Limit       atomic.Int64
	BusyNanos   atomic.Int64
	BlockedNano atomic.Int64
	OK          atomic.Int64
	Exhausted   atomic.Int64
	Dropped     atomic.Int64
	Retries     atomic.Int64
	Commits     atomic.Int64
	CommitFails atomic.Int64
}

func (m *CountingMetrics) SetQueueDepth(n int)       { m.QueueDepth.Store(int64(n)) }
func (m *CountingMetrics) SetInFlight(n int)         { m.InFlight.Store(int64(n)) }
func (m *CountingMetrics) SetConcurrencyLimit(n int) { m.Limit.Store(int64(n)) }
func (m *CountingMetrics) AddWorkerBusy(d time.Duration) {
	m.BusyNanos.Add(int64(d))
}
func (m *CountingMetrics) ObserveBlockedOnLimiter(d time.Duration) {
	m.BlockedNano.Add(int64(d))
}
func (m *CountingMetrics) ObserveRetry() { m.Retries.Add(1) }

func (m *CountingMetrics) ObserveProcess(_ time.Duration, result string) {
	switch result {
	case "ok":
		m.OK.Add(1)
	case "retry_exhausted":
		m.Exhausted.Add(1)
	default:
		m.Dropped.Add(1)
	}
}

func (m *CountingMetrics) ObserveCommit(_ time.Duration, err error) {
	m.Commits.Add(1)
	if err != nil {
		m.CommitFails.Add(1)
	}
}

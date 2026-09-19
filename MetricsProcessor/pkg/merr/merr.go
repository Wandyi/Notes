// Package merr models *partial* failure.
//
// The design rule this package exists to enforce: a failure that affects one
// point, one series, or one producer must never stop the aggregation of everything
// else. So errors are values that travel alongside results rather than control
// flow that unwinds a pipeline stage.
//
// The collector is intentionally *not* goroutine-safe. Each shard owns one, which
// means the hot path records an error with a plain slice append and no lock. The
// per-shard collectors are drained and merged only when a window closes.
package merr

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
)

// Code classifies a failure so that operators can alert on categories rather than
// on message text, and so the pipeline can apply different policies per category
// (drop, quarantine, retry, or just count).
type Code string

const (
	// CodeValidation - the point was structurally invalid. Dropped at the edge.
	CodeValidation Code = "validation"
	// CodeSequenceGap - a producer's sequence skipped and the reorder buffer gave
	// up waiting. The window is marked Partial; aggregation continues.
	CodeSequenceGap Code = "sequence_gap"
	// CodeDuplicate - a sequence number was seen twice (at-least-once replay).
	// The point is discarded to keep the fold idempotent.
	CodeDuplicate Code = "duplicate"
	// CodeLate - the point arrived after its window closed past allowed lateness.
	CodeLate Code = "late"
	// CodePanic - the fold for a single point panicked. Isolated and recorded.
	CodePanic Code = "panic"
	// CodeBackpressure - a shard queue was full and the submit deadline expired.
	CodeBackpressure Code = "backpressure"
	// CodeQuarantine - the source exceeded its error budget and is being shed.
	CodeQuarantine Code = "quarantine"
	// CodeDownstream - publishing a closed window failed.
	CodeDownstream Code = "downstream"
	// CodeOverflow - the collector itself hit its cap and dropped detail.
	CodeOverflow Code = "overflow"
)

// Sentinels for errors.Is checks at API boundaries.
var (
	ErrPipelineClosed = errors.New("metrics: pipeline is closed")
	ErrBackpressure   = errors.New("metrics: shard queue full")
	ErrQuarantined    = errors.New("metrics: source is quarantined")
)

// Error is one recorded failure with enough context to be actionable without a
// correlated log search: which series, which producer, and when.
type Error struct {
	Code   Code      `json:"code"`
	Series string    `json:"series,omitempty"`
	Source string    `json:"source,omitempty"`
	Shard  int       `json:"shard"`
	Msg    string    `json:"message"`
	At     time.Time `json:"at"`
	Err    error     `json:"-"`
}

func (e *Error) Error() string {
	var b strings.Builder
	b.WriteString(string(e.Code))
	if e.Source != "" {
		b.WriteString(" source=")
		b.WriteString(e.Source)
	}
	if e.Series != "" {
		b.WriteString(" series=")
		b.WriteString(strings.ReplaceAll(e.Series, "\x1f", "/"))
	}
	if e.Msg != "" {
		b.WriteString(": ")
		b.WriteString(e.Msg)
	}
	if e.Err != nil {
		b.WriteString(": ")
		b.WriteString(e.Err.Error())
	}
	return b.String()
}

func (e *Error) Unwrap() error { return e.Err }

// Is lets callers match on the code alone: errors.Is(err, &merr.Error{Code: ...}).
func (e *Error) Is(target error) bool {
	t, ok := target.(*Error)
	return ok && t.Code == e.Code && t.Series == "" && t.Source == ""
}

// New builds an Error stamped with the current time.
func New(code Code, msg string) *Error {
	return &Error{Code: code, Msg: msg, At: time.Now()}
}

// Wrap builds an Error around an underlying cause.
func Wrap(code Code, err error) *Error {
	return &Error{Code: code, Err: err, At: time.Now()}
}

// ---------------------------------------------------------------------------
// Collector
// ---------------------------------------------------------------------------

// Collector accumulates errors for one shard between window flushes.
//
// It is bounded on purpose. A single poisoned producer can emit millions of bad
// points a second; retaining every one would turn an input problem into an
// out-of-memory outage. Past the cap we keep exact *counts* per code and drop the
// detail, which preserves the alerting signal at constant cost.
type Collector struct {
	max     int
	errs    []*Error
	counts  map[Code]uint64
	dropped uint64
	shard   int
}

// NewCollector returns a collector that retains at most max detailed errors.
func NewCollector(shard, max int) *Collector {
	if max < 0 {
		max = 0
	}
	return &Collector{max: max, shard: shard, counts: make(map[Code]uint64, 8)}
}

// Add records e. It never blocks and never allocates once the cap is reached.
func (c *Collector) Add(e *Error) {
	if e == nil {
		return
	}
	if e.At.IsZero() {
		e.At = time.Now()
	}
	e.Shard = c.shard
	c.counts[e.Code]++
	if len(c.errs) < c.max {
		c.errs = append(c.errs, e)
		return
	}
	c.dropped++
}

// Addf is the formatting convenience used on cold paths only; the hot path builds
// the Error directly to avoid the fmt allocation.
func (c *Collector) Addf(code Code, series, source, format string, args ...any) {
	c.Add(&Error{Code: code, Series: series, Source: source, Msg: fmt.Sprintf(format, args...)})
}

// Count returns how many errors of a code have been recorded since the last drain.
func (c *Collector) Count(code Code) uint64 { return c.counts[code] }

// Total returns the number of errors recorded since the last drain.
func (c *Collector) Total() uint64 {
	var n uint64
	for _, v := range c.counts {
		n += v
	}
	return n
}

// Drain returns everything collected so far and resets the collector. Returns nil
// when nothing was recorded, so the common case costs one comparison.
func (c *Collector) Drain() *MultiError {
	if len(c.counts) == 0 {
		return nil
	}
	m := &MultiError{Errors: c.errs, Counts: c.counts, Dropped: c.dropped}
	c.errs = nil
	c.counts = make(map[Code]uint64, 8)
	c.dropped = 0
	return m
}

// ---------------------------------------------------------------------------
// MultiError
// ---------------------------------------------------------------------------

// MultiError is the aggregated failure report handed back with results. It
// implements Unwrap() []error so errors.Is and errors.As see through to every
// contained error.
type MultiError struct {
	Errors  []*Error        `json:"errors,omitempty"`
	Counts  map[Code]uint64 `json:"counts,omitempty"`
	Dropped uint64          `json:"dropped_detail,omitempty"`
}

// Add appends an error, initialising as needed. Safe on a nil-map MultiError.
func (m *MultiError) Add(e *Error) {
	if e == nil {
		return
	}
	if m.Counts == nil {
		m.Counts = make(map[Code]uint64, 4)
	}
	m.Counts[e.Code]++
	m.Errors = append(m.Errors, e)
}

// Merge folds other into m. Used to combine per-shard reports into one response.
func (m *MultiError) Merge(other *MultiError) {
	if other == nil {
		return
	}
	if m.Counts == nil {
		m.Counts = make(map[Code]uint64, len(other.Counts))
	}
	for k, v := range other.Counts {
		m.Counts[k] += v
	}
	m.Errors = append(m.Errors, other.Errors...)
	m.Dropped += other.Dropped
}

// Total is the number of failures observed, including those whose detail was
// dropped by the collector cap.
func (m *MultiError) Total() uint64 {
	if m == nil {
		return 0
	}
	var n uint64
	for _, v := range m.Counts {
		n += v
	}
	return n
}

// ErrorOrNil returns m as an error, or nil if it holds nothing. Use it whenever
// a MultiError is returned through an `error` interface -- a non-nil *MultiError
// in a nil-valued error slot is the classic Go typed-nil trap.
func (m *MultiError) ErrorOrNil() error {
	if m == nil || len(m.Counts) == 0 {
		return nil
	}
	return m
}

func (m *MultiError) Error() string {
	if m == nil || len(m.Counts) == 0 {
		return "<no errors>"
	}
	codes := make([]string, 0, len(m.Counts))
	for c, n := range m.Counts {
		codes = append(codes, fmt.Sprintf("%s=%d", c, n))
	}
	sort.Strings(codes)
	s := fmt.Sprintf("%d partial failures (%s)", m.Total(), strings.Join(codes, " "))
	if len(m.Errors) > 0 {
		s += "; first: " + m.Errors[0].Error()
	}
	if m.Dropped > 0 {
		s += fmt.Sprintf("; %d further details dropped", m.Dropped)
	}
	return s
}

// Unwrap exposes the contained errors to errors.Is / errors.As.
func (m *MultiError) Unwrap() []error {
	if m == nil {
		return nil
	}
	out := make([]error, len(m.Errors))
	for i, e := range m.Errors {
		out[i] = e
	}
	return out
}

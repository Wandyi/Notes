// Package model holds the wire and in-memory types shared by every service in the
// metrics platform: the ingest gateway, the aggregator, and the query API.
//
// Types here are deliberately dependency-free so that the contract can be vendored
// into producer services without dragging in the aggregation machinery.
package model

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"
)

// Kind describes how a series is folded. The kind determines which fields of an
// Aggregate are meaningful and, critically, whether event ordering matters:
//
//	KindCounter   - order matters (reset detection and rate need ordered deltas)
//	KindGauge     - order matters (last-write-wins by event time)
//	KindHistogram - order does not matter (folding is commutative)
type Kind uint8

const (
	KindCounter Kind = iota
	KindGauge
	KindHistogram
)

var kindNames = [...]string{"counter", "gauge", "histogram"}

func (k Kind) String() string {
	if int(k) >= len(kindNames) {
		return fmt.Sprintf("kind(%d)", uint8(k))
	}
	return kindNames[k]
}

// Valid reports whether k is one of the defined kinds.
func (k Kind) Valid() bool { return int(k) < len(kindNames) }

// OrderSensitive reports whether the fold for this kind depends on the order in
// which points are applied. The pipeline uses this to decide where it may take
// shortcuts, and the docs use it to justify the per-series ordering guarantee.
func (k Kind) OrderSensitive() bool { return k == KindCounter || k == KindGauge }

func (k Kind) MarshalJSON() ([]byte, error) { return json.Marshal(k.String()) }

func (k *Kind) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	return k.UnmarshalText([]byte(s))
}

func (k *Kind) UnmarshalText(b []byte) error {
	for i, n := range kindNames {
		if n == string(b) {
			*k = Kind(i)
			return nil
		}
	}
	return fmt.Errorf("model: unknown metric kind %q", string(b))
}

// ParseKind resolves a textual kind, defaulting to counter for the empty string.
func ParseKind(s string) (Kind, error) {
	if s == "" {
		return KindCounter, nil
	}
	var k Kind
	err := k.UnmarshalText([]byte(s))
	return k, err
}

// Point is a single observation emitted by a producing microservice.
//
// Two identifiers matter for correctness:
//
//   - SeriesKey identifies the logical time series. It is the partition key used
//     end to end (gateway -> broker -> aggregator shard), which is what makes the
//     per-series ordering guarantee hold across process boundaries.
//   - Source plus Seq identify a position in one producer's output stream. The
//     reorder buffer uses it to repair network-level reordering without needing a
//     global clock.
type Point struct {
	Service   string            `json:"service"`
	Name      string            `json:"name"`
	Labels    map[string]string `json:"labels,omitempty"`
	Kind      Kind              `json:"kind"`
	Value     float64           `json:"value"`
	EventTime time.Time         `json:"event_time"`

	// Source is the producer instance (pod name, host, replica id). Ordering is
	// only ever defined within a single Source.
	Source string `json:"source"`
	// Seq is a monotonically increasing sequence number scoped to the
	// (Source, SeriesKey) pair -- that is, to the StreamKey, not to the whole
	// producer. Scoping it per series is what makes it *contiguous* from the
	// point of view of the shard that owns the series; a per-producer counter
	// would arrive full of holes at every shard and the reorder buffer would
	// spend its life declaring gaps that were not gaps.
	//
	// Zero means "this producer does not sequence"; the pipeline then trusts
	// arrival order for that stream.
	Seq uint64 `json:"seq,omitempty"`

	// IngestTime is stamped by the gateway. It is used only for observability
	// (ingest lag) and for the wall-clock timeout of the reorder buffer, never
	// for windowing.
	IngestTime time.Time `json:"ingest_time,omitzero"`

	key    string // memoized series key
	stream string // memoized ordering-domain key
}

// Validation errors. They are wrapped rather than returned bare so that callers
// can classify with errors.Is while still seeing which field failed.
var (
	ErrNoService   = errors.New("model: service is required")
	ErrNoName      = errors.New("model: metric name is required")
	ErrNoSource    = errors.New("model: source is required")
	ErrNoTimestamp = errors.New("model: event_time is required")
	ErrBadKind     = errors.New("model: unknown metric kind")
	ErrBadValue    = errors.New("model: value must be a finite number")
	ErrTooManyTags = errors.New("model: too many labels")
)

// MaxLabels bounds label cardinality per point. Unbounded labels are the classic
// way to blow up a metrics backend's memory; we reject at the edge instead.
const MaxLabels = 32

// Validate performs the cheap structural checks the ingest gateway runs before a
// point is allowed into the pipeline. Rejecting here keeps malformed data from
// ever consuming a shard slot.
func (p *Point) Validate() error {
	switch {
	case p.Service == "":
		return ErrNoService
	case p.Name == "":
		return ErrNoName
	case p.Source == "":
		return ErrNoSource
	case p.EventTime.IsZero():
		return ErrNoTimestamp
	case !p.Kind.Valid():
		return ErrBadKind
	case math.IsNaN(p.Value) || math.IsInf(p.Value, 0):
		return ErrBadValue
	case len(p.Labels) > MaxLabels:
		return fmt.Errorf("%w: %d > %d", ErrTooManyTags, len(p.Labels), MaxLabels)
	}
	return nil
}

// SeriesKey returns the canonical identity of the time series this point belongs
// to. The value is memoized: it is computed once at the ingest edge and reused by
// every downstream hop, so the hot path never re-sorts labels.
func (p *Point) SeriesKey() string {
	if p.key == "" {
		p.key = BuildSeriesKey(p.Service, p.Name, p.Labels)
	}
	return p.key
}

// StreamKey identifies the ordering domain: one producer instance's view of one
// series. Sequence numbers are only comparable within a stream.
func (p *Point) StreamKey() string {
	if p.stream == "" {
		p.stream = p.Source + "\x1f" + p.SeriesKey()
	}
	return p.stream
}

// SetSeriesKey installs a precomputed key. The gateway uses this after it has
// computed the key for partitioning so the aggregator does not repeat the work.
func (p *Point) SetSeriesKey(k string) { p.key = k; p.stream = "" }

const (
	fieldSep = "\x1f" // unit separator; cannot appear in a well-formed label
	pairSep  = "\x1e" // record separator
)

// BuildSeriesKey renders a stable, collision-resistant identity for a series.
// Labels are sorted so that two producers emitting the same logical series in a
// different map order land on the same shard.
func BuildSeriesKey(service, name string, labels map[string]string) string {
	if len(labels) == 0 {
		return service + fieldSep + name
	}
	keys := make([]string, 0, len(labels))
	n := len(service) + len(name) + 2
	for k := range labels {
		keys = append(keys, k)
		n += len(k) + len(labels[k]) + 2
	}
	sort.Strings(keys)

	var b strings.Builder
	b.Grow(n)
	b.WriteString(service)
	b.WriteString(fieldSep)
	b.WriteString(name)
	for _, k := range keys {
		b.WriteString(pairSep)
		b.WriteString(k)
		b.WriteString("=")
		b.WriteString(labels[k])
	}
	return b.String()
}

// Batch is the unit of transfer between the producer SDK and the ingest gateway.
// Batching amortizes the HTTP and validation cost; it does not weaken ordering,
// because a batch is ordered and is dispatched in order.
type Batch struct {
	Source string   `json:"source"`
	Points []*Point `json:"points"`
}

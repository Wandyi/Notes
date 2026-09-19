package model

import (
	"math"
	"time"
)

// Aggregate is the folded state of one series over one closed window. It is the
// unit the aggregator publishes and the query API serves.
//
// Aggregates are *mergeable*: two Aggregates for the same series and window can be
// combined without loss. That property is what lets the system scale horizontally
// -- a second aggregator replica can fold its own slice of the traffic and the
// results can be unioned at query time.
type Aggregate struct {
	SeriesKey string            `json:"series_key"`
	Service   string            `json:"service"`
	Name      string            `json:"name"`
	Labels    map[string]string `json:"labels,omitempty"`
	Kind      Kind              `json:"kind"`

	WindowStart time.Time `json:"window_start"`
	WindowEnd   time.Time `json:"window_end"`

	Count uint64  `json:"count"`
	Sum   float64 `json:"sum"`
	Min   float64 `json:"min"`
	Max   float64 `json:"max"`

	// Last is the value of the point with the greatest event time in the window.
	// It is only meaningful because the fold sees points in order.
	Last          float64   `json:"last"`
	LastEventTime time.Time `json:"last_event_time"`

	// Delta and Rate apply to counters. Delta is the increase across the window
	// with counter resets accounted for; Rate is Delta per second.
	Delta  float64 `json:"delta,omitempty"`
	Rate   float64 `json:"rate,omitempty"`
	Resets uint32  `json:"resets,omitempty"`

	// Quantiles are computed from a mergeable sketch (histogram kind only).
	P50 float64 `json:"p50,omitempty"`
	P90 float64 `json:"p90,omitempty"`
	P99 float64 `json:"p99,omitempty"`

	// Per-series data-quality counters. These travel with the aggregate rather
	// than being buried in a log line, so a consumer can decide for itself
	// whether a window is trustworthy.
	OutOfOrder uint64 `json:"out_of_order,omitempty"`
	Late       uint64 `json:"late,omitempty"`
	Gaps       uint64 `json:"gaps,omitempty"`

	// Partial is set when the window closed while some input was known to be
	// missing (a declared sequence gap, a quarantined source, or a shutdown
	// flush). Consumers should treat Partial windows as lower confidence, not
	// as failures.
	Partial bool `json:"partial,omitempty"`
}

// Duration is the wall width of the window.
func (a *Aggregate) Duration() time.Duration { return a.WindowEnd.Sub(a.WindowStart) }

// Mean is Sum/Count, or zero for an empty aggregate.
func (a *Aggregate) Mean() float64 {
	if a.Count == 0 {
		return 0
	}
	return a.Sum / float64(a.Count)
}

// Merge folds other into a. Both must describe the same series and window; the
// caller is responsible for that check. Merge is associative and commutative
// except for Last, which is resolved by event time.
func (a *Aggregate) Merge(other *Aggregate) {
	if other.Count == 0 {
		return
	}
	if a.Count == 0 {
		a.Min, a.Max = other.Min, other.Max
	} else {
		a.Min = math.Min(a.Min, other.Min)
		a.Max = math.Max(a.Max, other.Max)
	}
	a.Count += other.Count
	a.Sum += other.Sum
	a.Delta += other.Delta
	a.Resets += other.Resets
	a.OutOfOrder += other.OutOfOrder
	a.Late += other.Late
	a.Gaps += other.Gaps
	a.Partial = a.Partial || other.Partial
	if other.LastEventTime.After(a.LastEventTime) {
		a.Last, a.LastEventTime = other.Last, other.LastEventTime
	}
	if d := a.Duration().Seconds(); d > 0 {
		a.Rate = a.Delta / d
	}
}

// ---------------------------------------------------------------------------
// Sketch
// ---------------------------------------------------------------------------

// sketchGamma controls bucket width. Buckets are laid out on a logarithmic grid,
// which bounds the *relative* error of any quantile at (gamma-1)/(gamma+1) --
// about 1.2% here -- regardless of the magnitude of the values. Latency data
// spans several orders of magnitude, so relative error is the right guarantee.
const sketchGamma = 1.025

var logGamma = math.Log(sketchGamma)

// Sketch is a sparse, mergeable, log-bucketed histogram (the same shape as
// DDSketch). Sparse because most series touch a handful of buckets; mergeable
// because two sketches combine by summing bucket counts, which is what allows a
// window to be folded independently on several shards or replicas and unioned
// later without re-reading the raw points.
type Sketch struct {
	buckets  map[int32]uint64
	zeros    uint64
	negative uint64
	total    uint64
}

// NewSketch returns an empty sketch.
func NewSketch() *Sketch { return &Sketch{buckets: make(map[int32]uint64, 8)} }

// Add records one observation.
func (s *Sketch) Add(v float64) {
	s.total++
	switch {
	case v == 0:
		s.zeros++
	case v < 0:
		// Negative latencies are not a thing; we count them so the data-quality
		// signal survives instead of silently skewing quantiles.
		s.negative++
	default:
		idx := int32(math.Ceil(math.Log(v) / logGamma))
		s.buckets[idx]++
	}
}

// Merge folds other into s.
func (s *Sketch) Merge(other *Sketch) {
	if other == nil {
		return
	}
	for idx, c := range other.buckets {
		s.buckets[idx] += c
	}
	s.zeros += other.zeros
	s.negative += other.negative
	s.total += other.total
}

// Count returns the number of observations.
func (s *Sketch) Count() uint64 { return s.total }

// Quantile returns the approximate q-quantile, q in [0,1].
func (s *Sketch) Quantile(q float64) float64 {
	if s == nil || s.total == 0 {
		return 0
	}
	rank := uint64(math.Ceil(q * float64(s.total)))
	if rank == 0 {
		rank = 1
	}
	if rank <= s.negative+s.zeros {
		return 0
	}
	rank -= s.negative + s.zeros

	// The bucket map is small (tens of entries in practice), so an ordered walk
	// over sorted keys is cheaper than maintaining a sorted structure on insert,
	// where the hot path is.
	keys := s.sortedKeys()
	var seen uint64
	for _, idx := range keys {
		seen += s.buckets[idx]
		if seen >= rank {
			// Midpoint of the bucket, which is where the relative-error bound
			// is tightest.
			return 2 * math.Pow(sketchGamma, float64(idx)) / (1 + sketchGamma)
		}
	}
	return 2 * math.Pow(sketchGamma, float64(keys[len(keys)-1])) / (1 + sketchGamma)
}

func (s *Sketch) sortedKeys() []int32 {
	keys := make([]int32, 0, len(s.buckets))
	for k := range s.buckets {
		keys = append(keys, k)
	}
	// Insertion sort: len is small and this avoids the sort.Slice reflection and
	// closure allocation on a path that runs once per window close.
	for i := 1; i < len(keys); i++ {
		for j := i; j > 0 && keys[j] < keys[j-1]; j-- {
			keys[j], keys[j-1] = keys[j-1], keys[j]
		}
	}
	return keys
}

package pipeline

import (
	"math"
	"time"

	"github.com/vaibhav/metricsprocessor/pkg/model"
)

// seriesState is the mutable fold state for one series inside one window.
//
// It is owned exclusively by one shard goroutine. There is no mutex here and
// there must never be one: exclusive ownership by a single goroutine is the
// concurrency-control mechanism, and adding a lock would only advertise that the
// ownership rule had been broken.
type seriesState struct {
	service string
	name    string
	labels  map[string]string
	kind    model.Kind

	count uint64
	sum   float64
	min   float64
	max   float64

	last   float64
	lastTS time.Time

	// Counter bookkeeping. prev is the last cumulative value seen *in event
	// order*; delta accumulates the monotonic increase with resets folded in.
	prev    float64
	prevSet bool
	delta   float64
	resets  uint32

	sketch *model.Sketch

	outOfOrder uint64
	late       uint64
	gaps       uint64
	partial    bool
}

func newSeriesState(p *model.Point) *seriesState {
	s := &seriesState{
		service: p.Service,
		name:    p.Name,
		labels:  p.Labels,
		kind:    p.Kind,
		min:     math.Inf(1),
		max:     math.Inf(-1),
	}
	if p.Kind == model.KindHistogram {
		s.sketch = model.NewSketch()
	}
	return s
}

// apply folds one point. Points reach this method in event order for their
// stream; the counter and gauge branches below are the reason that matters.
func (s *seriesState) apply(p *model.Point) {
	s.count++
	s.sum += p.Value
	if p.Value < s.min {
		s.min = p.Value
	}
	if p.Value > s.max {
		s.max = p.Value
	}

	switch p.Kind {
	case model.KindCounter:
		// Cumulative counters: the useful quantity is the increase, and the
		// increase is only computable from adjacent points in order. A restarted
		// producer resets its counter to zero, which shows up as a decrease; we
		// attribute the whole new value as the increase since the reset. Feed the
		// same points out of order and this silently produces garbage -- which is
		// precisely why the pipeline guarantees per-series ordering rather than
		// treating ordering as best effort.
		if s.prevSet {
			if p.Value >= s.prev {
				s.delta += p.Value - s.prev
			} else {
				s.delta += p.Value
				s.resets++
			}
		}
		s.prev, s.prevSet = p.Value, true

	case model.KindHistogram:
		// Commutative: order is irrelevant here, and the fold is mergeable across
		// shards and replicas.
		s.sketch.Add(p.Value)
	}

	// Last-write-wins by event time. The comparison is defensive: even with the
	// ordering guarantee, a declared sequence gap can let a stale point through,
	// and a gauge silently rolling backwards is a nasty class of bug.
	if p.EventTime.After(s.lastTS) || s.lastTS.IsZero() {
		s.last, s.lastTS = p.Value, p.EventTime
	} else if p.EventTime.Before(s.lastTS) {
		s.outOfOrder++
	}
}

// snapshot materializes the immutable Aggregate published downstream.
func (s *seriesState) snapshot(key string, start, end time.Time) model.Aggregate {
	a := model.Aggregate{
		SeriesKey:     key,
		Service:       s.service,
		Name:          s.name,
		Labels:        s.labels,
		Kind:          s.kind,
		WindowStart:   start,
		WindowEnd:     end,
		Count:         s.count,
		Sum:           s.sum,
		Min:           s.min,
		Max:           s.max,
		Last:          s.last,
		LastEventTime: s.lastTS,
		Delta:         s.delta,
		Resets:        s.resets,
		OutOfOrder:    s.outOfOrder,
		Late:          s.late,
		Gaps:          s.gaps,
		Partial:       s.partial,
	}
	if s.count == 0 {
		a.Min, a.Max = 0, 0
	}
	if s.kind == model.KindCounter {
		if d := end.Sub(start).Seconds(); d > 0 {
			a.Rate = s.delta / d
		}
	}
	if s.sketch != nil && s.sketch.Count() > 0 {
		a.P50 = s.sketch.Quantile(0.50)
		a.P90 = s.sketch.Quantile(0.90)
		a.P99 = s.sketch.Quantile(0.99)
	}
	return a
}

// windowState holds every series observed in one tumbling window on one shard.
type windowState struct {
	start  time.Time
	end    time.Time
	series map[string]*seriesState
}

func newWindowState(start time.Time, size time.Duration) *windowState {
	return &windowState{start: start, end: start.Add(size), series: make(map[string]*seriesState, 64)}
}

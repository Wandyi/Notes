// Package store persists closed windows and serves point-in-time queries.
//
// The interface is deliberately narrow. In production this is backed by a TSDB
// (Prometheus remote-write, Mimir, ClickHouse); the in-memory implementation here
// is what makes the service runnable and testable on its own, and it enforces the
// same retention and cardinality bounds a real backend would.
package store

import (
	"context"
	"errors"
	"sort"
	"sync"
	"time"

	"github.com/vaibhav/metricsprocessor/pkg/model"
)

// Query selects a slice of the aggregate space.
type Query struct {
	Service string
	Name    string
	Labels  map[string]string
	From    time.Time
	To      time.Time
	Limit   int
}

// Stats describes store occupancy, for capacity alerts.
type Stats struct {
	Series     int       `json:"series"`
	Aggregates int       `json:"aggregates"`
	Evicted    uint64    `json:"evicted"`
	Rejected   uint64    `json:"rejected_cardinality"`
	Oldest     time.Time `json:"oldest,omitzero"`
	Newest     time.Time `json:"newest,omitzero"`
}

// Store is the read/write contract used by the aggregator and the query API.
type Store interface {
	Put(ctx context.Context, aggs []model.Aggregate) error
	Query(ctx context.Context, q Query) ([]model.Aggregate, error)
	Stats() Stats
}

// ErrCardinalityExceeded is returned when a write would exceed the series cap.
// It is a first-class error rather than a silent drop because unbounded series
// growth is the single most common way a metrics backend dies.
var ErrCardinalityExceeded = errors.New("store: series cardinality limit reached")

// Memory is a bounded in-memory store with time-based retention.
//
// Concurrency: one RWMutex. This is a *read-mostly* structure written once per
// window per series and read per query, which is nothing like the per-point rate
// on the fold path -- so a simple lock here is correct and cheap, and the
// lock-free machinery that the pipeline needs would be unjustified complexity.
type Memory struct {
	mu     sync.RWMutex
	series map[string]*seriesHistory

	retention  time.Duration
	maxSeries  int
	maxPerName int

	evicted  uint64
	rejected uint64
}

type seriesHistory struct {
	meta model.Aggregate // service/name/labels/kind, without the window fields
	// windows is kept sorted by WindowStart; appends are almost always at the
	// end, so insertion is O(1) amortized with an O(log n) fallback.
	windows []model.Aggregate
}

// MemoryOptions configures the in-memory store.
type MemoryOptions struct {
	Retention           time.Duration
	MaxSeries           int
	MaxWindowsPerSeries int
}

// NewMemory builds an in-memory store.
func NewMemory(o MemoryOptions) *Memory {
	if o.Retention <= 0 {
		o.Retention = time.Hour
	}
	if o.MaxSeries <= 0 {
		o.MaxSeries = 100_000
	}
	if o.MaxWindowsPerSeries <= 0 {
		o.MaxWindowsPerSeries = 720
	}
	return &Memory{
		series:     make(map[string]*seriesHistory, 1024),
		retention:  o.Retention,
		maxSeries:  o.MaxSeries,
		maxPerName: o.MaxWindowsPerSeries,
	}
}

// Put stores aggregates, merging any that repeat a (series, window) pair.
//
// Merging rather than overwriting is what makes the write path idempotent and
// restart-safe: a replica that comes back mid-window and re-emits a partial view
// of it combines with what is already there instead of clobbering it.
func (m *Memory) Put(_ context.Context, aggs []model.Aggregate) error {
	if len(aggs) == 0 {
		return nil
	}
	now := time.Now()
	m.mu.Lock()
	defer m.mu.Unlock()

	var rejected error
	for i := range aggs {
		a := aggs[i]
		h := m.series[a.SeriesKey]
		if h == nil {
			if len(m.series) >= m.maxSeries {
				m.rejected++
				rejected = ErrCardinalityExceeded
				continue
			}
			h = &seriesHistory{meta: a}
			m.series[a.SeriesKey] = h
		}
		h.insert(a)
		if over := len(h.windows) - m.maxPerName; over > 0 {
			h.windows = append(h.windows[:0], h.windows[over:]...)
			m.evicted += uint64(over)
		}
	}
	m.gcLocked(now)
	return rejected
}

func (h *seriesHistory) insert(a model.Aggregate) {
	n := len(h.windows)
	// Fast path: this window is newer than everything stored.
	if n == 0 || h.windows[n-1].WindowStart.Before(a.WindowStart) {
		h.windows = append(h.windows, a)
		return
	}
	i := sort.Search(n, func(i int) bool { return !h.windows[i].WindowStart.Before(a.WindowStart) })
	if i < n && h.windows[i].WindowStart.Equal(a.WindowStart) {
		h.windows[i].Merge(&a)
		return
	}
	h.windows = append(h.windows, model.Aggregate{})
	copy(h.windows[i+1:], h.windows[i:])
	h.windows[i] = a
}

// gcLocked drops windows past retention. It runs on the write path rather than on
// a background goroutine so that memory is reclaimed in proportion to load, with
// no timer to tune and no goroutine to leak.
func (m *Memory) gcLocked(now time.Time) {
	cutoff := now.Add(-m.retention)
	for key, h := range m.series {
		drop := 0
		for drop < len(h.windows) && h.windows[drop].WindowEnd.Before(cutoff) {
			drop++
		}
		if drop > 0 {
			h.windows = append(h.windows[:0], h.windows[drop:]...)
			m.evicted += uint64(drop)
		}
		if len(h.windows) == 0 {
			delete(m.series, key)
		}
	}
}

// Query returns matching aggregates sorted by (window start, series key).
func (m *Memory) Query(_ context.Context, q Query) ([]model.Aggregate, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()

	var out []model.Aggregate
	for _, h := range m.series {
		if q.Service != "" && h.meta.Service != q.Service {
			continue
		}
		if q.Name != "" && h.meta.Name != q.Name {
			continue
		}
		if !labelsMatch(h.meta.Labels, q.Labels) {
			continue
		}
		for _, a := range h.windows {
			if !q.From.IsZero() && a.WindowEnd.Before(q.From) {
				continue
			}
			if !q.To.IsZero() && !a.WindowStart.Before(q.To) {
				continue
			}
			out = append(out, a)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if !out[i].WindowStart.Equal(out[j].WindowStart) {
			return out[i].WindowStart.Before(out[j].WindowStart)
		}
		return out[i].SeriesKey < out[j].SeriesKey
	})
	if q.Limit > 0 && len(out) > q.Limit {
		out = out[:q.Limit]
	}
	return out, nil
}

func labelsMatch(have, want map[string]string) bool {
	for k, v := range want {
		if have[k] != v {
			return false
		}
	}
	return true
}

// Stats reports occupancy.
func (m *Memory) Stats() Stats {
	m.mu.RLock()
	defer m.mu.RUnlock()
	s := Stats{Series: len(m.series), Evicted: m.evicted, Rejected: m.rejected}
	for _, h := range m.series {
		s.Aggregates += len(h.windows)
		if len(h.windows) == 0 {
			continue
		}
		if first := h.windows[0].WindowStart; s.Oldest.IsZero() || first.Before(s.Oldest) {
			s.Oldest = first
		}
		if last := h.windows[len(h.windows)-1].WindowEnd; last.After(s.Newest) {
			s.Newest = last
		}
	}
	return s
}

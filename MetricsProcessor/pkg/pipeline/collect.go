package pipeline

import (
	"context"
	"errors"
	"sort"
	"time"

	"github.com/vaibhav/metricsprocessor/pkg/merr"
	"github.com/vaibhav/metricsprocessor/pkg/model"
)

// Snapshot is the outcome of a bounded aggregation run: the aggregates, and every
// partial failure encountered while producing them.
type Snapshot struct {
	Aggregates []model.Aggregate `json:"aggregates"`
	Err        *merr.MultiError  `json:"errors,omitempty"`
	Stats      Stats             `json:"stats"`
	Windows    int               `json:"windows"`
}

// Collect runs a finite set of points through a private pipeline and returns
// everything it produced.
//
// This is the synchronous face of the same engine that runs the streaming
// service, which matters for testing and for request/response callers (a
// backfill job, an API that aggregates an uploaded batch). It never returns
// aggregates *or* an error: it returns aggregates *and* whatever went wrong, so
// a caller that hit three bad points out of a million still gets the million.
//
// The overflow policy is forced to PolicyBlock here: a bounded batch has a known
// end, so waiting for shard capacity is strictly better than shedding.
func Collect(ctx context.Context, cfg Config, pts []*model.Point) (*Snapshot, error) {
	cfg.Overflow = PolicyBlock
	if cfg.SubmitTimeout < time.Second {
		cfg.SubmitTimeout = 5 * time.Second
	}
	p, err := New(cfg)
	if err != nil {
		return nil, err
	}

	// Drain concurrently with submission. Doing it after Close would deadlock as
	// soon as the batch produced more than ResultQueueSize windows.
	type collected struct {
		aggs []model.Aggregate
		errs merr.MultiError
		n    int
	}
	ch := make(chan collected, 1)
	go func() {
		var c collected
		for res := range p.Results() {
			c.n++
			c.aggs = append(c.aggs, res.Aggregates...)
			c.errs.Merge(res.Err)
		}
		ch <- c
	}()

	submitErr := p.SubmitBatch(ctx, pts)

	// Give the flush a bounded chance to complete even if the caller's context is
	// already done; otherwise a cancelled request would report zero aggregates
	// for work that was actually finished.
	closeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	closeErr := p.Close(closeCtx)

	c := <-ch

	snap := &Snapshot{Aggregates: MergeAggregates(c.aggs), Stats: p.Stats(), Windows: c.n}
	if submitErr != nil {
		var m *merr.MultiError
		if errors.As(submitErr, &m) {
			c.errs.Merge(m)
		} else {
			c.errs.Add(&merr.Error{Code: merr.CodeDownstream, Err: submitErr})
		}
	}
	if closeErr != nil {
		c.errs.Add(&merr.Error{Code: merr.CodeDownstream, Msg: "shutdown deadline exceeded", Err: closeErr})
	}
	if e := c.errs.ErrorOrNil(); e != nil {
		snap.Err = &c.errs
	}
	return snap, nil
}

// MergeAggregates folds aggregates that describe the same series and window and
// returns them sorted by (window start, series key).
//
// Within one pipeline a series only ever lands on one shard, so this is a no-op
// on that path. It earns its keep when unioning output from several aggregator
// replicas, or from a replica that restarted mid-window: the fold is mergeable,
// so two partial views of a window combine into one correct view.
func MergeAggregates(in []model.Aggregate) []model.Aggregate {
	if len(in) == 0 {
		return nil
	}
	type k struct {
		series string
		start  int64
	}
	idx := make(map[k]int, len(in))
	out := make([]model.Aggregate, 0, len(in))
	for i := range in {
		key := k{in[i].SeriesKey, in[i].WindowStart.UnixNano()}
		if at, ok := idx[key]; ok {
			out[at].Merge(&in[i])
			continue
		}
		idx[key] = len(out)
		out = append(out, in[i])
	}
	sort.Slice(out, func(i, j int) bool {
		if !out[i].WindowStart.Equal(out[j].WindowStart) {
			return out[i].WindowStart.Before(out[j].WindowStart)
		}
		return out[i].SeriesKey < out[j].SeriesKey
	})
	return out
}

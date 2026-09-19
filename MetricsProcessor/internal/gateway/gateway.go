// Package gateway implements the ingest service: the only component producers
// talk to.
//
// Its job is narrow on purpose -- validate, key, partition, publish -- because it
// is the tier that scales with the number of producers and must therefore be
// stateless, cheap, and independently deployable. All aggregation state lives one
// hop away, in the aggregator.
package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/vaibhav/metricsprocessor/internal/httpx"
	"github.com/vaibhav/metricsprocessor/pkg/bus"
	"github.com/vaibhav/metricsprocessor/pkg/merr"
	"github.com/vaibhav/metricsprocessor/pkg/model"
)

// Config configures the gateway.
type Config struct {
	// MaxBatchPoints rejects oversized batches before they are decoded into
	// memory. An unbounded request body is a denial-of-service surface.
	MaxBatchPoints int
	// MaxBodyBytes bounds the request body.
	MaxBodyBytes int64
	// PublishTimeout bounds how long a request waits for transport capacity.
	PublishTimeout time.Duration
	Logger         *slog.Logger
}

// DefaultConfig returns production-shaped defaults.
func DefaultConfig() Config {
	return Config{
		MaxBatchPoints: 10_000,
		MaxBodyBytes:   8 << 20,
		PublishTimeout: 2 * time.Second,
		Logger:         slog.Default(),
	}
}

// Gateway is the stateless ingest tier.
type Gateway struct {
	cfg Config
	bus bus.Bus
	log *slog.Logger

	received atomic.Uint64
	accepted atomic.Uint64
	rejected atomic.Uint64
	failed   atomic.Uint64
	batches  atomic.Uint64
}

// New builds a gateway over the given transport.
func New(cfg Config, b bus.Bus) *Gateway {
	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}
	if cfg.MaxBatchPoints <= 0 {
		cfg.MaxBatchPoints = 10_000
	}
	if cfg.MaxBodyBytes <= 0 {
		cfg.MaxBodyBytes = 8 << 20
	}
	if cfg.PublishTimeout <= 0 {
		cfg.PublishTimeout = 2 * time.Second
	}
	return &Gateway{cfg: cfg, bus: b, log: cfg.Logger.With("service", "ingest-gateway")}
}

// Response is what a producer gets back. It reports per-point outcomes rather
// than a single pass/fail, so a producer with three bad points out of a thousand
// learns exactly which contract it broke while the other nine hundred and
// ninety seven are aggregated.
type Response struct {
	Accepted int           `json:"accepted"`
	Rejected int           `json:"rejected"`
	Errors   []*merr.Error `json:"errors,omitempty"`
}

// Routes returns the gateway's HTTP surface.
func (g *Gateway) Routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/metrics", g.handleIngest)
	mux.HandleFunc("GET /v1/stats", g.handleStats)
	httpx.Health(mux, func() error {
		if g.bus == nil {
			return errors.New("no transport")
		}
		return nil
	})
	return mux
}

func (g *Gateway) handleIngest(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, g.cfg.MaxBodyBytes)

	var batch model.Batch
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(&batch); err != nil {
		httpx.WriteError(w, http.StatusBadRequest, err, "malformed batch")
		return
	}
	if len(batch.Points) > g.cfg.MaxBatchPoints {
		httpx.WriteError(w, http.StatusRequestEntityTooLarge,
			errors.New("batch too large"), "reduce points per request")
		return
	}
	g.batches.Add(1)
	g.received.Add(uint64(len(batch.Points)))

	now := time.Now()
	resp := Response{}

	// Group by series key while preserving arrival order within each key. The
	// grouping is what lets one HTTP request become a handful of transport
	// operations instead of one per point, and preserving order within a key is
	// what keeps the end-to-end ordering guarantee intact across the batching.
	groups := make(map[string][]*model.Point, len(batch.Points))
	order := make([]string, 0, len(batch.Points))

	for _, p := range batch.Points {
		if p == nil {
			continue
		}
		if p.Source == "" {
			p.Source = batch.Source
		}
		p.IngestTime = now
		if err := p.Validate(); err != nil {
			resp.Rejected++
			g.rejected.Add(1)
			if len(resp.Errors) < 32 { // bound the response body
				resp.Errors = append(resp.Errors, &merr.Error{
					Code: merr.CodeValidation, Source: p.Source, Err: err, At: now,
				})
			}
			continue
		}
		key := p.SeriesKey()
		if _, seen := groups[key]; !seen {
			order = append(order, key)
		}
		groups[key] = append(groups[key], p)
	}

	ctx, cancel := context.WithTimeout(r.Context(), g.cfg.PublishTimeout)
	defer cancel()

	for _, key := range order {
		pts := groups[key]
		if err := g.bus.Publish(ctx, key, pts); err != nil {
			// Transport refused. Tell the producer to retry rather than
			// pretending we took the data: at-least-once only works if the
			// producer knows when we did not get it.
			g.failed.Add(uint64(len(pts)))
			g.log.Warn("publish failed", "key", key, "points", len(pts), "error", err)
			httpx.WriteJSON(w, http.StatusServiceUnavailable, Response{
				Accepted: resp.Accepted,
				Rejected: resp.Rejected,
				Errors: append(resp.Errors, &merr.Error{
					Code: merr.CodeDownstream, Msg: "transport unavailable; retry this batch", Err: err, At: now,
				}),
			})
			return
		}
		resp.Accepted += len(pts)
		g.accepted.Add(uint64(len(pts)))
	}

	switch {
	case resp.Accepted == 0 && resp.Rejected > 0:
		httpx.WriteJSON(w, http.StatusBadRequest, resp)
	case resp.Rejected > 0:
		// Partial success is its own outcome and deserves its own status code.
		httpx.WriteJSON(w, http.StatusMultiStatus, resp)
	default:
		httpx.WriteJSON(w, http.StatusAccepted, resp)
	}
}

func (g *Gateway) handleStats(w http.ResponseWriter, _ *http.Request) {
	stats := map[string]any{
		"batches":  g.batches.Load(),
		"received": g.received.Load(),
		"accepted": g.accepted.Load(),
		"rejected": g.rejected.Load(),
		"failed":   g.failed.Load(),
	}
	if s, ok := g.bus.(interface{ Stats() map[string]uint64 }); ok {
		stats["transport"] = s.Stats()
	}
	httpx.WriteJSON(w, http.StatusOK, stats)
}

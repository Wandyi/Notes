// Package aggregator wires the transport, the aggregation pipeline, and the
// store into one stateful service.
//
// This is the only tier that holds in-flight aggregation state, which is why it
// is separated from ingest: it scales with *series cardinality* rather than with
// producer count, and it cannot be restarted as casually as a stateless tier.
package aggregator

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/vaibhav/metricsprocessor/internal/httpx"
	"github.com/vaibhav/metricsprocessor/pkg/bus"
	"github.com/vaibhav/metricsprocessor/pkg/merr"
	"github.com/vaibhav/metricsprocessor/pkg/model"
	"github.com/vaibhav/metricsprocessor/pkg/pipeline"
	"github.com/vaibhav/metricsprocessor/pkg/store"
)

// Config configures the aggregator service.
type Config struct {
	Pipeline pipeline.Config
	// Group is the consumer group name on the transport.
	Group string
	// RecentErrors is how many recent partial failures to keep for the
	// /v1/errors endpoint. Operators need a sample, not a firehose.
	RecentErrors int
	Logger       *slog.Logger
}

// DefaultConfig returns production-shaped defaults.
func DefaultConfig() Config {
	return Config{
		Pipeline:     pipeline.DefaultConfig(),
		Group:        "aggregator",
		RecentErrors: 200,
		Logger:       slog.Default(),
	}
}

// Service is a running aggregator.
type Service struct {
	cfg   Config
	pipe  *pipeline.Pipeline
	store store.Store
	bus   bus.Bus
	log   *slog.Logger

	consumerWG sync.WaitGroup
	ready      atomic.Bool

	mu     sync.Mutex
	recent []*merr.Error // ring of recent partial failures

	stored     atomic.Uint64
	storeFails atomic.Uint64
	windows    atomic.Uint64
}

// New builds the service. Call Run to start it.
func New(cfg Config, b bus.Bus, st store.Store) (*Service, error) {
	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}
	if cfg.Group == "" {
		cfg.Group = "aggregator"
	}
	if cfg.RecentErrors <= 0 {
		cfg.RecentErrors = 200
	}
	p, err := pipeline.New(cfg.Pipeline)
	if err != nil {
		return nil, err
	}
	return &Service{
		cfg:   cfg,
		pipe:  p,
		store: st,
		bus:   b,
		log:   cfg.Logger.With("service", "aggregator"),
	}, nil
}

// Run consumes results until ctx is cancelled, subscribing to the transport
// first if it is a pull-style one. It returns once the pipeline has drained,
// flushed, and persisted everything.
//
// With a push transport (the brokerless HTTP publisher) there is nothing to
// subscribe to: envelopes arrive on POST /v1/ingest instead. Both paths converge
// on Ingest, so the service behaves identically either way.
func (s *Service) Run(ctx context.Context) error {
	// The result consumer must be running before the first window can close;
	// otherwise the shards would block on delivery.
	s.consumerWG.Add(1)
	go s.consumeResults()

	if s.bus != nil {
		if err := s.bus.Subscribe(ctx, s.cfg.Group, s.Ingest); err != nil {
			return err
		}
	}
	s.ready.Store(true)

	<-ctx.Done()
	s.ready.Store(false)
	return s.drain()
}

// BeginDrain fails readiness without stopping ingest. Callers invoke it the
// moment SIGTERM arrives so the load balancer removes this replica while it is
// still able to finish the requests it already accepted.
func (s *Service) BeginDrain() {
	s.ready.Store(false)
	s.log.Info("draining: readiness withdrawn")
}

// Ingest feeds one transport envelope into the pipeline. It is the single entry
// point for data, whether it arrived by subscription or by HTTP.
//
// Returning an error asks for redelivery. We only do that for conditions that are
// plausibly transient (backpressure). Data that will never be valid is dropped
// here, because redelivering it forever would block the partition and take down
// every healthy series behind it.
func (s *Service) Ingest(ctx context.Context, env bus.Envelope) error {
	var retryable bool
	var last error

	for _, p := range env.Points {
		// The gateway already computed this key to choose the partition; reusing
		// it keeps the aggregator's shard assignment identical to the
		// transport's partition assignment.
		p.SetSeriesKey(env.Key)
		if err := s.pipe.Submit(ctx, p); err != nil {
			last = err
			var e *merr.Error
			if errors.As(err, &e) {
				s.recordError(e)
				if e.Code == merr.CodeBackpressure {
					retryable = true
				}
			}
		}
	}
	if retryable && env.Attempt < 3 {
		return last
	}
	if last != nil {
		s.log.Debug("envelope had rejected points", "key", env.Key, "error", last)
	}
	return nil
}

// consumeResults persists closed windows.
func (s *Service) consumeResults() {
	defer s.consumerWG.Done()
	for res := range s.pipe.Results() {
		s.windows.Add(1)

		if err := s.store.Put(context.Background(), res.Aggregates); err != nil {
			// A store failure must not stop the fold. We count it, surface it,
			// and keep consuming -- a stalled consumer would backpressure into
			// the shards and then into ingest.
			s.storeFails.Add(1)
			s.recordError(&merr.Error{
				Code: merr.CodeDownstream, Shard: res.Shard,
				Msg: "store write failed", Err: err, At: time.Now(),
			})
			s.log.Error("store write failed", "shard", res.Shard, "error", err)
		} else {
			s.stored.Add(uint64(len(res.Aggregates)))
		}

		if res.Err != nil {
			for _, e := range res.Err.Errors {
				s.recordError(e)
			}
			s.log.Warn("window closed with partial failures",
				"shard", res.Shard,
				"window_start", res.WindowStart.Format(time.RFC3339),
				"reason", string(res.Reason),
				"aggregates", len(res.Aggregates),
				"failures", res.Err.Total(),
				"summary", res.Err.Error())
		}
	}
}

// drain shuts the pipeline down and waits for every flushed window to be stored.
func (s *Service) drain() error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	err := s.pipe.Close(ctx) // closes Results once shards have flushed
	s.consumerWG.Wait()      // ...which is what lets this return
	s.log.Info("drained", "windows", s.windows.Load(), "aggregates_stored", s.stored.Load())
	return err
}

func (s *Service) recordError(e *merr.Error) {
	if e == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.recent) == s.cfg.RecentErrors {
		copy(s.recent, s.recent[1:])
		s.recent[len(s.recent)-1] = e
		return
	}
	s.recent = append(s.recent, e)
}

// ---------------------------------------------------------------------------
// HTTP surface
// ---------------------------------------------------------------------------

// Routes returns the aggregator's HTTP surface. /v1/query exists so the query
// tier can scatter-gather across replicas; the rest is operational.
func (s *Service) Routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/ingest", s.handleIngest)
	mux.HandleFunc("GET /v1/query", s.handleQuery)
	mux.HandleFunc("GET /v1/stats", s.handleStats)
	mux.HandleFunc("GET /v1/errors", s.handleErrors)
	mux.HandleFunc("GET /v1/quarantine", s.handleQuarantine)
	httpx.Health(mux, func() error {
		if !s.ready.Load() {
			return errors.New("not subscribed")
		}
		return nil
	})
	return mux
}

// QueryResponse is the aggregator's read shape, and also what the query tier
// merges across replicas.
type QueryResponse struct {
	Aggregates []model.Aggregate `json:"aggregates"`
	Count      int               `json:"count"`
}

// handleIngest is the push-transport receive endpoint. It returns 503 rather than
// 400 when the pipeline sheds, so the publisher retries instead of discarding.
func (s *Service) handleIngest(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 8<<20)
	var env bus.Envelope
	if err := json.NewDecoder(r.Body).Decode(&env); err != nil {
		httpx.WriteError(w, http.StatusBadRequest, err, "malformed envelope")
		return
	}
	// No readiness gate here on purpose. Readiness tells the load balancer to
	// stop sending; a request that already arrived should still be accepted if
	// the pipeline can take it. Rejecting during the drain window would turn a
	// clean rolling deploy into a burst of retries.
	if err := s.Ingest(r.Context(), env); err != nil {
		httpx.WriteError(w, http.StatusServiceUnavailable, err, "retry this envelope")
		return
	}
	w.WriteHeader(http.StatusAccepted)
}

func (s *Service) handleQuery(w http.ResponseWriter, r *http.Request) {
	q, err := parseQuery(r)
	if err != nil {
		httpx.WriteError(w, http.StatusBadRequest, err, "invalid query")
		return
	}
	aggs, err := s.store.Query(r.Context(), q)
	if err != nil {
		httpx.WriteError(w, http.StatusInternalServerError, err, "query failed")
		return
	}
	httpx.WriteJSON(w, http.StatusOK, QueryResponse{Aggregates: aggs, Count: len(aggs)})
}

func parseQuery(r *http.Request) (store.Query, error) {
	v := r.URL.Query()
	from, err := httpx.ParseTime(v.Get("from"))
	if err != nil {
		return store.Query{}, err
	}
	to, err := httpx.ParseTime(v.Get("to"))
	if err != nil {
		return store.Query{}, err
	}
	q := store.Query{Service: v.Get("service"), Name: v.Get("name"), From: from, To: to}
	if l := v.Get("limit"); l != "" {
		n, err := strconv.Atoi(l)
		if err != nil {
			return store.Query{}, errors.New("limit must be an integer")
		}
		q.Limit = n
	}
	// Repeated ?label=k:v pairs keep the query string readable and avoid a
	// second encoding layer.
	for _, lv := range v["label"] {
		k, val, ok := strings.Cut(lv, ":")
		if !ok {
			return store.Query{}, errors.New("label must be key:value, got " + lv)
		}
		if q.Labels == nil {
			q.Labels = map[string]string{}
		}
		q.Labels[k] = val
	}
	return q, nil
}

func (s *Service) handleStats(w http.ResponseWriter, _ *http.Request) {
	out := map[string]any{
		"pipeline":          s.pipe.Stats(),
		"store":             s.store.Stats(),
		"windows_consumed":  s.windows.Load(),
		"aggregates_stored": s.stored.Load(),
		"store_failures":    s.storeFails.Load(),
	}
	if bs, ok := s.bus.(interface{ Stats() map[string]uint64 }); ok {
		out["transport"] = bs.Stats()
	}
	httpx.WriteJSON(w, http.StatusOK, out)
}

func (s *Service) handleErrors(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	out := append([]*merr.Error(nil), s.recent...)
	s.mu.Unlock()
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"errors": out, "count": len(out)})
}

func (s *Service) handleQuarantine(w http.ResponseWriter, _ *http.Request) {
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"quarantined": s.pipe.Quarantined()})
}

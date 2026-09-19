// Package query implements the public read tier.
//
// Aggregation state is sharded across aggregator replicas, so no single replica
// can answer a query on its own. This service scatters the query to every
// replica and gathers the answers. That works only because aggregates are
// *mergeable* -- the same property that lets a window be folded independently in
// several places -- which is a design decision made back in the data model, not
// a convenience discovered here.
//
// Availability posture: a query that reaches nine of ten replicas returns nine
// tenths of the data, clearly marked as degraded. Failing the whole request
// because one replica is rolling would make the read tier less available than
// the thing it reads from.
package query

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"sync"
	"time"

	"github.com/vaibhav/metricsprocessor/internal/aggregator"
	"github.com/vaibhav/metricsprocessor/internal/httpx"
	"github.com/vaibhav/metricsprocessor/pkg/model"
	"github.com/vaibhav/metricsprocessor/pkg/pipeline"
)

// Config configures the query tier.
type Config struct {
	// Upstreams are aggregator base URLs, e.g. http://aggregator-0:8082.
	Upstreams []string
	// FanoutTimeout bounds each upstream call. It is deliberately short: a slow
	// replica should cost freshness, not the whole request.
	FanoutTimeout time.Duration
	// MinReplicas is how many upstreams must answer for the response to count as
	// complete. Below it the response is still returned, but marked degraded.
	MinReplicas int
	Logger      *slog.Logger
}

// DefaultConfig returns production-shaped defaults.
func DefaultConfig() Config {
	return Config{FanoutTimeout: 2 * time.Second, Logger: slog.Default()}
}

// Service is the read facade.
type Service struct {
	cfg    Config
	client *http.Client
	log    *slog.Logger
}

// New builds the query service.
func New(cfg Config) *Service {
	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}
	if cfg.FanoutTimeout <= 0 {
		cfg.FanoutTimeout = 2 * time.Second
	}
	if cfg.MinReplicas <= 0 {
		cfg.MinReplicas = 1
	}
	return &Service{
		cfg: cfg,
		client: &http.Client{
			Timeout: cfg.FanoutTimeout,
			Transport: &http.Transport{
				MaxIdleConnsPerHost: 32,
				IdleConnTimeout:     90 * time.Second,
			},
		},
		log: cfg.Logger.With("service", "query-api"),
	}
}

// Response is the public read shape.
type Response struct {
	Aggregates []model.Aggregate `json:"aggregates"`
	Count      int               `json:"count"`
	// Replicas reports how many upstreams answered out of how many were asked,
	// so a caller can tell "no data" apart from "we could not see all of it".
	Replicas struct {
		Asked    int `json:"asked"`
		Answered int `json:"answered"`
	} `json:"replicas"`
	Degraded bool     `json:"degraded"`
	Errors   []string `json:"errors,omitempty"`
}

// Routes returns the query tier's HTTP surface.
func (s *Service) Routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/query", s.handleQuery)
	httpx.Health(mux, func() error {
		if len(s.cfg.Upstreams) == 0 {
			return errors.New("no upstreams configured")
		}
		return nil
	})
	return mux
}

func (s *Service) handleQuery(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), s.cfg.FanoutTimeout)
	defer cancel()

	type outcome struct {
		aggs []model.Aggregate
		err  error
	}
	results := make([]outcome, len(s.cfg.Upstreams))

	var wg sync.WaitGroup
	wg.Add(len(s.cfg.Upstreams))
	for i, base := range s.cfg.Upstreams {
		go func(i int, base string) {
			defer wg.Done()
			aggs, err := s.fetch(ctx, base, r.URL.RawQuery)
			results[i] = outcome{aggs, err}
		}(i, base)
	}
	wg.Wait()

	var resp Response
	var all []model.Aggregate
	for i, o := range results {
		if o.err != nil {
			resp.Errors = append(resp.Errors, fmt.Sprintf("%s: %v", s.cfg.Upstreams[i], o.err))
			s.log.Warn("upstream query failed", "upstream", s.cfg.Upstreams[i], "error", o.err)
			continue
		}
		resp.Replicas.Answered++
		all = append(all, o.aggs...)
	}
	resp.Replicas.Asked = len(s.cfg.Upstreams)

	// Merge is what makes scatter-gather correct: if a series was folded by two
	// replicas (a rebalance, a restart mid-window), their partial views combine
	// into the same answer a single replica would have produced.
	resp.Aggregates = pipeline.MergeAggregates(all)
	resp.Count = len(resp.Aggregates)
	resp.Degraded = resp.Replicas.Answered < resp.Replicas.Asked

	status := http.StatusOK
	if resp.Replicas.Answered == 0 {
		status = http.StatusServiceUnavailable
	} else if resp.Replicas.Answered < s.cfg.MinReplicas {
		// Answered something, but less than the configured completeness floor.
		status = http.StatusPartialContent
	}
	httpx.WriteJSON(w, status, resp)
}

func (s *Service) fetch(ctx context.Context, base, rawQuery string) ([]model.Aggregate, error) {
	u, err := url.Parse(base)
	if err != nil {
		return nil, err
	}
	u.Path = "/v1/query"
	u.RawQuery = rawQuery

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("upstream returned %s", resp.Status)
	}
	var body aggregator.QueryResponse
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	return body.Aggregates, nil
}

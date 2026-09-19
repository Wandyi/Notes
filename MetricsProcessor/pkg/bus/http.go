package bus

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/vaibhav/metricsprocessor/pkg/model"
	"github.com/vaibhav/metricsprocessor/pkg/pipeline"
)

// HTTPPublisher is a brokerless transport: it routes envelopes straight to the
// aggregator replica that owns the partition.
//
// It exists so the three-service split is runnable without standing up Kafka,
// and it is honest about what it gives up. There is no durable log, so an
// aggregator restart loses whatever was in flight, and there is no replay. What
// it *does* preserve is the property the design actually depends on: a given
// series key always reaches the same replica. Concurrent requests for one key can
// still arrive out of order, and that is precisely the reordering the aggregator's
// sequence-based reorder buffer is there to repair.
//
// Use this for development, small deployments, and as the reference for what a
// Bus implementation must guarantee. Use Kafka or JetStream when you need
// durability, replay, or consumer-group rebalancing.
type HTTPPublisher struct {
	endpoints  []string
	partitions int
	client     *http.Client
	maxRetries int
	log        *slog.Logger

	published atomic.Uint64
	retried   atomic.Uint64
	failed    atomic.Uint64
}

// HTTPOptions configures the publisher.
type HTTPOptions struct {
	// Endpoints are aggregator base URLs. Order matters and must be identical on
	// every gateway replica: it is half of the partition-to-owner mapping.
	Endpoints []string
	// Partitions is the ordering-domain count. Keeping it larger than the replica
	// count means replicas can be added later by re-mapping partitions rather
	// than by rehashing every series.
	Partitions int
	MaxRetries int
	Timeout    time.Duration
	Logger     *slog.Logger
}

// NewHTTPPublisher builds a brokerless publisher.
func NewHTTPPublisher(o HTTPOptions) (*HTTPPublisher, error) {
	if len(o.Endpoints) == 0 {
		return nil, errors.New("bus: at least one aggregator endpoint is required")
	}
	if o.Partitions < len(o.Endpoints) {
		o.Partitions = len(o.Endpoints)
	}
	if o.MaxRetries <= 0 {
		o.MaxRetries = 2
	}
	if o.Timeout <= 0 {
		o.Timeout = 3 * time.Second
	}
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	return &HTTPPublisher{
		endpoints:  o.Endpoints,
		partitions: o.Partitions,
		maxRetries: o.MaxRetries,
		log:        o.Logger,
		client: &http.Client{
			Timeout: o.Timeout,
			Transport: &http.Transport{
				MaxIdleConnsPerHost: 64,
				IdleConnTimeout:     90 * time.Second,
			},
		},
	}, nil
}

func (h *HTTPPublisher) Partitions() int { return h.partitions }

// Publish sends the envelope to the replica owning key's partition.
func (h *HTTPPublisher) Publish(ctx context.Context, key string, pts []*model.Point) error {
	if len(pts) == 0 {
		return nil
	}
	part := pipeline.PartitionFor(key, h.partitions)
	target := h.endpoints[part%len(h.endpoints)]

	body, err := json.Marshal(Envelope{Key: key, Points: pts})
	if err != nil {
		return err
	}

	var lastErr error
	for attempt := 0; attempt <= h.maxRetries; attempt++ {
		if attempt > 0 {
			h.retried.Add(1)
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt) * 50 * time.Millisecond):
			}
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, target+"/v1/ingest", bytes.NewReader(body))
		if err != nil {
			return err
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := h.client.Do(req)
		if err != nil {
			lastErr = err
			continue
		}
		io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
		resp.Body.Close()

		if resp.StatusCode < 300 {
			h.published.Add(1)
			return nil
		}
		if resp.StatusCode >= 500 || resp.StatusCode == http.StatusTooManyRequests {
			lastErr = fmt.Errorf("aggregator %s returned %s", target, resp.Status)
			continue
		}
		// 4xx: the aggregator will reject this envelope every time.
		h.failed.Add(1)
		return fmt.Errorf("aggregator %s rejected envelope: %s", target, resp.Status)
	}
	h.failed.Add(1)
	return lastErr
}

// Subscribe is not supported: consumers of this transport receive over HTTP.
func (h *HTTPPublisher) Subscribe(context.Context, string, Handler) error {
	return errors.New("bus: HTTPPublisher is publish-only; the aggregator receives on POST /v1/ingest")
}

// Close releases idle connections.
func (h *HTTPPublisher) Close() error {
	h.client.CloseIdleConnections()
	return nil
}

// Stats reports publisher health.
func (h *HTTPPublisher) Stats() map[string]uint64 {
	return map[string]uint64{
		"published": h.published.Load(),
		"retried":   h.retried.Load(),
		"failed":    h.failed.Load(),
	}
}

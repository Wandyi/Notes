// Package client is the producer-side SDK that microservices embed to emit
// metrics.
//
// It exists because two of the system's guarantees are only half implementable
// in the server: sequence numbering and batching both have to happen at the point
// of emission. Handing producers a library rather than a wire format is what
// keeps the ordering contract from being re-derived, subtly differently, in every
// service that reports metrics.
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/vaibhav/metricsprocessor/pkg/model"
)

// Options configures a Client.
type Options struct {
	// Endpoint is the ingest gateway base URL.
	Endpoint string
	// Service is the logical name of the emitting service.
	Service string
	// Source identifies this instance (pod name, hostname). Ordering is defined
	// per source, so two replicas must never share one.
	Source string

	// MaxBatch flushes early once this many points are buffered.
	MaxBatch int
	// FlushInterval bounds how long a point waits in the buffer. This is the
	// producer's contribution to end-to-end latency and is usually the largest
	// single term in it.
	FlushInterval time.Duration
	// MaxRetries on transport failure. Retries preserve order because the
	// buffer is re-sent whole, in order, before anything newer.
	MaxRetries int
	// MaxQueued bounds memory when the gateway is unreachable. Past it the
	// client drops *oldest* first: for live telemetry, fresh data is worth more
	// than a backlog nobody will look at.
	MaxQueued int

	HTTPClient *http.Client
	Logger     *slog.Logger
}

// Client buffers, sequences, and ships metrics to the ingest gateway.
type Client struct {
	opt Options
	log *slog.Logger
	hc  *http.Client

	mu   sync.Mutex
	buf  []*model.Point
	seqs map[string]uint64 // stream key -> next sequence

	dropped uint64
	sent    uint64
	failed  uint64

	// flushNow lets a full buffer jump the flush interval. It has capacity 1 and
	// is signalled non-blockingly, so a hot producer never waits on it.
	flushNow chan struct{}
	stop     chan struct{}
	closed   sync.Once
	wg       sync.WaitGroup
}

// New starts a client with a background flusher.
func New(o Options) (*Client, error) {
	if o.Endpoint == "" {
		return nil, errors.New("client: Endpoint is required")
	}
	if o.Service == "" || o.Source == "" {
		return nil, errors.New("client: Service and Source are required")
	}
	if o.MaxBatch <= 0 {
		o.MaxBatch = 500
	}
	if o.FlushInterval <= 0 {
		o.FlushInterval = 200 * time.Millisecond
	}
	if o.MaxRetries < 0 {
		o.MaxRetries = 0
	}
	if o.MaxQueued <= 0 {
		o.MaxQueued = 50_000
	}
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	if o.HTTPClient == nil {
		o.HTTPClient = &http.Client{Timeout: 5 * time.Second}
	}
	c := &Client{
		opt:      o,
		log:      o.Logger.With("component", "metrics-client", "source", o.Source),
		hc:       o.HTTPClient,
		buf:      make([]*model.Point, 0, o.MaxBatch),
		seqs:     make(map[string]uint64, 128),
		flushNow: make(chan struct{}, 1),
		stop:     make(chan struct{}),
	}
	c.wg.Add(1)
	go c.loop()
	return c, nil
}

// Count records a cumulative counter observation.
func (c *Client) Count(name string, value float64, labels map[string]string) {
	c.record(name, model.KindCounter, value, labels)
}

// Gauge records an instantaneous value.
func (c *Client) Gauge(name string, value float64, labels map[string]string) {
	c.record(name, model.KindGauge, value, labels)
}

// Observe records a value for distribution analysis (latency, sizes).
func (c *Client) Observe(name string, value float64, labels map[string]string) {
	c.record(name, model.KindHistogram, value, labels)
}

func (c *Client) record(name string, kind model.Kind, value float64, labels map[string]string) {
	p := &model.Point{
		Service: c.opt.Service, Name: name, Kind: kind, Value: value,
		Labels: labels, EventTime: time.Now(), Source: c.opt.Source,
	}

	c.mu.Lock()
	// Sequence at emission time and under the same lock that orders the buffer.
	// If sequencing happened at flush time, two goroutines racing into the buffer
	// could be numbered in an order that does not match the order they observed
	// reality in -- and the whole point of the sequence is to encode that order.
	key := p.StreamKey()
	c.seqs[key]++
	p.Seq = c.seqs[key]

	c.buf = append(c.buf, p)
	if len(c.buf) > c.opt.MaxQueued {
		drop := len(c.buf) - c.opt.MaxQueued
		c.buf = append(c.buf[:0], c.buf[drop:]...)
		c.dropped += uint64(drop)
	}
	full := len(c.buf) >= c.opt.MaxBatch
	c.mu.Unlock()

	if full {
		select {
		case c.flushNow <- struct{}{}:
		default: // a flush is already pending; nothing to do
		}
	}
}

func (c *Client) loop() {
	defer c.wg.Done()
	t := time.NewTicker(c.opt.FlushInterval)
	defer t.Stop()
	for {
		select {
		case <-t.C:
			c.flush(context.Background())
		case <-c.flushNow:
			c.flush(context.Background())
		case <-c.stop:
			return
		}
	}
}

// Flush ships whatever is buffered.
func (c *Client) Flush(ctx context.Context) error { return c.flush(ctx) }

func (c *Client) flush(ctx context.Context) error {
	c.mu.Lock()
	if len(c.buf) == 0 {
		c.mu.Unlock()
		return nil
	}
	batch := c.buf
	c.buf = make([]*model.Point, 0, c.opt.MaxBatch)
	c.mu.Unlock()

	err := c.send(ctx, batch)
	if err == nil {
		c.mu.Lock()
		c.sent += uint64(len(batch))
		c.mu.Unlock()
		return nil
	}

	// Put the batch back at the *front* so that order is preserved on retry.
	// Appending it to the tail would ship newer points before older ones and
	// hand the aggregator a reordering that no sequence number can repair,
	// because the sequence numbers would still be correct -- just delivered in an
	// order that maximizes reorder-buffer stalls.
	c.mu.Lock()
	c.failed += uint64(len(batch))
	c.buf = append(batch, c.buf...)
	if len(c.buf) > c.opt.MaxQueued {
		drop := len(c.buf) - c.opt.MaxQueued
		c.buf = append(c.buf[:0], c.buf[drop:]...)
		c.dropped += uint64(drop)
	}
	c.mu.Unlock()
	return err
}

func (c *Client) send(ctx context.Context, pts []*model.Point) error {
	body, err := json.Marshal(model.Batch{Source: c.opt.Source, Points: pts})
	if err != nil {
		return err
	}
	var lastErr error
	for attempt := 0; attempt <= c.opt.MaxRetries; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt) * 100 * time.Millisecond):
			}
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost,
			c.opt.Endpoint+"/v1/metrics", bytes.NewReader(body))
		if err != nil {
			return err
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := c.hc.Do(req)
		if err != nil {
			lastErr = err
			continue
		}
		io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
		resp.Body.Close()

		switch {
		case resp.StatusCode < 300:
			return nil
		case resp.StatusCode >= 500 || resp.StatusCode == http.StatusTooManyRequests:
			lastErr = fmt.Errorf("gateway returned %s", resp.Status)
		default:
			// 4xx other than 429 will not become valid by being retried.
			return fmt.Errorf("gateway rejected batch: %s", resp.Status)
		}
	}
	return lastErr
}

// Close flushes and stops the background loop.
func (c *Client) Close(ctx context.Context) error {
	var err error
	c.closed.Do(func() {
		close(c.stop)
		c.wg.Wait()
		err = c.flush(ctx)
	})
	return err
}

// Stats reports client-side counters. Producers should export these: a client
// silently dropping points is invisible from the server side, which only ever
// sees what did arrive.
func (c *Client) Stats() map[string]uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return map[string]uint64{
		"sent": c.sent, "failed": c.failed, "dropped": c.dropped,
		"buffered": uint64(len(c.buf)),
	}
}

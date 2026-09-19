// Command aggregator runs the stateful aggregation tier.
package main

import (
	"context"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/vaibhav/metricsprocessor/internal/aggregator"
	"github.com/vaibhav/metricsprocessor/internal/config"
	"github.com/vaibhav/metricsprocessor/internal/httpx"
	"github.com/vaibhav/metricsprocessor/pkg/pipeline"
	"github.com/vaibhav/metricsprocessor/pkg/store"
)

func main() {
	log := config.Logger("aggregator")

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	pcfg := pipeline.DefaultConfig()
	pcfg.Shards = config.Int("SHARDS", pcfg.Shards)
	pcfg.WindowSize = config.Duration("WINDOW_SIZE", pcfg.WindowSize)
	pcfg.AllowedLateness = config.Duration("ALLOWED_LATENESS", pcfg.AllowedLateness)
	pcfg.ReorderDepth = config.Int("REORDER_DEPTH", pcfg.ReorderDepth)
	pcfg.MaxReorderDelay = config.Duration("MAX_REORDER_DELAY", pcfg.MaxReorderDelay)
	pcfg.ShardQueueSize = config.Int("SHARD_QUEUE_SIZE", pcfg.ShardQueueSize)
	pcfg.ResultQueueSize = config.Int("RESULT_QUEUE_SIZE", pcfg.ResultQueueSize)
	pcfg.MaxFutureSkew = config.Duration("MAX_FUTURE_SKEW", 5*time.Minute)
	pcfg.QuarantineErrorRate = config.Float("QUARANTINE_ERROR_RATE", pcfg.QuarantineErrorRate)
	if config.String("OVERFLOW_POLICY", "drop_newest") == "block" {
		pcfg.Overflow = pipeline.PolicyBlock
	}

	st := store.NewMemory(store.MemoryOptions{
		Retention:           config.Duration("RETENTION", time.Hour),
		MaxSeries:           config.Int("MAX_SERIES", 100_000),
		MaxWindowsPerSeries: config.Int("MAX_WINDOWS_PER_SERIES", 720),
	})

	acfg := aggregator.DefaultConfig()
	acfg.Pipeline = pcfg
	acfg.Logger = log
	acfg.Group = config.String("CONSUMER_GROUP", "aggregator")

	// Push transport: envelopes arrive on POST /v1/ingest, so there is no bus to
	// subscribe to. Pass a bus here instead to run in pull mode.
	svc, err := aggregator.New(acfg, nil, st)
	if err != nil {
		log.Error("aggregator init", "error", err)
		os.Exit(2)
	}

	// The pipeline outlives the HTTP server by design. Shutdown runs strictly in
	// this order:
	//
	//   SIGTERM -> readiness fails (load balancer stops sending)
	//           -> HTTP server drains in-flight requests
	//           -> pipeline drains queues and flushes open windows
	//
	// Draining the pipeline first would flush every window and then keep
	// accepting points that nothing would ever flush.
	pipeCtx, stopPipeline := context.WithCancel(context.Background())
	defer stopPipeline()

	runDone := make(chan error, 1)
	go func() { runDone <- svc.Run(pipeCtx) }()

	go func() {
		<-ctx.Done()
		svc.BeginDrain()
	}()

	srv := &http.Server{
		Addr:              config.String("LISTEN_ADDR", ":8082"),
		Handler:           httpx.Recover(log, httpx.LogRequests(log, svc.Routes())),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	if err := httpx.Serve(ctx, srv, log, config.Duration("SHUTDOWN_GRACE", 15*time.Second)); err != nil {
		log.Error("server exited", "error", err)
	}
	stopPipeline()
	if err := <-runDone; err != nil {
		log.Error("pipeline drain", "error", err)
		os.Exit(1)
	}
	log.Info("stopped")
}

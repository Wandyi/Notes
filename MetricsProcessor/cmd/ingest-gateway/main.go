// Command ingest-gateway runs the stateless ingest tier.
package main

import (
	"context"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/vaibhav/metricsprocessor/internal/config"
	"github.com/vaibhav/metricsprocessor/internal/gateway"
	"github.com/vaibhav/metricsprocessor/internal/httpx"
	"github.com/vaibhav/metricsprocessor/pkg/bus"
)

func main() {
	log := config.Logger("ingest-gateway")

	// Signal-driven shutdown: SIGTERM is what Kubernetes sends before it removes
	// a pod, and honouring it is the difference between a clean rolling deploy
	// and a burst of dropped batches on every release.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	// The gateway is stateless and owns no aggregation, so all it needs is a
	// transport that routes a series key to a fixed owner.
	endpoints := config.List("AGGREGATOR_ENDPOINTS", nil)
	if len(endpoints) == 0 {
		log.Error("AGGREGATOR_ENDPOINTS is required (comma-separated aggregator base URLs)")
		os.Exit(2)
	}
	b, err := bus.NewHTTPPublisher(bus.HTTPOptions{
		Endpoints:  endpoints,
		Partitions: config.Int("BUS_PARTITIONS", 64),
		MaxRetries: config.Int("PUBLISH_RETRIES", 2),
		Timeout:    config.Duration("PUBLISH_TIMEOUT", 3*time.Second),
		Logger:     log,
	})
	if err != nil {
		log.Error("transport", "error", err)
		os.Exit(2)
	}
	defer b.Close()
	log.Info("transport ready", "endpoints", endpoints, "partitions", b.Partitions())

	gcfg := gateway.DefaultConfig()
	gcfg.Logger = log
	gcfg.MaxBatchPoints = config.Int("MAX_BATCH_POINTS", gcfg.MaxBatchPoints)
	gcfg.PublishTimeout = config.Duration("PUBLISH_TIMEOUT", gcfg.PublishTimeout)

	g := gateway.New(gcfg, b)

	srv := &http.Server{
		Addr:              config.String("LISTEN_ADDR", ":8081"),
		Handler:           httpx.Recover(log, httpx.LogRequests(log, g.Routes())),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	if err := httpx.Serve(ctx, srv, log, config.Duration("SHUTDOWN_GRACE", 15*time.Second)); err != nil {
		log.Error("server exited", "error", err)
		os.Exit(1)
	}
	log.Info("stopped")
}

// Command query-api runs the public read tier.
package main

import (
	"context"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/vaibhav/metricsprocessor/internal/config"
	"github.com/vaibhav/metricsprocessor/internal/httpx"
	"github.com/vaibhav/metricsprocessor/internal/query"
)

func main() {
	log := config.Logger("query-api")

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	upstreams := config.List("AGGREGATOR_ENDPOINTS", nil)
	if len(upstreams) == 0 {
		log.Error("AGGREGATOR_ENDPOINTS is required (comma-separated aggregator base URLs)")
		os.Exit(2)
	}

	qcfg := query.DefaultConfig()
	qcfg.Logger = log
	qcfg.Upstreams = upstreams
	qcfg.FanoutTimeout = config.Duration("FANOUT_TIMEOUT", qcfg.FanoutTimeout)
	qcfg.MinReplicas = config.Int("MIN_REPLICAS", len(upstreams))

	svc := query.New(qcfg)

	srv := &http.Server{
		Addr:              config.String("LISTEN_ADDR", ":8083"),
		Handler:           httpx.Recover(log, httpx.LogRequests(log, svc.Routes())),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	log.Info("read tier configured", "upstreams", upstreams, "min_replicas", qcfg.MinReplicas)
	if err := httpx.Serve(ctx, srv, log, config.Duration("SHUTDOWN_GRACE", 10*time.Second)); err != nil {
		log.Error("server exited", "error", err)
		os.Exit(1)
	}
	log.Info("stopped")
}

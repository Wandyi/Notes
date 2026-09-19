// Command demo runs all three services in one process over the in-process bus,
// drives them with synthetic traffic, and injects the exact failure modes the
// design claims to handle: out-of-order delivery, duplicate replay, invalid
// points, a counter reset, and a poisoned producer.
//
// It exists so the guarantees can be *observed* rather than asserted. Run:
//
//	go run ./cmd/demo
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"os"
	"sort"
	"time"

	"github.com/vaibhav/metricsprocessor/internal/aggregator"
	"github.com/vaibhav/metricsprocessor/internal/config"
	"github.com/vaibhav/metricsprocessor/internal/gateway"
	"github.com/vaibhav/metricsprocessor/internal/httpx"
	"github.com/vaibhav/metricsprocessor/internal/query"
	"github.com/vaibhav/metricsprocessor/pkg/bus"
	"github.com/vaibhav/metricsprocessor/pkg/client"
	"github.com/vaibhav/metricsprocessor/pkg/model"
	"github.com/vaibhav/metricsprocessor/pkg/pipeline"
	"github.com/vaibhav/metricsprocessor/pkg/store"
)

const (
	windowSize = 2 * time.Second
	runFor     = 8 * time.Second
)

func main() {
	os.Setenv("LOG_FORMAT", "text")
	os.Setenv("LOG_LEVEL", config.String("LOG_LEVEL", "warn"))
	log := config.Logger("demo")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// ---- transport -------------------------------------------------------
	b := bus.NewInProc(bus.InProcOptions{Partitions: 16, BufferSize: 4096, Logger: log})

	// ---- aggregator ------------------------------------------------------
	pcfg := pipeline.DefaultConfig()
	pcfg.Shards = 4
	pcfg.WindowSize = windowSize
	pcfg.AllowedLateness = 500 * time.Millisecond
	pcfg.ReorderDepth = 32
	pcfg.MaxReorderDelay = 150 * time.Millisecond
	pcfg.MaxFutureSkew = time.Minute
	pcfg.QuarantineErrorRate = 0.6
	pcfg.QuarantineMinSamples = 50
	pcfg.QuarantineCooldown = 10 * time.Second
	// A transform that rejects one specific producer's data, standing in for the
	// enrichment step that would fail on malformed input in a real deployment.
	pcfg.Transform = func(p *model.Point) error {
		if p.Source == "poison-pod" {
			return fmt.Errorf("unparseable payload from %s", p.Source)
		}
		return nil
	}

	st := store.NewMemory(store.MemoryOptions{Retention: time.Hour})
	acfg := aggregator.DefaultConfig()
	acfg.Pipeline = pcfg
	acfg.Logger = log

	agg, err := aggregator.New(acfg, b, st)
	if err != nil {
		fatal(err)
	}
	aggSrv := httptest.NewServer(agg.Routes())
	defer aggSrv.Close()

	runDone := make(chan error, 1)
	go func() { runDone <- agg.Run(ctx) }()

	// ---- gateway ---------------------------------------------------------
	gw := gateway.New(gateway.Config{Logger: log, MaxBatchPoints: 10000,
		MaxBodyBytes: 8 << 20, PublishTimeout: 2 * time.Second}, b)
	gwSrv := httptest.NewServer(httpx.Recover(log, gw.Routes()))
	defer gwSrv.Close()

	// ---- query tier ------------------------------------------------------
	qsvc := query.New(query.Config{Upstreams: []string{aggSrv.URL}, Logger: log, MinReplicas: 1})
	qSrv := httptest.NewServer(qsvc.Routes())
	defer qSrv.Close()

	section("topology")
	fmt.Printf("  ingest-gateway  %s\n  aggregator      %s\n  query-api       %s\n",
		gwSrv.URL, aggSrv.URL, qSrv.URL)
	fmt.Printf("  window %s, allowed lateness %s, %d shards, %d partitions\n",
		windowSize, pcfg.AllowedLateness, pcfg.Shards, b.Partitions())

	// ---- healthy traffic -------------------------------------------------
	section("driving healthy traffic")
	start := time.Now()
	stopLoad := make(chan struct{})
	loadDone := make(chan int, 3)
	for i := 0; i < 3; i++ {
		go produce(gwSrv.URL, fmt.Sprintf("pod-%d", i), stopLoad, loadDone)
	}

	// ---- injected failures ----------------------------------------------
	time.Sleep(runFor / 2)
	section("injecting failures")
	injectScrambledStream(gwSrv.URL)
	injectInvalidPoints(gwSrv.URL)
	injectLateArrival(gwSrv.URL)
	injectPoisonedProducer(gwSrv.URL)

	time.Sleep(runFor / 2)
	close(stopLoad)
	var produced int
	for i := 0; i < 3; i++ {
		produced += <-loadDone
	}
	fmt.Printf("  producers emitted %d points over %s\n", produced, time.Since(start).Round(time.Millisecond))

	// Let the last windows close, then shut the pipeline down so open windows
	// are flushed rather than lost.
	time.Sleep(2 * windowSize)
	cancel()
	if err := <-runDone; err != nil {
		fmt.Println("  drain:", err)
	}

	// ---- results ---------------------------------------------------------
	report(qSrv.URL, aggSrv.URL)
}

// produce drives steady traffic through the producer SDK.
func produce(endpoint, source string, stop <-chan struct{}, done chan<- int) {
	c, err := client.New(client.Options{
		Endpoint: endpoint, Service: "checkout", Source: source,
		MaxBatch: 200, FlushInterval: 100 * time.Millisecond, MaxRetries: 2,
		Logger: slog.Default(),
	})
	if err != nil {
		fatal(err)
	}
	routes := []string{"/pay", "/cart", "/session"}
	n := 0
	var total float64
	t := time.NewTicker(2 * time.Millisecond)
	defer t.Stop()
	for {
		select {
		case <-stop:
			c.Close(context.Background())
			done <- n
			return
		case <-t.C:
			route := routes[rand.Intn(len(routes))]
			// The pod label is not decoration. A cumulative counter is only
			// interpretable per emitting instance: without it, three replicas'
			// independently-rising totals collapse into one series and every
			// interleaving looks like a counter reset.
			labels := map[string]string{"route": route, "pod": source}
			total++
			c.Count("http_requests_total", total, labels)
			c.Observe("http_latency_ms", 5+rand.Float64()*95, labels)
			c.Gauge("inflight_requests", float64(rand.Intn(50)), labels)
			n += 3
		}
	}
}

// injectScrambledStream posts one series' points badly out of order, with a
// duplicate and a counter reset, and correct sequence numbers throughout. The
// aggregator must reconstruct the true delta anyway.
func injectScrambledStream(endpoint string) {
	now := time.Now()
	mk := func(seq uint64, v float64, off time.Duration) *model.Point {
		return &model.Point{
			Service: "billing", Name: "invoices_total", Kind: model.KindCounter,
			Value: v, EventTime: now.Add(off), Source: "billing-pod", Seq: seq,
		}
	}
	// True order: 100, 220, 40 (reset), 90 -> delta = 120 + 40 + 50 = 210.
	batch := model.Batch{Source: "billing-pod", Points: []*model.Point{
		mk(3, 40, 200*time.Millisecond),
		mk(1, 100, 0),
		mk(4, 90, 300*time.Millisecond),
		mk(2, 220, 100*time.Millisecond),
		mk(2, 220, 100*time.Millisecond), // at-least-once replay
	}}
	post(endpoint, batch, "  scrambled stream (seq 3,1,4,2,2-replayed) -> ")
}

func injectInvalidPoints(endpoint string) {
	now := time.Now()
	batch := model.Batch{Source: "sloppy-pod", Points: []*model.Point{
		{Service: "billing", Name: "ok_metric", Kind: model.KindGauge, Value: 1, EventTime: now, Source: "sloppy-pod"},
		{Service: "", Name: "missing_service", Kind: model.KindGauge, EventTime: now, Source: "sloppy-pod"},
		{Service: "billing", Name: "", Kind: model.KindGauge, EventTime: now, Source: "sloppy-pod"},
		{Service: "billing", Name: "no_timestamp", Kind: model.KindGauge, Source: "sloppy-pod"},
	}}
	post(endpoint, batch, "  batch with 3 invalid of 4 points     -> ")
}

// injectLateArrival sends a point whose window closed long ago. It cannot be
// folded without retracting a published aggregate, so it is counted and reported
// instead of being silently discarded.
func injectLateArrival(endpoint string) {
	now := time.Now()
	batch := model.Batch{Source: "slow-pod", Points: []*model.Point{
		{Service: "billing", Name: "stragglers", Kind: model.KindGauge, Value: 1,
			EventTime: now, Source: "slow-pod"},
		{Service: "billing", Name: "stragglers", Kind: model.KindGauge, Value: 99,
			EventTime: now.Add(-45 * time.Second), Source: "slow-pod"},
	}}
	post(endpoint, batch, "  point 45s behind its closed window  -> ")
}

// injectPoisonedProducer emits enough failing points to trip the error budget,
// after which the source is shed and stops costing the pipeline anything.
func injectPoisonedProducer(endpoint string) {
	now := time.Now()
	var pts []*model.Point
	for i := 0; i < 300; i++ {
		pts = append(pts, &model.Point{
			Service: "fraud", Name: "scores", Kind: model.KindGauge, Value: float64(i),
			EventTime: now.Add(time.Duration(i) * time.Millisecond), Source: "poison-pod",
		})
	}
	post(endpoint, model.Batch{Source: "poison-pod", Points: pts},
		"  300 points from a poisoned producer -> ")
}

func post(endpoint string, batch model.Batch, prefix string) {
	body, _ := json.Marshal(batch)
	resp, err := http.Post(endpoint+"/v1/metrics", "application/json", bytes.NewReader(body))
	if err != nil {
		fmt.Println(prefix, "error:", err)
		return
	}
	defer resp.Body.Close()
	var r gateway.Response
	json.NewDecoder(resp.Body).Decode(&r)
	fmt.Printf("%sHTTP %d, accepted=%d rejected=%d\n", prefix, resp.StatusCode, r.Accepted, r.Rejected)
	for _, e := range r.Errors {
		fmt.Printf("      %s\n", e.Error())
	}
}

func report(queryURL, aggURL string) {
	section("aggregates (via query-api scatter-gather)")
	var qr query.Response
	getJSON(queryURL+"/v1/query?limit=500", &qr)

	// Roll the per-window aggregates up per series for a readable summary.
	type row struct {
		name             string
		count            uint64
		delta, last, p99 float64
		resets           uint32
		gaps, late, ooo  uint64
		partial          bool
		kind             model.Kind
	}
	rows := map[string]*row{}
	for _, a := range qr.Aggregates {
		id := a.Service + "/" + a.Name
		r := rows[id]
		if r == nil {
			r = &row{name: id, kind: a.Kind}
			rows[id] = r
		}
		r.count += a.Count
		r.delta += a.Delta
		r.resets += a.Resets
		r.gaps += a.Gaps
		r.late += a.Late
		r.ooo += a.OutOfOrder
		r.partial = r.partial || a.Partial
		r.last = a.Last
		if a.P99 > r.p99 {
			r.p99 = a.P99
		}
	}
	ids := make([]string, 0, len(rows))
	for id := range rows {
		ids = append(ids, id)
	}
	sort.Strings(ids)

	fmt.Printf("  %-34s %-10s %8s %12s %10s %8s %6s\n",
		"SERIES", "KIND", "COUNT", "DELTA", "P99", "LAST", "FLAGS")
	for _, id := range ids {
		r := rows[id]
		flags := ""
		if r.partial {
			flags += "P"
		}
		if r.resets > 0 {
			flags += "R"
		}
		if r.gaps > 0 {
			flags += "G"
		}
		if r.late > 0 {
			flags += "L"
		}
		fmt.Printf("  %-34s %-10s %8d %12.1f %10.1f %8.1f %6s\n",
			id, r.kind, r.count, r.delta, r.p99, r.last, flags)
	}
	fmt.Printf("  windows merged from %d/%d replicas (degraded=%v)\n",
		qr.Replicas.Answered, qr.Replicas.Asked, qr.Degraded)

	// The scrambled billing stream is the headline claim: order was restored.
	if r := rows["billing/invoices_total"]; r != nil {
		fmt.Printf("\n  billing/invoices_total delta = %.0f (expected 210 with ordering restored;\n"+
			"    an unordered fold would report 350) resets=%d\n", r.delta, r.resets)
	}

	section("partial failures reported alongside the results")
	var er struct {
		Errors []json.RawMessage `json:"errors"`
		Count  int               `json:"count"`
	}
	getJSON(aggURL+"/v1/errors", &er)
	// One line per failure category: a flood of identical messages tells an
	// operator less than one example of each distinct thing that went wrong.
	seen := map[string]bool{}
	counts := map[string]int{}
	var order []string
	var samples []string
	for _, raw := range er.Errors {
		var e struct{ Code, Source, Message string }
		json.Unmarshal(raw, &e)
		counts[e.Code]++
		if !seen[e.Code] {
			seen[e.Code] = true
			order = append(order, e.Code)
			samples = append(samples, fmt.Sprintf("%-14s %s", e.Source, e.Message))
		}
	}
	fmt.Printf("  %d retained (bounded sample of all failures), by category:\n", er.Count)
	for i, code := range order {
		fmt.Printf("    %-13s x%-5d %s\n", code, counts[code], samples[i])
	}

	section("pipeline stats")
	var stats map[string]any
	getJSON(aggURL+"/v1/stats", &stats)
	printJSON(stats["pipeline"])
	fmt.Println("  store:")
	printJSON(stats["store"])

	var q map[string]any
	getJSON(aggURL+"/v1/quarantine", &q)
	fmt.Println("  quarantined producers:")
	printJSON(q["quarantined"])
}

// ---------------------------------------------------------------------------

func section(title string) { fmt.Printf("\n=== %s %s\n", title, dashes(66-len(title))) }

func dashes(n int) string {
	if n < 0 {
		n = 0
	}
	b := make([]byte, n)
	for i := range b {
		b[i] = '-'
	}
	return string(b)
}

func getJSON(url string, v any) {
	resp, err := http.Get(url)
	if err != nil {
		fmt.Println("  request failed:", err)
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if err := json.Unmarshal(body, v); err != nil {
		fmt.Println("  decode failed:", err)
	}
}

func printJSON(v any) {
	b, _ := json.MarshalIndent(v, "  ", "  ")
	fmt.Printf("  %s\n", b)
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "fatal:", err)
	os.Exit(1)
}

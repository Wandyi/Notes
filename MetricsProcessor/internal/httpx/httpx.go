// Package httpx holds the small amount of HTTP plumbing every service shares:
// JSON encoding, panic containment, request logging, and a uniform health
// contract. It is internal because it is glue, not API.
package httpx

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"runtime/debug"
	"time"
)

// WriteJSON renders v with the given status.
func WriteJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		slog.Default().Error("write response", "error", err)
	}
}

// ErrorBody is the uniform error shape across all three services.
type ErrorBody struct {
	Error  string `json:"error"`
	Detail string `json:"detail,omitempty"`
}

// WriteError renders an error response.
func WriteError(w http.ResponseWriter, status int, err error, detail string) {
	WriteJSON(w, status, ErrorBody{Error: err.Error(), Detail: detail})
}

// Recover contains a panic in one handler so that it cannot take down the
// process and every other in-flight request with it. Same principle as the
// pipeline's per-point isolation, applied at the request boundary.
func Recover(log *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				log.Error("handler panic",
					"path", r.URL.Path, "panic", rec, "stack", string(debug.Stack()))
				WriteError(w, http.StatusInternalServerError, errors.New("internal error"), "")
			}
		}()
		next.ServeHTTP(w, r)
	})
}

// statusRecorder captures the response code for logging.
type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

// LogRequests emits one structured line per request.
func LogRequests(log *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		// Health checks are high-rate and uninteresting; logging them buries the
		// lines that matter.
		if r.URL.Path == "/healthz" || r.URL.Path == "/readyz" {
			return
		}
		log.Info("request",
			"method", r.Method, "path", r.URL.Path, "status", rec.status,
			"duration_ms", float64(time.Since(start).Microseconds())/1000)
	})
}

// Health wires the two-probe contract Kubernetes expects: liveness answers "is
// this process wedged", readiness answers "should traffic come here". They are
// separate because a service that is briefly unable to serve (draining, upstream
// down) should be pulled from the load balancer, not restarted.
func Health(mux *http.ServeMux, ready func() error) {
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		WriteJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		if ready != nil {
			if err := ready(); err != nil {
				WriteError(w, http.StatusServiceUnavailable, err, "not ready")
				return
			}
		}
		WriteJSON(w, http.StatusOK, map[string]string{"status": "ready"})
	})
}

// Serve runs srv and shuts it down gracefully when ctx is cancelled.
func Serve(ctx context.Context, srv *http.Server, log *slog.Logger, grace time.Duration) error {
	errc := make(chan error, 1)
	go func() {
		log.Info("listening", "addr", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errc <- err
			return
		}
		errc <- nil
	}()
	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
		// Drain in-flight requests before exiting so a rolling deploy does not
		// return 502s for the requests that were already accepted.
		shutCtx, cancel := context.WithTimeout(context.Background(), grace)
		defer cancel()
		log.Info("shutting down", "grace", grace)
		return srv.Shutdown(shutCtx)
	}
}

// ParseTime accepts RFC3339 or a Unix seconds value, returning zero for "".
func ParseTime(s string) (time.Time, error) {
	if s == "" {
		return time.Time{}, nil
	}
	if t, err := time.Parse(time.RFC3339Nano, s); err == nil {
		return t, nil
	}
	var secs int64
	if err := json.Unmarshal([]byte(s), &secs); err == nil {
		return time.Unix(secs, 0).UTC(), nil
	}
	return time.Time{}, errors.New("expected RFC3339 or unix seconds, got " + s)
}

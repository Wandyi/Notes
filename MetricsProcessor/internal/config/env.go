// Package config reads service configuration from the environment.
//
// Environment variables rather than flags or files: the services are containers,
// and the twelve-factor shape means the same image runs in every environment with
// the deployment supplying the differences.
package config

import (
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"
)

// String returns the value of key or def.
func String(key, def string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return def
}

// Int returns the integer value of key or def.
func Int(key string, def int) int {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
		slog.Warn("ignoring unparseable env var", "key", key, "value", v)
	}
	return def
}

// Float returns the float value of key or def.
func Float(key string, def float64) float64 {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
		slog.Warn("ignoring unparseable env var", "key", key, "value", v)
	}
	return def
}

// Duration returns the duration value of key or def, e.g. "10s", "250ms".
func Duration(key string, def time.Duration) time.Duration {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
		slog.Warn("ignoring unparseable env var", "key", key, "value", v)
	}
	return def
}

// List returns a comma-separated list value of key or def.
func List(key string, def []string) []string {
	v, ok := os.LookupEnv(key)
	if !ok || v == "" {
		return def
	}
	parts := strings.Split(v, ",")
	out := parts[:0]
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// Logger builds the structured logger every service uses. JSON in production so
// the log pipeline can index fields; text locally so a human can read it.
func Logger(service string) *slog.Logger {
	level := slog.LevelInfo
	if err := level.UnmarshalText([]byte(String("LOG_LEVEL", "info"))); err != nil {
		level = slog.LevelInfo
	}
	var h slog.Handler
	opts := &slog.HandlerOptions{Level: level}
	if String("LOG_FORMAT", "json") == "text" {
		h = slog.NewTextHandler(os.Stdout, opts)
	} else {
		h = slog.NewJSONHandler(os.Stdout, opts)
	}
	l := slog.New(h).With("service", service)
	slog.SetDefault(l)
	return l
}

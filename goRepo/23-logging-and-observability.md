# Logging and Observability

Logging is the primary window into what a running Go service is actually doing, and in production it's often the *only* signal available when something breaks with no debugger attached. Sloppy logging — unstructured text, missing context, wrong severity, or leaked secrets — turns that window opaque exactly when you need it most, and can itself become a security incident or a performance problem. This doc covers the recurring logging and observability mistakes in Go services and the idiomatic fixes, mostly built around the standard library's `log/slog` package (Go 1.21+).

## 1. `fmt.Println` debugging left in production

**The Problem:** `fmt.Println`/`fmt.Printf` calls scattered through business logic have no severity, no structured fields, and go straight to stdout as plain text — they can't be filtered by level, can't be queried by field in a log aggregator (Datadog, Loki, CloudWatch), and are easy to forget and ship to production.

**❌ Bad**
```go
func ProcessOrder(orderID string, amount float64) error {
	fmt.Println("processing order:", orderID, amount)
	if amount <= 0 {
		fmt.Println("ERROR: invalid amount for order", orderID)
		return errors.New("invalid amount")
	}
	if err := chargeCard(orderID, amount); err != nil {
		fmt.Println("payment failed:", err)
		return err
	}
	fmt.Println("order processed successfully:", orderID)
	return nil
}
```

**Why it's wrong:**
- No log level: "ERROR:" is just a substring in stdout, not a queryable severity — you can't ship only errors to an on-call alert or filter debug noise out in prod.
- Output is unstructured text glued together with `:` and commas, so a log aggregator can't parse `orderID` or `amount` as fields to search or aggregate on.
- Easy to leave in accidentally after a debugging session, and impossible to redirect per-environment (JSON to a log shipper in prod, human-readable locally) without changing every call site.

**✅ Good**
```go
import "log/slog"

var logger = slog.New(slog.NewJSONHandler(os.Stdout, nil))

func ProcessOrder(orderID string, amount float64) error {
	logger.Info("processing order", "order_id", orderID, "amount", amount)
	if amount <= 0 {
		logger.Warn("invalid order amount", "order_id", orderID, "amount", amount)
		return errors.New("invalid amount")
	}
	if err := chargeCard(orderID, amount); err != nil {
		logger.Error("payment failed", "order_id", orderID, "err", err)
		return err
	}
	logger.Info("order processed", "order_id", orderID)
	return nil
}
```

**Why it works / Explanation:** `slog` emits structured key-value records (JSON here) with an explicit level on every call. A log pipeline can now filter to `level=error`, or query every line where `order_id="abc123"` across the whole fleet — impossible with free-form `fmt.Println` text. Swapping the handler (JSON vs text, stdout vs a network sink) is a one-line change at the logger's construction site, not a find-and-replace across the codebase.

**Design principle:** Structured logging — treat log output as machine-parseable data with a schema (level + message + fields), not human-oriented prose.

---

## 2. Logging sensitive data

**The Problem:** Logging middleware that dumps request headers or bodies for debugging convenience will, sooner or later, log an `Authorization` bearer token, a session cookie, or a password field — and logs are typically retained far longer, replicated more widely, and viewed by more people (support, on-call, third-party log vendors) than the primary datastore ever is.

**❌ Bad**
```go
func LoggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		r.Body = io.NopCloser(bytes.NewReader(body))
		// BUG: logs the Authorization header and the raw body verbatim —
		// which may contain passwords, card numbers, or session tokens
		log.Printf("request %s %s headers=%v body=%s", r.Method, r.URL.Path, r.Header, body)
		next.ServeHTTP(w, r)
	})
}
```

**Why it's wrong:**
- Bearer tokens and session cookies logged in plaintext become a credential leak the moment logs are exported to a SIEM, a third-party log vendor, or a developer's laptop for debugging.
- Request bodies for endpoints like `/login` or `/checkout` routinely contain passwords or full card numbers — logging them can violate PCI-DSS/GDPR and turn a routine debugging change into a compliance incident.
- "Just don't log that field" isn't enforceable once every header and the full body are dumped indiscriminately — the leak happens by default, not by exception.

**✅ Good**
```go
func LoggingMiddleware(logger *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		logger.Info("request received",
			"method", r.Method,
			"path", r.URL.Path,
			"remote_addr", r.RemoteAddr,
			"content_length", r.ContentLength,
			"api_key_suffix", redactSuffix(r.Header.Get("X-Api-Key")),
		)
		next.ServeHTTP(w, r)
	})
}

// redactSuffix keeps only the last 4 characters, enough to correlate a
// request to a specific key without exposing the secret itself.
func redactSuffix(s string) string {
	if len(s) <= 4 {
		return "****"
	}
	return "****" + s[len(s)-4:]
}
```

**Why it works / Explanation:** Instead of logging everything and hoping nothing sensitive slips through, the middleware explicitly allowlists the fields that are safe and useful (method, path, remote address, size), and any identifier that needs partial visibility goes through a redaction helper first. Adding a new header or body field to the log output is now a deliberate code change and code-review checkpoint, not an accidental side effect of `%v`.

**Design principle:** Allowlist over denylist for sensitive data — log exactly the fields you've reviewed as safe, rather than logging everything and trying to remember what to strip.

---

## 3. Excessive logging in hot loops

**The Problem:** Every logging call has real cost — formatting arguments, acquiring the writer's lock, doing I/O — and calling it once per item in a loop over millions of records turns logging itself into the bottleneck, while burying the one line that actually mattered under millions of routine ones.

**❌ Bad**
```go
func ImportRecords(logger *slog.Logger, records []Record) error {
	for _, r := range records {
		// BUG: one log line per record — for 5 million records this is
		// 5 million formatted writes, dwarfing the actual work of save()
		logger.Info("processing record", "id", r.ID, "payload", r)
		if err := save(r); err != nil {
			logger.Error("failed to save record", "id", r.ID, "err", err)
		}
	}
	return nil
}
```

**Why it's wrong:**
- The formatting and I/O cost of millions of log calls can dominate the wall-clock time of the import, making the loop far slower than the underlying `save()` work justifies.
- A single real failure becomes indistinguishable from millions of routine "processing record" lines — anyone grepping the log for the actual problem wades through noise that carries no signal.
- If the logger writes to a remote sink, this can also generate enough traffic to throttle or overload the logging backend itself.

**✅ Good**
```go
func ImportRecords(logger *slog.Logger, records []Record) error {
	start := time.Now()
	var failed int
	for i, r := range records {
		if err := save(r); err != nil {
			failed++
			if failed <= 10 { // sample the first few failures instead of logging every one
				logger.Error("failed to save record", "id", r.ID, "index", i, "err", err)
			}
		}
	}
	logger.Info("import complete",
		"total", len(records), "failed", failed, "duration", time.Since(start))
	return nil
}
```

**Why it works / Explanation:** The per-item log call is gone from the hot path entirely; the loop only logs when something actually goes wrong, and even then it caps how many failure lines it emits (sampling) so a systemic failure doesn't itself become a logging storm. A single summary line at the end gives an operator everything they need — volume, failure count, duration — without paying per-item logging cost.

**Design principle:** Log at the boundary, not inside the hot path — emit summaries or samples for high-frequency work, reserve per-event logging for genuinely low-frequency or anomalous events.

---

## 4. Inconsistent or missing log levels

**The Problem:** When every event — routine operations and genuine failures alike — gets logged at the same level (or the wrong one), on-call engineers either get paged for noise that isn't actionable, or a real production error sits logged at "debug" where it's filtered out in production and nobody sees it until a customer complains.

**❌ Bad**
```go
func HandleLogin(logger *slog.Logger, username string, err error) {
	if err != nil {
		// BUG: a real authentication failure logged at Debug — in production,
		// where the minimum level is usually Info or Warn, this is invisible
		logger.Debug("login failed", "user", username, "err", err)
		return
	}
	// BUG: a routine, expected event logged at Error — triggers alerting
	// rules and pages on-call for something that isn't a problem at all
	logger.Error("user logged in", "user", username)
}
```

**Why it's wrong:**
- The actual failure ("login failed") is filtered out entirely in production because `Debug` is typically disabled outside local development — the log line exists but is never written anywhere anyone can see it.
- The routine success case logged at `Error` fires alerting rules built around the error level, paging someone for every successful login — leading to alert fatigue and the alerting system being muted or ignored.
- Once levels stop meaning anything consistent, engineers can no longer trust the log level to decide what to look at first during an incident.

**✅ Good**
```go
func HandleLogin(logger *slog.Logger, username string, err error) {
	if err != nil {
		logger.Error("login failed", "user", username, "err", err)
		return
	}
	logger.Info("user logged in", "user", username)
}

// Debug: verbose, dev-only detail (e.g. raw request payloads)
// Info:  routine, expected events worth recording (logins, jobs started)
// Warn:  recoverable or unexpected but non-fatal conditions
// Error: something failed and needs attention
```

**Why it works / Explanation:** Levels now carry consistent meaning: `Error` means "this needs a human," `Info` means "routine, but worth having in the trail," and `Debug` is reserved for detail only useful with logging turned all the way up locally. Alerting rules built on `level=error` stay meaningful, and an incident review can trust that anything logged at `Error` is actually worth looking at.

**Design principle:** Level semantics as a contract — pick a small, consistent meaning for each level and apply it uniformly, so downstream tooling (alerts, dashboards, filters) can rely on it.

---

## 5. No correlation/trace IDs across a request's lifecycle

**The Problem:** A single incoming request often fans out across multiple goroutines (and sometimes multiple services); without a shared identifier stitching those log lines together, an engineer debugging one failing request in a busy production log stream has no way to tell which "charging payment" line belongs to which "checkout accepted" line.

**❌ Bad**
```go
func HandleCheckout(logger *slog.Logger, orderID string) {
	logger.Info("starting checkout", "order_id", orderID)
	go func() {
		// BUG: this runs concurrently with other requests' goroutines —
		// nothing in this log line ties it back to *this* checkout call
		logger.Info("charging payment")
	}()
	logger.Info("checkout accepted", "order_id", orderID)
}
```

**Why it's wrong:**
- Under load, hundreds of concurrent checkouts interleave their log lines; "charging payment" carries no identifier at all, so it can't be attributed to any specific request even though the other two lines have `order_id`.
- Reconstructing what happened for one failing request means manually correlating by timestamp proximity — unreliable and slow exactly when speed matters most.
- The problem compounds across service boundaries: if checkout calls a payments service over HTTP, there's no way to jump from one service's logs to the matching line in the other's.

**✅ Good**
```go
type ctxKey string

const requestIDKey ctxKey = "request_id"

func withRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, requestIDKey, id)
}

func loggerFromContext(ctx context.Context, base *slog.Logger) *slog.Logger {
	if id, ok := ctx.Value(requestIDKey).(string); ok {
		return base.With("request_id", id)
	}
	return base
}

func HandleCheckout(ctx context.Context, base *slog.Logger, orderID string) {
	ctx = withRequestID(ctx, newRequestID())
	logger := loggerFromContext(ctx, base)
	logger.Info("starting checkout", "order_id", orderID)

	go func(ctx context.Context) {
		loggerFromContext(ctx, base).Info("charging payment", "order_id", orderID)
	}(ctx)

	logger.Info("checkout accepted", "order_id", orderID)
}
```

**Why it works / Explanation:** The request ID is generated once at the entry point and carried through `context.Context` — the same mechanism used for cancellation and deadlines — so every goroutine spawned for that request can pull it back out and attach it to its own log lines via `.With(...)`. Every line for a given request now shares a `request_id` field, so a single query ("show me everything with `request_id=abc123`") reconstructs the entire request lifecycle in order, even across goroutines or services if the ID is also forwarded as a header downstream.

**Design principle:** Request-scoped context propagation — `context.Value` is the right tool for exactly this kind of cross-cutting, request-scoped metadata that every layer needs but shouldn't have to pass as an explicit parameter.

---

## 6. Swallowing errors instead of logging them with context

**The Problem:** Logging an error with no surrounding context — just the error string, with no indication of which operation, input, or entity it happened on — produces a log line that confirms *something* went wrong without giving anyone enough information to act on it.

**❌ Bad**
```go
func SaveUser(u User) error {
	if err := db.Insert(u); err != nil {
		log.Println(err) // BUG: "constraint violation" — on which user? which caller?
		return err
	}
	return nil
}
```

**Why it's wrong:**
- `log.Println(err)` might print something like `pq: duplicate key value violates unique constraint` with zero indication of which user, request, or code path triggered it — useless for anyone trying to reproduce or fix it.
- If `SaveUser` is called from ten different places, every failure produces an identical, indistinguishable log line, so the log can't tell you which caller is actually failing.
- If the returned error is also discarded further up the call stack, the *only* record of what happened is that one context-free line.

**✅ Good**
```go
func SaveUser(logger *slog.Logger, u User) error {
	if err := db.Insert(u); err != nil {
		logger.Error("failed to save user",
			"operation", "SaveUser",
			"user_id", u.ID,
			"email_domain", emailDomain(u.Email), // safe partial context, not the full address
			"err", err,
		)
		return fmt.Errorf("save user %s: %w", u.ID, err)
	}
	return nil
}
```

**Why it works / Explanation:** The log line now names the operation, the entity involved, and wraps the underlying error with `%w` so it can still be inspected programmatically with `errors.Is`/`errors.As` further up the stack. Two different `SaveUser` failures are now distinguishable by `user_id` alone, and the wrapped error preserves the original cause instead of discarding it.

**Design principle:** Contextual error logging — every logged error should answer "what operation, on what input, failed why," not just restate the error string.

---

## 7. Global logger singleton anti-pattern

**The Problem:** A package-level `var log = someLogger` reached for directly from every function works fine in a five-file CLI tool, but in a larger service it hard-codes a single global destination and configuration, makes it impossible to inject a test double, and can't carry request-scoped fields without resorting to more globals.

**❌ Bad**
```go
// BUG: package-level global, initialized once at package load time
var log = slog.New(slog.NewJSONHandler(os.Stdout, nil))

func ChargeCard(cardID string) error {
	log.Info("charging card", "card_id", cardID)
	// every function in this package is now permanently coupled to this
	// exact logger — no way to swap in a test logger, or add a per-request
	// field without mutating (and racing on) the shared global
	return chargeGateway(cardID)
}
```

**Why it's wrong:**
- Tests that want to assert "an error was logged," or want to silence logging entirely, have no seam to inject a test logger — they're stuck with whatever the global does.
- Attaching request-scoped fields (like a request ID) requires either mutating the shared global (a data race under concurrent requests) or maintaining a second, parallel mechanism just to work around it.
- Swapping the output destination per environment, or per test run, means editing package-level state rather than passing in a different logger at construction time.

**✅ Good**
```go
type PaymentService struct {
	logger *slog.Logger
	client *http.Client
}

func NewPaymentService(logger *slog.Logger, client *http.Client) *PaymentService {
	return &PaymentService{logger: logger, client: client}
}

func (s *PaymentService) ChargeCard(cardID string) error {
	s.logger.Info("charging card", "card_id", cardID)
	return s.chargeGateway(cardID)
}

func (s *PaymentService) chargeGateway(cardID string) error {
	// ... call out to the payment gateway using s.client
	return nil
}
```

**Why it works / Explanation:** The logger is a field, injected through the constructor like any other dependency. Production wiring passes a real JSON logger; tests pass a logger backed by an in-memory buffer (or a discarding handler) and can assert on its output; a request-scoped logger with extra fields (via `.With(...)`) can be created per-request and passed down without touching any shared state. None of this requires a second mechanism — it's the same dependency-injection pattern used for the HTTP client.

A package-level default logger is a reasonable exception for a genuinely simple, single-binary CLI tool with no test doubles and no per-request scoping to worry about — the anti-pattern bites specifically once a codebase has multiple call sites, unit tests that care about logging behavior, or per-request fields to attach.

**Design principle:** Dependency injection over globals — pass the logger in like any other collaborator, so it can be swapped, scoped, and tested.

---

## Key Takeaways
- Use structured logging (`log/slog` or similar) instead of `fmt.Println`, so output has levels and queryable fields.
- Never log secrets, tokens, or PII directly — allowlist safe fields and redact identifiers that need partial visibility.
- Don't log per-item inside hot loops — log summaries or sampled failures instead to avoid becoming the bottleneck and drowning signal in noise.
- Apply log levels consistently (debug/info/warn/error) so alerting and filtering stay meaningful.
- Propagate a request/trace ID through `context.Context` so every log line for a request can be correlated together.
- Log errors with operation and entity context, and wrap them (`%w`), instead of logging a bare error string.
- Inject loggers via constructors rather than reaching for a package-level global, except in genuinely simple CLI tools.

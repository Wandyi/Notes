# Design Patterns in Go

Design patterns exist to solve recurring structural problems, but ported literally from Java or C++ they tend to produce over-engineered Go that fights the language's conventions — interfaces should be small and implicit, construction should stay simple, and concurrency primitives like channels are often the most idiomatic way to express a pattern. This doc walks through the GoF-style patterns that actually show up in production Go code, each reframed the way experienced Go engineers actually write them, along with the naive alternative that causes real friction.

## 1. Functional options pattern

**The Problem:** As a constructor grows more optional settings, a plain parameter list either telescopes into an unreadable pile of positional arguments, or — since Go has no function/constructor overloading — forces multiple differently-named constructors just to cover common combinations.

**❌ Bad**
```go
type Server struct {
	addr     string
	timeout  time.Duration
	maxConns int
	tls      bool
	logger   *slog.Logger
}

// BUG: every new optional setting adds another positional parameter;
// callers must supply a value (often a zero value) for every option,
// whether they care about it or not
func NewServer(addr string, timeout time.Duration, maxConns int, tls bool, logger *slog.Logger) *Server {
	return &Server{addr: addr, timeout: timeout, maxConns: maxConns, tls: tls, logger: logger}
}

// call site is unreadable without checking the signature
srv := NewServer("0.0.0.0:8080", 30*time.Second, 100, false, nil)
```

**Why it's wrong:**
- Positional booleans and numbers (`false`, `100`) are meaningless at the call site without cross-referencing the signature, and it's easy to transpose two same-typed arguments without the compiler catching it.
- Adding one more optional setting means changing the signature — and every existing call site — even for callers who don't care about the new option.
- With no overloading, "give me a server with defaults except TLS on" has no clean expression; you either add a whole new constructor name or force every caller to specify everything.

**✅ Good**
```go
type Option func(*Server)

func WithTimeout(d time.Duration) Option { return func(s *Server) { s.timeout = d } }
func WithMaxConns(n int) Option          { return func(s *Server) { s.maxConns = n } }
func WithTLS() Option                    { return func(s *Server) { s.tls = true } }

func NewServer(addr string, opts ...Option) *Server {
	s := &Server{
		addr:     addr,
		timeout:  30 * time.Second, // sensible defaults baked in
		maxConns: 100,
		logger:   slog.Default(),
	}
	for _, opt := range opts {
		opt(s)
	}
	return s
}

// call site is self-documenting, and any subset of options can be omitted
srv := NewServer("0.0.0.0:8080", WithTimeout(5*time.Second), WithTLS())
```

**Why it works / Explanation:** `Option` is just a function that mutates the struct being built; `NewServer` applies sensible defaults first and then lets each supplied option override exactly what it cares about. New options can be added indefinitely without breaking any existing call site, since `opts ...Option` is variadic and every caller only names the options it actually wants. This is the same shape used throughout popular client libraries — `grpc.Dial(target, grpc.WithInsecure(), grpc.WithTimeout(...))` is the canonical example.

**Design principle:** Functional options — encode optional configuration as composable functions rather than parameters, keeping constructors stable as options grow.

---

## 2. Strategy pattern via interfaces

**The Problem:** Selecting behavior with a type/flag through a large `switch` or `if/else` chain works until a new case is needed — then every place that switches on the same flag has to be found and updated, and the dispatch logic stays tangled with the behavior for each case.

**❌ Bad**
```go
func CalculateDiscount(customerTier string, amount float64) float64 {
	switch customerTier {
	case "regular":
		return 0
	case "silver":
		return amount * 0.05
	case "gold":
		return amount * 0.10
	case "vip":
		return amount * 0.20
	default:
		// BUG: silently returns no discount for typos or new tiers
		// nobody remembered to add here
		return 0
	}
}
```

**Why it's wrong:**
- Adding a new customer tier means finding and editing this function (and any other function that happens to switch on the same string elsewhere) — the logic isn't open to extension, only to modification.
- A typo'd or new tier value silently falls through to `default` with no discount and no error, which is easy to miss in review and can quietly cost the business money.
- Unit testing "gold customers get 10% off" requires calling the whole function with a magic string, rather than testing the gold-tier rule in isolation.

**✅ Good**
```go
type DiscountStrategy interface {
	Discount(amount float64) float64
}

type RegularDiscount struct{}
func (RegularDiscount) Discount(amount float64) float64 { return 0 }

type GoldDiscount struct{}
func (GoldDiscount) Discount(amount float64) float64 { return amount * 0.10 }

type VIPDiscount struct{}
func (VIPDiscount) Discount(amount float64) float64 { return amount * 0.20 }

type Customer struct {
	Name     string
	Strategy DiscountStrategy
}

func (c Customer) FinalPrice(amount float64) float64 {
	return amount - c.Strategy.Discount(amount)
}

// the tier is resolved to a concrete strategy once, where the customer is loaded
customer := Customer{Name: "Ada", Strategy: GoldDiscount{}}
price := customer.FinalPrice(200.0)
```

**Why it works / Explanation:** Each tier's discount rule is now its own type implementing a one-method interface, so adding a new tier means adding a new type — no existing code changes. `Customer.FinalPrice` never needs to know how many tiers exist; it just calls the interface method. Each `Discount` implementation can be unit-tested completely independently of customer-loading logic.

**Design principle:** Strategy pattern via interfaces — replace conditional dispatch on a type/flag with polymorphism through a small interface, so new behavior is additive rather than a change to shared dispatch code.

---

## 3. Decorator pattern via middleware

**The Problem:** Cross-cutting concerns like logging, panic recovery, auth checks, and metrics don't belong inside business-logic handlers, but bolting them on by editing every handler directly duplicates the same boilerplate everywhere and tangles unrelated concerns together.

**❌ Bad**
```go
func GetOrderHandler(logger *slog.Logger, w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	defer func() {
		if err := recover(); err != nil {
			logger.Error("panic recovered", "err", err)
			w.WriteHeader(http.StatusInternalServerError)
		}
	}()
	logger.Info("request start", "path", r.URL.Path)
	// BUG: every handler in the codebase repeats this same timing,
	// recovery, and logging boilerplate around its actual business logic
	order := lookupOrder(r.URL.Query().Get("id"))
	json.NewEncoder(w).Encode(order)
	logger.Info("request done", "path", r.URL.Path, "duration", time.Since(start))
}
```

**Why it's wrong:**
- The same timing/recovery/logging boilerplate gets copy-pasted into every handler, and fixing a bug in that boilerplate means finding and editing every handler that has it.
- Business logic (`lookupOrder`) is buried in the middle of unrelated infrastructure code, making the handler harder to read and to unit test in isolation.
- Adding a new cross-cutting concern (say, auth) means touching every single handler function again.

**✅ Good**
```go
type Middleware func(http.Handler) http.Handler

func Logging(logger *slog.Logger) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			next.ServeHTTP(w, r)
			logger.Info("request", "path", r.URL.Path, "duration", time.Since(start))
		})
	}
}

func Recover(logger *slog.Logger) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			defer func() {
				if err := recover(); err != nil {
					logger.Error("panic recovered", "err", err)
					w.WriteHeader(http.StatusInternalServerError)
				}
			}()
			next.ServeHTTP(w, r)
		})
	}
}

func Chain(h http.Handler, mws ...Middleware) http.Handler {
	for i := len(mws) - 1; i >= 0; i-- {
		h = mws[i](h)
	}
	return h
}

// base handler stays focused purely on business logic
base := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
	json.NewEncoder(w).Encode(lookupOrder(r.URL.Query().Get("id")))
})
handler := Chain(base, Recover(logger), Logging(logger))
```

**Why it works / Explanation:** Each concern is its own `func(http.Handler) http.Handler` decorator that wraps the next handler in the chain, and `Chain` composes them in order without any of them knowing about each other. The base handler is left with only the business logic; adding a new concern means writing one new middleware function and adding it to the `Chain(...)` call, with zero changes to existing handlers.

**Design principle:** Decorator pattern via middleware chaining — layer behavior around a common function type instead of duplicating it inside every implementation.

---

## 4. Builder pattern for complex object construction

**The Problem:** Functional options work well for flat configuration, but when construction has ordered steps, validation that depends on what's already been set, or a natural "build up, then finalize" shape, a giant multi-argument constructor gets awkward — a fluent builder with a final validating `Build()` step fits better.

**❌ Bad**
```go
type Request struct {
	Method  string
	URL     string
	Headers map[string]string
	Body    []byte
	Timeout time.Duration
}

// BUG: five-plus parameters, no validation between them, and it's easy
// to pass values in the wrong order since several share the same type
func NewHTTPRequest(method, url string, headers map[string]string, body []byte, timeout time.Duration) (*Request, error) {
	if method == "" {
		return nil, errors.New("method is required")
	}
	return &Request{Method: method, URL: url, Headers: headers, Body: body, Timeout: timeout}, nil
}

req, err := NewHTTPRequest("POST", "https://api.example.com/orders", nil, payload, 5*time.Second)
```

**Why it's wrong:**
- All validation has to happen at the very end, in one pass, even for constraints that logically belong to a single field (e.g. "body must be under 1MB") — errors get reported far from where the bad value was supplied.
- Optional pieces (headers, body) still have to be passed as `nil` explicitly at every call site that doesn't need them.
- There's no natural place to build the request incrementally — e.g. adding headers one at a time — without constructing a `map[string]string` separately first.

**✅ Good**
```go
const maxBodySize = 1 << 20 // 1MB

type RequestBuilder struct {
	req *Request
	err error
}

func NewRequestBuilder(method, url string) *RequestBuilder {
	return &RequestBuilder{req: &Request{Method: method, URL: url, Headers: map[string]string{}}}
}

func (b *RequestBuilder) Header(key, value string) *RequestBuilder {
	if b.err == nil {
		b.req.Headers[key] = value
	}
	return b
}

func (b *RequestBuilder) Body(data []byte) *RequestBuilder {
	if b.err == nil {
		if len(data) > maxBodySize {
			b.err = fmt.Errorf("body exceeds max size of %d bytes", maxBodySize)
		} else {
			b.req.Body = data
		}
	}
	return b
}

func (b *RequestBuilder) Timeout(d time.Duration) *RequestBuilder {
	if b.err == nil {
		b.req.Timeout = d
	}
	return b
}

func (b *RequestBuilder) Build() (*Request, error) {
	if b.err != nil {
		return nil, b.err
	}
	if b.req.Timeout == 0 {
		b.req.Timeout = 30 * time.Second
	}
	return b.req, nil
}

req, err := NewRequestBuilder("POST", "https://api.example.com/orders").
	Header("Content-Type", "application/json").
	Body(payload).
	Timeout(5 * time.Second).
	Build()
```

**Why it works / Explanation:** Each step validates and mutates only what it's responsible for, and once `b.err` is set every subsequent chained call becomes a no-op, so `Build()` only has to check it once at the end and surface the first error encountered. The call site reads like a description of the request being assembled, and optional steps (`Header`, `Body`) can simply be omitted from the chain.

**Design principle:** Builder pattern — stage construction across chained method calls with a final validating `Build()`, useful when construction has order, incremental steps, or step-specific validation that functional options don't naturally express.

---

## 5. Singleton via `sync.Once`

**The Problem:** Some state genuinely needs exactly one instance shared process-wide (e.g. an expensive-to-construct config store loaded once from disk); Go's idiomatic way to build that lazily and safely under concurrent access is `sync.Once`, not a hand-rolled mutex-guarded nil check.

**❌ Bad**
```go
var (
	instance *ConfigStore
	mu       sync.Mutex
)

func GetConfigStore() *ConfigStore {
	mu.Lock()
	defer mu.Unlock()
	// BUG: every single call pays the full mutex lock/unlock cost,
	// forever, even though the store only ever needs to be built once
	if instance == nil {
		instance = &ConfigStore{values: loadFromDisk()}
	}
	return instance
}
```

**Why it's wrong:**
- Every call to `GetConfigStore` acquires the mutex, even years after the store was initialized once — unnecessary contention on a hot path if the getter is called frequently.
- It's easy to get subtly wrong under refactoring — e.g. someone "optimizes" by checking `instance == nil` before locking, reintroducing a classic double-checked-locking race.
- The pattern doesn't communicate "runs exactly once" as clearly to a reader as a dedicated primitive does.

**✅ Good**
```go
type ConfigStore struct {
	values map[string]string
}

var (
	instance *ConfigStore
	once     sync.Once
)

func GetConfigStore() *ConfigStore {
	once.Do(func() {
		instance = &ConfigStore{values: loadFromDisk()}
	})
	return instance
}
```

**Why it works / Explanation:** `sync.Once.Do` guarantees the initialization closure runs exactly once, even under concurrent first calls, and every call after that is a cheap atomic check with no lock contention. The intent — "build this lazily, exactly once" — is also immediately clear to a reader, unlike a hand-rolled mutex-guarded nil check.

That said, treat this as a tool for genuinely process-wide, stateless-to-share resources (like a compiled regex cache or a loaded config), not a default way to wire up dependencies. A global singleton is still a global: it can't be swapped for a test double, and every consumer becomes implicitly coupled to it rather than declaring the dependency explicitly (see the logging doc's critique of the global-logger anti-pattern — the same argument applies here). Prefer constructing the instance once in `main` and passing it down via dependency injection wherever that's practical, and reach for `sync.Once` deliberately when a true package-level singleton is unavoidable.

**Design principle:** Lazy singleton via `sync.Once` — the correct concurrency-safe idiom when a singleton is genuinely needed, applied deliberately rather than as a default construction strategy.

---

## 6. Dependency Injection via interfaces and constructor injection

**The Problem:** A handler that reaches directly for a package-level global (`var db *sql.DB`) hides its real dependencies from its own signature, making it impossible to substitute a fake in tests without mutating shared global state.

**❌ Bad**
```go
var db *sql.DB // BUG: hidden global dependency, set once somewhere in main()

func GetUser(id string) (*User, error) {
	row := db.QueryRow("SELECT name, email FROM users WHERE id = ?", id)
	var u User
	if err := row.Scan(&u.Name, &u.Email); err != nil {
		return nil, err
	}
	return &u, nil
}
```

**Why it's wrong:**
- Testing `GetUser` requires a real (or real-looking) `*sql.DB`, since the function never declares a dependency in its signature — nothing about `GetUser(id string)` hints that it needs a database at all.
- Tests that want different `db` behavior (e.g. simulate a not-found row) have to mutate the shared package-level `db` variable, which breaks under `t.Parallel()` and leaks state between tests.
- The function is permanently coupled to `database/sql` specifically — swapping to a different storage backend later means rewriting the function, not just its wiring.

**✅ Good**
```go
type Store interface {
	GetUser(id string) (*User, error)
}

type Handler struct {
	store Store
}

func NewHandler(store Store) *Handler {
	return &Handler{store: store}
}

func (h *Handler) GetUser(id string) (*User, error) {
	return h.store.GetUser(id)
}

// production wiring passes the real implementation
h := NewHandler(sqlStore)

// tests pass a fake with no database at all
type fakeStore struct{}
func (fakeStore) GetUser(id string) (*User, error) { return &User{Name: "Test User"}, nil }
testHandler := NewHandler(fakeStore{})
```

**Why it works / Explanation:** `Handler` declares exactly what it needs — something satisfying `Store` — through its constructor, and Go's structural typing means any type with a matching `GetUser` method works, including a zero-dependency fake used only in tests. Swapping the real storage backend means writing a new type that satisfies `Store`; nothing about `Handler` changes.

**Design principle:** Dependency injection via interfaces — declare dependencies explicitly as constructor parameters typed as narrow interfaces, so they can be substituted in tests and swapped in production without touching the consumer.

---

## 7. Observer/pub-sub pattern via channels

**The Problem:** Direct method calls from a publisher to every interested subscriber's concrete type couples the publisher to each subscriber's implementation and makes adding a new subscriber a change to the publisher's code; an in-process event bus decouples "something happened" from "here's exactly who reacts to it."

**❌ Bad**
```go
type OrderService struct {
	emailer   *EmailNotifier
	invoicer  *InvoiceGenerator
	analytics *AnalyticsRecorder
}

func (s *OrderService) CreateOrder(o Order) {
	saveOrder(o)
	// BUG: the publisher must know about, and directly call, every
	// current and future interested party by concrete type
	s.emailer.SendConfirmation(o)
	s.invoicer.Generate(o)
	s.analytics.RecordOrderCreated(o)
}
```

**Why it's wrong:**
- `OrderService` has to import and hold a reference to every subscriber's concrete type, and adding a new one (say, a loyalty-points service) means editing `CreateOrder` again.
- Testing order creation in isolation requires constructing or mocking every downstream dependency, even though "an order was created" and "what happens next" are logically separate concerns.
- Subscribers can't be added or removed at runtime, and one slow or failing subscriber directly blocks or fails order creation unless every call site adds its own error handling.

**✅ Good**
```go
type Event struct {
	Name    string
	Payload any
}

type EventBus struct {
	mu          sync.RWMutex
	subscribers map[string][]chan Event
}

func NewEventBus() *EventBus {
	return &EventBus{subscribers: make(map[string][]chan Event)}
}

func (b *EventBus) Subscribe(topic string) <-chan Event {
	ch := make(chan Event, 10)
	b.mu.Lock()
	b.subscribers[topic] = append(b.subscribers[topic], ch)
	b.mu.Unlock()
	return ch
}

func (b *EventBus) Publish(topic string, evt Event) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	for _, ch := range b.subscribers[topic] {
		select {
		case ch <- evt:
		default: // a slow subscriber doesn't block the publisher
		}
	}
}

// publisher only knows about the bus, never about individual subscribers
func (s *OrderService) CreateOrder(bus *EventBus, o Order) {
	saveOrder(o)
	bus.Publish("order.created", Event{Name: "order.created", Payload: o})
}

// each subscriber wires itself up independently
orders := bus.Subscribe("order.created")
go func() {
	for evt := range orders {
		sendConfirmationEmail(evt.Payload)
	}
}()
```

**Why it works / Explanation:** `OrderService` depends only on `EventBus.Publish`, with zero compile-time knowledge of who's listening or how many subscribers exist. Each subscriber independently calls `Subscribe` and runs its own goroutine to consume events, so adding a new subscriber never touches `CreateOrder` again, and a buffered channel with a non-blocking send means one slow subscriber can't stall order creation.

**Design principle:** Observer/pub-sub via channels — decouple "an event happened" from "who reacts to it" using channels as the notification mechanism, Go's native concurrency primitive for this.

---

## 8. Adapter pattern to satisfy a third-party or legacy interface

**The Problem:** A third-party client or legacy type often almost, but not quite, matches an interface your code needs to satisfy (a common example: something with a `LogMessage(string)` method instead of the `Write([]byte) (int, error)` that `io.Writer` requires) — a thin adapter type lets you reuse the existing implementation without modifying it or hand-rolling a replacement.

**❌ Bad**
```go
type LegacyLogger struct{}

func (l *LegacyLogger) LogMessage(msg string) {
	fmt.Println("[LEGACY]", msg)
}

// BUG: many stdlib and third-party APIs (log.New, http.Server.ErrorLog,
// io.Copy destinations, ...) expect an io.Writer, but LegacyLogger's
// LogMessage(string) doesn't match Write([]byte) (int, error) at all —
// it can't be plugged into any of them without changes
var errLog *log.Logger // nothing to construct this with, given only LegacyLogger
```

**Why it's wrong:**
- `LegacyLogger` can't be passed to `log.New`, `io.Copy`, or anything else expecting an `io.Writer`, even though conceptually it does the same job (accept a message, record it somewhere).
- Rewriting `LegacyLogger` to implement `Write` directly isn't always possible if it's vendored, third-party, or used elsewhere via its existing `LogMessage` call sites.
- Without an adapter, teams often hand-write a second, parallel logging implementation just to get an `io.Writer`, duplicating behavior that already exists.

**✅ Good**
```go
type legacyWriterAdapter struct {
	legacy *LegacyLogger
}

func (a *legacyWriterAdapter) Write(p []byte) (int, error) {
	a.legacy.LogMessage(string(p))
	return len(p), nil
}

func NewLegacyWriter(l *LegacyLogger) io.Writer {
	return &legacyWriterAdapter{legacy: l}
}

// LegacyLogger can now be plugged into anything expecting an io.Writer
stdLogger := log.New(NewLegacyWriter(&LegacyLogger{}), "", 0)
stdLogger.Println("server started")
```

**Why it works / Explanation:** `legacyWriterAdapter` implements exactly the one method (`Write`) needed to satisfy `io.Writer`, translating each write into a call to the legacy type's actual method. `LegacyLogger` itself is untouched — the adapter is a thin, separate type — so it still works everywhere its original `LogMessage` API is used, while also now working anywhere an `io.Writer` is expected.

**Design principle:** Adapter pattern — wrap an existing type in a small shim that implements the interface you need, translating calls, instead of modifying the original type or duplicating its behavior.

---

## Key Takeaways
- Replace telescoping constructor parameters with the functional options pattern (`New(required, ...Option)`).
- Replace type/flag switch statements with a small interface and one implementing type per case (strategy pattern).
- Layer cross-cutting HTTP concerns with `func(http.Handler) http.Handler` middleware instead of duplicating boilerplate in every handler.
- Use a fluent builder with a final `Build()` when construction needs ordered steps or step-specific validation.
- Use `sync.Once` for genuine lazy singletons, but treat singletons themselves as a deliberate exception, not a default.
- Inject dependencies (DB, HTTP client, logger) as interface parameters through constructors instead of package-level globals.
- Implement pub-sub with channels and a small event-bus type to decouple publishers from concrete subscriber types.
- Use a thin adapter type to make a legacy or third-party type satisfy an interface it doesn't natively implement.

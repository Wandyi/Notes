# Package Design and Project Structure

Package structure is one of the few design decisions in Go that the compiler actively enforces — import cycles are a hard build error, and `internal/` visibility is checked at build time, not just documented as a convention. Getting package boundaries wrong doesn't just make code harder to read; it produces compile errors, forces unrelated code to depend on implementation details it shouldn't, and makes a codebase progressively harder to navigate and evolve. This doc covers the recurring package- and project-structure mistakes in Go codebases and the idioms that avoid them.

## 1. Package name stuttering

**The Problem:** Because the package name already acts as the namespace prefix at every call site (`pkgname.Thing`), repeating the package name inside an exported type or function name produces a redundant, awkward-to-read call site.

**❌ Bad**
```go
package http

type HTTPClient struct {
	baseURL string
	client  *http.Client
}

func NewHTTPClient(baseURL string) *HTTPClient {
	return &HTTPClient{baseURL: baseURL, client: &http.Client{}}
}

// call site: http.NewHTTPClient(...) returns *http.HTTPClient — "HTTP" appears twice
c := http.NewHTTPClient("https://api.example.com")
```

**Why it's wrong:**
- Every call site reads redundantly: `http.HTTPClient`, `http.HTTPError`, `http.HTTPRequest` — the package qualifier already told the reader this is about HTTP.
- It signals the author wasn't thinking about the type from the caller's perspective (`pkgname.Type`), which tends to correlate with other naming issues throughout the same package.
- Once external code depends on the stuttering name, renaming it later is a breaking API change — the mistake gets locked in.

**✅ Good**
```go
package http

type Client struct {
	baseURL string
	client  *http.Client
}

func NewClient(baseURL string) *Client {
	return &Client{baseURL: baseURL, client: &http.Client{}}
}

// call site reads cleanly: the package qualifier already carries "HTTP"
c := http.NewClient("https://api.example.com")
```

**Why it works / Explanation:** Reading `http.Client` out loud already says everything `http.HTTPClient` said, with no redundancy — the package name is the namespace, so the type name only needs to describe what's distinctive about it within that namespace. This is the same convention behind stdlib names like `bytes.Buffer` (not `bytes.ByteBuffer`) and `strings.Reader` (not `strings.StringReader`).

**Design principle:** Package name as namespace — design exported names to read well as `pkgname.Name` at the call site, and drop words that just repeat the package name.

---

## 2. Circular import errors from poor package boundaries

**The Problem:** Two packages that each need a type or function the other one owns hit a hard compiler error in Go — unlike some languages, there's no forward-declaration escape hatch, so the dependency has to be restructured, not worked around.

**❌ Bad**
```go
// package user
package user

import "myapp/order" // needs order.Order to list a user's orders

type User struct {
	ID     string
	Name   string
	Orders []order.Order
}

// package order
package order

import "myapp/user" // needs user.User to know who placed the order — BUG: import cycle

type Order struct {
	ID    string
	Buyer user.User
}

// go build: import cycle not allowed
//   package myapp/user
//   	imports myapp/order
//   	imports myapp/user
```

**Why it's wrong:**
- The build fails outright — this isn't a style problem, it's a compile error that blocks everyone until it's fixed.
- It usually signals that `user` and `order` don't have a clean ownership boundary — each package is reaching into the other for a piece it needs, rather than one clearly owning the shared concept.
- The "quick fix" of merging the two packages just to avoid the cycle trades a compile error for a much larger, less cohesive package that mixes two separate domains.

**✅ Good**
```go
// package shared holds only the reference types both sides need
package shared

type UserRef struct {
	ID   string
	Name string
}

type OrderRef struct {
	ID string
}

// package user depends only on shared, never on order
package user

import "myapp/shared"

type User struct {
	ID     string
	Name   string
	Orders []shared.OrderRef
}

// package order depends only on shared, never on user
package order

import "myapp/shared"

type Order struct {
	ID    string
	Buyer shared.UserRef
}
```

**Why it works / Explanation:** `shared` sits below both `user` and `order` in the dependency graph, owning just the minimal reference types each side needs; both packages now depend downward on `shared`, and neither depends on the other, so the cycle is structurally impossible. This is usually the right move whenever two packages need "a bit of" each other — extract the shared piece rather than letting the dependency point in both directions.

**Design principle:** Acyclic package graph — restructure ownership (typically by extracting a shared, lower-level package) so dependencies flow in one direction; Go enforces this at compile time rather than treating it as a lint suggestion.

---

## 3. "God package" / catch-all `utils` package

**The Problem:** A catch-all `utils`/`common`/`helpers` package accumulates unrelated functions with no cohesive purpose beyond "didn't know where else to put it," and because everything imports it for at least one function, it becomes a dependency magnet coupling otherwise-unrelated parts of the codebase together.

**❌ Bad**
```go
package utils

func FormatCents(cents int64) string { /* ... */ return "" }

func IsValidEmail(s string) bool { /* ... */ return true }

func WithBackoff(fn func() error, attempts int) error { /* ... */ return nil }

func ParseCSV(r io.Reader) ([][]string, error) { /* ... */ return nil, nil }

// BUG: money formatting, email validation, retry logic, and CSV parsing
// share no cohesive purpose — but now the billing package, the auth
// package, and the import job all depend on the same "utils", so a
// change to any one function shows up as a diff every one of them
// has to consider, and nobody can describe what "utils" is for
```

**Why it's wrong:**
- The package has no describable single responsibility — asking "what is `utils` for?" has no good answer beyond "miscellaneous," making it hard to know where new code should go.
- Every consumer of any one function ends up importing (and nominally depending on) all the others, inflating the dependency graph and making it harder to reason about what a package actually needs.
- It becomes the path of least resistance for future code too — once it exists, the next unrelated helper goes there by default, and the package only grows less cohesive over time.

**✅ Good**
```go
package money

func FormatCents(cents int64) string { /* ... */ return "" }

package validation

func IsValidEmail(s string) bool { /* ... */ return true }

package retry

func WithBackoff(fn func() error, attempts int) error { /* ... */ return nil }

package csvutil

func Parse(r io.Reader) ([][]string, error) { /* ... */ return nil, nil }

// each package now has one describable purpose, and a consumer that only
// needs email validation imports "validation" — nothing else comes with it
```

**Why it works / Explanation:** Each function now lives in a package named for what it actually does, so a consumer's import list directly reflects its real dependencies — importing `validation` says something specific, unlike importing `utils`. Where a helper is really about a specific type (formatting a `Money` value, say), an even better fix is to make it a method on that type rather than a free function in a separate package at all.

**Design principle:** Cohesive packages — organize by domain/responsibility so a package's name and imports actually describe what it depends on and why, instead of by "miscellaneous."

---

## 4. Exposing too much API surface

**The Problem:** Exporting every type, function, and field by default "in case it's needed later" turns implementation details into a public API that external packages can and will depend on, which then locks the package into backward-compatibility constraints for things that were never meant to be part of its contract.

**❌ Bad**
```go
package cache

type Entry struct { // BUG: exported even though callers never construct these directly
	Key       string
	Value     any
	ExpiresAt time.Time
}

func NewEntry(key string, value any) *Entry { return &Entry{Key: key, Value: value} }

type Cache struct {
	Entries map[string]*Entry // BUG: exposes the internal storage representation directly
}

func (c *Cache) EvictExpired() {
	for k, e := range c.Entries {
		if time.Now().After(e.ExpiresAt) {
			delete(c.Entries, k)
		}
	}
}
```

**Why it's wrong:**
- `Cache.Entries` being exported means any external package can read, mutate, or replace the map directly — bypassing whatever invariants `EvictExpired` and future methods are supposed to maintain (thread-safety, TTL enforcement).
- `Entry` and `NewEntry` were only ever meant to be internal plumbing, but once exported, changing their shape later is a breaking change for any external code that started depending on them.
- The package's real public API is harder to spot amid everything else that got exported by default.

**✅ Good**
```go
package cache

type entry struct {
	key       string
	value     any
	expiresAt time.Time
}

type Cache struct {
	entries map[string]*entry // unexported: callers go through methods, not the map
}

func New() *Cache {
	return &Cache{entries: make(map[string]*entry)}
}

func (c *Cache) Set(key string, value any, ttl time.Duration) {
	c.entries[key] = &entry{key: key, value: value, expiresAt: time.Now().Add(ttl)}
}

func (c *Cache) Get(key string) (any, bool) {
	e, ok := c.entries[key]
	if !ok || time.Now().After(e.expiresAt) {
		return nil, false
	}
	return e.value, true
}

func (c *Cache) EvictExpired() {
	for k, e := range c.entries {
		if time.Now().After(e.expiresAt) {
			delete(c.entries, k)
		}
	}
}
```

**Why it works / Explanation:** `entry` and the storage map are now unexported, so the only way to interact with the cache is through `New`, `Set`, `Get`, and `EvictExpired` — the package's actual, intentional public API. Internal representation can change freely later without breaking any external caller, because it was never part of the contract to begin with.

**Design principle:** Minimal public API surface — default to unexported, and promote something to exported only when there's a real external consumer that needs it; it's far easier to widen an API later than to narrow one that's already in use.

---

## 5. Using `internal/` packages to enforce real architectural boundaries

**The Problem:** A convention documented only in a comment ("please don't import this from outside") relies on everyone reading and respecting the comment; Go's `internal/` directory turns that same intent into something the compiler rejects outright, which matters most in a multi-module workspace where "please don't" has no other enforcement mechanism.

**❌ Bad**
```go
// module github.com/acme/billing
// file: github.com/acme/billing/pkg/dbhelpers/dbhelpers.go
package dbhelpers

// BuildDSN was written as a private helper for the billing service's own
// database setup — never intended as a public API for other teams.
func BuildDSN(host, user, pass, dbname string) string {
	return fmt.Sprintf("postgres://%s:%s@%s/%s", user, pass, host, dbname)
}

// BUG: because it lives under pkg/, nothing stops a completely unrelated
// module from importing it directly:
//
//   module github.com/acme/shipping
//   import "github.com/acme/billing/pkg/dbhelpers"
//
// now billing can never change BuildDSN's signature (or delete it) without
// potentially breaking shipping's build — an accidental public API.
```

**Why it's wrong:**
- Nothing in the language or the build stops the cross-module import — the "this is private" intent exists only as a comment (or isn't documented at all), so it's routinely violated by accident.
- Once another team's module depends on it, `billing` has lost the ability to freely refactor or remove `dbhelpers` — a helper meant to be an implementation detail has become a de facto public contract.
- Discovering who actually depends on it requires manually auditing other repositories, since Go tooling has no way to distinguish "intentional API" from "accidentally imported internal helper" when both live under an ordinary importable path.

**✅ Good**
```go
// module github.com/acme/billing
// file: github.com/acme/billing/internal/dbhelpers/dbhelpers.go
package dbhelpers

func BuildDSN(host, user, pass, dbname string) string {
	return fmt.Sprintf("postgres://%s:%s@%s/%s", user, pass, host, dbname)
}

// any import of "github.com/acme/billing/internal/dbhelpers" from code that
// is NOT rooted at github.com/acme/billing now fails at compile time:
//
//   go build github.com/acme/shipping/...
//   package github.com/acme/shipping/foo
//   	imports github.com/acme/billing/internal/dbhelpers: use of internal
//   	package github.com/acme/billing/internal/dbhelpers not allowed
```

**Why it works / Explanation:** Go's build tooling specifically checks the `internal/` segment of an import path and only permits imports from packages rooted at the directory that contains `internal`'s parent — here, anything under `github.com/acme/billing/...`. Moving `dbhelpers` under `internal/` turns "please don't import this" from a comment into a build failure for anyone outside `billing` who tries, with zero runtime cost and no extra tooling required.

**Design principle:** Compiler-enforced boundaries — use `internal/` for anything that's an implementation detail of a module, so architectural boundaries are enforced by `go build`, not by documentation that can be ignored.

---

## 6. Premature "clean architecture" over-layering

**The Problem:** Introducing a full repository/service/use-case/DTO layering for a small application before a second implementation or consumer actually exists adds files and indirection to trace through, without yet buying the flexibility those layers are meant to provide.

**❌ Bad**
```go
// domain/user.go
type User struct{ ID, Name string }

// domain/repository.go
type UserRepository interface{ FindByID(id string) (*User, error) }

// usecase/get_user.go
type GetUserRequest struct{ ID string }
type GetUserResponse struct{ ID, Name string }

type GetUserUseCase struct{ repo domain.UserRepository }

func (uc *GetUserUseCase) Execute(req GetUserRequest) (*GetUserResponse, error) {
	u, err := uc.repo.FindByID(req.ID)
	if err != nil {
		return nil, err
	}
	// BUG: mapping one struct to a near-identical DTO for a single field
	// set, for an application with exactly one consumer of this data
	return &GetUserResponse{ID: u.ID, Name: u.Name}, nil
}

// service/user_service.go
type UserService struct{ uc *usecase.GetUserUseCase }

func (s *UserService) GetUser(id string) (*usecase.GetUserResponse, error) {
	return s.uc.Execute(usecase.GetUserRequest{ID: id}) // just forwards the call
}

// handler/user_handler.go — tracing "get a user" means reading all 4 files above
func (h *UserHandler) GetUser(w http.ResponseWriter, r *http.Request) {
	resp, err := h.service.GetUser(r.PathValue("id"))
	if err != nil {
		http.Error(w, err.Error(), http.StatusNotFound)
		return
	}
	json.NewEncoder(w).Encode(resp)
}
```

**Why it's wrong:**
- Understanding what happens for a single "get a user by ID" request requires reading four files across four packages (`domain`, `usecase`, `service`, `handler`), most of which do nothing but forward the call to the next layer.
- The DTOs (`GetUserRequest`/`GetUserResponse`) duplicate `User` field-for-field with no actual difference in shape — they exist because the pattern calls for them, not because a real boundary needs translation yet.
- None of this layering buys anything today: there's exactly one repository implementation and exactly one caller of `UserService.GetUser`, so the abstraction has no second case to justify it.

**✅ Good**
```go
// user/user.go
type User struct{ ID, Name string }

type Store interface {
	FindByID(id string) (*User, error)
}

type Handler struct {
	store Store
}

func NewHandler(store Store) *Handler {
	return &Handler{store: store}
}

func (h *Handler) GetUser(w http.ResponseWriter, r *http.Request) {
	u, err := h.store.FindByID(r.PathValue("id"))
	if err != nil {
		http.Error(w, err.Error(), http.StatusNotFound)
		return
	}
	json.NewEncoder(w).Encode(u)
}

// extract a separate use-case/service layer later, when a second consumer
// (a gRPC endpoint, a batch job) actually needs the same lookup logic
// with different surrounding behavior
```

**Why it works / Explanation:** The handler depends on one small interface (`Store`) and returns the domain type directly — there's exactly one place to look to understand "get a user." The `Store` interface is still there, so swapping the implementation or injecting a test fake works exactly as it would with the heavier layering; what's gone is the layers that had nothing real to do yet.

**Design principle:** Start concrete, extract on second use — Go's culture favors flatter, more direct code over speculative layering; add an abstraction when a second implementation or consumer actually shows up, not in anticipation of one (this is the same "interface pollution" trap covered in the interfaces doc, applied at the package-architecture level).

---

## 7. Defining interfaces in the implementation package instead of at the point of consumption

**The Problem:** Defining an interface in the same package as its concrete implementation forces every consumer to import that package — and everything it transitively depends on — just to get the interface type, even consumers that only ever use a test fake and never touch the real implementation.

**❌ Bad**
```go
// package store
package store

type Store interface {
	GetUser(id string) (*User, error)
	SaveUser(u *User) error
}

type PostgresStore struct {
	db *sql.DB
}

func (p *PostgresStore) GetUser(id string) (*User, error) { /* ... */ return nil, nil }
func (p *PostgresStore) SaveUser(u *User) error            { /* ... */ return nil }

// BUG: any consumer that wants to depend only on the Store abstraction
// still has to `import "myapp/store"`, which pulls in PostgresStore and,
// transitively, "database/sql" and the postgres driver — even a consumer
// whose tests only ever construct an in-memory fake.
```

**Why it's wrong:**
- A package that logically only needs "something that can fetch and save users" ends up with a compile-time dependency on the postgres driver, purely because that's where the interface happened to be declared.
- Swapping storage backends, or writing a lightweight in-memory implementation for tests, still requires importing the heavyweight `store` package just to reference the interface type by name.
- It inverts the natural dependency direction: the abstraction is defined next to one specific implementation, rather than next to the code that actually needs it.

**✅ Good**
```go
// package user (the consumer) declares exactly the interface it needs
package user

type UserStore interface {
	GetUser(id string) (*User, error)
	SaveUser(u *User) error
}

type Service struct {
	store UserStore
}

func NewService(store UserStore) *Service {
	return &Service{store: store}
}

// package postgres (the implementation) knows nothing about "user"'s
// interface — it just happens to have matching methods
package postgres

type Store struct {
	db *sql.DB
}

func (s *Store) GetUser(id string) (*user.User, error) { /* ... */ return nil, nil }
func (s *Store) SaveUser(u *user.User) error             { /* ... */ return nil }

// wiring in main is the one place allowed to know about both concrete types
svc := user.NewService(&postgres.Store{})
```

**Why it works / Explanation:** `user.UserStore` is declared right next to the code that consumes it, and `postgres.Store` satisfies it implicitly through structural typing — `postgres` doesn't even need to import `user`'s interface type, only the `User` struct it operates on. A test for `user.Service` can define its own tiny fake implementing `UserStore` without ever importing `postgres`, `database/sql`, or a driver.

**Design principle:** Interfaces belong at the point of consumption, not next to their implementation — this keeps consumers decoupled from concrete dependencies they don't actually need.

---

## 8. Flat `main.go` doing everything vs a reasonable minimal structure

**The Problem:** A single `main.go` that wires HTTP routes, writes SQL, and validates data all in one file is fast to start but doesn't stay maintainable — yet the fix isn't necessarily the full layered architecture from the previous sections; there's a practical middle ground.

**❌ Bad**
```go
func main() {
	db, _ := sql.Open("postgres", os.Getenv("DATABASE_URL"))

	http.HandleFunc("/users/", func(w http.ResponseWriter, r *http.Request) {
		id := strings.TrimPrefix(r.URL.Path, "/users/")
		row := db.QueryRow("SELECT id, name, email FROM users WHERE id = $1", id) // BUG: raw SQL inline in the HTTP layer
		var u struct{ ID, Name, Email string }
		if err := row.Scan(&u.ID, &u.Name, &u.Email); err != nil {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		if !strings.Contains(u.Email, "@") { // BUG: validation logic mixed into the handler too
			http.Error(w, "invalid user data", http.StatusInternalServerError)
			return
		}
		json.NewEncoder(w).Encode(u)
	})

	log.Fatal(http.ListenAndServe(":8080", nil))
	// routing, SQL, and validation all live in one function, in one file;
	// testing the validation rule alone requires spinning up a real DB
}
```

**Why it's wrong:**
- SQL, HTTP routing, and validation are all tangled into a single anonymous function, so testing any one concern means exercising all of them together, including a live database connection.
- As more endpoints are added, `main.go` grows without bound and becomes the one file everyone on the team is editing simultaneously, a recurring source of merge conflicts.
- There's no seam at which to inject a fake store for testing, or to reuse the user-fetching logic from anywhere other than this exact HTTP handler.

**✅ Good**

A practical middle ground between "everything in one file" and the over-layered extreme from item 6:

```
myapp/
  cmd/
    api/
      main.go            # wiring only: config, DB connection, router, shutdown
  user/
    user.go              # User type + validation rules
    store.go             # Store interface (declared where it's consumed)
    handler.go           # HTTP handlers, depend only on the Store interface
  internal/
    postgres/
      store.go            # concrete Store implementation; only main imports it
```

```go
// cmd/api/main.go
func main() {
	db, err := sql.Open("postgres", os.Getenv("DATABASE_URL"))
	if err != nil {
		log.Fatal(err)
	}
	handler := user.NewHandler(postgres.NewUserStore(db))

	mux := http.NewServeMux()
	mux.HandleFunc("/users/", handler.GetUser)
	log.Fatal(http.ListenAndServe(":8080", mux))
}

// user/handler.go
func (h *Handler) GetUser(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/users/")
	u, err := h.store.GetUser(id)
	if err != nil {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	json.NewEncoder(w).Encode(u)
}
```

**Why it works / Explanation:** `main.go` does only wiring — reading config, opening the DB, constructing the handler, starting the server — and every other concern lives in a package named for what it does. `user` depends on a `Store` interface it declares itself, so its handler and validation logic can be tested with a fake store and no database; `postgres` is the only package that imports `database/sql`, and only `main` imports `postgres`. There's no repository/use-case/DTO layering here — just one interface at the one real boundary (storage) that currently has more than one shape (Postgres in production, a fake in tests).

**Design principle:** Structure for the boundaries you actually have — separate concerns into packages along real seams (HTTP layer, domain logic, storage), without adding layers beyond the ones the application currently needs.

---

## Key Takeaways
- Don't repeat the package name inside exported type/function names (`http.Client`, not `http.HTTPClient`).
- Avoid circular imports by extracting shared types into a lower-level package so dependencies flow one way.
- Don't dump unrelated helpers into a catch-all `utils`/`common` package — organize by domain/responsibility instead.
- Default to unexported and promote to exported deliberately, rather than exporting everything "just in case."
- Use `internal/` to make architectural boundaries compiler-enforced instead of documentation-only.
- Don't introduce repository/service/use-case/DTO layering until a second implementation or consumer actually needs it.
- Declare interfaces in the consuming package, not in the package that implements them, to avoid unnecessary coupling.
- Keep `main.go` limited to wiring, and split HTTP/domain/storage into a few focused packages — a middle ground between flat and over-layered.

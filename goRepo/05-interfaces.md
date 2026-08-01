# Interfaces

Interfaces are Go's primary abstraction mechanism, and their implicit, structural satisfaction is both the language's greatest strength and a steady source of production incidents. Because there's no `implements` keyword, satisfaction can silently break during a refactor, nil-ness can hide inside a non-nil interface value, and it's easy to reach for `any` or a speculative interface where a concrete type or a smaller interface would be safer. This file walks through the interface pitfalls that show up repeatedly in real Go services, along with the idioms the standard library uses to avoid them.

## 1. The typed-nil interface trap

**The Problem:** An interface value is really a pair: a concrete type descriptor and a value. When a nil pointer of a concrete type is assigned to an interface variable, the interface itself is *not* nil — it has a non-nil type descriptor and a nil value. `err != nil` then evaluates to true even though the underlying pointer is nil, which is one of the most common "gotcha" bugs in Go error handling.

**❌ Bad**
```go
package main

import "fmt"

type MyError struct{ msg string }

func (e *MyError) Error() string { return e.msg }

func mayFail(succeed bool) error {
	var e *MyError // nil, but still typed as *MyError
	if !succeed {
		e = &MyError{msg: "boom"}
	}
	return e // BUG: returned interface has a non-nil type descriptor, even when e is nil
}

func main() {
	err := mayFail(true) // succeeded - e stayed nil
	if err != nil {
		fmt.Println("unexpected error path taken!") // BUG: this runs anyway
	}
}
```

**Why it's wrong:**
- `err != nil` is true even though the underlying `*MyError` is nil, because the `error` interface value holds `(type=*MyError, value=nil)`, not the fully-nil `(type=nil, value=nil)`.
- If any code goes on to call `err.Error()`, it dereferences a nil `*MyError` receiver and panics — turning a logic bug into a crash.
- This is especially dangerous because `mayFail`'s signature (`func(bool) error`) gives no hint that anything is wrong; the bug is purely in the return statement.

**✅ Good**
```go
package main

import "fmt"

type MyError struct{ msg string }

func (e *MyError) Error() string { return e.msg }

func mayFail(succeed bool) error {
	if succeed {
		return nil // explicit, untyped nil interface - no hidden type descriptor
	}
	return &MyError{msg: "boom"}
}

func main() {
	err := mayFail(true)
	if err != nil {
		fmt.Println("unexpected error path taken!") // never runs - err is truly nil
	}
}
```

**Why it works / Explanation:** Returning the bare `nil` literal directly produces a fully-nil interface value — there is no concrete type attached at all. The rule of thumb: never return a typed pointer variable as an interface without first checking whether it's actually nil; return the `nil` literal explicitly in that case.

**Design principle:** Prefer explicit `return nil` over returning a possibly-nil concrete-typed variable through an interface-typed return — it's the only way to guarantee the interface value itself is nil.

---

## 2. Interface pollution

**The Problem:** Defining an interface "for testability" or "for future flexibility" before a second implementation actually exists adds a layer of indirection that pays for itself only if that second implementation (or a test fake) ever materializes. In practice, many such interfaces live their entire life with exactly one implementation, permanently taxing readability for no benefit.

**❌ Bad**
```go
package main

type DB struct{} // stand-in for something like *sql.DB

type User struct {
	ID   int
	Name string
}

type UserStore interface {
	GetUser(id int) (*User, error)
	SaveUser(u *User) error
	DeleteUser(id int) error
	ListUsers() ([]*User, error)
}

type postgresUserStore struct{ db *DB }

func NewUserStore(db *DB) UserStore { // BUG: returns an interface with only one implementation, ever
	return &postgresUserStore{db: db}
}

func (s *postgresUserStore) GetUser(id int) (*User, error) { return nil, nil }
func (s *postgresUserStore) SaveUser(u *User) error         { return nil }
func (s *postgresUserStore) DeleteUser(id int) error        { return nil }
func (s *postgresUserStore) ListUsers() ([]*User, error)    { return nil, nil }
```

**Why it's wrong:**
- Every caller depends on `UserStore` instead of `*postgresUserStore`, but there is no second implementation to justify the abstraction — the interface adds a layer of navigation (jump-to-definition lands on an interface, not the code) with zero substitutability benefit.
- Any Postgres-specific capability (transactions, `context` deadlines tied to a specific driver, connection pool introspection) either has to be shoehorned into the shared interface or requires an ugly type assertion back to the concrete type, defeating the abstraction anyway.
- Mocking this interface in tests still requires hand-writing or generating a fake with all four methods, even for tests that only exercise `GetUser`.

**✅ Good**
```go
package main

type DB struct{}

type User struct {
	ID   int
	Name string
}

type UserStore struct{ db *DB } // concrete type - exported, usable directly

func NewUserStore(db *DB) *UserStore {
	return &UserStore{db: db}
}

func (s *UserStore) GetUser(id int) (*User, error) { return nil, nil }
func (s *UserStore) SaveUser(u *User) error         { return nil }
func (s *UserStore) DeleteUser(id int) error        { return nil }
func (s *UserStore) ListUsers() ([]*User, error)    { return nil, nil }
```

**Why it works / Explanation:** Returning the concrete `*UserStore` costs nothing today and loses nothing either — Go's implicit interface satisfaction means any consumer package can still define a small interface over the one or two methods it actually calls, whenever it actually needs one (e.g. for testing). The abstraction gets added at the point of real need, shaped by that need.

**Design principle:** Follow "accept interfaces, return structs" — return concrete types from constructors, and let interfaces emerge in consumer code only once a genuine second implementation or test seam exists; remember that "the bigger the interface, the weaker the abstraction."

---

## 3. Overusing `any` where generics would preserve type safety

**The Problem:** Before generics, a container had to use `interface{}` (`any`) to hold arbitrary values, pushing every type check to runtime via type assertions. Continuing to reach for `any` out of habit in modern Go throws away compile-time type safety that a generic type parameter would give for free.

**❌ Bad**
```go
package main

import "fmt"

type Cache struct {
	data map[string]any
}

func NewCache() *Cache {
	return &Cache{data: make(map[string]any)}
}

func (c *Cache) Set(key string, val any) {
	c.data[key] = val
}

func (c *Cache) Get(key string) any {
	return c.data[key]
}

func main() {
	c := NewCache()
	c.Set("age", 30)
	v := c.Get("age")
	age := v.(int) // BUG: every caller must know and assert the right type; panics if it's ever wrong
	fmt.Println(age + 1)
}
```

**Why it's wrong:**
- The compiler cannot check that `"age"` actually holds an `int` — a later change that stores a `string` under the same key compiles fine and only fails with a runtime panic at the assertion.
- Every call site that reads from the cache needs its own type assertion, duplicating the same runtime check (and the same panic risk) across the codebase.

**✅ Good**
```go
package main

import "fmt"

type Cache[T any] struct {
	data map[string]T
}

func NewCache[T any]() *Cache[T] {
	return &Cache[T]{data: make(map[string]T)}
}

func (c *Cache[T]) Set(key string, val T) {
	c.data[key] = val
}

func (c *Cache[T]) Get(key string) (T, bool) {
	v, ok := c.data[key]
	return v, ok
}

func main() {
	c := NewCache[int]()
	c.Set("age", 30)
	age, ok := c.Get("age") // age is int - compiler-checked, no assertion, no panic risk
	if ok {
		fmt.Println(age + 1)
	}
}
```

**Why it works / Explanation:** `Cache[T]` fixes the element type at instantiation (`NewCache[int]()`), so the compiler enforces that every `Set`/`Get` call agrees on the type — there is no assertion left to fail, because there's no `any` left to assert from.

**Design principle:** Use a generic type parameter instead of `any` whenever a container or function needs to preserve a single, consistent type across its API — reserve `any` for genuinely heterogeneous data (e.g. arbitrary JSON).

---

## 4. Type assertion panics

**The Problem:** The single-value form of a type assertion, `v := x.(T)`, panics if `x` does not hold a `T`. This is easy to write carelessly against loosely-typed data — most commonly the `map[string]interface{}` produced by decoding arbitrary JSON — where a missing or wrong-typed key is a routine occurrence, not an exceptional one.

**❌ Bad**
```go
package main

import (
	"encoding/json"
	"fmt"
)

func handlePayload(raw map[string]interface{}) {
	name := raw["name"].(string) // BUG: panics if "name" is missing or isn't a string
	fmt.Println("Name:", name)
}

func main() {
	var payload map[string]interface{}
	json.Unmarshal([]byte(`{"age": 30}`), &payload)
	handlePayload(payload) // panic: interface conversion: interface {} is nil, not string
}
```

**Why it's wrong:**
- `raw["name"]` on a missing key returns the zero value for `interface{}`, which is untyped `nil` — asserting `nil.(string)` panics immediately, crashing whatever request or job triggered this code path.
- Because the panic depends on the *shape* of incoming data rather than a code change, it tends to surface unpredictably in production against real-world payloads that differ slightly from what was tested.

**✅ Good**
```go
package main

import (
	"encoding/json"
	"fmt"
)

func handlePayload(raw map[string]interface{}) {
	name, ok := raw["name"].(string)
	if !ok {
		name = "unknown"
	}
	fmt.Println("Name:", name)
}

func main() {
	var payload map[string]interface{}
	json.Unmarshal([]byte(`{"age": 30}`), &payload)
	handlePayload(payload) // "Name: unknown" - no panic
}
```

**Why it works / Explanation:** The comma-ok form `v, ok := x.(T)` never panics — `ok` is `false` whenever the assertion doesn't hold, letting the code choose a fallback instead of crashing. This should be the default form for any assertion against data whose shape isn't fully guaranteed by the type system.

**Design principle:** Reserve the panicking single-value assertion for cases where failure would indicate a genuine programming bug (an invariant you control); use the comma-ok form for anything derived from external input.

---

## 5. Small vs. large interfaces

**The Problem:** The standard library's `io.Reader` and `io.Writer` — each a single method — are easy to implement, easy to compose, and easy to fake in tests. An interface that accumulates many methods becomes hard to implement fully, hard to mock, and forces every implementation (real or fake) to provide behavior for methods a given caller may never use.

**❌ Bad**
```go
package main

type Storage interface {
	Get(key string) ([]byte, error)
	Put(key string, val []byte) error
	Delete(key string) error
	List(prefix string) ([]string, error)
	Exists(key string) (bool, error)
	Copy(src, dst string) error
	Move(src, dst string) error
	Size(key string) (int64, error)
	Checksum(key string) (string, error)
	Close() error
}

// BUG: any test double for a function that only calls Get/Put must still implement
// all ten methods, most of them as meaningless stubs.
```

**Why it's wrong:**
- A function that only needs to read and write values is forced to depend on `Storage` in full, coupling it to nine methods it never calls.
- Every test fake has to implement all ten methods (even as panicking stubs) just to satisfy the interface, adding maintenance overhead disproportionate to what any single test actually exercises.

**✅ Good**
```go
package main

type Getter interface {
	Get(key string) ([]byte, error)
}

type Putter interface {
	Put(key string, val []byte) error
}

func Backup(g Getter, key string) ([]byte, error) {
	return g.Get(key) // only depends on the one method it actually needs
}
```

**Why it works / Explanation:** `Backup` declares exactly the capability it needs (`Getter`), and any type with a matching `Get` method — including a one-line test fake — satisfies it automatically. Larger capabilities can still be composed from these small interfaces (`type ReadWriter interface { Getter; Putter }`) wherever something genuinely needs both.

**Design principle:** This is the interface segregation principle applied to Go: keep interfaces as small as the consumer's actual needs, and compose bigger ones from smaller ones only where the composition is genuinely required.

---

## 6. Implicit satisfaction has no compiler-enforced link

**The Problem:** Because interface satisfaction is structural, nothing ties a type's definition to the interfaces it happens to satisfy. A refactor that changes a method's signature can silently break satisfaction — the type still compiles standalone, and the failure only appears wherever that type is *used* as the interface, which might be a different package altogether.

**❌ Bad**
```go
// file: logutil/logger.go
package logutil

import "fmt"

type Logger struct{}

// Originally: func (l Logger) Write(p []byte) (int, error) - satisfied io.Writer.
// A later "cleanup" changed the signature:
func (l Logger) Write(msg string) (int, error) { // BUG: no longer satisfies io.Writer
	fmt.Print(msg)
	return len(msg), nil
}

// file: pipeline/pipeline.go
package pipeline

import (
	"io"

	"myapp/logutil"
)

func setupPipe(w io.Writer) { /* ... */ }

func Start() {
	setupPipe(logutil.Logger{}) // BUG: compile error surfaces HERE, in an unrelated package
}
```

**Why it's wrong:**
- `logutil.Logger` and its `Write` method compile perfectly fine on their own — nothing in `logutil` flags that the type used to satisfy `io.Writer` and no longer does.
- The compile error appears in `pipeline`, potentially owned by a different team, with a message about `io.Writer` that gives no direct pointer back to the signature change that caused it.

**✅ Good**
```go
package logutil

import (
	"fmt"
	"io"
)

type Logger struct{}

var _ io.Writer = Logger{} // compile-time assertion: breaks immediately, right next to the definition

func (l Logger) Write(p []byte) (int, error) {
	fmt.Print(string(p))
	return len(p), nil
}
```

**Why it works / Explanation:** The blank-identifier assignment `var _ io.Writer = Logger{}` forces the compiler to check the assignment right where `Logger` is defined. If a future edit ever breaks `io.Writer` satisfaction, the build fails in `logutil` itself, with a clear, local error — not three packages away.

**Design principle:** Add a compile-time interface assertion (`var _ Iface = (*Type)(nil)`) next to any type that is meant to satisfy an important interface, so drift is caught at the definition site, not at a distant call site.

---

## 7. Comparing interface values panics on uncomparable dynamic types

**The Problem:** Interface values are always comparable as far as the compiler is concerned, but the comparison is only actually safe at runtime if the dynamic type underneath is itself comparable. Using an `any` value as a map key, or comparing two `any` values directly, panics the moment the concrete type turns out to be a slice, map, or func.

**❌ Bad**
```go
package main

func main() {
	cache := map[any]bool{}

	var a any = []int{1, 2, 3}
	cache[a] = true // BUG: panics: runtime error: hash of unhashable type []int
}
```

**Why it's wrong:**
- `map[any]bool{}` compiles without complaint — `any` is a valid, comparable-looking map key type at compile time.
- The panic only happens when an actual uncomparable value (a slice here) is used as a key, so this can pass code review and even most tests, only to blow up on a specific input in production.

**✅ Good**
```go
package main

import "fmt"

func main() {
	cache := map[string]bool{}

	key := fmt.Sprint([]int{1, 2, 3}) // convert to a comparable representation first
	cache[key] = true
}
```

**Why it works / Explanation:** Converting the uncomparable value into an explicitly comparable representation (a string, in this case) before using it as a key sidesteps the runtime check entirely — the map key type is now genuinely, statically comparable.

**Design principle:** Never use `any`/`interface{}` as a map key type or equality operand unless every possible dynamic type stored in it is guaranteed comparable — prefer a concrete, comparable key type instead.

---

## 8. Defining interfaces in the wrong package

**The Problem:** Go's idiom is for interfaces to be defined by the *consumer* of a dependency, shaped exactly to what that consumer needs — not by the package that implements the behavior. Defining the interface next to the implementation instead forces every consumer to import the implementation package just to reference the interface type, and invites import cycles as the codebase grows.

**❌ Bad**
```go
// file: storage/storage.go
package storage

type Store interface {
	Get(id string) (string, error)
	Put(id, val string) error
}

type Postgres struct{}

func (p *Postgres) Get(id string) (string, error) { return "", nil }
func (p *Postgres) Put(id, val string) error      { return nil }

// file: handler/handler.go
package handler

import "myapp/storage" // BUG: forced to depend on the entire storage package just to reference one interface

func Serve(s storage.Store) { /* ... */ }
```

**Why it's wrong:**
- `handler` now imports `storage` purely to spell the interface name, even though it might only ever call `Get` — any change to unrelated parts of `storage` can trigger rebuilds and re-review of `handler`.
- If `storage` ever needs to depend on something in `handler` (for example, to log through a `handler`-defined logger interface), the two packages create an import cycle that Go's build simply refuses to resolve.

**✅ Good**
```go
// file: storage/storage.go
package storage

type Postgres struct{}

func (p *Postgres) Get(id string) (string, error) { return "", nil }
func (p *Postgres) Put(id, val string) error      { return nil }

// file: handler/handler.go
package handler

type Getter interface { // defined by the consumer, shaped by what handler actually needs
	Get(id string) (string, error)
}

func Serve(g Getter) { /* ... */ } // storage.Postgres satisfies this automatically - no import required
```

**Why it works / Explanation:** `handler` no longer imports `storage` at all — `storage.Postgres` satisfies `handler.Getter` implicitly, because Go interface satisfaction doesn't require either side to know about the other. The dependency arrow now points the natural direction: implementation depends on nothing extra, consumer defines exactly what it needs.

**Design principle:** Define interfaces in the consumer package, sized to the consumer's needs — this is the standard library's own convention (`io.Reader`, `sort.Interface`) and it eliminates a whole class of unnecessary coupling and import cycles.

---

## 9. Forcing interface satisfaction with no-op/must-not-call methods

**The Problem:** When a type is forced to implement a method it fundamentally cannot support meaningfully, the usual workaround — a stub that panics or silently does nothing — creates a value that violates the interface's implicit contract. Any code that treats the type generically through that interface can now crash or misbehave in ways the type system gave no hint of.

**❌ Bad**
```go
package main

import "fmt"

type Animal interface {
	Eat()
	Sleep()
	Fly()
}

type Dog struct{}

func (Dog) Eat()   { fmt.Println("eating") }
func (Dog) Sleep() { fmt.Println("sleeping") }
func (Dog) Fly()   { panic("dogs can't fly") } // BUG: forced to implement a method that must never be called

func main() {
	var a Animal = Dog{}
	a.Eat()
	a.Fly() // panics - any code that treats Dog as a generic Animal can crash
}
```

**Why it's wrong:**
- `Dog` satisfies `Animal` according to the compiler, but the `Fly` implementation is a lie — it violates the Liskov Substitution Principle: `Dog` cannot actually be used everywhere an `Animal` is expected.
- Generic code written against `Animal` (for example, a function that calls `Fly` on every animal in a zoo) has no way to know, from the type system alone, which animals will panic.

**✅ Good**
```go
package main

import "fmt"

type Eater interface{ Eat() }
type Sleeper interface{ Sleep() }
type Flyer interface{ Fly() }

type Dog struct{}

func (Dog) Eat()   { fmt.Println("eating") }
func (Dog) Sleep() { fmt.Println("sleeping") }
// Dog simply doesn't implement Flyer - callers that need flight ask for a Flyer explicitly

type Bird struct{}

func (Bird) Eat()   { fmt.Println("eating") }
func (Bird) Sleep() { fmt.Println("sleeping") }
func (Bird) Fly()   { fmt.Println("flying") }

func main() {
	animals := []Eater{Dog{}, Bird{}}
	for _, a := range animals {
		a.Eat() // safe: every Eater can genuinely eat, no hidden panics
	}
}
```

**Why it works / Explanation:** Splitting the capability into `Eater`, `Sleeper`, and `Flyer` lets each type implement only the behaviors it can genuinely support. Code that needs flight asks for a `Flyer` specifically, and `Dog` — which was never a `Flyer` — simply can't be passed there, catching the mismatch at compile time instead of at a runtime panic.

**Design principle:** When a type can't honestly implement part of an interface, that's a signal to segregate the interface, not to stub the method — satisfying an interface should never require lying about capability.

---

## 10. Interfaces satisfied only by the pointer type

**The Problem:** If any method needed for interface satisfaction has a pointer receiver, only the pointer type has that method in its method set — assigning a plain value to an interface variable fails to compile, which surprises anyone who forgets that method sets differ between `T` and `*T`.

**❌ Bad**
```go
package main

import "fmt"

type Animal interface {
	Speak() string
}

type Dog struct{ name string }

func (d *Dog) Speak() string { return d.name + " says woof" } // pointer receiver only

func main() {
	var a Animal = Dog{name: "Rex"} // BUG: compile error - Dog does not implement Animal
	fmt.Println(a.Speak())
}
```

**Why it's wrong:**
- `Speak` is declared on `*Dog`, so `Dog`'s value method set is empty with respect to `Animal` — the compiler correctly rejects `Dog{}` as an `Animal`, but the error message ("Dog does not implement Animal") doesn't immediately explain *why* to someone unfamiliar with method sets.
- This is a frequent point of confusion when refactoring a type from value receivers to pointer receivers (e.g. to allow mutation) without updating every place that constructs it as a bare value for interface use.

**✅ Good**
```go
package main

import "fmt"

type Animal interface {
	Speak() string
}

type Dog struct{ name string }

func (d *Dog) Speak() string { return d.name + " says woof" }

func main() {
	var a Animal = &Dog{name: "Rex"} // *Dog's method set includes Speak - this satisfies Animal
	fmt.Println(a.Speak())           // "Rex says woof"
}
```

**Why it works / Explanation:** Storing `&Dog{...}` in the interface variable uses `*Dog`'s method set, which includes every pointer-receiver method. Whenever a type has any pointer-receiver methods, plan for it to be used as `*T` everywhere it needs to satisfy an interface.

**Design principle:** Decide up front whether a type is value-like or reference-like; if any method needs a pointer receiver, construct and pass the type as a pointer consistently, including everywhere it's assigned to an interface.

---

## Key Takeaways
- A nil concrete pointer returned through an interface makes `err != nil` true — return the `nil` literal explicitly instead.
- Don't define interfaces speculatively for a single implementation; return concrete types and let interfaces emerge where a real second implementation or test seam appears.
- Prefer generic type parameters over `any` when a container or function needs to preserve one consistent type, to keep type checks at compile time.
- Use the comma-ok form of type assertions for anything derived from external or loosely-typed data; reserve the panicking form for internal invariants.
- Keep interfaces small and single-purpose (interface segregation) so they're easy to implement and easy to fake in tests.
- Add a compile-time assertion (`var _ Iface = Type{}`) next to a type's definition to catch broken interface satisfaction immediately, not at a distant call site.
- Never use `any`/`interface{}` as a map key or equality operand unless every possible dynamic type is guaranteed comparable.
- Define interfaces in the consumer package, sized to its actual needs, instead of alongside the implementation.
- Don't force a type to satisfy an interface via a must-not-call or no-op method — segregate the interface instead.
- Remember that pointer-receiver methods only exist in the pointer type's method set — construct and pass such types as pointers wherever they need to satisfy an interface.

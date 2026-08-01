# Structs and Embedding

Structs and embedding are how Go builds composite data types and approximates code reuse without classical inheritance, but the mechanics are full of sharp edges. Method sets that silently differ between value and pointer receivers, embedding that looks like inheritance but isn't, and memory layout choices all affect both correctness and performance. Getting these wrong produces bugs ranging from compile errors surfacing in unrelated packages, to runtime panics, to multi-megabyte memory bloat that only shows up under load. This file catalogs the structural mistakes seen most often in production Go codebases, with the reasoning needed to avoid them.

## 1. Inconsistent receiver types

**The Problem:** Mixing value receivers and pointer receivers across the methods of the same type is easy to do without noticing, because Go compiles each method independently. The consequence only appears later, when a value (not a pointer) of that type is passed somewhere that expects an interface — the value's method set silently excludes any method with a pointer receiver.

**❌ Bad**
```go
package main

type Counter struct {
	count int
}

func (c Counter) Get() int { return c.count } // value receiver
func (c *Counter) Inc()    { c.count++ }       // pointer receiver

type Incrementer interface {
	Get() int
	Inc()
}

func run(inc Incrementer) {
	inc.Inc()
}

func main() {
	c := Counter{}
	run(c) // BUG: compile error - Counter does not implement Incrementer
	//     (Inc has a pointer receiver, so it's not in Counter's value method set)
}
```

**Why it's wrong:**
- The method set of a value type `T` only includes methods declared with receiver `T`; methods declared with receiver `*T` are excluded. `*T`'s method set includes both.
- The failure is a compile error, but it appears at the call site (`run(c)`), which can be far away from — and much later than — the mixed-receiver declaration that actually caused it, making it confusing to diagnose.
- It also signals a design smell: if one method needs to mutate state via a pointer, the type almost certainly has mutable identity, and every method should probably take a pointer receiver for consistency.

**✅ Good**
```go
package main

import "fmt"

type Counter struct {
	count int
}

func (c *Counter) Get() int { return c.count } // pointer receiver
func (c *Counter) Inc()     { c.count++ }       // pointer receiver

type Incrementer interface {
	Get() int
	Inc()
}

func run(inc Incrementer) {
	inc.Inc()
}

func main() {
	c := &Counter{}
	run(c) // *Counter's method set includes both Get and Inc
	fmt.Println(c.Get()) // 1
}
```

**Why it works / Explanation:** Making every method on `Counter` use a pointer receiver means `*Counter` (and only `*Counter`) has a complete, consistent method set. Callers always work with `*Counter`, so there's never a question of which methods are "missing" on a plain value.

**Design principle:** Pick value or pointer receivers for a type deliberately, based on whether it has mutable state or is expensive to copy, then use that receiver kind for *every* method on the type — consistency avoids accidental interface-satisfaction failures.

---

## 2. Struct embedding ambiguity and accidental shadowing

**The Problem:** When a struct embeds two types that both define a method (or field) with the same name, Go doesn't reject the embedding — it compiles fine right up until you actually call the ambiguous selector, at which point you get a compile error far from the embedding declaration. The mirror-image problem is shadowing: giving the outer struct its own method with the same name as a promoted one silently wins, with no warning that anything was overridden.

**❌ Bad**
```go
package main

import "fmt"

type Reader struct{}

func (Reader) Name() string { return "reader" }

type Writer struct{}

func (Writer) Name() string { return "writer" }

type ReadWriter struct {
	Reader
	Writer
}

func main() {
	rw := ReadWriter{}     // compiles fine - the ambiguity isn't detected here
	fmt.Println(rw.Name()) // BUG: compile error only here - "ambiguous selector rw.Name"
}
```

**Why it's wrong:**
- `ReadWriter{}` and even `var rw ReadWriter` compile without complaint; the error surfaces only at the ambiguous call site, which could be in a different file or written by someone unaware two embedded types collide.
- The same mechanism applies to fields, and to shadowing: if `ReadWriter` later grows its own `Name` field or method, it silently wins over *both* embedded ones with zero compiler warning — callers get behavior they didn't ask for.

**✅ Good**
```go
package main

import "fmt"

type Reader struct{}

func (Reader) Name() string { return "reader" }

type Writer struct{}

func (Writer) Name() string { return "writer" }

type ReadWriter struct {
	Reader
	Writer
}

func (rw ReadWriter) Name() string {
	return rw.Reader.Name() + "/" + rw.Writer.Name() // explicit, unambiguous resolution
}

func main() {
	rw := ReadWriter{}
	fmt.Println(rw.Name()) // "reader/writer" - no ambiguity, no accidental shadowing
}
```

**Why it works / Explanation:** Defining `Name` directly on `ReadWriter` resolves the diamond by making the outer type the single, unambiguous owner of that selector. Any "shadowing" is now intentional and documented in the outer method's body, rather than an accident of embedding order.

**Design principle:** Prefer shallow, single-purpose embedding; when two embedded types can plausibly collide on a name, resolve it explicitly on the outer type instead of hoping callers never trigger the ambiguity.

---

## 3. Struct comparability panics at runtime

**The Problem:** Go structs are comparable with `==` only if every field is comparable — slices, maps, and funcs are not. The compiler enforces this for a *statically typed* struct comparison, but when two struct values are boxed into `any` (or any interface type), the compiler allows the `==` because interface types are always comparable at compile time. The panic only happens at runtime, and only if the dynamic type turns out to be uncomparable.

**❌ Bad**
```go
package main

import "fmt"

type Config struct {
	Name string
	Tags []string // slice field makes Config itself uncomparable
}

func main() {
	var a, b any
	a = Config{Name: "x", Tags: []string{"a"}}
	b = Config{Name: "x", Tags: []string{"a"}}
	fmt.Println(a == b) // BUG: panics at runtime: comparing uncomparable type main.Config
}
```

**Why it's wrong:**
- This compiles cleanly — `a == b` is legal syntax for two `any` values — so there is no compiler warning anywhere in the code.
- The panic only fires at runtime, and only for this specific dynamic type; a similar comparison with a different, fully-comparable struct type would work fine, making the bug easy to miss in testing if the slice field is only populated in some code paths.
- This commonly bites when structs end up as map keys, switch cases, or arguments to generic equality helpers that operate on `any`.

**✅ Good**
```go
package main

import "fmt"

type Config struct {
	Name string
	Tags []string
}

func equal(a, b Config) bool {
	if a.Name != b.Name || len(a.Tags) != len(b.Tags) {
		return false
	}
	for i := range a.Tags {
		if a.Tags[i] != b.Tags[i] {
			return false
		}
	}
	return true
}

func main() {
	a := Config{Name: "x", Tags: []string{"a"}}
	b := Config{Name: "x", Tags: []string{"a"}}
	fmt.Println(equal(a, b)) // true - no panic, comparison logic is explicit and type-safe
}
```

**Why it works / Explanation:** Writing an explicit `equal` function that takes concrete `Config` values sidesteps interface comparison entirely — there is no `any` in sight, so there is no runtime comparability check to fail. The compiler would in fact reject `a == b` directly on two `Config` values at compile time, which is strictly better than discovering the problem in production.

**Design principle:** Avoid relying on `==` for types that contain slices, maps, or funcs; if a type must go through `any`, write and use an explicit equality method instead of trusting the interface comparison operator.

---

## 4. Copying structs that contain `sync.Mutex`

**The Problem:** Copying a struct that embeds a `sync.Mutex` (or any other synchronization primitive) copies the lock's internal state too. The two copies are now two independent, uncoordinated locks — locking one does nothing to protect access to the other, silently destroying the mutual exclusion the mutex was supposed to provide.

**❌ Bad**
```go
package main

import "sync"

type Counter struct {
	mu    sync.Mutex
	count int
}

func (c *Counter) Inc() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.count++
}

func main() {
	c := Counter{}
	c2 := c // BUG: copies the mutex along with the struct
	c.Inc()
	c2.Inc() // locks a completely independent Mutex - no mutual exclusion between c and c2
}
```

**Why it's wrong:**
- `c` and `c2` now have separate, independently-initialized mutexes even though they started from the "same" data — any invariant the mutex was protecting can be violated by concurrent access through both copies.
- `go vet` flags this exact pattern: running `go vet` on this file reports `assignment copies lock value to c2: main.Counter contains sync.Mutex`. Ignoring or not running `go vet` in CI means this ships silently.
- The bug is invisible in single-threaded tests and often only manifests as data corruption under real concurrent load in production.

**✅ Good**
```go
package main

import "sync"

type Counter struct {
	mu    sync.Mutex
	count int
}

func (c *Counter) Inc() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.count++
}

func process(c *Counter) { // always take a pointer - never copy a Counter by value
	c.Inc()
}

func main() {
	c := &Counter{}
	process(c)
	process(c)
}
```

**Why it works / Explanation:** By only ever handling `*Counter`, there is exactly one `sync.Mutex` in existence for that logical counter, so every lock/unlock pair coordinates against the same state. Passing structs containing locks (or other no-copy types like `sync.WaitGroup`) by pointer is the only safe option.

**Design principle:** Types containing mutexes (or anything documented as "must not be copied") should never be passed, returned, or assigned by value — enforce this with `go vet` in CI, not just code review.

---

## 5. Struct field alignment and padding

**The Problem:** The Go compiler inserts padding between struct fields so each field starts at an address matching its alignment requirement. Field order therefore directly affects a struct's total size — a careless ordering of mixed-size fields (bools next to int64s) can waste a large fraction of the struct's memory to padding, which adds up fast across millions of allocations.

**❌ Bad**
```go
package main

import (
	"fmt"
	"unsafe"
)

type Bad struct {
	A bool  // 1 byte, then 7 bytes of padding so B can start on an 8-byte boundary
	B int64 // 8 bytes
	C bool  // 1 byte, then 7 bytes of padding so D can start on an 8-byte boundary
	D int64 // 8 bytes
	E int32 // 4 bytes, then 4 bytes of trailing padding to align the struct's total size
}

func main() {
	fmt.Println(unsafe.Sizeof(Bad{})) // BUG: 40 bytes for only 22 bytes of real data
}
```

**Why it's wrong:**
- `unsafe.Sizeof(Bad{})` reports 40 bytes, even though the actual fields (`bool+int64+bool+int64+int32`) only need 22 bytes — 18 bytes (45%) are pure padding.
- At scale (a slice of a million `Bad` values, or a hot cache keyed by this struct), that wasted padding directly inflates memory usage and hurts cache locality, with no compiler warning that the layout is suboptimal.

**✅ Good**
```go
package main

import (
	"fmt"
	"unsafe"
)

type Good struct {
	B int64 // 8 bytes
	D int64 // 8 bytes
	E int32 // 4 bytes
	A bool  // 1 byte
	C bool  // 1 byte, then 2 bytes of trailing padding
}

func main() {
	fmt.Println(unsafe.Sizeof(Good{})) // 24 bytes - same fields, ordered largest to smallest
}
```

**Why it works / Explanation:** Ordering fields from largest alignment requirement to smallest packs them tightly, leaving only the minimal padding needed at the very end to satisfy the struct's overall alignment. Same data, 40% smaller footprint, with zero change in behavior.

**Design principle:** For structs allocated in bulk or on hot paths, order fields by descending size/alignment and verify with `unsafe.Sizeof`/`unsafe.Alignof` rather than guessing — the compiler will not reorder fields for you.

---

## 6. Large structs passed or returned by value

**The Problem:** Passing or returning a struct by value copies every byte of it on every call. For small structs this is free or even faster than indirection; for large structs (embedded arrays, many fields, or nested structs) it becomes a real, measurable cost, especially inside hot loops.

**❌ Bad**
```go
package main

import "fmt"

type Matrix struct {
	data [1024]float64 // 8 KB
}

func sum(m Matrix) float64 { // BUG: copies all 8 KB on every call
	total := 0.0
	for _, v := range m.data {
		total += v
	}
	return total
}

func main() {
	var m Matrix
	fmt.Println(sum(m))
}
```

**Why it's wrong:**
- Every call to `sum` copies 8 KB onto the stack before doing any work — call it in a loop and the copying cost alone can dominate the function's runtime.
- Larger structs also increase stack growth pressure and can force escapes to the heap in surrounding code, compounding the cost.

**✅ Good**
```go
package main

import "fmt"

type Matrix struct {
	data [1024]float64 // 8 KB
}

func sum(m *Matrix) float64 { // only 8 bytes (a pointer) copied, regardless of Matrix's size
	total := 0.0
	for _, v := range m.data {
		total += v
	}
	return total
}

func main() {
	var m Matrix
	fmt.Println(sum(&m))
}
```

**Why it works / Explanation:** Passing a pointer copies a single machine word no matter how large the underlying struct is. The tradeoff is that the callee can now see mutations made elsewhere through the same pointer — for large, mutable, or long-lived data that's usually fine or even desired, but it does give up the aliasing-freedom that value semantics provide.

**Design principle:** Default to value semantics for small, immutable structs (safety, no aliasing, no heap pressure); switch to pointers once a struct is large or must be shared/mutated — profile before assuming either direction is "faster."

---

## 7. Violating "make the zero value useful"

**The Problem:** A well-designed Go type works correctly the moment it's declared, with no explicit initialization — this is why `sync.Mutex`, `bytes.Buffer`, and `strings.Builder` all function correctly at their zero value. A type that panics or misbehaves unless a `New()`/`Init()` constructor is called first violates this idiom and creates a trap for any caller who reasonably assumes `var x T` is safe to use.

**❌ Bad**
```go
package main

type Cache struct {
	m map[string]string
}

func (c *Cache) Set(key, val string) {
	c.m[key] = val // BUG: panics - assignment to entry in nil map, since m was never initialized
}

func main() {
	c := Cache{} // looks perfectly usable - it isn't
	c.Set("a", "1")
}
```

**Why it's wrong:**
- `Cache{}` is valid Go and compiles without any hint that it's unsafe to use directly; the panic ("assignment to entry in nil map") only happens the first time `Set` is called.
- Every caller of this type now has to know an undocumented rule ("call `NewCache()` first") that the type system does nothing to enforce.

**✅ Good**
```go
package main

import "fmt"

type Cache struct {
	m map[string]string
}

func (c *Cache) Set(key, val string) {
	if c.m == nil {
		c.m = make(map[string]string) // lazily initialize on first use
	}
	c.m[key] = val
}

func main() {
	c := Cache{} // zero value now works correctly, no constructor required
	c.Set("a", "1")
	fmt.Println(c.m["a"])
}
```

**Why it works / Explanation:** Lazily initializing the map inside the method that needs it means the zero-valued `Cache` is fully functional. Callers never need to remember a special construction step, and there's no window where an "empty-looking" value is actually unsafe.

**Design principle:** Design types so `var x T` (or `T{}`) is immediately safe to use — reserve constructors for cases where meaningful configuration is genuinely required, not as a workaround for an unsafe zero value.

---

## 8. Embedding to fake inheritance

**The Problem:** Embedding a concrete type promotes its methods onto the outer struct, which looks like inheritance — but Go embedding has no dynamic dispatch. If the embedded type's own method calls another of its own methods, that call is resolved statically against the embedded type, even if the outer type has "overridden" that method. This surprises anyone coming from a language with virtual method calls.

**❌ Bad**
```go
package main

import "fmt"

type Animal struct{}

func (a Animal) Speak() string { return "..." }

func (a Animal) Greet() string {
	return "Hello, I say: " + a.Speak() // always calls Animal.Speak - no virtual dispatch through embedding
}

type Dog struct {
	Animal
}

func (d Dog) Speak() string { return "Woof" }

func main() {
	d := Dog{}
	fmt.Println(d.Greet()) // BUG: prints "Hello, I say: ...", NOT "Hello, I say: Woof"
}
```

**Why it's wrong:**
- `Dog.Speak` never runs when `Greet` is called, even though it looks like `Dog` "overrides" `Speak` — `Animal.Greet` calls `a.Speak()` where `a` is statically typed as `Animal`, full stop.
- This is a silent logic bug, not a compile error or panic: the code runs and produces a plausible-looking (but wrong) result, which is exactly the kind of mistake that survives into production.

**✅ Good**
```go
package main

import "fmt"

type Speaker interface {
	Speak() string
}

type Greeter struct {
	Speaker
}

func (g Greeter) Greet() string {
	return "Hello, I say: " + g.Speaker.Speak() // dispatches through the interface value at runtime
}

type Dog struct{}

func (Dog) Speak() string { return "Woof" }

func main() {
	g := Greeter{Speaker: Dog{}}
	fmt.Println(g.Greet()) // "Hello, I say: Woof"
}
```

**Why it works / Explanation:** Composing with an *interface* field instead of a concrete embedded type gets genuine dynamic dispatch: `g.Speaker.Speak()` calls whatever concrete implementation was stored in `Speaker` at construction time, so swapping in `Dog` (or anything else implementing `Speaker`) changes the behavior of `Greet` as expected.

**Design principle:** Go embedding is composition and method promotion, not inheritance — when you need "override" semantics, compose with an interface, not a concrete embedded type.

---

## 9. Unexported fields and tag ordering silently affecting serialization

**The Problem:** `encoding/json` and most reflection-based tools (database mappers, validators, config loaders) can only see a struct's *exported* fields. An unexported field is invisible to them by design — no error, no warning, the data is simply never read or written.

**❌ Bad**
```go
package main

import (
	"encoding/json"
	"fmt"
)

type User struct {
	Name string
	age  int // unexported - encoding/json can't see it at all
}

func main() {
	u := User{Name: "Ann", age: 30}
	b, _ := json.Marshal(u)
	fmt.Println(string(b)) // BUG: {"Name":"Ann"} - age silently vanished, no error
}
```

**Why it's wrong:**
- `json.Marshal` returns a perfectly valid, error-free result — there is nothing to catch in a test that only checks `err == nil`.
- The same silent omission applies to any reflection-driven library (ORMs, struct-tag-based validators, config decoders), so a field can quietly fail to round-trip through several layers of a system before anyone notices data is missing.

**✅ Good**
```go
package main

import (
	"encoding/json"
	"fmt"
)

type User struct {
	Name string `json:"name"`
	Age  int    `json:"age"` // exported, and explicitly tagged
}

func main() {
	u := User{Name: "Ann", Age: 30}
	b, _ := json.Marshal(u)
	fmt.Println(string(b)) // {"name":"Ann","age":30}
}
```

**Why it works / Explanation:** Exporting the field makes it visible to reflection-based tooling at all; the tag then controls exactly how it's named in the serialized form, independent of the Go field name.

**Design principle:** Any field that must survive serialization, persistence, or reflection-based processing must be exported — treat "does this need to leave the struct" as the deciding factor for capitalization, not just internal convenience.

---

## 10. Struct tags with typos

**The Problem:** Struct tags are just string literals attached to a field — the compiler checks that the tag is syntactically well-formed, but it has no idea what `json`, `db`, or any other tag key is supposed to mean, so a typo in the tag value compiles cleanly and fails only at runtime, as silently wrong data.

**❌ Bad**
```go
package main

import (
	"encoding/json"
	"fmt"
)

type Product struct {
	Name  string  `json:"neme"` // BUG: typo - should be "name"
	Price float64 `json:"price"`
}

func main() {
	input := []byte(`{"name":"Widget","price":9.99}`)
	var p Product
	json.Unmarshal(input, &p)
	fmt.Printf("%+v\n", p) // BUG: {Name: Price:9.99} - Name silently stayed empty, no error
}
```

**Why it's wrong:**
- `json.Unmarshal` returns `nil` error — the input JSON key `"name"` simply doesn't match the tag `"neme"`, so the field is left at its zero value with no indication anything went wrong.
- The bug is purely in a string literal, so nothing in the type system, `go build`, or a superficial `go vet` run catches it; it typically surfaces as "why is this field always empty in production" days or weeks later.

**✅ Good**
```go
package main

import (
	"encoding/json"
	"fmt"
)

type Product struct {
	Name  string  `json:"name"`
	Price float64 `json:"price"`
}

func main() {
	input := []byte(`{"name":"Widget","price":9.99}`)
	var p Product
	json.Unmarshal(input, &p)
	fmt.Printf("%+v\n", p) // {Name:Widget Price:9.99}
}
```

**Why it works / Explanation:** Matching the tag exactly to the wire-format key lets `Unmarshal` populate the field correctly. Beyond careful review, the reliable defense is a round-trip test (marshal then unmarshal, or unmarshal a known fixture and assert on the populated fields) that would fail the moment the tag drifts from the expected key.

**Design principle:** Treat struct tags as untyped, unchecked strings — protect them with round-trip serialization tests or a struct-tag linter (e.g. `staticcheck`), since the compiler will never validate their semantic correctness.

---

## 11. Nil embedded pointers panicking on promoted method calls

**The Problem:** Embedding a *pointer* type (rather than a value type) means the embedded field's zero value is `nil`. Any promoted method call on the outer struct dereferences that nil pointer inside the embedded type's method — the outer struct looks perfectly constructed, but using it panics.

**❌ Bad**
```go
package main

import "fmt"

type Engine struct {
	HorsePower int
}

func (e *Engine) Start() string {
	return fmt.Sprintf("starting %d hp engine", e.HorsePower)
}

type Car struct {
	*Engine // embedded pointer - zero value is nil
	Model string
}

func main() {
	c := Car{Model: "Tesla"} // Engine left nil
	fmt.Println(c.Start())   // BUG: panics - nil pointer dereference inside the promoted method
}
```

**Why it's wrong:**
- `Car{Model: "Tesla"}` compiles and looks like a complete, valid value — nothing about the literal signals that `Engine` is missing.
- The panic happens inside `Engine.Start`, on the line `e.HorsePower`, which can be confusing to debug because the stack trace points into a method the caller of `Car{...}` never directly wrote or saw.

**✅ Good**
```go
package main

import "fmt"

type Engine struct {
	HorsePower int
}

func (e *Engine) Start() string {
	return fmt.Sprintf("starting %d hp engine", e.HorsePower)
}

type Car struct {
	*Engine
	Model string
}

func NewCar(model string, hp int) Car {
	return Car{
		Engine: &Engine{HorsePower: hp}, // always initialize embedded pointers
		Model:  model,
	}
}

func main() {
	c := NewCar("Tesla", 500)
	fmt.Println(c.Start()) // "starting 500 hp engine"
}
```

**Why it works / Explanation:** Routing construction through `NewCar` guarantees the embedded `*Engine` is never nil by the time any promoted method is called. Where a constructor isn't feasible, embedding the value type (`Engine` instead of `*Engine`) removes the nil possibility entirely, at the cost of a larger struct.

**Design principle:** Zero-value safety extends to embedded pointers — never leave a promoted-method-bearing pointer field nil by default; either embed by value or provide a constructor that guarantees initialization.

---

## Key Takeaways
- Use the same receiver kind (value or pointer) for every method on a type to avoid confusing interface-satisfaction failures.
- Diamond method/field collisions from embedding compile fine until called, and shadowing overrides embedded members silently — resolve ambiguous names explicitly on the outer type.
- Comparing `any` values that wrap structs with slice/map/func fields compiles but panics at runtime; write explicit equality functions instead.
- Never copy a struct containing `sync.Mutex` or similar no-copy types — always pass by pointer, and enforce this with `go vet`.
- Poorly ordered struct fields waste memory to padding; order fields by descending size and verify with `unsafe.Sizeof`.
- Passing/returning large structs by value copies their full size on every call — use pointers for large or hot-path structs, values for small immutable ones.
- Design the zero value to be immediately usable, following the example of `sync.Mutex`, `bytes.Buffer`, and `strings.Builder`.
- Go embedding gives you method promotion, not dynamic dispatch — use interface composition when you need real "override" behavior.
- Unexported fields are invisible to `encoding/json` and other reflection-based tooling, with no warning that data is being dropped.
- Struct tags are unchecked strings — a typo compiles cleanly and silently breaks serialization; protect against it with round-trip tests.
- Embedding a pointer type leaves a nil field by default; promoted method calls on an uninitialized embedded pointer panic.

# Generics Gotchas

Go generics (introduced in 1.18) let you write one function or type that works over many concrete types, but they come with sharp edges that aren't obvious from the tutorials: constraint syntax that silently excludes types you meant to support, nil-comparison rules that differ from ordinary Go, and a temptation to reach for type parameters when a plain function would read better. Misusing generics tends to produce code that's harder to read than the interface-based code it replaced, or compile errors that are confusing until you understand the underlying rule. This doc walks through the mistakes that show up most often once generics leave toy examples and hit a real codebase.

## 1. Overusing Generics Where a Concrete Type Would Do

**The Problem:** It's tempting to make every new helper function generic "just in case," even when it only has one real caller with one real type today. The result is extra cognitive overhead — readers have to parse type parameters and constraint unions to understand code that never varies.

**❌ Bad**
```go
type Numeric interface {
	~int | ~int8 | ~int16 | ~int32 | ~int64 |
		~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 |
		~float32 | ~float64
}

// SumOrderTotals is only ever called with []int64 (amounts in cents)
// anywhere in this codebase.
func SumOrderTotals[T Numeric](vals []T) T {
	var total T
	for _, v := range vals {
		total += v
	}
	return total
}

func main() {
	cents := []int64{1099, 2500, 750}
	fmt.Println(SumOrderTotals(cents))
}
```

**Why it's wrong:**
- A reader has to parse a twelve-type constraint union just to understand a function that, in practice, only ever handles `int64` cents — the generality adds nothing but reading time.
- The wide constraint invites misuse: a future caller could pass `[]float64` and silently introduce floating-point rounding error into a money calculation, a bug the narrower concrete signature would have prevented outright.

**✅ Good**
```go
// SumOrderTotals sums order totals expressed in cents.
func SumOrderTotals(vals []int64) int64 {
	var total int64
	for _, v := range vals {
		total += v
	}
	return total
}
```

**Why it works / Explanation:** The concrete signature says exactly what the function does and what it accepts — no constraint to decode, no unintended instantiations possible. If a second, genuinely different caller shows up later (say, `[]int32` durations), that's the moment to introduce a type parameter, informed by two real use cases instead of a guess.

**Design principle:** Clarity over cleverness (a form of YAGNI) — add a type parameter when you have two or more concrete call sites that need it today, not because the language makes it possible.

---

## 2. Constraint Confusion: `~T` vs Plain `T`

**The Problem:** A constraint element written as a bare type name (e.g. `int`) matches only that exact type. It does **not** match user-defined named types whose underlying type is `int`, even though those types are otherwise usable anywhere an `int` is. Forgetting the `~` (approximation element) is one of the most common generics compile errors.

**❌ Bad**
```go
type Ordered interface {
	int // exact type only
}

func Max[T Ordered](a, b T) T {
	if a > b {
		return a
	}
	return b
}

type UserID int

func main() {
	var a, b UserID = 5, 9
	fmt.Println(Max(a, b))
	// BUG: compile error:
	// UserID does not satisfy Ordered (possibly missing ~ for int in constraint Ordered)
}
```

**Why it's wrong:**
- `UserID` has underlying type `int` and supports every operation `int` does, but the constraint `interface{ int }` has a type set containing only the single type `int` — `UserID` is a distinct named type and is excluded.
- This bites hardest with domain types like `UserID`, `Cents`, or `Seconds` — exactly the named integer/float wrapper types production code tends to accumulate — so the constraint breaks for the very types most likely to be passed in.

**✅ Good**
```go
type Ordered interface {
	~int
}

func Max[T Ordered](a, b T) T {
	if a > b {
		return a
	}
	return b
}

type UserID int

func main() {
	var a, b UserID = 5, 9
	fmt.Println(Max(a, b)) // 9 -- compiles: UserID's underlying type is int
}
```

**Why it works / Explanation:** `~int` (the "approximation element") expands the type set to every type whose *underlying* type is `int`, including `int` itself and any named type like `UserID`. Use `~T` by default in constraints you author unless you have a specific reason to lock the constraint to the exact type `T`.

**Design principle:** Prefer approximation elements (`~T`) in constraints you author, so the constraint matches how Go programmers actually use named types, not just built-in ones.

---

## 3. `comparable` Guarantees Less Than People Assume

**The Problem:** `comparable` only guarantees a type supports `==`/`!=` and can be used as a map key. It says nothing about ordering (`<`, `>`) and nothing about deep equality of nested slices, maps, or funcs — a struct containing a slice field is *not* comparable, full stop, and will fail to satisfy `comparable` at compile time.

**❌ Bad**
```go
type Cache[K comparable, V any] struct {
	mu   sync.Mutex
	data map[K]V
}

func NewCache[K comparable, V any]() *Cache[K, V] {
	return &Cache[K, V]{data: make(map[K]V)}
}

func (c *Cache[K, V]) Get(key K) (V, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	v, ok := c.data[key]
	return v, ok
}

type SearchFilter struct {
	Category string
	Tags     []string // slice field -- makes SearchFilter non-comparable
}

func main() {
	c := NewCache[SearchFilter, []string]()
	// BUG: compile error: SearchFilter does not satisfy comparable
	_ = c
}
```

**Why it's wrong:**
- Go can't compile `==` for a struct containing a slice (or map, or func) field, so `comparable` rejects `SearchFilter` outright — this only surfaces once you try to instantiate `Cache[SearchFilter, ...]`, which can be far from where `SearchFilter` was originally defined.
- Even for types that *do* satisfy `comparable`, developers often assume it means "deeply equatable" (like two slices with the same elements being `==`) — it doesn't; `comparable` is strictly about `==`/`!=` validity and map-key usability, not structural/deep equality, and it says nothing about ordering either.

**✅ Good**
```go
type SearchFilter struct {
	Category string
	Tags     []string
}

// CacheKey derives a comparable key from a non-comparable struct.
func (f SearchFilter) CacheKey() string {
	return f.Category + "|" + strings.Join(f.Tags, ",")
}

func main() {
	c := NewCache[string, []string]()
	filter := SearchFilter{Category: "books", Tags: []string{"scifi", "used"}}
	c.data[filter.CacheKey()] = []string{"result1", "result2"} // via a Set method in real code
}
```

**Why it works / Explanation:** Instead of forcing the non-comparable struct itself into a generic `comparable` slot, derive a comparable (here, `string`) key from it. This sidesteps the constraint entirely and is usually what you wanted anyway — a stable cache key, not a struct you'll never actually compare with `==`.

**Design principle:** Treat `comparable` as "safe for `==` and map keys," full stop — reach for an explicit key/hash function whenever the natural type isn't comparable, rather than fighting the constraint.

---

## 4. Assuming Generics Are Automatically Faster Than `interface{}`

**The Problem:** Go's generics use a hybrid compilation strategy ("GC shape stenciling") that's often, but not always, faster than the equivalent `interface{}`/`any`-based code with type assertions. When the generic code still ends up calling an interface method internally, you get the same dynamic dispatch either way — the type parameter alone buys you nothing at runtime.

**❌ Bad (an unverified assumption baked into the design)**
```go
type Shape interface {
	Area() float64
}

type Rect struct{ W, H float64 }

func (r Rect) Area() float64 { return r.W * r.H }

// "This is generic, so it must be faster than the interface-based version below."
func TotalArea[S Shape](shapes []S) float64 {
	var total float64
	for _, s := range shapes {
		total += s.Area() // still an interface method call under the hood
	}
	return total
}

func TotalAreaAny(shapes []Shape) float64 {
	var total float64
	for _, s := range shapes {
		total += s.Area()
	}
	return total
}
```

**Why it's wrong:**
- Constraining `S` to the interface `Shape` means `TotalArea`'s body still dispatches through `Shape`'s method table on every call to `.Area()` — the generic version didn't eliminate the interface, it just moved where the interface type appears in the signature.
- Benchmarking these two on a slice of a few thousand `Rect` values typically shows them within a few percent of each other — the assumed win doesn't materialize, and the generic version's real benefit here is avoiding a manual type assertion at the call boundary, not faster dispatch inside the loop.

**✅ Good (measure first)**
```go
func BenchmarkTotalArea(b *testing.B) {
	shapes := make([]Rect, 10_000)
	for i := range shapes {
		shapes[i] = Rect{W: 2, H: 3}
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		TotalArea(shapes)
	}
}

func BenchmarkTotalAreaAny(b *testing.B) {
	shapes := make([]Shape, 10_000)
	for i := range shapes {
		shapes[i] = Rect{W: 2, H: 3}
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		TotalAreaAny(shapes)
	}
}
```

**Why it works / Explanation:** Only a benchmark tells you whether a given generic function actually avoids interface dispatch (it can, when the constraint lets the compiler stencil out a concrete, monomorphized version operating on primitive kinds directly) or whether it's dispatching through an interface exactly like the `any` version, as `TotalArea` does here. Don't rewrite `any`-based code to use generics purely for performance without a benchmark proving there's a gain in your specific case.

**Design principle:** Measure, don't assume — the same rule that applies to any other optimization applies to generics; see the profiling/benchmarking notes elsewhere in this reference for how to set up a fair `go test -bench` comparison before committing to a rewrite.

---

## 5. No Generic Methods on Existing (or New) Types

**The Problem:** Go does not allow a method to introduce its own type parameters beyond the ones already bound by its receiver type. You cannot "add" a generic method to an existing concrete type, and even on a generic type, a method can only use the receiver's type parameters — not new ones of its own.

**❌ Bad**
```go
type Stack[T any] struct {
	items []T
}

func (s *Stack[T]) Push(v T) {
	s.items = append(s.items, v)
}

// BUG: does not compile -- methods cannot declare their own type parameters.
func (s *Stack[T]) MapTo[U any](f func(T) U) []U {
	out := make([]U, len(s.items))
	for i, v := range s.items {
		out[i] = f(v)
	}
	return out
}
```

**Why it's wrong:**
- The compiler rejects `MapTo` with an error to the effect of "methods cannot have type parameters" — `U` isn't bound anywhere on `Stack[T]`'s receiver, and Go's method model has no mechanism to introduce a new type parameter per call.
- This isn't a syntax slip you can work around with different punctuation; it's a deliberate language limitation, so the fix has to be structural, not cosmetic.

**✅ Good**
```go
type Stack[T any] struct {
	items []T
}

func (s *Stack[T]) Push(v T) {
	s.items = append(s.items, v)
}

// MapStack is a free function instead of a method, so it's free to
// introduce its own type parameter U.
func MapStack[T, U any](s *Stack[T], f func(T) U) []U {
	out := make([]U, len(s.items))
	for i, v := range s.items {
		out[i] = f(v)
	}
	return out
}

func main() {
	s := &Stack[int]{items: []int{1, 2, 3}}
	strs := MapStack(s, func(n int) string { return fmt.Sprintf("#%d", n) })
	fmt.Println(strs) // [#1 #2 #3]
}
```

**Why it works / Explanation:** Moving the transformation to a standalone generic function sidesteps the restriction entirely — `MapStack` can bind `T` and `U` independently, since a function's type parameters belong to the function itself, unconstrained by any receiver. This is the standard workaround anywhere you want a "generic method"-shaped operation in Go.

**Design principle:** Model receiver-bound generics (`Stack[T]`'s own methods) and free-standing generic transformations (`MapStack[T, U]`) as separate concerns — Go's type system keeps them separate on purpose.

---

## 6. Type Inference Can't Always Find the Type Argument

**The Problem:** Go's type inference works from the types of the *arguments* you pass. If a type parameter appears only in the return type — never in a parameter — there's nothing for the compiler to infer from, and you must supply the type argument explicitly.

**❌ Bad**
```go
func Decode[T any](data []byte) (T, error) {
	var v T
	err := json.Unmarshal(data, &v)
	return v, err
}

type User struct {
	Name string `json:"name"`
}

func main() {
	data := []byte(`{"name":"Ada"}`)
	user, err := Decode(data)
	// BUG: compile error: cannot infer T (T only appears in the return type)
	_ = user
	_ = err
}
```

**Why it's wrong:**
- `Decode`'s only parameter is `[]byte`; nothing in the call `Decode(data)` tells the compiler what `T` should be, since `T` never appears in an argument's type — only in the return type, which inference doesn't consult.
- This is an extremely common shape for generic constructors, decoders, and factory functions, so the failure mode shows up constantly in real code, not just contrived examples.

**✅ Good**
```go
func main() {
	data := []byte(`{"name":"Ada"}`)
	user, err := Decode[User](data) // explicit type argument
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println(user.Name) // Ada
}
```

**Why it works / Explanation:** Writing `Decode[User](data)` supplies the type argument explicitly, which is required whenever the type parameter can't be inferred from the arguments. This is not a workaround or a hack — it's the normal, expected way to call generic functions whose type parameter only appears in the return position.

**Design principle:** Know the shape of Go's type inference (arguments in, not return types) so you're not surprised when explicit instantiation (`Func[Type](args)`) is required rather than optional.

---

## 7. Comparing a Generic Type Parameter to `nil`

**The Problem:** Inside a generic function body, `v == nil` only compiles if `T`'s constraint guarantees `T` is a nilable kind (pointer, interface, slice, map, channel, or func). With an unconstrained `any` (or any constraint that permits non-nilable types like `int`), the comparison fails to compile, because there's no type-set guarantee that `nil` is a valid value of `T`.

**❌ Bad**
```go
func FirstNonNil[T any](vals []T) (T, bool) {
	for _, v := range vals {
		if v == nil { // BUG: compile error:
			// invalid operation: v == nil (mismatched types T and untyped nil)
			continue
		}
		return v, true
	}
	var zero T
	return zero, false
}
```

**Why it's wrong:**
- `T any` includes types like `int` for which `nil` is not a valid value at all, so the compiler can't allow `v == nil` unconditionally — it would become a type error the moment someone instantiated `FirstNonNil[int]`.
- Developers coming from languages with a universal "null" often expect `== nil` to just work against any generic value; in Go it depends entirely on the constraint.

**✅ Good**
```go
func FirstNonZero[T comparable](vals []T) (T, bool) {
	var zero T
	for _, v := range vals {
		if v != zero { // compare against the type's own zero value instead of nil
			return v, true
		}
	}
	return zero, false
}

func main() {
	ptrs := []*int{nil, nil, new(int)}
	v, ok := FirstNonZero(ptrs)
	fmt.Println(v, ok) // a non-nil *int, true
}
```

**Why it works / Explanation:** Comparing against `var zero T` works for any `comparable` type, nilable or not — for pointers/interfaces/slices-as-elements the "zero value" is `nil` anyway, so this subsumes the nil check for nilable types while also being meaningful for `int`, `string`, and friends. If you specifically need "is this nil" semantics for pointer/interface-like kinds only, constrain `T` to a nilable-only constraint rather than accepting a bare `any`.

**Design principle:** Zero-value comparison (`comparable` + `var zero T`) is the generic-safe generalization of a nil check — reach for it instead of assuming `nil` is universally comparable.

---

## 8. Reinventing `slices` and `maps` Helpers the Standard Library Already Has

**The Problem:** Since Go 1.21, the standard library ships generic `slices` and `maps` packages covering the utility functions most codebases used to hand-roll (`Contains`, `Index`, `Sort`, `Equal`, and more). Writing your own generic version of these is easy to justify in the moment but adds a maintenance burden and a subtly different API for something that's now a language-provided primitive.

**❌ Bad**
```go
func ContainsString(list []string, target string) bool {
	for _, v := range list {
		if v == target {
			return true
		}
	}
	return false
}

func Keys[K comparable, V any](m map[K]V) []K {
	out := make([]K, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
```

**Why it's wrong:**
- This is exactly `slices.Contains` and (a slice-returning variant of) `maps.Keys`, reimplemented with no additional behavior — every caller now has two APIs to remember (the stdlib one used in some places, this one in others) instead of one.
- Hand-rolled versions don't get the scrutiny, benchmarking, or edge-case handling the stdlib versions have already received, and they don't improve as the standard library does.

**✅ Good**
```go
import "slices"

func main() {
	list := []string{"a", "b", "c"}
	fmt.Println(slices.Contains(list, "b")) // true

	m := map[string]int{"a": 1, "b": 2}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	fmt.Println(keys) // [a b]
}
```

**Why it works / Explanation:** `slices.Contains`, `slices.Sort`, `slices.Index`, and friends have been in the standard library since Go 1.21 and cover the vast majority of "generic slice helper" needs directly. (`maps.Keys`/`maps.Values` exist too, but as of Go 1.23 they return iterators (`iter.Seq[K]`) rather than slices — wrap with `slices.Collect(maps.Keys(m))` if you need a `[]K`.) Check `slices` and `maps` before writing a new generic utility function; you're very likely duplicating something already there.

**Design principle:** Don't reinvent the standard library — a quick check of `slices`/`maps`/`cmp` before writing a generic helper saves you from maintaining a shadow copy of stdlib functionality.

---

## Key Takeaways
- Don't add type parameters to a function with only one real caller/type — a concrete signature is easier to read.
- Use `~T` (approximation element) in constraints you author so named types with the right underlying type aren't excluded; plain `T` matches only the exact type.
- `comparable` guarantees `==`/`!=` and map-key usability only — not ordering, not deep equality of nested slices/maps/funcs.
- Generics don't automatically beat `interface{}`-based dispatch — benchmark before assuming a rewrite is faster.
- Methods can't introduce their own type parameters; use a free generic function instead.
- Type inference works from argument types only — when a type parameter appears solely in the return type, you must instantiate explicitly (`Func[Type](args)`).
- `v == nil` doesn't compile for an unconstrained type parameter; compare against a zero value (`comparable` + `var zero T`) instead.
- Check the standard library's `slices`, `maps`, and `cmp` packages before writing your own generic utility function.

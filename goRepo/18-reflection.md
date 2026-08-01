# Reflection Gotchas

Reflection (the `reflect` package) lets Go code inspect and manipulate values whose types aren't known until runtime — essential for `encoding/json`, ORMs, and generic-feeling utilities written before Go had generics. But it trades away the compiler's type checking for runtime checks you must write yourself, it's substantially slower than static code, and its API is full of operations that panic on the wrong input. This doc covers the reflection mistakes that reach production: hot-path performance regressions, panics from unexported fields or wrong `Kind()` assumptions, `DeepEqual` surprises, and reflection-shaped solutions that generics or codegen would serve better.

## 1. Reflection in Hot Paths

**The Problem:** Every `reflect` operation — `ValueOf`, `.Field()`, `.Interface()`, `.Set()` — carries type-introspection overhead that a direct field access or static assignment simply doesn't pay. Code that looks innocuous in isolation (a generic "copy struct fields" helper) becomes a measurable hotspot the moment it runs per-request or per-item in a tight loop.

**❌ Bad**
```go
type User struct {
	ID    int
	Name  string
	Email string
}

// CopyFieldsReflect copies every field from src into dst using reflection,
// so it works for "any" struct pair with matching layout.
func CopyFieldsReflect(dst, src interface{}) {
	dv := reflect.ValueOf(dst).Elem()
	sv := reflect.ValueOf(src).Elem()
	for i := 0; i < sv.NumField(); i++ {
		dv.Field(i).Set(sv.Field(i))
	}
}

func handleRequest(src *User) *User {
	dst := &User{}
	CopyFieldsReflect(dst, src) // called on every incoming request
	return dst
}
```

**Why it's wrong:**
- Each call pays for `reflect.ValueOf`'s type-descriptor lookup, a loop over `NumField()` with per-field `Kind()` bookkeeping inside `Set()`, and none of it is inlinable — the compiler cannot see through the `interface{}` boundary to optimize any of this away.
- In practice this is commonly measured as one to two orders of magnitude slower than a direct struct copy, though the exact multiplier depends on struct shape and Go version — at request-handling scale, this reliably shows up as a hotspot in a CPU profile, not a "maybe."

**✅ Good**
```go
func CopyUser(dst, src *User) {
	*dst = *src // one struct assignment (a memmove under the hood), no type introspection
}

func handleRequest(src *User) *User {
	dst := &User{}
	CopyUser(dst, src)
	return dst
}
```

**Why it works / Explanation:** `*dst = *src` is resolved entirely at compile time — the compiler knows the exact field layout and emits a direct copy, with none of reflection's runtime bookkeeping. For known, fixed struct shapes, always prefer direct assignment (or a hand-written/generated copy function) over a "works for anything" reflective helper.

**Design principle:** Pay for generality only where you need it — reflection-based "works for any struct" helpers are a near-guaranteed hotspot at scale; measure with a profiler (see the profiling/benchmarking notes elsewhere in this reference) before deciding whether a specific reflective call is actually affordable in your workload.

---

## 2. Panics From `.Interface()` on Unexported Fields

**The Problem:** Calling `.Interface()` (or most other value-extracting methods) on a `reflect.Value` obtained from an unexported struct field panics at runtime. The compiler can't catch this for you, because the field is only "unexported" at the source level — reflection happily lets you *see* it, then refuses to let you *read* it.

**❌ Bad**
```go
type user struct {
	Name string
	id   int // unexported
}

func main() {
	u := user{Name: "Alice", id: 42}
	v := reflect.ValueOf(u)
	f := v.Field(1) // the unexported `id` field
	fmt.Println(f.Interface())
	// BUG: panics:
	// reflect: reflect.Value.Interface: cannot return value obtained from unexported field or method
}
```

**Why it's wrong:**
- `v.Field(1)` succeeds — reflection can locate and describe unexported fields — but the panic only happens later, at `.Interface()`, so the failure is one step removed from where the "mistake" actually is, making it easy to miss in code review.
- This surfaces most often in generic marshaling/mapping/logging utilities that walk every field of an arbitrary struct without checking exportedness first, and it will panic on any caller's type that happens to have a private field — a landmine for a library that doesn't control its callers' structs.

**✅ Good**
```go
func PublicFields(v interface{}) map[string]interface{} {
	rt := reflect.TypeOf(v)
	rv := reflect.ValueOf(v)
	out := make(map[string]interface{})
	for i := 0; i < rt.NumField(); i++ {
		field := rt.Field(i)
		if !field.IsExported() { // Go 1.17+; equivalent to field.PkgPath == ""
			continue // unexported -- reflection can't safely read this, so skip it
		}
		out[field.Name] = rv.Field(i).Interface()
	}
	return out
}
```

**Why it works / Explanation:** Checking `field.IsExported()` (or the older `field.PkgPath == ""` check) before calling `.Interface()` lets the function skip fields it has no business reading instead of crashing on them. If you truly must read an unexported field's value (rare, and usually a design smell), that requires the `unsafe` package to bypass the check entirely — a deliberate, dangerous, and easy-to-get-wrong escape hatch that should be reserved for extremely well-audited library internals, not everyday code.

**Design principle:** Reflection exposes structure, not access — always gate field reads on exportedness rather than assuming every field you can *see* is one you can *read*.

---

## 3. Wrong `Kind()` Assumptions

**The Problem:** Methods like `.Elem()`, `.Index()`, and `.Field()` are only valid for specific `Kind()`s (pointer/interface, slice/array/string, struct, respectively). Calling them without checking `Kind()` first panics the moment the input isn't the shape you assumed — and unlike a type assertion, there's no `, ok` form for most of these calls.

**❌ Bad**
```go
func FirstElement(v interface{}) interface{} {
	rv := reflect.ValueOf(v)
	return rv.Index(0).Interface()
	// BUG: panics if v isn't indexable, e.g.:
	// reflect: call of reflect.Value.Index on int Value
}

func main() {
	FirstElement(42) // panics
}
```

**Why it's wrong:**
- `Index` is only defined for `Slice`, `Array`, and `String` kinds; calling it on anything else (an `int`, a `struct`, a `map`) panics immediately, and the panic message is the only clue about what went wrong.
- This kind of helper is usually written to be "generic" over many caller inputs, which is exactly the situation where an unexpected `Kind()` is most likely to show up in production, from a caller the author never tested against.

**✅ Good**
```go
func FirstElement(v interface{}) (interface{}, error) {
	rv := reflect.ValueOf(v)
	switch rv.Kind() {
	case reflect.Slice, reflect.Array, reflect.String:
		if rv.Len() == 0 {
			return nil, fmt.Errorf("FirstElement: empty %s", rv.Kind())
		}
		return rv.Index(0).Interface(), nil
	default:
		return nil, fmt.Errorf("FirstElement: unsupported kind %s", rv.Kind())
	}
}
```

**Why it works / Explanation:** Checking `Kind()` up front turns a runtime panic into a regular, recoverable error — the caller finds out their input was the wrong shape without taking down the process. Any reflection-based function that accepts `interface{}` should validate `Kind()` (and often `Len()`, nil-ness, etc.) before calling kind-specific methods.

**Design principle:** Defensive `Kind()` checks are reflection's substitute for the compile-time type checking you've given up — never skip them on attacker- or caller-controlled input.

---

## 4. `reflect.DeepEqual` Pitfalls

**The Problem:** `reflect.DeepEqual` has several behaviors that surprise people expecting "structural equality": non-nil function values are *never* deeply equal to each other (even two references to the literal same function value), `NaN` fields are never equal to `NaN` (mirroring `NaN != NaN` at the float level), and unexported fields are included in the comparison — meaning two structs from a package you don't control can compare unequal for reasons you can't see or fix from outside that package.

**❌ Bad**
```go
type Config struct {
	Timeout float64
	OnError func(error)
}

func main() {
	a := Config{Timeout: math.NaN(), OnError: func(error) {}}
	b := Config{Timeout: math.NaN(), OnError: func(error) {}}

	fmt.Println(reflect.DeepEqual(a, b)) // false
	// Two compounding problems here:
	//   1. NaN != NaN, even under DeepEqual's float comparison.
	//   2. Non-nil func values are NEVER deeply equal, even if they wrap identical logic.
}
```

**Why it's wrong:**
- A test asserting `reflect.DeepEqual(want, got)` on a struct with a `float64` field will fail forever if that field can legitimately be `NaN` — there's no way to make two `NaN`s "equal" via `DeepEqual`, because it defers to ordinary float comparison semantics.
- Any struct with a `func` field (common for config structs with callback/hook fields) can never round-trip through `DeepEqual` as "equal" once both sides are non-nil, regardless of whether the funcs are behaviorally identical — this is easy to miss until a previously-passing test starts failing after an unrelated refactor introduces a callback field.

**✅ Good**
```go
import (
	"github.com/google/go-cmp/cmp"
	"github.com/google/go-cmp/cmp/cmpopts"
)

func TestConfigEqual(t *testing.T) {
	a := Config{Timeout: math.NaN(), OnError: func(error) {}}
	b := Config{Timeout: math.NaN(), OnError: func(error) {}}

	diff := cmp.Diff(a, b,
		cmpopts.EquateNaNs(),
		cmpopts.IgnoreFields(Config{}, "OnError"),
	)
	if diff != "" {
		t.Errorf("Config mismatch (-want +got):\n%s", diff)
	}
}
```

**Why it works / Explanation:** `go-cmp` (`github.com/google/go-cmp/cmp`) is explicit about the traps `DeepEqual` hides: it panics by default on incomparable fields like funcs (forcing you to consciously ignore or handle them via `cmpopts.IgnoreFields` or a custom `cmp.Comparer`) instead of silently reporting "not equal," and `cmpopts.EquateNaNs()` lets you opt in to treating `NaN == NaN` when that's the comparison semantics you actually want for a given test. Reserve `reflect.DeepEqual` for quick, informal checks, and reach for `go-cmp` in tests where you need to control exactly what "equal" means.

**Design principle:** Prefer explicit, configurable equality (`go-cmp`) over an implicit one (`DeepEqual`) whenever a type has funcs, floats, or externally-defined structs with unexported fields.

---

## 5. Reflection Where Generics or Codegen Would Do Better

**The Problem:** Reflection-based "works for anything" utilities — deep copy, deep merge, generic diffing — are tempting because they're written once. But for hot, well-known types, they trade away both compile-time type safety and runtime speed. Generics (for straightforward structural operations) or code generation (for anything type-specific, like `stringer`-style tools) usually produce faster, more correct code for the types that actually matter in production.

**❌ Bad**
```go
// DeepCopyReflect makes a deep copy of any struct value using reflection.
func DeepCopyReflect(dst, src interface{}) {
	copyValue(reflect.ValueOf(dst).Elem(), reflect.ValueOf(src).Elem())
}

func copyValue(dst, src reflect.Value) {
	switch src.Kind() {
	case reflect.Slice:
		dst.Set(reflect.MakeSlice(src.Type(), src.Len(), src.Len()))
		for i := 0; i < src.Len(); i++ {
			copyValue(dst.Index(i), src.Index(i))
		}
	case reflect.Struct:
		for i := 0; i < src.NumField(); i++ {
			copyValue(dst.Field(i), src.Field(i))
		}
	default:
		dst.Set(src)
	}
}
```

**Why it's wrong:**
- This "works for anything" but is slow (a full reflective tree-walk on every call), fragile (it doesn't handle maps, pointers, or unexported fields, and will panic or silently misbehave on any of those), and gives the compiler nothing to check — a field type mismatch is a runtime panic, not a build failure.
- For the small set of hot, well-known types actually copied in a request path, this reflective machinery is strictly worse on every axis than a type-specific alternative.

**✅ Good**
```go
type Order struct {
	ID    string
	Items []LineItem
}

// Clone is hand-written (or go generate-produced, e.g. via a "clone" tool
// modeled on stringer) for this specific, hot type.
func (o Order) Clone() Order {
	items := make([]LineItem, len(o.Items))
	copy(items, o.Items)
	return Order{ID: o.ID, Items: items}
}
```

**Why it works / Explanation:** A hand-written or generated `Clone` method compiles to direct field copies — no reflection cost, and the compiler catches it immediately if `Order`'s fields change and `Clone` falls out of sync. For simple, uniform cases (shallow-copying a slice, for instance), the generic stdlib helper `slices.Clone` already covers you without reflection at all. Reserve reflection-based generic deep-copy for genuinely dynamic, type-unknown-at-compile-time situations (e.g. a generic config-merging library) — not for the fixed set of domain types your own service defines.

**Design principle:** Match the tool to how dynamic the problem really is — codegen and generics for known, hot types; reflection only for genuinely type-erased, dynamic situations.

---

## 6. Struct Tag Reflection Mistakes

**The Problem:** Custom tag-driven logic (validators, mappers, ORM-style struct scanners) that loops over `NumField()` naively tends to miss three real-world cases: embedded/anonymous fields (which should usually be recursed into, not treated as opaque leaf values), unexported fields (which panic on `.Interface()`), and fields with no tag at all (which should be skipped, not treated as "required" by default).

**❌ Bad**
```go
type Address struct {
	City string `validate:"required"`
}

type Person struct {
	Address        // embedded/anonymous
	Name    string `validate:"required"`
	age     int    // unexported, no tag
}

func Validate(v interface{}) error {
	rt := reflect.TypeOf(v)
	rv := reflect.ValueOf(v)
	for i := 0; i < rt.NumField(); i++ {
		tag := rt.Field(i).Tag.Get("validate")
		val := rv.Field(i).Interface()
		// BUG: panics on `age` (unexported), and never validates
		// Address.City since embedded structs are never recursed into.
		if tag == "required" && val == "" {
			return fmt.Errorf("%s is required", rt.Field(i).Name)
		}
	}
	return nil
}
```

**Why it's wrong:**
- `rv.Field(i).Interface()` panics the instant it reaches the unexported `age` field, so `Validate` crashes on any `Person`, not just malformed ones.
- Even without the panic, `Address`'s embedded `City` field is never checked — the loop treats `Address` as a single opaque field and never looks inside it, so a required-but-empty `City` silently passes validation.

**✅ Good**
```go
func Validate(v interface{}) error {
	rt := reflect.TypeOf(v)
	rv := reflect.ValueOf(v)
	for i := 0; i < rt.NumField(); i++ {
		field := rt.Field(i)

		if field.Anonymous && field.Type.Kind() == reflect.Struct {
			if err := Validate(rv.Field(i).Interface()); err != nil { // recurse into embedded structs
				return err
			}
			continue
		}
		if !field.IsExported() {
			continue // unexported fields can't be read safely -- and shouldn't be validated externally anyway
		}
		tag, ok := field.Tag.Lookup("validate")
		if !ok || tag != "required" {
			continue // no tag (or a tag we don't recognize) -- don't guess at a default
		}
		if rv.Field(i).Kind() == reflect.String && rv.Field(i).String() == "" {
			return fmt.Errorf("%s is required", field.Name)
		}
	}
	return nil
}
```

**Why it works / Explanation:** Recursing into anonymous struct fields makes embedding transparent to validation (matching how Go treats embedded fields for method promotion), skipping unexported fields avoids the panic entirely, and using `Tag.Lookup` (which reports whether the tag key was present at all) instead of `Tag.Get` (which just returns `""` for both "absent" and "explicitly empty") avoids silently treating untagged fields as required.

**Design principle:** Tag-driven reflection code should treat "no tag," "unexported," and "embedded" as first-class cases to check for, not edge cases to discover in production.

---

## 7. Settability Panics

**The Problem:** `reflect.Value.Set*` methods require the `Value` to be *addressable* — which means it must have been obtained through a pointer's `.Elem()`, not directly from a plain (non-pointer) interface value. Calling `.Set()` (or `.SetString()`, `.SetInt()`, etc.) on a non-addressable `Value` panics.

**❌ Bad**
```go
type User struct {
	Name string
}

func Rename(v interface{}, name string) {
	rv := reflect.ValueOf(v)
	rv.FieldByName("Name").SetString(name)
	// BUG: panics:
	// reflect: reflect.Value.SetString using unaddressable value
}

func main() {
	u := User{Name: "old"}
	Rename(u, "new") // u passed by value -- rv is never addressable
}
```

**Why it's wrong:**
- Passing `u` (a value, not `&u`) into `interface{}` copies it into the interface, and `reflect.ValueOf` on that copy produces a `Value` with no addressable backing memory — there's nothing for `Set` to write through, so it panics rather than silently doing nothing.
- This is easy to trip on because the *read* side (`.FieldByName("Name").String()`) works fine on the same non-addressable `Value` — only the write side enforces addressability, so the mistake isn't visible until you specifically try to mutate.

**✅ Good**
```go
func Rename(v interface{}, name string) error {
	rv := reflect.ValueOf(v)
	if rv.Kind() != reflect.Ptr || rv.IsNil() {
		return fmt.Errorf("Rename: v must be a non-nil pointer")
	}
	rv = rv.Elem() // dereferencing a pointer's Value yields an addressable Value
	f := rv.FieldByName("Name")
	if !f.IsValid() || !f.CanSet() {
		return fmt.Errorf("Rename: no settable Name field")
	}
	f.SetString(name)
	return nil
}

func main() {
	u := &User{Name: "old"}
	if err := Rename(u, "new"); err != nil {
		log.Fatal(err)
	}
	fmt.Println(u.Name) // new
}
```

**Why it works / Explanation:** Requiring a pointer and calling `.Elem()` gives you a `Value` that points at the original memory, which is addressable and therefore settable. Checking `CanSet()` before calling `Set*` turns "wrong shape of input" into a returned error instead of a panic — the same defensive pattern as checking `Kind()` before kind-specific calls.

**Design principle:** Mutation through reflection always requires a pointer chain back to real memory — treat `CanSet()` as a mandatory guard, not an optional nicety.

---

## 8. Nil Interfaces and Invalid `reflect.Value`s

**The Problem:** `reflect.ValueOf(nil)` (or `reflect.ValueOf` of a nil interface with no concrete type) produces the zero `reflect.Value`, for which `IsValid()` returns `false`. Calling almost any other method on that zero `Value` — `Kind()` returns `Invalid` harmlessly, but `NumField()`, `Field()`, `Interface()`, and most others — panics.

**❌ Bad**
```go
func Describe(v interface{}) string {
	rv := reflect.ValueOf(v)
	return fmt.Sprintf("%d fields", rv.NumField())
	// BUG: panics if v is nil:
	// reflect: call of reflect.Value.NumField on zero Value
}

func main() {
	var err error // nil interface, no concrete type
	Describe(err) // panics
}
```

**Why it's wrong:**
- A nil `interface{}` argument is a completely ordinary, easy-to-produce input (an unset error, an absent optional field, a zero-value map lookup) — code that panics on it will eventually see it in production, not just in adversarial tests.
- The panic happens on the very first reflective call after `ValueOf`, so there's no opportunity to "notice" the problem partway through — the function needs to check for it before doing anything else.

**✅ Good**
```go
func Describe(v interface{}) string {
	rv := reflect.ValueOf(v)
	if !rv.IsValid() {
		return "<nil>"
	}
	if rv.Kind() != reflect.Struct {
		return fmt.Sprintf("<%s>", rv.Kind())
	}
	return fmt.Sprintf("%d fields", rv.NumField())
}
```

**Why it works / Explanation:** `IsValid()` is the one method that's always safe to call on any `reflect.Value`, including the zero `Value` — checking it first is the standard guard against nil-interface input before doing anything else with the value.

**Design principle:** Treat `IsValid()` as the first line of defense in any reflection-based function that accepts `interface{}` — it's the reflection equivalent of a nil check.

---

## Key Takeaways
- Reflection-based field copying is a near-guaranteed hotspot at scale; prefer direct assignment or generated code for known, hot types.
- `.Interface()` on an unexported field panics — check `field.IsExported()` before reading.
- Kind-specific methods (`.Index()`, `.Field()`, `.Elem()`) panic on the wrong `Kind()` — check `Kind()` first.
- `reflect.DeepEqual` treats non-nil funcs as always unequal and `NaN` as never equal to itself; prefer `go-cmp` for configurable test comparisons.
- For hot, well-known types, prefer generics or `go generate`-based codegen over a reflection-based generic utility.
- Naive struct-tag loops miss embedded fields (never recursed into), unexported fields (panic), and missing tags (silently misread as "required").
- `Set*` methods require an addressable `Value` (obtained via a pointer's `.Elem()`); check `CanSet()` before calling them.
- A nil interface produces an invalid zero `reflect.Value`; check `IsValid()` before calling other methods on it.

# Strings, Runes, and Bytes Gotchas

Go strings are immutable, UTF-8-encoded byte sequences — not arrays of characters — and almost every string-handling bug in production Go traces back to forgetting that. `len()`, indexing, and slicing all operate on bytes, not on the "characters" (runes) a human sees, which means innocuous-looking code silently mishandles any text with accents, CJK characters, or emoji. This doc covers the byte/rune/string distinctions, the immutability rules and their performance implications, and the sharp edges around converting between `string` and `[]byte`.

## 1. `len(s)` Counts Bytes, Not Characters

**The Problem:** `len(s)` on a string returns the number of bytes in its UTF-8 encoding, not the number of characters (runes) a person would count. Any code that treats `len(s)` as a character count breaks the moment non-ASCII text shows up — which, in production, is not a hypothetical.

**❌ Bad**
```go
// Truncate shortens s to at most maxLen characters... or so the author intended.
func Truncate(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen] // BUG: maxLen is being applied as a byte count
}

func main() {
	name := "héllo" // 'é' is U+00E9, encoded as 2 bytes in UTF-8 (0xC3 0xA9)
	fmt.Println(Truncate(name, 2)) // "h" + the first byte of "é" -- invalid UTF-8
}
```

**Why it's wrong:**
- `"héllo"` is 6 bytes but only 5 characters; `s[:2]` slices off `'h'` (1 byte) plus only the *first* byte of the 2-byte encoding of `'é'`, producing a string that is not valid UTF-8 and typically prints as `h` followed by a replacement/garbage character.
- This class of bug is invisible in development if test data is all ASCII, and shows up only once real user-entered names, addresses, or free text (which routinely contain accents, CJK text, or emoji) pass through the same code path.

**✅ Good**
```go
func Truncate(s string, maxRunes int) string {
	if utf8.RuneCountInString(s) <= maxRunes {
		return s
	}
	r := []rune(s)
	return string(r[:maxRunes])
}

func main() {
	name := "héllo"
	fmt.Println(Truncate(name, 2)) // "hé" -- exactly 2 characters, valid UTF-8
}
```

**Why it works / Explanation:** Converting to `[]rune` decodes the string into one entry per Unicode code point, so slicing `r[:maxRunes]` cuts on character boundaries instead of arbitrary byte offsets. For a pure count (no truncation), `utf8.RuneCountInString` avoids the `[]rune` allocation entirely. Note that even rune-counting isn't the same as "user-perceived characters" for combining marks or emoji with modifiers — but it's correct far more often than a raw byte count.

**Design principle:** Decide explicitly whether a size limit means bytes or characters, and use the API that matches (`len`/byte-slicing for bytes, `[]rune`/`utf8.RuneCountInString` for characters) — never assume they're interchangeable.

---

## 2. Indexing `s[i]` Gives You a Byte, Not a Rune

**The Problem:** `s[i]` yields the raw byte at position `i` in the UTF-8 encoding, typed as `byte` (`uint8`). Looping over a string with a plain numeric index and treating `s[i]` as "the i-th character" corrupts any multi-byte rune it touches.

**❌ Bad**
```go
func PrintChars(s string) {
	for i := 0; i < len(s); i++ {
		fmt.Printf("%c", s[i]) // BUG: s[i] is a byte, not a rune
	}
	fmt.Println()
}

func main() {
	PrintChars("héllo") // prints "hÃ©llo" -- classic UTF-8-as-Latin-1 mojibake
}
```

**Why it's wrong:**
- `'é'` is encoded as bytes `0xC3 0xA9`. Printed individually with `%c`, those two bytes render as the two separate Unicode code points `U+00C3` (`Ã`) and `U+00A9` (`©`) — the exact "mojibake" pattern anyone who has seen `café` become `cafÃ©` will recognize.
- The bug only appears with non-ASCII input; ASCII text (where every byte happens to equal its rune value) looks completely correct in casual testing, letting this ship unnoticed.

**✅ Good**
```go
func PrintChars(s string) {
	for _, r := range s { // range over a string decodes one rune at a time
		fmt.Printf("%c", r)
	}
	fmt.Println()
}

func main() {
	PrintChars("héllo") // héllo
}
```

**Why it works / Explanation:** `for i, r := range s` decodes the string's UTF-8 bytes into runes as it iterates, giving you `i` as the *byte offset* of the start of each rune and `r` as the decoded rune (`rune`, i.e. `int32`) itself — never a partial multi-byte sequence. If you need random access by character position (not just sequential iteration), convert to `[]rune(s)` once and index that instead.

**Design principle:** Iterate strings with `range` (or `[]rune` for random access) whenever you need characters — reserve byte indexing (`s[i]`) for when you genuinely mean bytes.

---

## 3. `+=` Concatenation in a Loop Is O(n²)

**The Problem:** Go strings are immutable — there is no way to append to one in place. Every `s += x` allocates a brand-new backing array sized to hold both operands and copies both into it, discarding the old backing array entirely. Do that inside a loop and the total work is quadratic in the final string's length.

**❌ Bad**
```go
func BuildCSV(rows [][]string) string {
	var out string
	for _, row := range rows {
		out += strings.Join(row, ",") // BUG: reallocates + copies the whole result so far, every iteration
		out += "\n"
	}
	return out
}
```

**Why it's wrong:**
- Because `string` values can't be mutated in place (unlike a `[]byte`'s backing array, which *can* sometimes grow into spare capacity), each `+=` must allocate fresh memory the size of the combined result and copy both the old contents and the new piece into it — the old buffer becomes garbage immediately.
- For `n` rows this is roughly `1 + 2 + 3 + ... + n` bytes copied in total, i.e. O(n²) — fine for a handful of rows, disastrous for a CSV export with tens of thousands.

**✅ Good**
```go
func BuildCSV(rows [][]string) string {
	var b strings.Builder
	for _, row := range rows {
		b.WriteString(strings.Join(row, ","))
		b.WriteByte('\n')
	}
	return b.String()
}
```

**Why it works / Explanation:** `strings.Builder` owns a growable `[]byte` internally and only converts to a `string` once, at the end, via an unsafe (but safe-in-this-context) cast that avoids a final copy. Writes into the builder amortize to O(1) each thanks to Go's usual slice-growth doubling, so building the whole result is O(n) instead of O(n²). This is the same allocation-avoidance idea covered from the memory/GC angle elsewhere in this reference — here the root cause is specifically string immutability, not just "avoid allocations in general."

**Design principle:** Never mutate-by-reassignment a `string` in a loop — use `strings.Builder` (or `bytes.Buffer`) so accumulation happens in a single growable buffer.

---

## 4. `string` ↔ `[]byte` Conversions Always Logically Copy

**The Problem:** Converting `[]byte(s)` or `string(b)` produces an independent copy of the data (with a couple of compiler-recognized exceptions for read-only, non-escaping usage, like a map lookup). Mutating the resulting `[]byte` can never affect the original string — strings are immutable, so there is no way for it to.

**❌ Bad**
```go
// Redact tries to mask a string's contents by mutating its bytes.
func Redact(s string) string {
	b := []byte(s) // "I'll just tweak the bytes in place"
	for i := range b {
		b[i] = '*'
	}
	return s // BUG: returns the ORIGINAL, un-redacted string -- b was an independent copy
}

func main() {
	fmt.Println(Redact("secret")) // "secret", not "******"
}
```

**Why it's wrong:**
- `[]byte(s)` must copy: if it aliased `s`'s backing storage, mutating `b` would mutate `s`, breaking the language's guarantee that strings never change after creation — so the runtime always gives you a fresh, independent array.
- The bug here is doubly easy to write because the mutation of `b` "looks like" it should matter, and the function typechecks and runs without error — it just silently returns the wrong (unmodified) value because the return statement references `s`, not the mutated `b`.

**✅ Good**
```go
func Redact(s string) string {
	b := []byte(s)
	for i := range b {
		b[i] = '*'
	}
	return string(b) // convert back explicitly to get the redacted copy
}

func main() {
	fmt.Println(Redact("secret")) // ******
}
```

**Why it works / Explanation:** Returning `string(b)` converts the mutated copy back into a new, independent string — this is the correct and only way to get a "modified" string in Go, since there is no in-place mutation path. Worth knowing: the compiler *does* special-case a `[]byte`-to-`string` conversion used directly as a map key in a lookup (e.g. `m[string(byteSlice)]`) to skip the allocation, but only because that usage is read-only and the converted string never escapes — it's an optimization for a specific pattern, not a loophole for mutation.

**Design principle:** Treat every `string`⇄`[]byte` conversion as a real copy with real allocation cost, and never assume mutating one side affects the other.

---

## 5. Case-Insensitive Comparison: `ToLower` vs `EqualFold`

**The Problem:** `strings.ToLower(a) == strings.ToLower(b)` is a common way to compare strings case-insensitively, but Unicode's lowercase mapping and its case-*folding* rules (used specifically for caseless comparison) aren't always the same thing — some scripts have characters that fold together for comparison purposes without one being a "lowercase version" of the other in the simple sense `ToLower` uses.

**❌ Bad**
```go
func EqualCaseInsensitive(a, b string) bool {
	return strings.ToLower(a) == strings.ToLower(b)
}

func main() {
	a := "ΛΟΓΟΣ" // all-caps Greek "logos", ends in capital sigma Σ
	b := "λογος" // correctly-cased lowercase Greek, ends in final-form sigma ς

	fmt.Println(EqualCaseInsensitive(a, b)) // false
}
```

**Why it's wrong:**
- `strings.ToLower` maps capital sigma `Σ` to the regular (non-final) lowercase sigma `σ`, context-independently — so `ToLower(a)` ends in `σ`, but `b` correctly uses the final-form sigma `ς` at the end of the word per Greek orthography, and `σ` ≠ `ς` as code points.
- The two strings are the same word, case-insensitively, to any Greek reader — but the naive `ToLower`-based comparison reports them as different, because it never unifies `σ` and `ς`.

**✅ Good**
```go
func main() {
	a := "ΛΟΓΟΣ"
	b := "λογος"

	fmt.Println(strings.EqualFold(a, b)) // true
}
```

**Why it works / Explanation:** `strings.EqualFold` compares strings under Unicode simple case-folding rules, which are specifically designed for caseless matching and correctly unify characters like `Σ`, `σ`, and `ς` that all represent "the same letter" for comparison purposes, even though they aren't related by a simple one-to-one lowercase mapping. Use `EqualFold` whenever the goal is *comparison*; reserve `ToLower`/`ToUpper` for when you actually need to transform text for *display*.

**Design principle:** Case-folding and case-mapping solve different problems — fold for comparison (`EqualFold`), map for display (`ToLower`/`ToUpper`) — don't use one where the other is needed.

---

## 6. `unsafe` Zero-Copy String/Byte Tricks Corrupt "Immutable" Strings

**The Problem:** Performance-sensitive code sometimes reaches for `unsafe` to alias a string's backing bytes as a `[]byte` without copying, to skip the allocation `[]byte(s)` normally requires. But the resulting slice shares memory with the string, so writing to it mutates data the rest of the program — and the language itself — assumes can never change.

**❌ Bad**
```go
// UnsafeBytes aliases s's backing array as a []byte, with zero copying.
func UnsafeBytes(s string) []byte {
	return unsafe.Slice((*byte)(unsafe.Pointer(unsafe.StringData(s))), len(s))
}

func main() {
	s := "immutable"
	b := UnsafeBytes(s)
	b[0] = 'I' // BUG: corrupts the memory backing "s"
	fmt.Println(s) // "Immutable" -- a core language guarantee just broke
}
```

**Why it's wrong:**
- Strings are guaranteed immutable throughout the language — other code (map internals, string interning, concurrent readers) may rely on that guarantee never being violated; this trick breaks it silently, with no panic and no warning, just a mutated value nobody expected.
- String literals can be interned/shared by the compiler and runtime, so in the worst case, mutating one "copy" of a literal string's bytes via this trick could corrupt *other, unrelated* variables that happen to reference the same interned backing data — true action-at-a-distance, and extremely hard to debug.

**✅ Good**
```go
func RealBytes(s string) []byte {
	b := make([]byte, len(s))
	copy(b, s) // an explicit, real copy
	return b
}

func main() {
	s := "immutable"
	b := RealBytes(s)
	b[0] = 'I' // mutates only the copy
	fmt.Println(s) // "immutable" -- unchanged, as expected
	fmt.Println(string(b)) // "Immutable"
}
```

**Why it works / Explanation:** A real copy costs one allocation but preserves every guarantee the language makes about string immutability — no aliasing, no risk to unrelated code, no dependence on internal compiler/runtime behavior that isn't part of the language specification. The `unsafe`-based zero-copy trick should essentially never appear outside extremely well-audited standard-library-adjacent code that has fully reasoned about every caller and every code path that touches the resulting slice.

**Design principle:** Never trade a language-level invariant (string immutability) for a micro-optimization unless you've profiled a genuine bottleneck and can prove no caller ever observes the violation — and even then, prefer `strings.Builder`/`bytes.Buffer`-based designs that avoid needing the trick at all.

---

## 7. Preallocating `strings.Builder` with `Grow()`

**The Problem:** `strings.Builder` avoids the O(n²) blowup of repeated `+=`, but it still grows its internal buffer geometrically (typically doubling) as you write to it if you never tell it how much space you'll need — meaning a handful of avoidable reallocate-and-copy cycles when the final size is easy to estimate up front.

**❌ Bad**
```go
func RenderLog(entries []string) string {
	var b strings.Builder
	for _, e := range entries {
		b.WriteString(e)
		b.WriteByte('\n')
	}
	return b.String()
}
```

**Why it's wrong:**
- Without a size hint, the builder starts with a small (possibly zero) internal buffer and grows it by reallocating and copying every time a write would exceed current capacity — for a large `entries` slice with a computable total size, that's several wasted reallocate+copy cycles that a single upfront allocation would have avoided entirely.
- This is a much smaller problem than the raw `+=` case (it's not O(n²)), but in a hot template-rendering or log-formatting path called constantly, the extra allocations still add up in GC pressure and CPU time.

**✅ Good**
```go
func RenderLog(entries []string) string {
	size := 0
	for _, e := range entries {
		size += len(e) + 1 // +1 for the newline
	}

	var b strings.Builder
	b.Grow(size) // one allocation sized for the known final length
	for _, e := range entries {
		b.WriteString(e)
		b.WriteByte('\n')
	}
	return b.String()
}
```

**Why it works / Explanation:** `Grow(n)` ensures the builder's internal buffer has room for at least `n` more bytes in a single allocation, so the subsequent writes never trigger a reallocation. Computing the size upfront costs one extra pass over `entries`, but that pass is cheap (just `len()` calls) compared to the reallocations it avoids.

**Design principle:** When the final size is knowable (or reasonably estimable) ahead of time, tell the allocator — `Grow()` for `strings.Builder`, the capacity argument to `make` for slices — rather than letting geometric growth discover it the slow way.

---

## 8. `strings.Split("", sep)` Returns One Empty Element, Not Zero

**The Problem:** `strings.Split` on an empty input string returns `[]string{""}` — a one-element slice containing an empty string — not an empty slice. Code that assumes "no input means no elements to process" will process one spurious empty entry.

**❌ Bad**
```go
func ProcessTags(csv string) []string {
	tags := strings.Split(csv, ",")
	var out []string
	for _, t := range tags {
		out = append(out, strings.ToUpper(t)) // BUG: csv=="" still produces one output element
	}
	return out
}

func main() {
	fmt.Println(ProcessTags("")) // []string{""} -- one spurious empty tag, not an empty slice
}
```

**Why it's wrong:**
- `strings.Split("", ",")` returns `[]string{""}` by definition (splitting an empty string on any separator yields a single empty substring) — `len(tags)` is 1, not 0, which contradicts the natural assumption that "no tags provided" should produce "no tags to process."
- Downstream code that assumes it received real data for every element in the slice (e.g. validating each tag is non-empty, or using the count as "number of tags") gets a false positive from this single empty-string element.

**✅ Good**
```go
func ProcessTags(csv string) []string {
	if csv == "" {
		return nil
	}
	tags := strings.Split(csv, ",")
	out := make([]string, 0, len(tags))
	for _, t := range tags {
		if t == "" {
			continue
		}
		out = append(out, strings.ToUpper(t))
	}
	return out
}
```

**Why it works / Explanation:** Handling the empty-input case explicitly (returning `nil`/an empty slice before ever calling `Split`) and filtering out empty elements from the general case covers both "no input at all" and "input with stray empty fields" (e.g. `"a,,b"` or a trailing comma), which `strings.Split` alone doesn't distinguish for you.

**Design principle:** Know your stdlib functions' documented edge-case behavior (`Split` on empty input, `Split` with consecutive separators) and add an explicit guard rather than assuming "empty in, empty out."

---

## Key Takeaways
- `len(s)` counts bytes, not characters — use `[]rune`/`utf8.RuneCountInString` when you mean characters.
- `s[i]` yields a byte, not a rune — use `for _, r := range s` or `[]rune(s)` to work with characters.
- `+=` on strings in a loop is O(n²) due to immutability; use `strings.Builder`.
- `string`↔`[]byte` conversions always logically copy; mutating a converted `[]byte` never affects the original string.
- Use `strings.EqualFold` for case-insensitive comparison; reserve `ToLower`/`ToUpper` for display transformations.
- Never use `unsafe` zero-copy tricks to alias a string's bytes as a mutable `[]byte` outside extremely well-audited library code.
- Preallocate `strings.Builder` with `Grow()` when the final size is knowable, to skip avoidable reallocations.
- `strings.Split("", sep)` returns `[]string{""}`, not an empty slice — guard for it explicitly.

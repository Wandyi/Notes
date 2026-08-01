# JSON and Serialization

`encoding/json` is where a huge amount of production Go code meets the outside world — HTTP APIs, message queues, config files, third-party webhooks. It's also one of the quietest sources of bugs in the language: struct tags are unchecked strings, unexported fields vanish without a word, and type coercion happens via reflection with rules that are easy to get wrong and expensive to get wrong silently. None of these mistakes fail to compile — they fail in production, usually as "the field is just... empty" or "the API leaked a field it shouldn't have."

## 1. Struct Tag Typos Silently Ignored

**The Problem:** A `json:"..."` tag is just a plain string literal as far as the compiler is concerned — nothing checks that `neme` was supposed to be `name`. The struct still compiles, `go build` is happy, and the mismatch only shows up as wrong data on the wire or an unpopulated field, usually discovered by a confused consumer of the API rather than by the author.

**❌ Bad**
```go
type User struct {
	ID   int    `json:"id"`
	Name string `json:"neme"` // BUG: typo — should be "name"
}

func main() {
	u := User{ID: 1, Name: "Alice"}
	b, _ := json.Marshal(u)
	fmt.Println(string(b)) // {"id":1,"neme":"Alice"} — wrong key on the wire
}
```

**Why it's wrong:**
- Every consumer expecting `"name"` in the response gets nothing; on their side `Name` unmarshals to its zero value `""`, with no error anywhere in the chain.
- The bug survives code review (tags are easy to skim past) and survives `go build`/`go vet` in the common case — `go vet`'s struct tag check mainly catches malformed tag *syntax* (e.g. missing quotes, bad `,omitempty` spelling), not semantically wrong key names, since it has no way to know what the "correct" name was supposed to be.

**✅ Good**
```go
type User struct {
	ID   int    `json:"id"`
	Name string `json:"name"`
}

func TestUser_JSONRoundTrip(t *testing.T) {
	u := User{ID: 1, Name: "Alice"}
	b, err := json.Marshal(u)
	if err != nil {
		t.Fatal(err)
	}

	var got User
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatal(err)
	}
	if got != u {
		t.Errorf("round-trip mismatch: got %+v, want %+v", got, u)
	}
}
```

**Why it works / Explanation:** Fixing the tag itself is trivial once spotted — the real fix is process: a marshal/unmarshal round-trip test for every JSON-facing struct catches tag typos immediately, because a typo'd tag produces a struct that doesn't survive `Marshal` followed by `Unmarshal` unchanged. This test costs nothing to write and catches an entire class of bug that no static tool will reliably catch for you.

**Design principle:** Don't trust unchecked string literals to be correct just because they compile — round-trip tests turn a silent runtime mismatch into a loud, immediate test failure.

---

## 2. Unexported Struct Fields Are Invisible to `encoding/json`

**The Problem:** `encoding/json` uses reflection, but reflection cannot read or write unexported (lowercase) fields from another package — and even within the same package, `encoding/json` deliberately skips them. People coming from languages where reflection "sees everything" are routinely surprised that a lowercase field is silently dropped on both `Marshal` and `Unmarshal`, with zero error or warning.

**❌ Bad**
```go
type Event struct {
	ID   string `json:"id"`
	kind string // BUG: unexported — invisible to encoding/json
}

func main() {
	e := Event{ID: "evt_1", kind: "signup"}
	b, _ := json.Marshal(e)
	fmt.Println(string(b)) // {"id":"evt_1"} — kind is just gone

	var e2 Event
	json.Unmarshal([]byte(`{"id":"evt_2","kind":"login"}`), &e2)
	fmt.Println(e2) // {evt_2 } — kind stayed "" even though the input had it
}
```

**Why it's wrong:**
- On marshal, `kind` never appears in the output — not as `null`, not as an error, just absent, as if the field didn't exist.
- On unmarshal, a `"kind"` key in the input JSON is silently ignored; `e2.kind` stays at its zero value `""` even though the data was right there in the payload.
- Because nothing errors, this can hide in a codebase for a long time — the struct "looks" like it round-trips fine until someone finally checks the actual field value.

**✅ Good**
```go
// Option 1: export the field if there's no reason to hide it.
type Event struct {
	ID   string `json:"id"`
	Kind string `json:"kind"`
}

// Option 2: keep it unexported for encapsulation, but give encoding/json
// an explicit path to it via custom marshaling.
type SecureEvent struct {
	ID   string
	kind string
}

func (e SecureEvent) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		ID   string `json:"id"`
		Kind string `json:"kind"`
	}{ID: e.ID, Kind: e.kind})
}

func (e *SecureEvent) UnmarshalJSON(data []byte) error {
	var aux struct {
		ID   string `json:"id"`
		Kind string `json:"kind"`
	}
	if err := json.Unmarshal(data, &aux); err != nil {
		return err
	}
	e.ID, e.kind = aux.ID, aux.Kind
	return nil
}
```

**Why it works / Explanation:** The simplest fix is almost always to export the field — most structs meant to be serialized have no real encapsulation need for JSON purposes. When a field genuinely must stay unexported (e.g. to force construction through a validating constructor), implementing `MarshalJSON`/`UnmarshalJSON` on the type gives you an explicit, visible bridge instead of relying on reflection to reach where it structurally cannot go.

**Design principle:** Reflection-based serialization only sees what the language's visibility rules expose — if it must be private, make the serialization path explicit instead of hoping reflection will do the impossible.

---

## 3. Zero Value vs. "Field Absent" Ambiguity (and the `omitempty` Trap)

**The Problem:** A plain `int`, `bool`, or `string` field can't represent "the client didn't send this field" separately from "the client explicitly sent the zero value." For PATCH-style partial updates this is a real correctness bug, not a cosmetic one: `{"count": 0}` and `{}` unmarshal into the exact same Go value, so "set count to zero" and "leave count alone" become indistinguishable.

**❌ Bad**
```go
type UserPatch struct {
	Name  string `json:"name"`
	Count int    `json:"count"`
}

func applyPatch(u *User, body []byte) error {
	var patch UserPatch
	if err := json.Unmarshal(body, &patch); err != nil {
		return err
	}
	// BUG: can't tell "count omitted" from "count explicitly set to 0" —
	// this always overwrites Count, even when the client never mentioned it.
	u.Name = patch.Name
	u.Count = patch.Count
	return nil
}
```

**Why it's wrong:**
- A client sending `{"name": "Bob"}` to update only the name silently zeroes out `Count` too, because the missing field and an explicit `0` are indistinguishable once unmarshaled.
- The inverse `omitempty` trap bites on the way *out*: `Count int `json:"count,omitempty"`` will drop the field entirely from the JSON response whenever `Count == 0` — so a legitimate "count is zero" API response comes out as `{}` instead of `{"count":0}`, and the same happens for `false` booleans and `""` strings, which is rarely what you want for values that are meaningful at their zero value.

**✅ Good**
```go
type UserPatch struct {
	Name  *string `json:"name"`  // nil = not provided, non-nil = explicit value
	Count *int    `json:"count"` // distinguishes "omit" from "set to 0"
}

func applyPatch(u *User, body []byte) error {
	var patch UserPatch
	if err := json.Unmarshal(body, &patch); err != nil {
		return err
	}
	if patch.Name != nil {
		u.Name = *patch.Name
	}
	if patch.Count != nil {
		u.Count = *patch.Count // correctly applies an explicit 0
	}
	return nil
}
```

**Why it works / Explanation:** A pointer field is `nil` only when the key was genuinely absent from the JSON (or explicitly `null`); any presence of the key — including `0`, `false`, or `""` — produces a non-nil pointer to that value. This gives the handler a real three-way signal (absent / null / present-with-value) instead of collapsing "absent" and "zero" into one case. On the output side, drop `omitempty` for any field where the zero value is a meaningful piece of data you always want visible to the client — reserve `omitempty` for fields that are genuinely optional metadata.

**Design principle:** Model "absence" explicitly instead of overloading a zero value to mean two different things — `*T` (or a dedicated `Option`-style wrapper) is Go's idiom for a nullable/optional field.

---

## 4. Losing Numeric Precision Unmarshaling into `interface{}`/`any`

**The Problem:** When `encoding/json` decodes a JSON number into an `any`/`interface{}` target, it always uses `float64` — and `float64` can only represent integers exactly up to 2^53. A large ID (common with 64-bit database keys or Twitter/Discord-style snowflake IDs) that goes through an `any` decode can come out a different number than it went in, with no error raised anywhere.

**❌ Bad**
```go
data := []byte(`{"id": 9007199254740993, "name": "widget"}`)

var v any
if err := json.Unmarshal(data, &v); err != nil {
	log.Fatal(err)
}
m := v.(map[string]interface{})
fmt.Println(m["id"]) // 9.007199254740992e+15 — BUG: off by one, silently
```

**Why it's wrong:**
- `9007199254740993` (2^53 + 1) cannot be represented exactly as a `float64`; it rounds to the nearest representable value, `9007199254740992`, and nothing about that rounding is surfaced as an error — the wrong ID is just there.
- This class of bug is nearly invisible in testing with small sample IDs and only appears once real, large production IDs flow through the same code path — often as "records not found" for an ID that "clearly" exists.

**✅ Good**
```go
// Preferred: decode into a concrete, typed struct — encoding/json parses
// the number text directly into the target integer type, no float64 involved.
type Widget struct {
	ID   int64  `json:"id"`
	Name string `json:"name"`
}

var w Widget
if err := json.Unmarshal(data, &w); err != nil {
	log.Fatal(err)
}
fmt.Println(w.ID) // 9007199254740993 — exact

// When the shape is genuinely dynamic, use UseNumber() instead of any's
// default float64 behavior.
dec := json.NewDecoder(bytes.NewReader(data))
dec.UseNumber()
var v2 any
_ = dec.Decode(&v2)
m2 := v2.(map[string]interface{})
n := m2["id"].(json.Number)
id, _ := n.Int64()
fmt.Println(id) // 9007199254740993 — exact
```

**Why it works / Explanation:** The most robust fix is to avoid `any` altogether and decode into a struct with the correct concrete numeric type (`int64`, `uint64`, etc.) — `encoding/json` parses the literal digits straight into that type without ever routing through `float64`. When the payload's shape genuinely isn't known ahead of time, `Decoder.UseNumber()` makes numbers decode as `json.Number` (a string under the hood) so you can choose `Int64()`, `Float64()`, or big-integer parsing explicitly, on your terms, instead of having the decoder silently pick `float64` for you.

**Design principle:** Prefer concrete types over `any` at deserialization boundaries — the compiler and the decoder can both do the right thing only when they know what the right thing is.

---

## 5. `time.Time` Marshaling Format Assumptions

**The Problem:** Go's `time.Time` marshals to and parses from RFC3339 by default — but plenty of external systems (legacy APIs, other languages' default JSON libraries, spreadsheet exports) send timestamps in other formats entirely, and unmarshaling those directly into a `time.Time` field just errors out. Symmetrically, people assume marshaled `time.Time` output is always UTC — it isn't; it faithfully reproduces whatever `Location` the value carries.

**❌ Bad**
```go
type Event struct {
	Timestamp time.Time `json:"timestamp"`
}

data := []byte(`{"timestamp": "2024-01-15 10:30:00"}`) // not RFC3339 — no "T", no zone
var e Event
err := json.Unmarshal(data, &e)
fmt.Println(err)
// parsing time "2024-01-15 10:30:00" as "2006-01-02T15:04:05Z07:00": cannot parse " 10:30:00" as "T"

// And on the way out, "UTC" is not guaranteed just because it's JSON:
loc, _ := time.LoadLocation("America/New_York")
t := time.Date(2024, 1, 15, 10, 30, 0, 0, loc)
b, _ := json.Marshal(t)
fmt.Println(string(b)) // "2024-01-15T10:30:00-05:00" — BUG: not UTC, preserves the Location
```

**Why it's wrong:**
- Any upstream system that doesn't send exact RFC3339 makes `json.Unmarshal` fail outright on a `time.Time` field — a brittle dependency on an undocumented assumption about a third party's format.
- Assuming marshaled output is UTC and then comparing/storing it as if it were leads to timestamps that are off by whatever the source `Location`'s offset was, since `MarshalJSON` never normalizes to UTC on its own.

**✅ Good**
```go
type FlexTime struct {
	time.Time
}

var supportedLayouts = []string{
	time.RFC3339,
	"2006-01-02 15:04:05",
	"2006-01-02",
}

func (ft *FlexTime) UnmarshalJSON(data []byte) error {
	s := strings.Trim(string(data), `"`)
	for _, layout := range supportedLayouts {
		if t, err := time.Parse(layout, s); err == nil {
			ft.Time = t.UTC()
			return nil
		}
	}
	return fmt.Errorf("unrecognized timestamp format: %q", s)
}

func (ft FlexTime) MarshalJSON() ([]byte, error) {
	return json.Marshal(ft.Time.UTC().Format(time.RFC3339))
}
```

**Why it works / Explanation:** A custom `UnmarshalJSON` that tries a short list of known upstream formats turns "this API sends timestamps in a weird way" into a one-time, explicit, testable adapter instead of a recurring parse failure. Normalizing to `.UTC()` inside both `MarshalJSON` and `UnmarshalJSON` makes the wrapper type's on-the-wire behavior consistent regardless of what `Location` a caller happened to construct the underlying `time.Time` with.

**Design principle:** Never assume a timestamp format or timezone across a serialization boundary — make the conversion explicit and centralize it in one type instead of hoping the default matches.

---

## 6. Ignoring `json.Unmarshal` Errors (and Trusting a Partially-Filled Struct)

**The Problem:** `json.Unmarshal` can populate some fields of a struct correctly and still return a non-nil error, because a type mismatch on one field doesn't stop it from continuing to decode the rest of the object. Ignoring the returned error — or checking it but still using the struct anyway — means silently operating on a half-populated value.

**❌ Bad**
```go
type Config struct {
	Name    string `json:"name"`
	Port    int    `json:"port"`
	Timeout int    `json:"timeout"`
}

data := []byte(`{"name": "svc", "port": "8080", "timeout": 30}`) // port is a string, not a number

var cfg Config
json.Unmarshal(data, &cfg) // BUG: error return value discarded entirely

fmt.Printf("%+v\n", cfg)
// {Name:svc Port:0 Timeout:30} — Name and Timeout got set; Port silently stayed 0
```

**Why it's wrong:**
- `Port` should be `8080`, but because the JSON sent a string where an `int` was expected, decoding hits a type error on that field — and the struct is left with `Port: 0`, which looks exactly like a legitimately-configured zero, not a decode failure.
- Because the error is discarded, `Timeout` still got set correctly *after* the error on `Port`, since `Unmarshal` keeps decoding remaining fields — reinforcing the false impression that "it mostly worked," when in fact the caller has no idea anything went wrong at all.

**✅ Good**
```go
var cfg Config
if err := json.Unmarshal(data, &cfg); err != nil {
	// BUG-free: treat any error as "do not trust cfg" — don't use a
	// partially-decoded struct just because some fields look plausible.
	return fmt.Errorf("decoding config: %w", err)
}
```

**Why it works / Explanation:** Checking the error and returning immediately means the caller never has a chance to read `cfg.Port` believing it's `0` by configuration rather than by decode failure. The general rule for `encoding/json`: if `Unmarshal` returns a non-nil error, treat the destination value as unreliable in its entirety, even for the fields that happen to look correctly populated — Go's docs describe this "continue after error" behavior explicitly, and it exists for convenience (collecting one representative error instead of aborting on the first field), not as a guarantee that everything else decoded is trustworthy.

**Design principle:** Never use a value produced by a fallible operation after ignoring (or explicitly discarding) that operation's error — partial success is not success.

---

## 7. Loading Entire Payloads Into Memory Instead of Streaming

**The Problem:** `json.Unmarshal([]byte, ...)` requires the entire JSON document in memory as a byte slice before decoding even starts. For large HTTP request bodies, large files, or large arrays, this creates an avoidable memory spike proportional to payload size — `json.NewDecoder(io.Reader).Decode(...)` can consume directly from the stream instead.

**❌ Bad**
```go
func handleUpload(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body) // BUG: buffers the entire request body in memory
	if err != nil {
		http.Error(w, "read error", http.StatusBadRequest)
		return
	}

	var records []Record
	if err := json.Unmarshal(body, &records); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	process(records)
}
```

**Why it's wrong:**
- A 200 MB upload means at least 200 MB held in `body`, plus the additional allocations `Unmarshal` makes building the resulting `[]Record` and its constituent strings/slices — under concurrent requests this multiplies fast and is a common cause of surprise OOMs under load spikes.
- The full byte slice has to be read and buffered before any decoding can even begin, adding latency for no benefit when the data is going to be decoded field-by-field anyway.

**✅ Good**
```go
func handleUpload(w http.ResponseWriter, r *http.Request) {
	dec := json.NewDecoder(r.Body) // reads directly from the request body stream

	// Consume the opening '[' of the array manually, then decode elements
	// one at a time so memory use stays bounded regardless of array length.
	if _, err := dec.Token(); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	for dec.More() {
		var rec Record
		if err := dec.Decode(&rec); err != nil {
			http.Error(w, "bad json", http.StatusBadRequest)
			return
		}
		process(rec) // handle one record at a time; nothing large stays resident
	}
}
```

**Why it works / Explanation:** `json.NewDecoder` reads from the underlying `io.Reader` in small internal chunks as needed rather than requiring the whole payload up front, and `dec.Decode` can be called repeatedly to pull one JSON value at a time out of a larger stream (including a stream of many independent top-level values, not just one array). Combined with `dec.Token()`/`dec.More()` for manually walking an array's elements, this bounds memory use to roughly the size of one record instead of the size of the whole payload.

**Design principle:** Prefer streaming decoders over whole-buffer unmarshaling whenever payload size isn't small and bounded — memory use should scale with what you're processing right now, not with the size of the entire input.

---

## 8. Forgetting `json:"-"` for Fields That Must Never Be Serialized

**The Problem:** A struct that's both an internal/DB model and the thing directly passed to `json.Marshal` for an API response has no protection against a future field addition leaking sensitive data — anyone adding a new exported field to that struct for an unrelated reason silently makes it visible in every JSON response that uses the struct.

**❌ Bad**
```go
type User struct {
	ID           int    `json:"id"`
	Email        string `json:"email"`
	PasswordHash string `json:"password_hash"` // BUG: added for an internal migration script, forgot this is also the API response type
}

func getUserHandler(w http.ResponseWriter, u User) {
	json.NewEncoder(w).Encode(u) // leaks password_hash to every API caller
}
```

**Why it's wrong:**
- `PasswordHash` was likely added innocently — maybe for a debugging script or an internal admin tool — but because `User` doubles as the HTTP response type, it now leaks a credential hash to every client that calls this endpoint.
- Nothing about `go build`, `go vet`, or a normal code review flags this: the struct compiles fine and the diff looks like an unrelated, harmless field addition.

**✅ Good**
```go
// Option 1: explicit exclusion — safe as long as everyone remembers the tag.
type User struct {
	ID           int    `json:"id"`
	Email        string `json:"email"`
	PasswordHash string `json:"-"` // never serialized, in either direction
}

// Option 2 (more robust): a dedicated response DTO that structurally
// cannot leak fields that were never added to it in the first place.
type UserResponse struct {
	ID    int    `json:"id"`
	Email string `json:"email"`
}

func getUserHandler(w http.ResponseWriter, u User) {
	json.NewEncoder(w).Encode(UserResponse{ID: u.ID, Email: u.Email})
}
```

**Why it works / Explanation:** `json:"-"` is an immediate, explicit fix, but it depends on every future editor of the struct remembering to tag anything sensitive the same way — a single missed tag on a new field reintroduces the leak. Keeping a separate, minimal response DTO removes that dependency on remembering: a field can only appear in the JSON output if someone deliberately added it to `UserResponse`, so accidental exposure of new internal/DB fields becomes structurally impossible rather than merely discouraged.

**Design principle:** Don't let your persistence model double as your API contract — a dedicated response type makes "what's exposed" an explicit allowlist instead of an implicit denylist someone has to remember to maintain.

---

## Key Takeaways
- Struct tag typos compile fine and produce wrong JSON silently — cover JSON-facing structs with marshal/unmarshal round-trip tests.
- Unexported fields are invisible to `encoding/json` on both marshal and unmarshal — export the field or bridge it with custom `MarshalJSON`/`UnmarshalJSON`.
- A missing field and an explicit zero value unmarshal identically into plain types — use pointers for PATCH semantics, and audit `omitempty` on fields where zero is meaningful.
- Decoding numbers into `any` routes through `float64` and loses precision above 2^53 — decode into typed structs or use `Decoder.UseNumber()`.
- `time.Time` marshaling/unmarshaling assumes RFC3339 and preserves whatever `Location` it was given — never assume UTC or a specific incoming format across a serialization boundary.
- `Unmarshal` can return an error after partially populating the target — never use the result if `err != nil`.
- `json.Unmarshal([]byte, ...)` buffers the whole payload — prefer `json.NewDecoder` streaming for large inputs.
- Sensitive internal fields leak through shared marshal structs by default — use `json:"-"` or, better, a dedicated response DTO.

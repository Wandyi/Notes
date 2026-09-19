package pipeline

import (
	"sync"
	"sync/atomic"
	"time"
)

// shardCounters are the only fields of a shard touched from outside its own
// goroutine, so they are the only ones that are atomic. Everything else relies on
// exclusive ownership.
type shardCounters struct {
	received       atomic.Uint64
	folded         atomic.Uint64
	emitted        atomic.Uint64
	rejected       atomic.Uint64
	late           atomic.Uint64
	duplicates     atomic.Uint64
	gaps           atomic.Uint64
	panics         atomic.Uint64
	windows        atomic.Uint64
	quarantines    atomic.Uint64
	droppedResults atomic.Uint64
	windowsOpen    atomic.Int64
}

// Stats is a point-in-time view of pipeline health. It is deliberately a plain
// struct of counters rather than a gauge snapshot: counters are safe to scrape at
// any rate and safe to reset by restart, and the consumer computes rates.
type Stats struct {
	Accepted       uint64    `json:"accepted"`        // handed to a shard queue
	Received       uint64    `json:"received"`        // dequeued by a shard
	Folded         uint64    `json:"folded"`          // applied to an aggregate
	Emitted        uint64    `json:"emitted"`         // aggregates published
	Windows        uint64    `json:"windows"`         // windows closed
	Rejected       uint64    `json:"rejected"`        // failed validation or transform
	Backpressure   uint64    `json:"backpressure"`    // shed because a shard queue was full
	Late           uint64    `json:"late"`            // arrived after their window closed
	Duplicates     uint64    `json:"duplicates"`      // replayed sequence numbers
	Gaps           uint64    `json:"gaps"`            // sequence numbers abandoned
	Panics         uint64    `json:"panics"`          // folds contained by isolation
	Quarantines    uint64    `json:"quarantines"`     // sources shed
	Shed           uint64    `json:"shed"`            // points rejected due to quarantine
	DroppedResults uint64    `json:"dropped_results"` // windows lost to a shutdown deadline
	QueueDepth     int       `json:"queue_depth"`     // points waiting across all shards
	QueueCapacity  int       `json:"queue_capacity"`
	OpenWindows    int       `json:"open_windows"`
	CollectedAt    time.Time `json:"collected_at"`
}

// quarantineTable is the shared, read-mostly set of shed producers.
//
// Writes happen on a breaker trip (rare); reads happen on every Submit (hot).
// sync.Map is the right fit for exactly that access pattern -- a plain RWMutex
// would put every submitting goroutine on one contended cache line.
type quarantineTable struct {
	m   sync.Map // source -> *atomic.Int64 (unix nanos until which the source is shed)
	hit atomic.Uint64
}

func newQuarantineTable() *quarantineTable { return &quarantineTable{} }

// trip sheds source until the given time. It reports whether this call newly
// quarantined the source (as opposed to extending an existing one), so callers
// only log a state change.
func (q *quarantineTable) trip(source string, until time.Time) bool {
	v, loaded := q.m.LoadOrStore(source, newExpiry(until))
	if !loaded {
		return true
	}
	e := v.(*atomic.Int64)
	prev := e.Load()
	e.Store(until.UnixNano())
	return prev < time.Now().UnixNano() // was not currently quarantined
}

// blocked reports whether source is currently shed.
func (q *quarantineTable) blocked(source string, now time.Time) bool {
	v, ok := q.m.Load(source)
	if !ok {
		return false
	}
	e := v.(*atomic.Int64)
	if e.Load() > now.UnixNano() {
		q.hit.Add(1)
		return true
	}
	// Expired. Delete lazily so the map does not accumulate dead producers.
	q.m.Delete(source)
	return false
}

// list returns the currently shed sources, for the admin endpoint.
func (q *quarantineTable) list(now time.Time) map[string]time.Time {
	out := map[string]time.Time{}
	q.m.Range(func(k, v any) bool {
		if ns := v.(*atomic.Int64).Load(); ns > now.UnixNano() {
			out[k.(string)] = time.Unix(0, ns)
		}
		return true
	})
	return out
}

func newExpiry(t time.Time) *atomic.Int64 {
	v := &atomic.Int64{}
	v.Store(t.UnixNano())
	return v
}

package pipeline

import (
	"container/heap"
	"time"

	"github.com/vaibhav/metricsprocessor/pkg/model"
)

// reorderBuffer repairs network-level reordering within a single producer stream.
//
// Why this exists: channels give FIFO, and a single shard goroutine gives a total
// order over what it receives -- but neither says anything about the order in
// which a producer's points *arrived* at the process. Retries, multiple HTTP
// connections, and broker partition rebalances all reorder. Sequence numbers are
// the only thing that survives those hops, so they are what we sort on.
//
// The buffer is bounded in two dimensions, because an unbounded reorder buffer is
// just a memory leak with a deadline:
//
//	depth    - how many points may wait for a missing predecessor
//	maxDelay - how long the stream may stall waiting for it
//
// When either bound is hit we declare a gap, skip forward, and keep folding. That
// is the core "partial failure does not stop aggregation" decision applied to
// ordering: a permanently lost point degrades one series' completeness rather than
// stalling the stream forever.
type reorderBuffer struct {
	next      uint64 // sequence number we are waiting for
	started   bool
	h         seqHeap
	depth     int
	maxDelay  time.Duration
	stalledAt time.Time // when the buffer last became non-empty
	lastSeen  time.Time // for idle eviction
	// dupes counts replays discarded from inside the buffer. Duplicates caught
	// on the way in are reported synchronously; these are found later, while
	// draining, and would otherwise vanish from the observability picture.
	dupes uint64
}

type pushResult uint8

const (
	pushOK        pushResult = iota // point admitted (possibly still buffered)
	pushDuplicate                   // sequence already consumed; discard
)

func newReorderBuffer(depth int, maxDelay time.Duration) *reorderBuffer {
	return &reorderBuffer{depth: depth, maxDelay: maxDelay}
}

// push admits p and appends every point that is now in-order to out. gap is the
// number of sequence numbers abandoned, if the depth bound forced an advance.
func (rb *reorderBuffer) push(p *model.Point, now time.Time, out []*model.Point) (_ []*model.Point, res pushResult, gap uint64) {
	rb.lastSeen = now

	if !rb.started {
		// Stream start. We cannot assume the first point to *arrive* is the first
		// point in the stream -- that is the very reordering we exist to repair --
		// and we cannot assume sequence 1 either, because we may be joining
		// mid-stream after a restart or a partition reassignment. So the buffer
		// parks points until either the depth or the delay bound tells it that it
		// has seen enough to pick a starting position. That start-up delay is the
		// price of a correct origin, and it is paid once per stream.
		if rb.h.Len() == 0 {
			rb.stalledAt = now
		}
		heap.Push(&rb.h, p)
		if rb.h.Len() > rb.depth {
			out = rb.begin(out)
		}
		return out, pushOK, 0
	}

	switch {
	case p.Seq < rb.next:
		// Already folded. At-least-once delivery makes this normal, not
		// exceptional: dropping it is what keeps the fold idempotent.
		return out, pushDuplicate, 0

	case p.Seq == rb.next:
		out = append(out, p)
		rb.next++
		return rb.drain(out), pushOK, 0
	}

	// Out of order: park it.
	if rb.h.Len() == 0 {
		rb.stalledAt = now
	}
	heap.Push(&rb.h, p)
	if rb.h.Len() > rb.depth {
		out, gap = rb.forceAdvance(out, now)
	}
	return out, pushOK, gap
}

// flush is called on the maintenance tick. It forces the buffer forward if the
// stream has been stalled longer than maxDelay.
func (rb *reorderBuffer) flush(now time.Time) (out []*model.Point, gap uint64) {
	if rb.h.Len() == 0 || now.Sub(rb.stalledAt) < rb.maxDelay {
		return nil, 0
	}
	if !rb.started {
		return rb.begin(nil), 0
	}
	return rb.forceAdvance(nil, now)
}

// begin picks the stream's starting sequence as the lowest one buffered so far,
// then releases everything contiguous from there.
func (rb *reorderBuffer) begin(out []*model.Point) []*model.Point {
	rb.started = true
	rb.next = rb.h[0].Seq
	return rb.drain(out)
}

// drainAll empties the buffer unconditionally. Used at shutdown, where holding
// points back to preserve order is strictly worse than emitting them.
func (rb *reorderBuffer) drainAll(out []*model.Point) ([]*model.Point, uint64) {
	var gap uint64
	if !rb.started && rb.h.Len() > 0 {
		out = rb.begin(out)
	}
	for rb.h.Len() > 0 {
		p := heap.Pop(&rb.h).(*model.Point)
		if p.Seq > rb.next {
			gap += p.Seq - rb.next
		}
		rb.next = p.Seq + 1
		out = append(out, p)
	}
	rb.stalledAt = time.Time{}
	return out, gap
}

// forceAdvance abandons the wait for rb.next, jumps to the lowest buffered
// sequence, and emits everything that is contiguous from there.
func (rb *reorderBuffer) forceAdvance(out []*model.Point, now time.Time) ([]*model.Point, uint64) {
	head := rb.h[0]
	gap := head.Seq - rb.next
	rb.next = head.Seq
	out = append(out, heap.Pop(&rb.h).(*model.Point))
	rb.next++
	out = rb.drain(out)
	if rb.h.Len() > 0 {
		rb.stalledAt = now
	} else {
		rb.stalledAt = time.Time{}
	}
	return out, gap
}

// drain pops every buffered point that is now contiguous with next.
func (rb *reorderBuffer) drain(out []*model.Point) []*model.Point {
	for rb.h.Len() > 0 {
		switch head := rb.h[0]; {
		case head.Seq < rb.next: // duplicate that got parked; discard
			heap.Pop(&rb.h)
			rb.dupes++
		case head.Seq == rb.next:
			out = append(out, heap.Pop(&rb.h).(*model.Point))
			rb.next++
		default:
			return out
		}
	}
	rb.stalledAt = time.Time{}
	return out
}

// pending reports how many points are waiting on a missing predecessor.
func (rb *reorderBuffer) pending() int { return rb.h.Len() }

// takeDupes returns and clears the count of replays discarded during a drain.
func (rb *reorderBuffer) takeDupes() uint64 {
	n := rb.dupes
	rb.dupes = 0
	return n
}

// seqHeap is a min-heap on Seq.
type seqHeap []*model.Point

func (h seqHeap) Len() int           { return len(h) }
func (h seqHeap) Less(i, j int) bool { return h[i].Seq < h[j].Seq }
func (h seqHeap) Swap(i, j int)      { h[i], h[j] = h[j], h[i] }
func (h *seqHeap) Push(x any)        { *h = append(*h, x.(*model.Point)) }
func (h *seqHeap) Pop() (x any) {
	old := *h
	n := len(old)
	x = old[n-1]
	old[n-1] = nil
	*h = old[:n-1]
	return x
}

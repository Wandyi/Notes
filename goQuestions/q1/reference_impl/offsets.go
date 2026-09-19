package kafkaworker

import (
	"context"
	"sync"
)

// OffsetTracker computes, per partition, the highest offset that is safe to
// commit given that records complete out of order.
//
// The invariant that makes at-least-once work is:
//
//	commit(P) = the lowest offset dispatched from P that has not yet been
//	            acknowledged; if every dispatched offset is acknowledged,
//	            commit(P) = highest dispatched offset + 1.
//
// Committing anything higher would mark an in-flight record as processed —
// if the process dies before that record completes, it is never redelivered
// and the system silently degrades to at-most-once.
//
// Offsets are NOT assumed to be contiguous. Log compaction, aborted
// transactions and control records all leave gaps, so the tracker remembers
// the offsets it actually dispatched instead of counting upwards.
type OffsetTracker struct {
	mu    sync.Mutex
	parts map[TopicPartition]*partitionState

	// retired is closed and replaced every time the watermark advances. It
	// lets WaitDrained block without polling and without a sync.Cond, which
	// cannot be selected on alongside a context.
	retired chan struct{}
}

type partitionState struct {
	// dispatched holds offsets handed to workers, in ascending order.
	// head is the index of the first entry not yet retired.
	dispatched []int64
	head       int

	// acked holds offsets that completed but are not yet retired because an
	// older offset is still in flight. Bounded by in-flight records.
	acked map[int64]struct{}

	highestDispatched int64
	hasDispatched     bool

	lastCommitted int64
	hasCommitted  bool
}

// NewOffsetTracker returns an empty tracker.
func NewOffsetTracker() *OffsetTracker {
	return &OffsetTracker{
		parts:   make(map[TopicPartition]*partitionState),
		retired: make(chan struct{}),
	}
}

// Assign registers partitions gained in a rebalance. Any state carried over
// from a previous assignment of the same partition is discarded, because the
// new assignment starts from the broker's committed offset.
func (t *OffsetTracker) Assign(tps ...TopicPartition) {
	t.mu.Lock()
	defer t.mu.Unlock()
	for _, tp := range tps {
		t.parts[tp] = &partitionState{acked: make(map[int64]struct{})}
	}
}

// Revoke drops all state for partitions lost in a rebalance. Call it only
// after in-flight records for those partitions have drained and their final
// offsets have been committed.
func (t *OffsetTracker) Revoke(tps ...TopicPartition) {
	t.mu.Lock()
	defer t.mu.Unlock()
	for _, tp := range tps {
		delete(t.parts, tp)
	}
}

// Track records that an offset has been dispatched to a worker. It must be
// called from the single fetch/dispatch goroutine, in ascending offset order
// per partition, before the record can be acknowledged.
//
// It returns false if the partition is not assigned, which happens when a
// rebalance revoked it between fetch and dispatch; the caller should drop
// the record rather than process a partition it no longer owns.
func (t *OffsetTracker) Track(r Record) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	p, ok := t.parts[r.TP()]
	if !ok {
		return false
	}
	p.dispatched = append(p.dispatched, r.Offset)
	p.highestDispatched = r.Offset
	p.hasDispatched = true
	return true
}

// Ack records that a dispatched offset reached a terminal outcome: processed
// successfully, or retried to exhaustion and written to the dead-letter sink.
// A record that is still being retried must NOT be acked — the commit
// watermark holding still is exactly what preserves at-least-once.
func (t *OffsetTracker) Ack(r Record) {
	t.mu.Lock()
	defer t.mu.Unlock()
	p, ok := t.parts[r.TP()]
	if !ok {
		// Partition was revoked while the record was in flight. The new
		// owner will reprocess from the last committed offset.
		return
	}
	p.acked[r.Offset] = struct{}{}

	before := p.head
	// Retire the contiguous prefix of completed offsets.
	for p.head < len(p.dispatched) {
		off := p.dispatched[p.head]
		if _, done := p.acked[off]; !done {
			break
		}
		delete(p.acked, off)
		p.head++
	}
	advanced := p.head > before

	// Reclaim the retired prefix so the slice does not grow without bound.
	if p.head > 0 && p.head == len(p.dispatched) {
		p.dispatched = p.dispatched[:0]
		p.head = 0
	} else if p.head > 1024 {
		p.dispatched = append(p.dispatched[:0], p.dispatched[p.head:]...)
		p.head = 0
	}

	if advanced {
		close(t.retired)
		t.retired = make(chan struct{})
	}
}

// WaitDrained blocks until no dispatched-but-un-acked records remain for the
// given partitions, or ctx expires. It is the drain step of a rebalance: the
// partition's final offsets are only meaningful once its in-flight work has
// finished.
func (t *OffsetTracker) WaitDrained(ctx context.Context, tps ...TopicPartition) error {
	for {
		t.mu.Lock()
		n := 0
		for _, tp := range tps {
			if p, ok := t.parts[tp]; ok {
				n += len(p.dispatched) - p.head
			}
		}
		wait := t.retired
		t.mu.Unlock()

		if n == 0 {
			return nil
		}
		select {
		case <-wait:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// Committable returns the offsets to send in the next OffsetCommit request,
// in Kafka's "next offset to consume" form. Partitions whose watermark has
// not moved since the last successful commit are omitted, so an idle
// consumer issues empty commits instead of rewriting the same offsets.
func (t *OffsetTracker) Committable() map[TopicPartition]int64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	out := make(map[TopicPartition]int64, len(t.parts))
	for tp, p := range t.parts {
		wm, ok := p.watermark()
		if !ok {
			continue
		}
		if p.hasCommitted && wm == p.lastCommitted {
			continue
		}
		out[tp] = wm
	}
	return out
}

// Commit marks the offsets returned by a previous Committable call as durably
// committed. Call it only after the broker acknowledges the commit.
func (t *OffsetTracker) Commit(offsets map[TopicPartition]int64) {
	t.mu.Lock()
	defer t.mu.Unlock()
	for tp, off := range offsets {
		if p, ok := t.parts[tp]; ok {
			p.lastCommitted = off
			p.hasCommitted = true
		}
	}
}

// InFlight reports how many dispatched offsets have not been acked, summed
// across partitions. It is a useful invariant check: after a clean drain it
// must be zero.
func (t *OffsetTracker) InFlight() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	n := 0
	for _, p := range t.parts {
		n += len(p.dispatched) - p.head
	}
	return n
}

// watermark returns the next offset to consume for this partition.
func (p *partitionState) watermark() (int64, bool) {
	if p.head < len(p.dispatched) {
		// Oldest offset still in flight — commit up to, but not including, it.
		return p.dispatched[p.head], true
	}
	if p.hasDispatched {
		return p.highestDispatched + 1, true
	}
	return 0, false
}

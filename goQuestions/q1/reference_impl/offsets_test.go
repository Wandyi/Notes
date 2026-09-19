package kafkaworker

import (
	"context"
	"testing"
	"time"
)

var tp0 = TopicPartition{Topic: "orders", Partition: 0}

func rec(off int64) Record {
	return Record{Topic: tp0.Topic, Partition: tp0.Partition, Offset: off}
}

func TestWatermarkHoldsForOldestInFlight(t *testing.T) {
	tr := NewOffsetTracker()
	tr.Assign(tp0)

	for off := int64(100); off <= 104; off++ {
		if !tr.Track(rec(off)) {
			t.Fatalf("track %d rejected", off)
		}
	}

	// Complete out of order, leaving 100 in flight.
	tr.Ack(rec(102))
	tr.Ack(rec(104))
	tr.Ack(rec(101))

	if got := tr.Committable()[tp0]; got != 100 {
		t.Fatalf("watermark = %d, want 100 (offset 100 still in flight)", got)
	}

	// Completing the oldest collapses the whole contiguous run.
	tr.Ack(rec(100))
	if got := tr.Committable()[tp0]; got != 103 {
		t.Fatalf("watermark = %d, want 103", got)
	}

	tr.Ack(rec(103))
	if got := tr.Committable()[tp0]; got != 105 {
		t.Fatalf("watermark = %d, want 105 (all done)", got)
	}
	if n := tr.InFlight(); n != 0 {
		t.Fatalf("in-flight = %d, want 0", n)
	}
}

// Compaction and transaction markers leave holes in the offset sequence; the
// watermark must follow the offsets actually dispatched, not offset+1 counting.
func TestWatermarkToleratesOffsetGaps(t *testing.T) {
	tr := NewOffsetTracker()
	tr.Assign(tp0)
	for _, off := range []int64{10, 17, 18, 40} {
		tr.Track(rec(off))
	}
	tr.Ack(rec(10))
	tr.Ack(rec(17))
	if got := tr.Committable()[tp0]; got != 18 {
		t.Fatalf("watermark = %d, want 18", got)
	}
	tr.Ack(rec(18))
	tr.Ack(rec(40))
	if got := tr.Committable()[tp0]; got != 41 {
		t.Fatalf("watermark = %d, want 41", got)
	}
}

func TestCommittableSkipsUnchangedPartitions(t *testing.T) {
	tr := NewOffsetTracker()
	tr.Assign(tp0)
	tr.Track(rec(1))
	tr.Ack(rec(1))

	offsets := tr.Committable()
	if len(offsets) != 1 {
		t.Fatalf("first commit should include the partition, got %v", offsets)
	}
	tr.Commit(offsets)

	if got := tr.Committable(); len(got) != 0 {
		t.Fatalf("unchanged watermark should not be recommitted, got %v", got)
	}

	tr.Track(rec(2))
	tr.Ack(rec(2))
	if got := tr.Committable()[tp0]; got != 3 {
		t.Fatalf("watermark = %d, want 3", got)
	}
}

func TestRevokedPartitionDropsTrackAndAck(t *testing.T) {
	tr := NewOffsetTracker()
	tr.Assign(tp0)
	tr.Track(rec(5))
	tr.Revoke(tp0)

	if tr.Track(rec(6)) {
		t.Fatal("Track on a revoked partition must be rejected")
	}
	tr.Ack(rec(5)) // must not panic
	if got := tr.Committable(); len(got) != 0 {
		t.Fatalf("revoked partition still committable: %v", got)
	}
}

func TestWaitDrained(t *testing.T) {
	tr := NewOffsetTracker()
	tr.Assign(tp0)
	tr.Track(rec(1))
	tr.Track(rec(2))

	go func() {
		time.Sleep(10 * time.Millisecond)
		tr.Ack(rec(2))
		tr.Ack(rec(1))
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := tr.WaitDrained(ctx, tp0); err != nil {
		t.Fatalf("WaitDrained: %v", err)
	}
	if n := tr.InFlight(); n != 0 {
		t.Fatalf("in-flight = %d, want 0", n)
	}
}

func TestWaitDrainedRespectsContext(t *testing.T) {
	tr := NewOffsetTracker()
	tr.Assign(tp0)
	tr.Track(rec(1)) // never acked

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if err := tr.WaitDrained(ctx, tp0); err == nil {
		t.Fatal("WaitDrained should have timed out")
	}
}

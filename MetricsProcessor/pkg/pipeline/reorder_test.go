package pipeline

import (
	"testing"
	"time"

	"github.com/vaibhav/metricsprocessor/pkg/model"
)

func seqPoint(seq uint64) *model.Point {
	return &model.Point{Service: "s", Name: "n", Source: "src", Seq: seq, EventTime: time.Unix(0, int64(seq))}
}

func seqsOf(pts []*model.Point) []uint64 {
	out := make([]uint64, len(pts))
	for i, p := range pts {
		out[i] = p.Seq
	}
	return out
}

func equalSeqs(a []uint64, b ...uint64) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestReorderReleasesInSequenceOrder(t *testing.T) {
	rb := newReorderBuffer(2, time.Second)
	now := time.Unix(100, 0)

	var got []uint64
	// Arrival order 3,1,2,4 -- the depth bound (2) forces a start once three
	// points are parked, and the origin is the lowest sequence seen, not the
	// first one to arrive.
	for _, seq := range []uint64{3, 1, 2, 4} {
		out, res, gap := rb.push(seqPoint(seq), now, nil)
		if res != pushOK {
			t.Fatalf("seq %d: got %v, want pushOK", seq, res)
		}
		if gap != 0 {
			t.Fatalf("seq %d: unexpected gap %d", seq, gap)
		}
		got = append(got, seqsOf(out)...)
	}
	if !equalSeqs(got, 1, 2, 3, 4) {
		t.Fatalf("released %v, want [1 2 3 4]", got)
	}
}

func TestReorderDeclaresGapAfterStall(t *testing.T) {
	rb := newReorderBuffer(8, 50*time.Millisecond)
	t0 := time.Unix(100, 0)

	// Establish the stream at sequence 1.
	out, _, _ := rb.push(seqPoint(1), t0, nil)
	out, _ = rb.flush(t0.Add(time.Second)) // start-up delay elapses
	if !equalSeqs(seqsOf(out), 1) {
		t.Fatalf("start: released %v, want [1]", seqsOf(out))
	}

	// 2 is lost in the network; 3 and 4 arrive.
	for _, seq := range []uint64{3, 4} {
		out, _, gap := rb.push(seqPoint(seq), t0.Add(2*time.Second), nil)
		if len(out) != 0 || gap != 0 {
			t.Fatalf("seq %d should still be parked, got %v gap=%d", seq, seqsOf(out), gap)
		}
	}

	// Before the stall deadline nothing is released: we are still hoping for 2.
	if out, gap := rb.flush(t0.Add(2*time.Second + 10*time.Millisecond)); len(out) != 0 || gap != 0 {
		t.Fatalf("released too early: %v gap=%d", seqsOf(out), gap)
	}

	// After it, the gap is declared and the stream moves on rather than stalling
	// forever behind one lost point.
	out, gap := rb.flush(t0.Add(3 * time.Second))
	if !equalSeqs(seqsOf(out), 3, 4) {
		t.Fatalf("released %v, want [3 4]", seqsOf(out))
	}
	if gap != 1 {
		t.Fatalf("gap = %d, want 1", gap)
	}
}

func TestReorderSuppressesDuplicates(t *testing.T) {
	rb := newReorderBuffer(1, time.Second)
	now := time.Unix(100, 0)

	rb.push(seqPoint(1), now, nil)
	rb.push(seqPoint(2), now, nil) // exceeds depth 1 -> starts at 1
	out, _ := rb.flush(now.Add(time.Hour))
	_ = out

	// At-least-once delivery replays 1 and 2. Both must be discarded so the fold
	// stays idempotent.
	for _, seq := range []uint64{1, 2} {
		out, res, _ := rb.push(seqPoint(seq), now, nil)
		if res != pushDuplicate {
			t.Fatalf("replay of seq %d: got %v, want pushDuplicate", seq, res)
		}
		if len(out) != 0 {
			t.Fatalf("replay of seq %d released %v", seq, seqsOf(out))
		}
	}
}

func TestReorderDepthBoundsMemory(t *testing.T) {
	rb := newReorderBuffer(4, time.Hour)
	now := time.Unix(100, 0)

	// Establish the stream, then withhold 100 and flood with later sequences.
	rb.push(seqPoint(99), now, nil)
	rb.flush(now.Add(time.Hour))

	for seq := uint64(101); seq <= 200; seq++ {
		rb.push(seqPoint(seq), now, nil)
		if rb.pending() > rb.depth+1 {
			t.Fatalf("buffer grew to %d with depth %d", rb.pending(), rb.depth)
		}
	}
}

func TestPartitionIsStableAndDeterministic(t *testing.T) {
	// The whole ordering guarantee rests on gateway and aggregator agreeing on
	// this function, in separate processes, across restarts.
	key := model.BuildSeriesKey("checkout", "http_requests", map[string]string{"route": "/pay", "code": "200"})
	first := PartitionFor(key, 16)
	for i := 0; i < 1000; i++ {
		if got := PartitionFor(key, 16); got != first {
			t.Fatalf("partition drifted: %d != %d", got, first)
		}
	}
	// Label map ordering must not change the key.
	same := model.BuildSeriesKey("checkout", "http_requests", map[string]string{"code": "200", "route": "/pay"})
	if same != key {
		t.Fatalf("series key depends on label map order:\n%q\n%q", key, same)
	}
	if PartitionFor("anything", 1) != 0 {
		t.Fatal("single partition must always be 0")
	}
}

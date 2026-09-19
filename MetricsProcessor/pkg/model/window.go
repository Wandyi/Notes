package model

import "time"

// WindowStart snaps t down to the start of the tumbling window of the given size.
//
// Windows are aligned to the epoch rather than to process start, so every shard,
// every replica, and every restart agree on window boundaries without any
// coordination. That agreement is what makes independently produced Aggregates
// mergeable.
func WindowStart(t time.Time, size time.Duration) time.Time {
	if size <= 0 {
		return t
	}
	n := t.UnixNano()
	// Floor division that is correct for pre-epoch timestamps too.
	d := int64(size)
	off := n % d
	if off < 0 {
		off += d
	}
	return time.Unix(0, n-off).UTC()
}

// WindowFor returns the [start, end) bounds of the window containing t.
func WindowFor(t time.Time, size time.Duration) (start, end time.Time) {
	start = WindowStart(t, size)
	return start, start.Add(size)
}

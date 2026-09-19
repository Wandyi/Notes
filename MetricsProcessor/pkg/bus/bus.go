// Package bus is the transport seam between the ingest gateway and the
// aggregator.
//
// The interface exists to make one property explicit and testable: *the transport
// must preserve order per partition key*. Per-series ordering inside the
// aggregator is worth nothing if the hop before it reorders. Any implementation
// of Bus -- the in-process one here, Kafka, NATS JetStream, Kinesis -- must
// deliver envelopes with the same key to the same consumer, in publish order.
//
// Mapping to Kafka: Envelope.Key is the record key, Partitions is the topic's
// partition count, and Subscribe's group is the consumer group. The FNV-1a
// partitioner in pkg/pipeline must be configured as the producer partitioner so
// that gateway and broker agree.
package bus

import (
	"context"
	"errors"

	"github.com/vaibhav/metricsprocessor/pkg/model"
)

// Envelope is one ordered unit of transfer. Grouping points by key lets a batch
// of a thousand points cost one transport operation without breaking ordering,
// because every point in an envelope shares a key.
type Envelope struct {
	Key    string         `json:"key"`
	Points []*model.Point `json:"points"`
	// Attempt counts redeliveries. A handler can use it to decide when to stop
	// retrying a poisoned envelope and let it go to the dead-letter sink.
	Attempt int `json:"attempt"`
}

// Handler consumes envelopes for one partition. It is called from a single
// goroutine per partition, so a handler sees its partition's envelopes strictly
// in order and needs no locking of its own.
//
// Returning an error requests redelivery. Returning ErrDrop discards the envelope
// without retrying, which is the right answer for data that will never become
// valid.
type Handler func(ctx context.Context, e Envelope) error

// ErrDrop tells the bus not to retry.
var ErrDrop = errors.New("bus: drop envelope")

// ErrClosed is returned by Publish after Close.
var ErrClosed = errors.New("bus: closed")

// Bus is a partitioned, ordered, at-least-once message transport.
type Bus interface {
	// Publish appends points to the partition owning key. It blocks when the
	// partition is full: losing telemetry silently at the transport layer is
	// worse than pushing back on the gateway, which can shed with a status code.
	Publish(ctx context.Context, key string, pts []*model.Point) error
	// Subscribe starts one consumer goroutine per partition and returns once
	// they are running. Cancel ctx to stop them.
	Subscribe(ctx context.Context, group string, h Handler) error
	// Partitions is the ordering domain count.
	Partitions() int
	Close() error
}

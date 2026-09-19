// Package kafkaworker contains the concurrency core of a high-throughput
// Kafka consumer: bounded worker pool, per-partition offset watermark
// tracking for at-least-once delivery, and an adaptive concurrency limiter
// that applies backpressure when downstream dependencies slow down.
//
// The package deliberately depends only on the standard library. The Kafka
// client (franz-go, sarama, confluent-kafka-go) is injected through the
// Fetcher and Committer interfaces so the concurrency logic is testable
// without a broker.
package kafkaworker

import (
	"context"
	"time"
)

// TopicPartition identifies a Kafka partition.
type TopicPartition struct {
	Topic     string
	Partition int32
}

// Record is a single Kafka message.
type Record struct {
	Topic     string
	Partition int32
	Offset    int64
	Key       []byte
	Value     []byte
	Timestamp time.Time
}

// TP returns the record's topic-partition.
func (r Record) TP() TopicPartition {
	return TopicPartition{Topic: r.Topic, Partition: r.Partition}
}

// Fetcher is the read side of the Kafka client. Poll must block until at
// least one record is available, ctx is cancelled, or a fatal error occurs.
type Fetcher interface {
	Poll(ctx context.Context) ([]Record, error)
}

// Committer is the offset-commit side of the Kafka client. Offsets are
// "next offset to consume", matching Kafka's OffsetCommit semantics.
type Committer interface {
	Commit(ctx context.Context, offsets map[TopicPartition]int64) error
}

// Handler processes a single record. It must be safe for concurrent use and
// must respect ctx cancellation. Returning a non-nil error means the record
// was not processed successfully; the pool applies the configured retry and
// dead-letter policy.
//
// Because delivery is at-least-once, Handle must be idempotent: the same
// record can be delivered again after a crash, a rebalance, or a commit
// that never reached the broker.
type Handler interface {
	Handle(ctx context.Context, r Record) error
}

// HandlerFunc adapts a function to Handler.
type HandlerFunc func(ctx context.Context, r Record) error

// Handle implements Handler.
func (f HandlerFunc) Handle(ctx context.Context, r Record) error { return f(ctx, r) }

// DeadLetter receives records that exhausted their retry budget. It is the
// only place where a record is dropped from the processing path, so it must
// be durable (a DLQ topic, an outbox table) — anything else silently turns
// at-least-once into at-most-once.
type DeadLetter interface {
	Publish(ctx context.Context, r Record, cause error) error
}

// DeadLetterFunc adapts a function to DeadLetter.
type DeadLetterFunc func(ctx context.Context, r Record, cause error) error

// Publish implements DeadLetter.
func (f DeadLetterFunc) Publish(ctx context.Context, r Record, cause error) error {
	return f(ctx, r, cause)
}

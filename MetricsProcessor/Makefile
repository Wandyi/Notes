GO      ?= go
SERVICES = ingest-gateway aggregator query-api
BIN      = bin

.PHONY: all build test race bench lint demo docker clean

all: build test

build:
	@mkdir -p $(BIN)
	@for s in $(SERVICES) demo; do \
		echo "  building $$s"; \
		$(GO) build -trimpath -ldflags="-s -w" -o $(BIN)/$$s ./cmd/$$s || exit 1; \
	done

## test runs the unit and integration suites.
test:
	$(GO) test ./... -count=1

## race is the gate that matters for this codebase: the ordering and isolation
## guarantees are all claims about concurrent behaviour.
race:
	$(GO) test -race -count=1 ./...

bench:
	$(GO) test -run XXX -bench . -benchmem -benchtime 3s ./pkg/pipeline/

lint:
	$(GO) vet ./...
	gofmt -l . | tee /dev/stderr | (! read)

## demo runs all three services in one process with injected failures.
demo:
	$(GO) run ./cmd/demo

docker:
	@for s in $(SERVICES); do \
		echo "  image metrics/$$s"; \
		docker build -f deploy/Dockerfile --build-arg SERVICE=$$s -t metrics/$$s:dev . || exit 1; \
	done

up: docker
	docker compose -f deploy/docker-compose.yml up

clean:
	rm -rf $(BIN)

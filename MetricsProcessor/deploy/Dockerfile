# One Dockerfile, three images. The services share a module, so building them
# from one file keeps their base image, Go version, and hardening in lockstep --
# three near-identical Dockerfiles drift within a quarter.
ARG GO_VERSION=1.24

FROM golang:${GO_VERSION}-alpine AS build
WORKDIR /src

# Dependency layer first so source edits do not invalidate the module cache.
# go.* rather than "go.mod go.sum": the module currently has no third-party
# dependencies and therefore no go.sum, and a COPY wildcard that matches nothing
# fails the build.
COPY go.* ./
RUN go mod download

COPY . .
ARG SERVICE
RUN test -n "$SERVICE" || (echo "build arg SERVICE is required" && exit 1) && \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/service ./cmd/${SERVICE}

# Distroless-style final stage: no shell, no package manager, nothing for an
# attacker to pivot into if they get code execution in the service.
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/service /service
USER nonroot:nonroot
EXPOSE 8081 8082 8083
ENTRYPOINT ["/service"]

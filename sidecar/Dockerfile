# The sidecar is a single static binary with the UI embedded — the smallest
# image that can run it is a build stage plus a bare runtime.
FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY main.go ui.html ./
RUN CGO_ENABLED=0 go build -o /blurd-sidecar .

FROM alpine:3.20
RUN adduser -D -u 65532 app
USER app
COPY --from=build /blurd-sidecar /blurd-sidecar
EXPOSE 8790
ENTRYPOINT ["/blurd-sidecar"]

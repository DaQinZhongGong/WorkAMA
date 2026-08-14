FROM golang:1.26-alpine AS build
WORKDIR /src
COPY apps/gateway/go.mod ./go.mod
COPY apps/gateway/go.sum ./go.sum
COPY apps/gateway/vendor ./vendor
COPY apps/gateway/cmd ./cmd
COPY apps/gateway/internal ./internal
RUN CGO_ENABLED=0 go build -mod=vendor -tags=pgx -o /out/gateway ./cmd/gateway

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /out/gateway /usr/local/bin/gateway
EXPOSE 8080
ENTRYPOINT ["gateway"]

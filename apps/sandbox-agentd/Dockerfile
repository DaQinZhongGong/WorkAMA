FROM golang:1.26-alpine AS build
ENV GOPROXY=https://goproxy.cn,direct
WORKDIR /src
COPY apps/sandbox-agentd/go.mod apps/sandbox-agentd/go.sum ./
RUN go mod download
COPY apps/sandbox-agentd/cmd ./cmd
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/sandbox-agentd ./cmd/sandbox-agentd

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin sandbox
COPY --from=build /out/sandbox-agentd /usr/local/bin/sandbox-agentd
WORKDIR /workspace
USER sandbox
ENTRYPOINT ["/usr/local/bin/sandbox-agentd"]
CMD ["serve"]

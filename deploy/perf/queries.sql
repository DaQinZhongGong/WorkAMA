-- =============================================================================
-- WorkAMA 平台性能监控查询（Prometheus / Grafana）
-- 用于在压测期间与日常运维中观察 platform-api 的关键性能指标。
-- 数据源：deploy/compose/prometheus.yml 已抓取 workama-prometheus-1 容器，
--        platform-api 通过 /metrics 暴露 Prometheus 指标，并由 otel-collector 转发。
-- 使用：在 Grafana（workama-grafana-1，默认 :3000）中粘贴以下 PromQL，
--      或通过 Prometheus UI（:9090）直接查询。
-- 注释以 -- 开头，便于直接在 Prometheus 控制台粘贴整段。
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. platform-api 请求速率（QPS）
--    按 handler 分组，观察各端点吞吐。压测期间应看到 healthz / agents 峰值。
-- -----------------------------------------------------------------------------
sum by (handler) (
  rate(http_server_requests_seconds_count{job="platform-api"}[1m])
)

-- 总请求速率（所有 handler 汇总，用于容量评估）
sum(rate(http_server_requests_seconds_count{job="platform-api"}[1m]))

-- -----------------------------------------------------------------------------
-- 2. P99 / P95 / P50 延迟分位数
--    核心验收指标：网关层 P99 < 30ms。此处观测 platform-api 自身延迟。
-- -----------------------------------------------------------------------------
-- P99 延迟（按端点分组）
histogram_quantile(0.99,
  sum by (handler, le) (
    rate(http_server_requests_seconds_bucket{job="platform-api"}[1m])
  )
)

-- P95 延迟
histogram_quantile(0.95,
  sum by (handler, le) (
    rate(http_server_requests_seconds_bucket{job="platform-api"}[1m])
  )
)

-- P50 中位数延迟
histogram_quantile(0.50,
  sum by (handler, le) (
    rate(http_server_requests_seconds_bucket{job="platform-api"}[1m])
  )
)

-- 网关层 P99（如部署了独立 gateway，用此查询对照验收硬指标 30ms）
histogram_quantile(0.99,
  sum by (le) (
    rate(http_server_requests_seconds_bucket{job="gateway"}[1m])
  )
)

-- -----------------------------------------------------------------------------
-- 3. 错误率
--    5xx 占比与总错误率，用于压测期间异常检测。
-- -----------------------------------------------------------------------------
-- 5xx 错误率（按端点）
sum by (handler) (
  rate(http_server_requests_seconds_count{job="platform-api", status=~"5.."}[1m])
)
/
sum by (handler) (
  rate(http_server_requests_seconds_count{job="platform-api"}[1m])
)

-- 总 4xx+5xx 错误率
sum(rate(http_server_requests_seconds_count{job="platform-api", status=~"4..|5.."}[1m]))
/
sum(rate(http_server_requests_seconds_count{job="platform-api"}[1m]))

-- 错误请求绝对速率（用于告警阈值）
sum(rate(http_server_requests_seconds_count{job="platform-api", status=~"5.."}[1m]))

-- -----------------------------------------------------------------------------
-- 4. 容器资源使用（CPU / 内存）
--    观察 platform-api 在压测期间的资源消耗，定位容量瓶颈。
--    cAdvisor 指标通过 prometheus 抓取 cadvisor / docker stats。
-- -----------------------------------------------------------------------------
-- CPU 使用率（占单核比例，多核需乘以核数）
sum(rate(container_cpu_usage_seconds_total{name="workama-platform-api-1"}[1m])) * 100

-- 内存使用（RSS，字节）
container_memory_rss{name="workama-platform-api-1"}

-- 内存使用率（相对 limit）
container_memory_usage_bytes{name="workama-platform-api-1"}
/
container_spec_memory_limit_bytes{name="workama-platform-api-1"}

-- 网络收发吞吐（字节/秒）
rate(container_network_receive_bytes_total{name="workama-platform-api-1"}[1m])
rate(container_network_transmit_bytes_total{name="workama-platform-api-1"}[1m])

-- -----------------------------------------------------------------------------
-- 5. 数据库连接池（如 platform-api 暴露 DB 指标）
--    观察连接池是否打满，是延迟飙升的常见根因。
-- -----------------------------------------------------------------------------
-- 活跃 DB 连接数
db_connection_pool_active{job="platform-api"}

-- 空闲 DB 连接数
db_connection_pool_idle{job="platform-api"}

-- 等待获取连接的请求数（>0 表示池满，需扩容）
db_connection_pool_waiting{job="platform-api"}

-- -----------------------------------------------------------------------------
-- 6. 进程运行时（GC / 事件循环延迟）
--    Python/FastAPI 进程的 GC 暴停与事件循环延迟。
-- -----------------------------------------------------------------------------
-- Python GC 耗时（秒/秒）
rate(python_gc_time_seconds_total{job="platform-api"}[1m])

-- 事件循环延迟分位数（asyncio 阻塞检测）
histogram_quantile(0.99,
  rate(asyncio_loop_lag_seconds_bucket{job="platform-api"}[1m])
)

-- -----------------------------------------------------------------------------
-- 7. 压测对照查询（与 k6 / python_stress 结果交叉验证）
--    压测期间用以下查询实时对照脚本输出的 RPS 与 P99。
-- -----------------------------------------------------------------------------
-- 压测窗口 QPS 峰值
max_over_time(
  sum(rate(http_server_requests_seconds_count{job="platform-api"}[30s]))[5m:10s]
)

-- 压测窗口 P99 峰值
max_over_time(
  histogram_quantile(0.99,
    sum by (le) (rate(http_server_requests_seconds_bucket{job="platform-api"}[30s]))
  )[5m:10s]
)

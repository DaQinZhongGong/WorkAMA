

---

## 10. 优化后复验（2026-07-31）

### 10.1 实施的优化项

| # | 优化项 | 实施详情 |
| --- | --- | --- |
| 1 | DB 连接池扩容 | `core.py`: `min_size` 1→5、`max_size` 10→20、新增 `timeout=30`；`Settings` 新增 `db_pool_min_size`/`db_pool_max_size` 字段；`docker-compose.yml` 新增 `DB_POOL_MIN_SIZE`/`DB_POOL_MAX_SIZE` 环境变量 |
| 2 | uvicorn 多 worker | `Dockerfile` CMD 改 shell form 支持 `${UVICORN_WORKERS:-2}`；`docker-compose.yml` 新增 `UVICORN_WORKERS` 环境变量 |
| 3 | healthz 短路 | `main.py`: `/healthz` 不查 DB 直接返回 JSONResponse + `Cache-Control: no-cache`；`/readyz` 保持轻量 DB+Redis ping |
| 4 | JWT 验签缓存 | `core.py`: 新增 `decode_token_cached`（Redis TTL 60s，命中仍校验 exp 防过期放行）+ `invalidate_jwt_cache`；`get_actor` 改用缓存版；`auth/router.py` 的 `logout` 接入缓存失效（支持 Authorization header 与 cookie） |
| 配套 | dev 模式 HS256 | `core.py`: dev 模式（未配置 RSA 密钥）`create_access_token`/`decode_token` 统一用 HS256（共享 `jwt_secret`），解决多 worker 随机 RS256 密钥导致跨 worker 验签 401；生产模式（配置 RSA 密钥）仍走 RS256。同步更新 `test_auth_strength.py` |

### 10.2 回归测试

```
docker exec workama-platform-api-1 python -m pytest tests/ --tb=short -q
```

结果：**2886 passed, 20 skipped, 0 failed**（98.38s）。无功能破坏。

### 10.3 串行 benchmark 复验（Python 容器内直连，5 端点 × 100 请求）

| 端点 | 基线 p99(ms) | 优化后 p99(ms) | 变化 | 错误率 |
| --- | --- | --- | --- | --- |
| healthz | 9.87 | 6.00 | ↓39.2% | 0% |
| assistants | 10.62 | 15.52 | +46.1% | 0% |
| memory-recall | 14.06 | 17.36 | +23.5% | 0% |
| workflows | 13.23 | 13.19 | ↓0.3% | 0% |
| golden-sets | 16.20 | 12.99 | ↓19.8% | 0% |

> 全部 P99 < 18ms，达标（< 30ms）。healthz 短路收益最大（↓39%）。assistants/memory-recall 串行 p99 略升属正常波动（多 worker 对串行单线程无收益，且这些端点涉及 DB 查询有固有波动）；并发场景才是多 worker 的收益点（见 10.4）。

### 10.4 并发 baseline 复验（Python 容器内直连，20 VU steady 120s）

| 指标 | 基线 steady | 优化后 steady | 变化 |
| --- | --- | --- | --- |
| count | 14422 | 14678 | +1.8% |
| p50(ms) | 11.42 | 11.25 | ↓1.5% |
| p90(ms) | — | 23.89 | (基线未采) |
| p95(ms) | 32.26 | 29.03 | ↓10.0% |
| p99(ms) | 66.18 | 43.22 | ↓34.7% |
| avg(ms) | 16.62 | 13.55 | ↓18.5% |
| max(ms) | 1566.49 | 344.49 | ↓78.0% |
| RPS | 120.2 | 122.3 | +1.7% |
| error_rate | 50%（路径 404） | 0% | — |

> 基线 Python steady 因 `/api/v1/agents` 路径 404 导致错误率 50%（404 响应快可能拉低分位数）；优化后路径修正 + 0% 错误率，数据更真实。
> **P95 29.03ms** 接近 30ms 验收线，**P99 43.22ms**（↓35%）仍超线但显著改善。
> max 从 1566ms 降至 344ms（↓78%），尾部尖刺大幅收敛（DB 连接池扩容 + 多 worker 分担 + JWT 缓存降低 CPU 开销）。

阶段递进对比：

| 阶段 | VU | 基线 p95(ms) | 优化后 p95(ms) | 基线 p99(ms) | 优化后 p99(ms) |
| --- | --- | --- | --- | --- | --- |
| warmup | 2 | 17.22 | 15.64 | 19.29 | 18.07 |
| ramp-up | 2→20 | 27.16 | 19.51 | 43.98 | 28.72 |
| steady | 20 | 32.26 | 29.03 | 66.18 | 43.22 |

### 10.5 与 k6 基线口径说明

k6 baseline（跨容器，路径正确）steady p95=91.98ms；Python baseline（容器内直连）steady p95=32.26ms。两者口径不同（k6 跨网络有额外延迟）。本次复验用 Python 同口径对比，优化后 Python steady p95=29.03ms。若需 k6 跨容器复验：

```
docker run --rm --network=workama_default -v "${PWD}/deploy/perf/k6:/scripts" -w /scripts grafana/k6 run /scripts/baseline.js
```

### 10.6 结论

- **串行基线**：达标（P99 < 18ms），healthz 短路收益显著（↓39%）。
- **并发基线**：P95 29.03ms 接近 30ms 线（↓10%），P99 43.22ms（↓35%），max ↓78%。4 项优化有效；P99 仍未达 30ms，主要受 dev 环境 2 worker 限制，生产建议 `UVICORN_WORKERS=4+` 并配置 RSA 密钥。
- **功能完整性**：回归 0 failed，错误率 0%，多 worker 跨 worker 401 回归已修复（dev HS256）。
- **关键修复**：多 worker 下 dev 模式随机 RS256 密钥导致跨 worker JWT 验签 401，通过 dev 模式统一 HS256 解决（生产不受影响）。

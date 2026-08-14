#!/usr/bin/env python3
"""
WorkAMA 平台性能压测备选脚本（Python 标准库实现）。

当宿主机无法拉取 grafana/k6 镜像时，在 workama-platform-api-1 容器内执行此脚本，
复现 baseline.js + api-benchmark.js 的指标口径，作为同源基线数据来源。

设计要点：
- 仅使用 Python 标准库（urllib + concurrent.futures），不依赖 requests。
- baseline 模式：阶段化加压（warmup/ramp-up/steady/ramp-down），采集延迟分布。
- benchmark 模式：单线程串行，每端点固定 N 请求，输出对比表格。
- 输出 P50/P90/P95/P99/avg/min/max + RPS + error_rate，与 k6 指标对齐。

执行示例（在 platform-api 容器内）：
    docker cp deploy/perf/python_stress.py workama-platform-api-1:/tmp/stress.py
    docker exec workama-platform-api-1 python /tmp/stress.py baseline
    docker exec workama-platform-api-1 python /tmp/stress.py benchmark

环境变量：
    STRESS_BASE_URL   默认 http://localhost:8000（容器内直连）
    STRESS_EMAIL      默认 tester@workama.example.com
    STRESS_PASSWORD   默认 WorkAMA-Test-2026!
    STRESS_BENCH_REQS 默认 100（benchmark 模式每端点请求数）
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

# ---------- 全局配置 ----------
BASE_URL = os.environ.get("STRESS_BASE_URL", "http://localhost:8000").rstrip("/")
EMAIL = os.environ.get("STRESS_EMAIL", "tester@workama.example.com")
PASSWORD = os.environ.get("STRESS_PASSWORD", "WorkAMA-Test-2026!")
BENCH_REQS = int(os.environ.get("STRESS_BENCH_REQS", "100"))
TIMEOUT = 10  # 单请求超时秒数


# ---------- 工具函数 ----------
def http_request(method: str, path: str, token: str = "", body: bytes | None = None) -> tuple[int, float]:
    """发起一次 HTTP 请求，返回 (status, duration_ms)。失败返回 (0, duration)。"""
    url = f"{BASE_URL}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()  # 读完以释放连接
            status = resp.status
    except urllib.error.HTTPError as e:
        # HTTP 错误（4xx/5xx）仍记录状态与延迟
        try:
            e.read()
        except Exception:
            pass
        status = e.code
    except Exception:
        status = 0
    duration_ms = (time.perf_counter() - start) * 1000.0
    return status, duration_ms


def login() -> str:
    """登录获取 access_token。"""
    body = json.dumps({"email": EMAIL, "password": PASSWORD}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
    return data.get("access_token", "")


def percentiles(samples: list[float]) -> dict:
    """计算延迟分位数（毫秒）。"""
    if not samples:
        return {"count": 0, "p50": None, "p90": None, "p95": None, "p99": None,
                "avg": None, "min": None, "max": None, "rps": None, "error_rate": None}
    s = sorted(samples)
    n = len(s)

    def pct(p: float) -> float:
        if n == 1:
            return s[0]
        k = (n - 1) * p
        f = int(k)
        c = min(f + 1, n - 1)
        return s[f] + (s[c] - s[f]) * (k - f)

    return {
        "count": n,
        "p50": round(pct(0.50), 3),
        "p90": round(pct(0.90), 3),
        "p95": round(pct(0.95), 3),
        "p99": round(pct(0.99), 3),
        "avg": round(statistics.fmean(s), 3),
        "min": round(s[0], 3),
        "max": round(s[-1], 3),
    }


def fmt_table(rows: list[dict]) -> str:
    """将指标行渲染为对齐表格字符串。"""
    cols = ["endpoint", "count", "p50_ms", "p90_ms", "p95_ms", "p99_ms",
            "avg_ms", "min_ms", "max_ms", "rps", "error_rate"]
    lines = ["\t".join(c for c in cols)]
    for r in rows:
        lines.append("\t".join(
            ("" if r.get(c) is None else
             (f"{r[c]:.3f}" if isinstance(r[c], float) else str(r[c])))
            for c in cols))
    return "\n".join(lines)


# ---------- baseline 模式 ----------
@dataclass
class PhaseResult:
    name: str
    samples: list[float] = field(default_factory=list)
    errors: int = 0
    duration_s: float = 0.0


def run_phase(name: str, target_vus: int, duration_s: float, token: str,
              ramp: bool = False, ramp_from: int = 1) -> PhaseResult:
    """执行一个加压阶段，模拟 VU 并发请求。"""
    result = PhaseResult(name=name)
    end_time = time.perf_counter() + duration_s

    def worker():
        local_samples: list[float] = []
        local_errors = 0
        while time.perf_counter() < end_time:
            # 交替请求 healthz 与 assistants，模拟混合负载
            s1, d1 = http_request("GET", "/healthz")
            local_samples.append(d1)
            if s1 == 0 or s1 >= 400:
                local_errors += 1
            # 注：原路径 /api/v1/agents 返回 404，已校正为 /api/v1/assistants
            s2, d2 = http_request("GET", "/api/v1/assistants", token=token)
            local_samples.append(d2)
            if s2 == 0 or s2 >= 400:
                local_errors += 1
            # 模拟思考时间
            time.sleep(0.3)
        return local_samples, local_errors

    # 当前 VU 数（ramp 模式下随时间线性增长）
    if ramp and target_vus > ramp_from:
        # 分批拉起 VU，每批间隔 = duration / (target - from)
        step = (target_vus - ramp_from)
        interval = duration_s / max(step, 1)
        futures = []
        with ThreadPoolExecutor(max_workers=target_vus) as pool:
            for i in range(ramp_from, target_vus):
                futures.append(pool.submit(worker))
                time.sleep(interval)
            # 阶段末等待所有 worker 退出
            for f in as_completed(futures):
                s, e = f.result()
                result.samples.extend(s)
                result.errors += e
    else:
        with ThreadPoolExecutor(max_workers=target_vus) as pool:
            futures = [pool.submit(worker) for _ in range(target_vus)]
            for f in as_completed(futures):
                s, e = f.result()
                result.samples.extend(s)
                result.errors += e

    result.duration_s = duration_s
    return result


def run_baseline():
    """执行阶段化基线压测。"""
    print("=" * 70)
    print("WorkAMA 性能基线压测（Python 标准库实现，等价 k6 baseline.js）")
    print(f"目标: {BASE_URL}")
    print("=" * 70)

    t0 = time.perf_counter()
    token = login()
    if not token:
        print("ERROR: 登录失败，无法继续压测")
        sys.exit(2)
    print(f"[setup] 登录成功，token 长度={len(token)}，耗时={ (time.perf_counter()-t0)*1000:.1f}ms")

    phases = [
        ("warmup", 2, 30, False),
        ("ramp-up", 20, 60, True),
        ("steady", 20, 120, False),
        ("ramp-down", 0, 30, False),  # ramp-down 仅为记录用，实际靠停止 worker
    ]

    # 简化：ramp-down 不再追加请求，仅标记
    phase_results = []
    token_holder = token
    for name, vus, dur, ramp in phases:
        if name == "ramp-down":
            print(f"\n[{name}] ramp-down 阶段：停止加压，记录恢复点（{dur}s）")
            time.sleep(dur)
            phase_results.append(PhaseResult(name=name, samples=[], errors=0, duration_s=dur))
            continue
        print(f"\n[{name}] VUs={vus} duration={dur}s ramp={ramp} ...")
        pr = run_phase(name, vus, dur, token_holder, ramp=ramp, ramp_from=2 if name == "ramp-up" else vus)
        phase_results.append(pr)
        # 输出阶段即时摘要
        m = percentiles(pr.samples)
        err_rate = (pr.errors / len(pr.samples)) if pr.samples else 0.0
        rps = (len(pr.samples) / pr.duration_s) if pr.duration_s else 0.0
        print(f"  -> count={m['count']} p50={m['p50']} p95={m['p95']} p99={m['p99']} "
              f"avg={m['avg']} err={err_rate*100:.2f}% rps={rps:.1f}")

    # 汇总：steady 阶段为代表性结果
    steady = next((p for p in phase_results if p.name == "steady"), None)
    total_samples = []
    total_errors = 0
    for p in phase_results:
        total_samples.extend(p.samples)
        total_errors += p.errors

    print("\n" + "=" * 70)
    print("基线压测汇总")
    print("=" * 70)
    if steady and steady.samples:
        m = percentiles(steady.samples)
        err_rate = (steady.errors / len(steady.samples))
        rps = len(steady.samples) / steady.duration_s
        print(f"[steady 阶段代表数据]")
        print(f"  count={m['count']} p50={m['p50']}ms p90={m['p90']}ms p95={m['p95']}ms "
              f"p99={m['p99']}ms avg={m['avg']}ms min={m['min']}ms max={m['max']}ms")
        print(f"  RPS={rps:.1f} error_rate={err_rate*100:.2f}%")

    all_m = percentiles(total_samples)
    all_err = (total_errors / len(total_samples)) if total_samples else 0.0
    print(f"\n[全阶段汇总]")
    print(f"  count={all_m['count']} p50={all_m['p50']}ms p90={all_m['p90']}ms "
          f"p95={all_m['p95']}ms p99={all_m['p99']}ms avg={all_m['avg']}ms")
    print(f"  error_rate={all_err*100:.2f}%")

    # 输出机器可读 JSON，便于报告引用
    summary = {
        "mode": "baseline",
        "base_url": BASE_URL,
        "steady": percentiles(steady.samples) if steady else None,
        "steady_error_rate": (steady.errors / len(steady.samples)) if steady and steady.samples else None,
        "steady_rps": (len(steady.samples) / steady.duration_s) if steady and steady.samples else None,
        "all": all_m,
        "all_error_rate": all_err,
    }
    print("\nWORKAMA_BASELINE_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    print("=" * 70)


# ---------- benchmark 模式 ----------
# 注：任务描述原路径 /api/v1/agents、/api/v1/workflows-v2 在当前部署中返回 404，
# 经 openapi 与实际探测校正为 /api/v1/assistants、/api/v1/workflows。
# 如实记录于 baseline-report.md 的「路径校正」一节。
ENDPOINTS = [
    {"name": "healthz", "method": "GET", "path": "/healthz", "body": None},
    {"name": "assistants", "method": "GET", "path": "/api/v1/assistants", "body": None},
    {"name": "memory-recall", "method": "POST", "path": "/api/v1/memory-vectors/recall",
     "body": json.dumps({"query": "test", "limit": 5}).encode()},
    {"name": "workflows", "method": "GET", "path": "/api/v1/workflows", "body": None},
    {"name": "golden-sets", "method": "GET", "path": "/api/v1/knowledge/golden-sets?limit=5", "body": None},
]


def run_benchmark():
    """多端点基准对比：单线程串行，每端点 N 请求。"""
    print("=" * 70)
    print("WorkAMA API 多端点基准对比（Python 标准库实现，等价 k6 api-benchmark.js）")
    print(f"目标: {BASE_URL}  每端点请求数: {BENCH_REQS}")
    print("=" * 70)

    token = login()
    if not token:
        print("ERROR: 登录失败，无法继续压测")
        sys.exit(2)
    print(f"[setup] 登录成功，token 长度={len(token)}")

    rows = []
    for ep in ENDPOINTS:
        samples = []
        errors = 0
        statuses = {}
        t0 = time.perf_counter()
        for _ in range(BENCH_REQS):
            status, dur = http_request(ep["method"], ep["path"], token=token, body=ep["body"])
            samples.append(dur)
            statuses[status] = statuses.get(status, 0) + 1
            if status == 0 or status >= 400:
                errors += 1
        wall = time.perf_counter() - t0
        m = percentiles(samples)
        rps = (len(samples) / wall) if wall > 0 else 0.0
        err_rate = (errors / len(samples)) if samples else 0.0
        row = {
            "endpoint": ep["name"],
            "count": m["count"],
            "p50_ms": m["p50"],
            "p90_ms": m["p90"],
            "p95_ms": m["p95"],
            "p99_ms": m["p99"],
            "avg_ms": m["avg"],
            "min_ms": m["min"],
            "max_ms": m["max"],
            "rps": round(rps, 2),
            "error_rate": round(err_rate, 4),
            "statuses": statuses,
        }
        rows.append(row)
        print(f"  {ep['name']:<16} count={row['count']} p50={row['p50_ms']} "
              f"p95={row['p95_ms']} p99={row['p99_ms']} avg={row['avg_ms']} "
              f"rps={row['rps']} err={row['error_rate']*100:.2f}% statuses={statuses}")

    print("\n" + "=" * 70)
    print("基准对比表格")
    print("=" * 70)
    print(fmt_table(rows))
    print("\nWORKAMA_BENCH_TABLE=" + json.dumps(rows, ensure_ascii=False))
    print("=" * 70)


# ---------- 入口 ----------
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    if mode == "baseline":
        run_baseline()
    elif mode == "benchmark":
        run_benchmark()
    else:
        print(f"未知模式: {mode}，可选: baseline | benchmark")
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""聚焦测量：仅打 /healthz（纯 liveness，服务端零依赖）。

目的：归因 §10.10 混合基线中出现的 427ms worst-case 尖刺。因 /healthz 在服务端
不做任何 DB/Redis/业务工作（直接返回静态 JSON），其延迟完全由「客户端 urllib 线程
GIL 等待 + Docker 桥接网络抖动」决定。若 healthz-only 稳态 max 与 assistants-only
（41.9ms）同量级，则证明 427ms 为客户端/网络产物，非服务端依赖阻塞；decoupling
（healthz 纯 liveness / readyz 做依赖 ping）已生效，无需服务端改动。
口径同 §10.10（跨容器 / 限流已隔离 / 20 VU 稳态 120s）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = os.environ.get("STRESS_BASE_URL", "http://platform-api:8000").rstrip("/")
TIMEOUT = 10


def req(path: str) -> tuple[int, float]:
    r = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(r, timeout=TIMEOUT) as resp:
            resp.read()
            return resp.status, (time.perf_counter() - t0) * 1000.0
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except Exception:
            pass
        return e.code, (time.perf_counter() - t0) * 1000.0
    except Exception:
        return 0, (time.perf_counter() - t0) * 1000.0


def pct(samples: list[float], p: float) -> float:
    s = sorted(samples)
    n = len(s)
    if n == 1:
        return s[0]
    k = (n - 1) * p
    f = int(k)
    c = min(f + 1, n - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def main() -> None:
    vus = 20
    dur = 120.0
    samples: list[float] = []
    errors = 0
    end = time.perf_counter() + dur

    def worker():
        local = []
        local_err = 0
        while time.perf_counter() < end:
            s, d = req("/healthz")
            local.append(d)
            if s == 0 or s >= 400:
                local_err += 1
            time.sleep(0.3)
        return local, local_err

    with ThreadPoolExecutor(max_workers=vus) as pool:
        futs = [pool.submit(worker) for _ in range(vus)]
        for f in as_completed(futs):
            s, e = f.result()
            samples.extend(s)
            errors += e

    s = sorted(samples)
    n = len(s)
    rps = n / dur
    err_rate = errors / n if n else 0.0
    print(f"[healthz-only 20VU x {dur:.0f}s] n={n} rps={rps:.1f} err={err_rate*100:.2f}%")
    print(f"  p50={pct(s,0.5):.3f} p90={pct(s,0.9):.3f} p95={pct(s,0.95):.3f} "
          f"p99={pct(s,0.99):.3f} p99.9={pct(s,0.999):.3f} max={s[-1]:.3f} min={s[0]:.3f}")
    print("WORKAMA_HEALTHZ_SUMMARY=" + json.dumps({
        "n": n, "rps": round(rps, 1), "error_rate": round(err_rate, 4),
        "p50": round(pct(s, 0.5), 3), "p90": round(pct(s, 0.9), 3),
        "p95": round(pct(s, 0.95), 3), "p99": round(pct(s, 0.99), 3),
        "p99_9": round(pct(s, 0.999), 3), "max": round(s[-1], 3), "min": round(s[0], 3),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""聚焦测量：仅打 /api/v1/assistants（L1 命中热路径：actor L1 + list L1），20 VU 稳态。

用于在 L1 缓存上线后隔离「带认证 + 双 L1 命中」端点的尾延迟，区分 worst-case 尖刺
究竟落在 L1 热路径还是无认证探针（/healthz）。与 python_stress.py baseline 同口径
（跨容器 / 限流已隔离 / 20 VU），仅去掉 healthz 混合、延长稳态到 120s。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = os.environ.get("STRESS_BASE_URL", "http://platform-api:8000").rstrip("/")
EMAIL = os.environ.get("STRESS_EMAIL", "tester@workama.example.com")
PASSWORD = os.environ.get("STRESS_PASSWORD", "WorkAMA-Test-2026!")
TOKEN = ""
TIMEOUT = 10


def req(path: str, token: str) -> tuple[int, float]:
    url = f"{BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = urllib.request.Request(url, headers=headers, method="GET")
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


def login() -> str:
    body = json.dumps({"email": EMAIL, "password": PASSWORD}).encode()
    r = urllib.request.Request(f"{BASE_URL}/api/v1/auth/login", data=body,
                               headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode()).get("access_token", "")


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
    global TOKEN
    TOKEN = login()
    if not TOKEN:
        print("ERROR: login failed")
        raise SystemExit(2)
    vus = 20
    dur = 120.0
    samples: list[float] = []
    errors = 0
    end = time.perf_counter() + dur

    def worker():
        local = []
        local_err = 0
        while time.perf_counter() < end:
            s, d = req("/api/v1/assistants", TOKEN)
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
    print(f"[assistants-only 20VU x {dur:.0f}s] n={n} rps={rps:.1f} err={err_rate*100:.2f}%")
    print(f"  p50={pct(s,0.5):.3f} p90={pct(s,0.9):.3f} p95={pct(s,0.95):.3f} "
          f"p99={pct(s,0.99):.3f} p99.9={pct(s,0.999):.3f} max={s[-1]:.3f} min={s[0]:.3f}")
    print("WORKAMA_ASSISTANTS_SUMMARY=" + json.dumps({
        "n": n, "rps": round(rps, 1), "error_rate": round(err_rate, 4),
        "p50": round(pct(s, 0.5), 3), "p90": round(pct(s, 0.9), 3),
        "p95": round(pct(s, 0.95), 3), "p99": round(pct(s, 0.99), 3),
        "p99_9": round(pct(s, 0.999), 3), "max": round(s[-1], 3), "min": round(s[0], 3),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

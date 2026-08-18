#!/usr/bin/env python3
"""
非 GIL 绑定的跨容器负载生成器（multiprocessing，每进程 1 线程）。

背景（见 baseline-report.md §10.11）：原 `python_stress.py` 用 urllib + ThreadPoolExecutor
在**单个 CPython 进程**内跑 20 VU，所有线程共享一个 GIL。请求 I/O 完成后线程需抢回 GIL
才能执行 `perf_counter()` 与统计，20 线程争用把被测端点的 p99.9/max 系统性抬高（healthz
零工作却出现 514ms）。那是**测量假象**，非服务端缺陷。

本脚本改用 `multiprocessing`：每个 VU 跑在**独立进程、单线程串行**，进程内无 GIL 竞争，
`time.perf_counter()` 测到的是真实 网络 + 服务端 延迟。并发度（VU 数）由进程数决定，
与 ThreadPoolExecutor 版本口径一致（同为 20 VU / 0.3s 思考时间），仅消除 GIL 尾尖。

支持两类目标（由 STRESS_BASE_URL 与 --mode 决定）：
  platform-api 路径（token = Bearer JWT）：
    --mode healthz       GET /healthz
    --mode assistants    GET /api/v1/assistants
    --mode mixed         healthz + assistants 交替（复刻原 baseline 口径）
  gateway 路径（internal token = X-Internal-Token + X-Workspace-ID）：
    --mode g_healthz     GET  /healthz                      （pg 健康检查）
    --mode g_models      GET  /v1/models                    （auth+限流+预算+PG 读渠道/模型）
    --mode g_chat        POST /v1/chat/completions          （完整 10 步管道 + 本地验证模型）

用法（platform-worker 容器内）：
    STRESS_BASE_URL=http://platform-api:8000 python python_stress_mp.py --mode assistants --vus 20 --dur 120
    STRESS_BASE_URL=http://gateway:8080       python python_stress_mp.py --mode g_chat --vus 20 --dur 120
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from multiprocessing import Process, Queue

BASE_URL = os.environ.get("STRESS_BASE_URL", "http://platform-api:8000").rstrip("/")
EMAIL = os.environ.get("STRESS_EMAIL", "tester@workama.example.com")
PASSWORD = os.environ.get("STRESS_PASSWORD", "WorkAMA-Test-2026!")
TIMEOUT = 10

# 网关内部调用凭证（与 compose INTERNAL_TOKEN / platform-api JWT 的 ws 声明一致）
INTERNAL_TOKEN = os.environ.get("STRESS_INTERNAL_TOKEN", "workama-dev-internal-token-2026")
WORKSPACE_ID = os.environ.get("STRESS_WORKSPACE_ID", "wsp_01KXETQWY55XXWGK9DXR55N217")
GW_HEADERS = {
    "X-Internal-Token": INTERNAL_TOKEN,
    "X-Workspace-ID": WORKSPACE_ID,
    "Content-Type": "application/json",
}
CHAT_BODY = json.dumps(
    {"model": "workama-chat", "messages": [{"role": "user", "content": "ping"}]}
).encode()


def login() -> str:
    body = json.dumps({"email": EMAIL, "password": PASSWORD}).encode()
    r = urllib.request.Request(
        f"{BASE_URL}/api/v1/auth/login", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(r, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode()).get("access_token", "")


def raw_req(method: str, path: str, headers: dict | None = None, body: bytes | None = None):
    """通用请求：返回 (status, 延迟ms)。进程内单线程，无 GIL 竞争。"""
    r = urllib.request.Request(f"{BASE_URL}{path}", data=body, headers=(headers or {}), method=method)
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


def req(path: str, token: str) -> tuple[int, float]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return raw_req("GET", path, headers)


def worker(mode: str, token: str, dur: float, q: Queue) -> None:
    """单进程单线程串行请求循环；无 GIL 竞争，测量即真实延迟。"""
    samples: list[float] = []
    errors = 0
    end = time.perf_counter() + dur
    while time.perf_counter() < end:
        if mode in ("healthz", "g_healthz"):
            s, d = raw_req("GET", "/healthz")
        elif mode == "assistants":
            s, d = req("/api/v1/assistants", token)
        elif mode == "g_models":
            s, d = raw_req("GET", "/v1/models", GW_HEADERS)
        elif mode == "g_chat":
            s, d = raw_req("POST", "/v1/chat/completions", GW_HEADERS, CHAT_BODY)
        elif mode == "mixed":  # 复刻原 baseline 口径：healthz + assistants 交替
            s1, d1 = req("/healthz", "")
            samples.append(d1)
            if s1 == 0 or s1 >= 400:
                errors += 1
            s, d = req("/api/v1/assistants", token)
        else:
            raise SystemExit(f"unknown mode: {mode}")
        samples.append(d)
        if s == 0 or s >= 400:
            errors += 1
        time.sleep(0.3)
    q.put((samples, errors))


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
    ap = argparse.ArgumentParser(description="non-GIL multiprocessing load generator")
    ap.add_argument(
        "--mode",
        choices=["healthz", "assistants", "mixed", "g_healthz", "g_models", "g_chat"],
        default="assistants",
    )
    ap.add_argument("--vus", type=int, default=20, help="并发进程数（每进程 1 线程）")
    ap.add_argument("--dur", type=float, default=120.0, help="稳态时长（秒）")
    args = ap.parse_args()

    token = ""
    if args.mode in ("assistants", "mixed"):
        token = login()
        if not token:
            print("ERROR: login failed")
            raise SystemExit(2)

    q: Queue = Queue()
    procs = [Process(target=worker, args=(args.mode, token, args.dur, q)) for _ in range(args.vus)]
    for p in procs:
        p.start()

    samples: list[float] = []
    errors = 0
    for _ in procs:
        s, e = q.get()
        samples.extend(s)
        errors += e
    for p in procs:
        p.join()

    s = sorted(samples)
    n = len(s)
    rps = n / args.dur
    err_rate = errors / n if n else 0.0
    print(f"[{args.mode} GIL-free {args.vus}VU x {args.dur:.0f}s] n={n} rps={rps:.1f} err={err_rate*100:.2f}%")
    print(f"  p50={pct(s,0.5):.3f} p90={pct(s,0.9):.3f} p95={pct(s,0.95):.3f} "
          f"p99={pct(s,0.99):.3f} p99.9={pct(s,0.999):.3f} max={s[-1]:.3f} min={s[0]:.3f}")
    print("WORKAMA_MP_SUMMARY=" + json.dumps({
        "mode": args.mode, "vus": args.vus, "dur_s": args.dur,
        "n": n, "rps": round(rps, 1), "error_rate": round(err_rate, 4),
        "p50": round(pct(s, 0.5), 3), "p90": round(pct(s, 0.9), 3),
        "p95": round(pct(s, 0.95), 3), "p99": round(pct(s, 0.99), 3),
        "p99_9": round(pct(s, 0.999), 3), "max": round(s[-1], 3), "min": round(s[0], 3),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

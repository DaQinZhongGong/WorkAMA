#!/usr/bin/env python3
"""生产环境变量预检（prod-check）。

在 `make prod-up` 之前校验 deploy/compose/.env.production：
1. 文件存在且可读；
2. REQUIRED 键全部非空（INTERNAL_TOKEN / JWT_SECRET / KEY_PEPPER /
   ENCRYPTION_KEY / POSTGRES_PASSWORD 等）；
3. 安全密钥不等于任何已知占位符/开发默认值，且长度达标；
4. ENCRYPTION_KEY 是合法 Fernet key（32 字节 url-safe/standard base64）。

设计动机：compose 的 ${VAR:?} 插值只兜"缺失"，本工具把"填了但填的是
弱值/占位符"也拦在拉起之前，错误信息逐项列出，避免带病启动。
退出码：0=通过；1=存在缺失/非法项。结果可 --json 落盘供证据链。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

DEFAULT_ENV_PATH = Path("deploy/compose/.env.production")

# (key, min_length, 说明)
REQUIRED: list[tuple[str, int, str]] = [
    ("INTERNAL_TOKEN", 16, "服务间内部令牌"),
    ("JWT_SECRET", 32, "JWT 签名密钥"),
    ("KEY_PEPPER", 16, "哈希胡椒"),
    ("ENCRYPTION_KEY", 44, "Fernet 主密钥(32字节 base64)"),
    ("POSTGRES_PASSWORD", 8, "Postgres 口令"),
]

KNOWN_WEAK = {
    "change-this-jwt-secret",
    "change-this-internal-token",
    "change-this-key-pepper",
    "workama-dev-internal-token-2026",
    "workama-local-internal-token-change-before-production",
    "workama-local-key-pepper-change-before-production",
    "workama_dev",
    "workama_minio",
}
WEAK_PREFIXES = ("change-this", "example", "fill-me", "todo")
WEAK_ENCRYPTION_KEYS = {"QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI="}


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def is_valid_fernet_key(value: str) -> bool:
    for enc in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            if len(enc(value)) == 32:
                return True
        except Exception:  # noqa: BLE001 - 非法编码按不支持处理
            continue
    return False


def check(env_path: Path) -> tuple[bool, list[str]]:
    problems: list[str] = []
    if not env_path.is_file():
        return False, [
            f"缺少 {env_path}；请复制 deploy/compose/.env.production.template 并填入真值"
        ]
    values = parse_env_file(env_path)
    for key, min_len, desc in REQUIRED:
        val = values.get(key, "")
        if not val:
            problems.append(f"{key} 缺失或为空（{desc}）")
            continue
        if val.lower() in KNOWN_WEAK or val in WEAK_ENCRYPTION_KEYS:
            problems.append(f"{key} 为已知占位符/弱默认值，生产禁止")
            continue
        low = val.lower()
        if any(low.startswith(p) for p in WEAK_PREFIXES):
            problems.append(f"{key} 以占位符前缀开头（{val[:12]}…），疑似未替换")
            continue
        if len(val) < min_len:
            problems.append(f"{key} 长度 {len(val)} < 要求 {min_len}")
            continue
    if values.get("ENCRYPTION_KEY") and not is_valid_fernet_key(values["ENCRYPTION_KEY"]):
        problems.append("ENCRYPTION_KEY 不是合法 Fernet key（需 32 字节 url-safe base64）")
    return (not problems), problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH)
    parser.add_argument("--json", type=Path, default=None, help="结果 JSON 输出路径")
    args = parser.parse_args()

    ok, problems = check(args.env_file)
    result = {"ok": ok, "env_file": str(args.env_file), "problems": problems}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for p in problems:
        print(f"[prod-env-check] FAIL {p}", file=sys.stderr)
    print(
        f"prod-env-check: {'PASS' if ok else 'FAIL'} ({args.env_file}, "
        f"{len(problems)} problem(s))"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

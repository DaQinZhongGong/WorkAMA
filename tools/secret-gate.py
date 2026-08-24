#!/usr/bin/env python3
"""WorkAMA 密钥扫描门禁（CI / pre-commit 可复用）。

扫描 git 跟踪文件，拒绝**真实密钥泄漏**。设计原则：

- 文档化占位符（``change-this-*``、``workama-local-*-change-before-production``、
  已知弱 base64 默认值 ``QkJC...=``）是**安全设计**：它们作为 dev 默认值存在于源码，
  生产环境由 ``validate_production_secrets`` / 网关 ``resolve*`` 在启动时拒绝。
  因此门禁**不**把它们当作泄漏，避免对正常 dev 默认值误报。
- 门禁只抓真正的危险信号：
  1. 真实 ``.env``（非模板）被 git 跟踪 —— 直接失败（必须 gitignore）；
  2. AWS 访问密钥 ``AKIA[0-9A-Z]{16}``；
  3. OpenAI 风格 ``sk-`` 长密钥（仅非测试源码的赋值上下文）；
  4. 非测试源码里把高熵随机串硬编码进 secret 类变量（且不在已知安全占位符集）。

用法::

    python tools/secret-gate.py            # 扫描并报告，发现即退出 1
    python tools/secret-gate.py --quiet    # 仅输出失败项
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 已知安全占位符 / 弱默认值（源码里作为 dev 默认值出现，非泄漏）。
KNOWN_SAFE_VALUES = {
    "change-this-jwt-secret",
    "change-this-key-pepper",
    "change-this-pepper",
    "change-this-internal-token",
    "workama-local-jwt-secret-change-before-production",
    "workama-local-key-pepper-change-before-production",
    "workama-dev-internal-token-2026",
    "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI=",
}

# 真实泄漏模式
_AWS_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_OPENAI_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
# secret 类变量的赋值（兼容 = 与 : 风格，带引号）
_SECRET_ASSIGN_RE = re.compile(
    r'(?i)([\w-]*(?:secret|token|password|passwd|api[_-]?key|private[_-]?key|'
    r'access[_-]?key|encryption[_-]?key|key[_-]?pepper)[\w-]*)\s*[:=]\s*["\']([^"\']+)["\']'
)

# allowlist：跳过这些路径（basename 或路径片段命中）
_ALLOWLIST_BASENAME = {
    ".env.example",
    ".env.production.template",
    "values.yaml",
    "values-staging.yaml",
}
_ALLOWLIST_PATH_FRAGMENTS = (
    "examples/",
    "tests/",
    "docs/",
    ".github/",
    "deploy/helm/",
    "tools/",
    "node_modules/",
)
_ALLOWLIST_SUFFIX = (
    ".md",
    ".txt",
    ".rst",
    ".lock",
    ".png",
    ".svg",
    ".jpg",
    ".ico",
    ".woff2",
)


def _is_allowlisted(rel: str) -> bool:
    base = os.path.basename(rel)
    if base in _ALLOWLIST_BASENAME:
        return True
    if base.startswith(".env.example") or base.startswith(".env.production.template"):
        return True
    for frag in _ALLOWLIST_PATH_FRAGMENTS:
        if frag in rel:
            return True
    for suf in _ALLOWLIST_SUFFIX:
        if rel.endswith(suf):
            return True
    return False


def _is_test_path(rel: str) -> bool:
    base = os.path.basename(rel)
    return "/tests/" in rel or base.endswith("_test.go") or base.endswith("test_.py") or "/test/" in rel


def _is_high_entropy(value: str) -> bool:
    """粗略高熵判断：长度 >= 24 且同时含字母与数字（随机密钥特征）。"""
    if len(value) < 24:
        return False
    has_alpha = any(c.isalpha() for c in value)
    has_digit = any(c.isdigit() for c in value)
    return has_alpha and has_digit


def _tracked_files() -> list[str]:
    out = subprocess.check_output(["git", "ls-files"], cwd=REPO_ROOT, text=True)
    return [line for line in out.splitlines() if line.strip()]


def _tracked_real_env_files(tracked: list[str]) -> list[str]:
    """真实 .env（非模板）被跟踪 => 直接失败。"""
    bad = []
    for rel in tracked:
        base = os.path.basename(rel)
        if base == ".env" or (base.startswith(".env.") and base not in _ALLOWLIST_BASENAME):
            bad.append(rel)
    return bad


def scan(tracked: list[str], quiet: bool = False) -> list[str]:
    findings: list[str] = []
    for rel in tracked:
        if _is_allowlisted(rel):
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        in_test = _is_test_path(rel)
        for i, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("/*"):
                continue
            # 1) AWS 访问密钥（任何位置都视为泄漏）
            if _AWS_RE.search(line):
                findings.append(f"{rel}:{i}: {stripped[:160]}")
                continue
            # 2) OpenAI 风格密钥（仅非测试源码赋值上下文）
            if not in_test and _OPENAI_RE.search(line):
                m = _SECRET_ASSIGN_RE.search(line)
                if m and m.group(2) not in KNOWN_SAFE_VALUES:
                    findings.append(f"{rel}:{i}: {stripped[:160]}")
                    continue
            # 3) 非测试源码里高熵真密钥硬编码（排除已知安全占位符 / URL）
            if not in_test:
                m = _SECRET_ASSIGN_RE.search(line)
                if m:
                    val = m.group(2)
                    if val in KNOWN_SAFE_VALUES:
                        continue
                    if "://" in val:
                        # URL（如 token_url）不是密钥
                        continue
                    if _is_high_entropy(val):
                        findings.append(f"{rel}:{i}: {stripped[:160]}")
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description="WorkAMA secret-gate scanner")
    ap.add_argument("--quiet", action="store_true", help="仅输出失败项")
    args = ap.parse_args()

    tracked = _tracked_files()
    real_env = _tracked_real_env_files(tracked)
    findings = scan(tracked, quiet=args.quiet)

    failures = list(findings)
    if real_env:
        failures.append(
            "REAL_ENV_TRACKED: "
            + ", ".join(real_env)
            + " (real .env must not be committed; gitignore it)"
        )

    if not args.quiet:
        print(f"secret-gate: scanned {len(tracked)} tracked files")
        if real_env:
            print("real .env tracked (must ignore):")
            for r in real_env:
                print(f"  - {r}")

    if failures:
        print("secret-gate: FAILED — real secret leaks found:")
        for f in failures:
            print(f"  {f}")
        return 1

    if not args.quiet:
        print("secret-gate: PASS — no real secret leaks in tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())

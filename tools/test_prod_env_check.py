"""prod_env_check 单元测试：缺失/弱值/合法 Fernet/占位符/全绿路径。"""
from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("prod_env_check", TOOLS_DIR / "prod_env_check.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["prod_env_check"] = mod
spec.loader.exec_module(mod)

STRONG_FERNET = base64.urlsafe_b64encode(b"k" * 32).decode()


def _write(tmp_path, content: str):
    p = tmp_path / ".env.production"
    p.write_text(content, encoding="utf-8")
    return p


def test_missing_file(tmp_path):
    ok, problems = mod.check(tmp_path / "nope.env")
    assert ok is False and "缺少" in problems[0]


def test_empty_required(tmp_path):
    p = _write(tmp_path, "INTERNAL_TOKEN=\nJWT_SECRET=x\n")
    ok, problems = mod.check(p)
    assert ok is False
    joined = "\n".join(problems)
    assert "INTERNAL_TOKEN" in joined and "KEY_PEPPER" in joined


def test_known_weak_and_placeholder_prefix(tmp_path):
    p = _write(
        tmp_path,
        f"INTERNAL_TOKEN=change-this-internal-token\nJWT_SECRET={'a' * 40}\n"
        f"KEY_PEPPER={'b' * 20}\nENCRYPTION_KEY={STRONG_FERNET}\n"
        f"POSTGRES_PASSWORD={'c' * 10}\nMINIO_ROOT_PASSWORD=example-weak\n",
    )
    ok, problems = mod.check(p)
    assert ok is False
    assert any("INTERNAL_TOKEN" in x and "占位符" in x for x in problems)


def test_short_value_rejected(tmp_path):
    p = _write(
        tmp_path,
        f"INTERNAL_TOKEN=short\nJWT_SECRET={'a' * 40}\nKEY_PEPPER={'b' * 20}\n"
        f"ENCRYPTION_KEY={STRONG_FERNET}\nPOSTGRES_PASSWORD={'c' * 10}\n",
    )
    ok, problems = mod.check(p)
    assert ok is False and any("长度" in x and "INTERNAL_TOKEN" in x for x in problems)


def test_invalid_fernet_rejected(tmp_path):
    p = _write(
        tmp_path,
        "INTERNAL_TOKEN=tokentokentokento\nJWT_SECRET=" + "j" * 40 + "\n"
        "KEY_PEPPER=" + "p" * 20 + "\nENCRYPTION_KEY=not-a-fernet\n"
        "POSTGRES_PASSWORD=" + "c" * 10 + "\n",
    )
    ok, problems = mod.check(p)
    assert ok is False and any("Fernet" in x for x in problems)


def test_all_green(tmp_path):
    p = _write(
        tmp_path,
        "INTERNAL_TOKEN=unique-prod-token-2026!\nJWT_SECRET=" + "j" * 48 + "\n"
        "KEY_PEPPER=" + "p" * 24 + "\nENCRYPTION_KEY=" + STRONG_FERNET + "\n"
        "POSTGRES_PASSWORD=" + "c" * 16 + "\n# comment line\nWORKAMA_ENV=production\n",
    )
    ok, problems = mod.check(p)
    assert ok is True and problems == []

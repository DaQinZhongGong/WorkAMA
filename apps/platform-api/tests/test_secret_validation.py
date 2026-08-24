"""生产密钥硬化校验（与网关 INTERNAL_TOKEN 拒绝逻辑一致）。

仅覆盖 ``validate_production_secrets`` 的纯函数语义：production 下拒绝占位符 /
弱密钥，development / test 下不强制。
"""

from __future__ import annotations

import base64
import os

import pytest

from workama_platform.core import (
    Settings,
    validate_production_secrets,
)


def _strong_fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _settings(**overrides: str) -> Settings:
    base = dict(
        workama_env="production",
        jwt_secret="a" * 32,
        key_pepper="b" * 32,
        internal_token="c" * 32,
        encryption_key=_strong_fernet_key(),
    )
    base.update(overrides)
    return Settings(**base)


def test_production_with_strong_secrets_passes() -> None:
    # 强密钥应通过校验，不抛异常。
    validate_production_secrets(_settings())


def test_development_never_enforced() -> None:
    # 开发环境即使全是占位符也不应阻断（保证本地开发可用）。
    dev = _settings(
        workama_env="development",
        jwt_secret="change-this-jwt-secret",
        key_pepper="change-this-key-pepper",
        internal_token="change-this-internal-token",
        encryption_key="QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI=",
    )
    validate_production_secrets(dev)


def test_production_rejects_placeholder_jwt_secret() -> None:
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        validate_production_secrets(_settings(jwt_secret="change-this-jwt-secret"))


def test_production_rejects_local_dev_jwt_secret() -> None:
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        validate_production_secrets(
            _settings(jwt_secret="workama-local-jwt-secret-change-before-production")
        )


def test_production_rejects_short_jwt_secret() -> None:
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        validate_production_secrets(_settings(jwt_secret="short"))


def test_production_rejects_placeholder_key_pepper() -> None:
    with pytest.raises(RuntimeError, match="KEY_PEPPER"):
        validate_production_secrets(_settings(key_pepper="change-this-key-pepper"))


def test_production_rejects_local_dev_key_pepper() -> None:
    with pytest.raises(RuntimeError, match="KEY_PEPPER"):
        validate_production_secrets(
            _settings(key_pepper="workama-local-key-pepper-change-before-production")
        )


def test_production_rejects_placeholder_internal_token() -> None:
    with pytest.raises(RuntimeError, match="INTERNAL_TOKEN"):
        validate_production_secrets(_settings(internal_token="change-this-internal-token"))


def test_production_rejects_empty_encryption_key() -> None:
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        validate_production_secrets(_settings(encryption_key=""))


def test_production_rejects_known_weak_encryption_key() -> None:
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        validate_production_secrets(
            _settings(encryption_key="QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI=")
        )


def test_production_rejects_invalid_encryption_key() -> None:
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        validate_production_secrets(_settings(encryption_key="not-a-fernet-key"))


def test_production_reports_all_problems() -> None:
    # 多个弱密钥时，错误信息应列出全部问题。
    with pytest.raises(RuntimeError, match="JWT_SECRET") as exc:
        validate_production_secrets(
            _settings(
                jwt_secret="change-this-jwt-secret",
                key_pepper="change-this-key-pepper",
                internal_token="change-this-internal-token",
                encryption_key="QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI=",
            )
        )
    msg = str(exc.value)
    assert "KEY_PEPPER" in msg
    assert "INTERNAL_TOKEN" in msg

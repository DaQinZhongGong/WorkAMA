"""WorkAMA 平台版本信息。

本模块是平台版本的单一事实来源，供 ``enterprise_version`` 端点及构建脚本引用。
位于 platform-api 根目录（非 ``src/workama_platform`` 包内），导入时需要保证
platform-api 根目录在 ``sys.path`` 上（测试期由 ``pyproject.toml`` 的
``pythonpath`` 配置 ``.`` 提供）。
"""
from __future__ import annotations

PLATFORM_VERSION = "v7.176"
ENTERPRISE_BUILD = True
BUILD_DATE = "2026-07-31"

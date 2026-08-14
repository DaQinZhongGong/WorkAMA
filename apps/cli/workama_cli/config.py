"""Configuration store for the WorkAMA CLI v2.

Configuration and credentials are persisted under ``~/.workama/``::

    ~/.workama/
      credentials   # JSON: {"base_url": ..., "token": ..., "workspace_id": ...}

Environment variable overrides (highest priority):

* ``WORKAMA_API_URL``       — overrides ``base_url``
* ``WORKAMA_API_TOKEN``     — overrides ``token``
* ``WORKAMA_WORKSPACE_ID``  — overrides ``workspace_id``
* ``WORKAMA_CONFIG_DIR``    — overrides the config directory (used by tests)
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


DEFAULT_BASE_URL = "http://localhost:20200"

ENV_BASE_URL = "WORKAMA_API_URL"
ENV_TOKEN = "WORKAMA_API_TOKEN"
ENV_WORKSPACE_ID = "WORKAMA_WORKSPACE_ID"
ENV_CONFIG_DIR = "WORKAMA_CONFIG_DIR"


class ConfigError(ValueError):
    """Raised when the configuration file cannot be read or written."""


def default_config_dir() -> Path:
    explicit = os.environ.get(ENV_CONFIG_DIR)
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".workama"


def credentials_path(config_dir: Path | None = None) -> Path:
    return (config_dir or default_config_dir()) / "credentials"


class Config:
    """Read and write WorkAMA CLI credentials as a JSON file."""

    def __init__(self, config_dir: Path | str | None = None):
        self.config_dir = Path(config_dir) if config_dir else default_config_dir()
        self.path = self.config_dir / "credentials"

    # -- low-level file IO -------------------------------------------------
    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Cannot read credentials file {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"Credentials file {self.path} is not a JSON object")
        return raw

    def _write(self, data: dict[str, Any]) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.config_dir,
                prefix=".credentials.",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            if os.name != "nt":
                os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary, self.path)
            if os.name != "nt":
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            raise ConfigError(f"Cannot write credentials file {self.path}: {exc}") from exc
        finally:
            if temporary and temporary.exists():
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    # -- public API --------------------------------------------------------
    @property
    def base_url(self) -> str:
        return os.environ.get(ENV_BASE_URL) or self._read().get("base_url") or DEFAULT_BASE_URL

    @property
    def token(self) -> str | None:
        return os.environ.get(ENV_TOKEN) or self._read().get("token")

    @property
    def workspace_id(self) -> str | None:
        return os.environ.get(ENV_WORKSPACE_ID) or self._read().get("workspace_id")

    def snapshot(self) -> dict[str, Any]:
        """Return the effective configuration (env vars win over file)."""
        data = self._read()
        return {
            "base_url": os.environ.get(ENV_BASE_URL) or data.get("base_url") or DEFAULT_BASE_URL,
            "token": os.environ.get(ENV_TOKEN) or data.get("token"),
            "workspace_id": os.environ.get(ENV_WORKSPACE_ID) or data.get("workspace_id"),
        }

    def save(self, *, base_url: str | None = None, token: str | None = None, workspace_id: str | None = None) -> None:
        data = self._read()
        if base_url is not None:
            data["base_url"] = base_url
        if token is not None:
            data["token"] = token
        if workspace_id is not None:
            data["workspace_id"] = workspace_id
        self._write(data)

    def clear(self) -> bool:
        if not self.path.exists():
            return False
        try:
            self.path.unlink()
            return True
        except OSError as exc:
            raise ConfigError(f"Cannot remove credentials file {self.path}: {exc}") from exc

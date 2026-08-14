from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


DEFAULTS: dict[str, str] = {
    "gateway_url": "http://localhost:20202",
    "platform_url": "http://localhost:20200",
    "agent_ws_url": "ws://localhost:20201",
    "model": "workama-chat",
}

ENVIRONMENT_KEYS = {
    "gateway_url": "WORKAMA_GATEWAY_URL",
    "platform_url": "WORKAMA_PLATFORM_URL",
    "agent_ws_url": "WORKAMA_AGENT_WS_URL",
    "model": "WORKAMA_MODEL",
    "api_key": "WORKAMA_API_KEY",
    "access_token": "WORKAMA_ACCESS_TOKEN",
    "workspace_id": "WORKAMA_WORKSPACE_ID",
}

SECRET_KEYS = {"api_key", "access_token"}


class ConfigError(ValueError):
    pass


def default_config_path() -> Path:
    explicit_path = os.environ.get("WORKAMA_CONFIG_PATH")
    if explicit_path:
        return Path(explicit_path).expanduser()

    explicit_dir = os.environ.get("WORKAMA_CONFIG_DIR")
    if explicit_dir:
        return Path(explicit_dir).expanduser() / "config.json"

    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home()))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "workama" / "config.json"


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_config_path()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"current_profile": "default", "profiles": {"default": {}}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Cannot read config file {self.path}: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("profiles", {}), dict):
            raise ConfigError(f"Config file {self.path} has an invalid shape")
        raw.setdefault("current_profile", "default")
        raw.setdefault("profiles", {})
        raw["profiles"].setdefault("default", {})
        return raw

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
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
            raise ConfigError(f"Cannot write config file {self.path}: {exc}") from exc
        finally:
            if temporary and temporary.exists():
                temporary.unlink(missing_ok=True)

    def profile_names(self) -> list[str]:
        return sorted(self._read()["profiles"])

    def current_profile(self) -> str:
        return str(self._read().get("current_profile") or "default")

    def values(self, profile: str | None = None) -> dict[str, Any]:
        data = self._read()
        selected = profile or str(data.get("current_profile") or "default")
        values = dict(DEFAULTS)
        values.update(data["profiles"].get(selected, {}))
        for key, env_name in ENVIRONMENT_KEYS.items():
            if os.environ.get(env_name):
                values[key] = os.environ[env_name]
        return values

    def get(self, key: str, profile: str | None = None) -> Any:
        return self.values(profile).get(key)

    def set(self, key: str, value: str, profile: str | None = None) -> None:
        data = self._read()
        selected = profile or str(data.get("current_profile") or "default")
        data["profiles"].setdefault(selected, {})[key] = value
        self._write(data)

    def delete(self, key: str, profile: str | None = None) -> bool:
        data = self._read()
        selected = profile or str(data.get("current_profile") or "default")
        profile_data = data["profiles"].get(selected, {})
        if key in profile_data:
            del profile_data[key]
            self._write(data)
            return True
        return False

    def use(self, profile: str) -> None:
        data = self._read()
        data["profiles"].setdefault(profile, {})
        data["current_profile"] = profile
        self._write(data)

    def display_values(
        self, profile: str | None = None, *, show_secrets: bool = False
    ) -> dict[str, Any]:
        values = self.values(profile)
        if show_secrets:
            return values
        for key in SECRET_KEYS:
            if values.get(key):
                values[key] = "<configured>"
        return values


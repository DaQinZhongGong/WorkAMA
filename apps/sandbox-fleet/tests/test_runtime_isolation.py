from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from workama_sandbox import main
from workama_sandbox.main import (
    _classify_runtime_name,
    _read_own_container_id,
    detect_runtime_isolation,
    enforce_strict_microvm,
)


class _FakeContainer:
    def __init__(self, runtime: str):
        self.attrs = {"HostConfig": {"Runtime": runtime}}


# ---------------------------------------------------------------------------
# _classify_runtime_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("runsc", "gvisor"),
        ("RUNSC", "gvisor"),
        ("kata-fc", "firecracker"),
        ("kata", "firecracker"),
        ("firecracker", "firecracker"),
        ("runc", "runc-dev"),
        ("", "runc-dev"),
        ("some-exotic-runtime", "unknown"),
    ],
)
def test_classify_runtime_name_maps_docker_identifiers(raw, expected):
    assert _classify_runtime_name(raw) == expected


# ---------------------------------------------------------------------------
# detect_runtime_isolation: env override
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["gvisor", "runc-dev", "firecracker", "unknown"])
def test_detect_runtime_isolation_honors_env_override(monkeypatch, value):
    monkeypatch.setenv("WORKAMA_SANDBOX_RUNTIME_OVERRIDE", value)
    # Docker socket and /proc/1/cgroup should NOT be consulted when override is set.
    monkeypatch.setattr(main, "docker_client", MagicMock())
    monkeypatch.setattr(main, "_read_own_container_id", lambda: "")
    assert detect_runtime_isolation() == value


def test_detect_runtime_isolation_override_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("WORKAMA_SANDBOX_RUNTIME_OVERRIDE", "  GVISOR  ")
    monkeypatch.setattr(main, "docker_client", MagicMock())
    monkeypatch.setattr(main, "_read_own_container_id", lambda: "")
    assert detect_runtime_isolation() == "gvisor"


def test_detect_runtime_isolation_ignores_invalid_override(monkeypatch):
    """An unrecognized override value must NOT short-circuit detection."""
    monkeypatch.setenv("WORKAMA_SANDBOX_RUNTIME_OVERRIDE", "nonsense")
    fake_docker = MagicMock()
    fake_docker.containers.get.return_value = _FakeContainer("runsc")
    monkeypatch.setattr(main, "docker_client", fake_docker)
    monkeypatch.setattr(main, "_read_own_container_id", lambda: "abc123def456")
    assert detect_runtime_isolation() == "gvisor"


# ---------------------------------------------------------------------------
# detect_runtime_isolation: Docker socket path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "runtime,expected",
    [
        ("runsc", "gvisor"),
        ("kata-fc", "firecracker"),
        ("runc", "runc-dev"),
        ("", "runc-dev"),
    ],
)
def test_detect_runtime_isolation_via_docker_socket(monkeypatch, runtime, expected):
    monkeypatch.delenv("WORKAMA_SANDBOX_RUNTIME_OVERRIDE", raising=False)
    fake_docker = MagicMock()
    fake_docker.containers.get.return_value = _FakeContainer(runtime)
    monkeypatch.setattr(main, "docker_client", fake_docker)
    monkeypatch.setattr(main, "_read_own_container_id", lambda: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    assert detect_runtime_isolation() == expected
    fake_docker.containers.get.assert_called_once()


def test_detect_runtime_isolation_falls_through_when_docker_socket_fails(monkeypatch, tmp_path):
    """When the Docker socket raises, we fall back to /proc/1/cgroup scan."""
    monkeypatch.delenv("WORKAMA_SANDBOX_RUNTIME_OVERRIDE", raising=False)
    fake_docker = MagicMock()
    fake_docker.containers.get.side_effect = RuntimeError("docker unavailable")
    monkeypatch.setattr(main, "docker_client", fake_docker)
    monkeypatch.setattr(main, "_read_own_container_id", lambda: "0123456789abcdef0123456789abcdef")

    cgroup_payload = "0::/runsc/something\n"

    def _fake_read_text(self, **_kwargs):
        # Match the cgroup path on any OS (Windows backslash vs POSIX forward slash).
        if str(self).replace("\\", "/").endswith("/proc/1/cgroup"):
            return cgroup_payload
        raise OSError("no file")

    monkeypatch.setattr(Path, "read_text", _fake_read_text)

    assert detect_runtime_isolation() == "gvisor"


def test_detect_runtime_isolation_unknown_when_all_sources_fail(monkeypatch):
    monkeypatch.delenv("WORKAMA_SANDBOX_RUNTIME_OVERRIDE", raising=False)
    fake_docker = MagicMock()
    fake_docker.containers.get.side_effect = RuntimeError("no docker")
    monkeypatch.setattr(main, "docker_client", fake_docker)
    monkeypatch.setattr(main, "_read_own_container_id", lambda: "")
    # /proc/1/cgroup read also fails (raises OSError).
    monkeypatch.setattr(
        main.Path,
        "read_text",
        lambda self, **kw: (_ for _ in ()).throw(OSError("no cgroup file")),
    )
    assert detect_runtime_isolation() == "unknown"


# ---------------------------------------------------------------------------
# _read_own_container_id
# ---------------------------------------------------------------------------


def test_read_own_container_id_extracts_docker_id(monkeypatch):
    sample = (
        "12:cpuset:/docker/0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
        "11:memory:/docker/0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
    )
    monkeypatch.setattr(
        main.Path,
        "read_text",
        lambda self, **kw: sample,
    )
    assert _read_own_container_id() == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def test_read_own_container_id_handles_systemd_scope_suffix(monkeypatch):
    sample = "0::/system.slice/docker-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef.scope\n"
    monkeypatch.setattr(
        main.Path,
        "read_text",
        lambda self, **kw: sample,
    )
    assert _read_own_container_id() == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def test_read_own_container_id_returns_empty_when_not_in_container(monkeypatch):
    monkeypatch.setattr(
        main.Path,
        "read_text",
        lambda self, **kw: (_ for _ in ()).throw(OSError("no file")),
    )
    assert _read_own_container_id() == ""


# ---------------------------------------------------------------------------
# enforce_strict_microvm
# ---------------------------------------------------------------------------


def test_enforce_strict_microvm_noop_when_not_required(monkeypatch):
    monkeypatch.setattr(main.settings, "sandbox_require_microvm", False)
    # Even non-gVisor isolation should be tolerated when strict mode is off.
    enforce_strict_microvm("runc-dev")
    enforce_strict_microvm("unknown")
    enforce_strict_microvm("firecracker")
    enforce_strict_microvm("gvisor")


def test_enforce_strict_microvm_passes_when_gvisor_detected(monkeypatch):
    monkeypatch.setattr(main.settings, "sandbox_require_microvm", True)
    enforce_strict_microvm("gvisor")  # must not raise


@pytest.mark.parametrize("isolation", ["runc-dev", "firecracker", "unknown"])
def test_enforce_strict_microvm_raises_when_non_gvisor(monkeypatch, isolation):
    monkeypatch.setattr(main.settings, "sandbox_require_microvm", True)
    with pytest.raises(RuntimeError, match="strict microVM required but runtime is not gVisor"):
        enforce_strict_microvm(isolation)


def test_enforce_strict_microvm_uses_detect_runtime_isolation_when_arg_omitted(monkeypatch):
    monkeypatch.setattr(main.settings, "sandbox_require_microvm", True)
    monkeypatch.setattr(main, "detect_runtime_isolation", lambda: "runc-dev")
    with pytest.raises(RuntimeError, match="strict microVM required"):
        enforce_strict_microvm()


def test_enforce_strict_microvm_does_not_call_detect_when_not_required(monkeypatch):
    """When strict mode is off, detect_runtime_isolation must NOT be invoked."""
    monkeypatch.setattr(main.settings, "sandbox_require_microvm", False)
    called = {"count": 0}

    def _detect():
        called["count"] += 1
        return "runc-dev"

    monkeypatch.setattr(main, "detect_runtime_isolation", _detect)
    enforce_strict_microvm()
    assert called["count"] == 0

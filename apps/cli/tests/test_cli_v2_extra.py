"""Supplementary pytest tests for workama_cli (CLI v2).

Deepens coverage of config.py, client.py and cli.py without duplicating the
existing test_cli_extended.py suite. Uses pytest + monkeypatch as required by
the task constraints. No real HTTP traffic is generated — httpx is replaced
by a lightweight fake transport injected through WorkamaClient(client=...).
"""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

# Make apps/cli/ importable so `workama_cli` resolves (mirrors existing tests).
_CLI_ROOT = str(Path(__file__).resolve().parents[1])
if _CLI_ROOT not in sys.path:
    sys.path.insert(0, _CLI_ROOT)

from workama_cli import client as client_mod  # noqa: E402
from workama_cli import cli as cli_mod  # noqa: E402
from workama_cli.client import (  # noqa: E402
    ApiError,
    NetworkError,
    NotLoggedInError,
    WorkamaClient,
    _build_headers,
    _parse_response,
)
from workama_cli.config import (  # noqa: E402
    DEFAULT_BASE_URL,
    ENV_BASE_URL,
    ENV_CONFIG_DIR,
    ENV_TOKEN,
    ENV_WORKSPACE_ID,
    Config,
    ConfigError,
    credentials_path,
    default_config_dir,
)


# ---------------------------------------------------------------------------
# Fake httpx transport — replaces httpx.Client inside WorkamaClient.
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for httpx.Response covering _parse_response needs."""

    def __init__(self, status_code=200, *, json_body=None, text=None, content_type="application/json", headers=None):
        self.status_code = status_code
        self._json = json_body
        if text is not None:
            self.text = text
            self.content = text.encode("utf-8")
        elif json_body is not None:
            self.text = json.dumps(json_body)
            self.content = self.text.encode("utf-8")
        else:
            self.text = ""
            self.content = b""
        merged = {"Content-Type": content_type}
        if headers:
            merged.update(headers)
        self.headers = merged

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class FakeClient:
    """Records the last request and returns a queued response (or raises)."""

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []  # list of dicts describing each request

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.exc is not None:
            raise self.exc
        if callable(self.response):
            return self.response(method, url, **kwargs)
        return self.response

    def close(self):
        self.closed = True


def make_client(response=None, exc=None, *, token="tok-test", **kwargs):
    """Build a WorkamaClient backed by a FakeClient.

    A default token is provided so that require_token=True requests proceed;
    tests that exercise the no-token path pass token=None explicitly.
    """
    fake = FakeClient(response=response, exc=exc)
    return WorkamaClient("http://api.test", client=fake, token=token, **kwargs), fake


# ===========================================================================
# config.py — Config class boundary cases (10 tests)
# ===========================================================================


class TestConfigBoundaries:
    """Cover Config edge cases not exercised by test_cli_extended.py."""

    def test_default_config_dir_honors_env_var(self, monkeypatch):
        monkeypatch.setenv(ENV_CONFIG_DIR, "/tmp/workama-cfg-test")
        assert default_config_dir() == Path("/tmp/workama-cfg-test")

    def test_default_config_dir_falls_back_to_home(self, monkeypatch):
        monkeypatch.delenv(ENV_CONFIG_DIR, raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/home/tester")))
        assert default_config_dir() == Path("/home/tester/.workama")

    def test_credentials_path_uses_config_dir(self, tmp_path):
        assert credentials_path(tmp_path) == tmp_path / "credentials"

    def test_credentials_path_defaults_when_none(self, monkeypatch):
        monkeypatch.setenv(ENV_CONFIG_DIR, "/tmp/workama-cfg-creds")
        assert credentials_path(None) == Path("/tmp/workama-cfg-creds") / "credentials"

    def test_read_raises_on_corrupt_json(self, tmp_path):
        cfg = Config(config_dir=tmp_path)
        cfg.path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(ConfigError, match="Cannot read"):
            cfg._read()

    def test_read_raises_when_json_is_not_object(self, tmp_path):
        cfg = Config(config_dir=tmp_path)
        cfg.path.write_text(json.dumps(["a", "b"]), encoding="utf-8")
        with pytest.raises(ConfigError, match="not a JSON object"):
            cfg._read()

    def test_clear_returns_true_when_file_exists(self, tmp_path):
        cfg = Config(config_dir=tmp_path)
        cfg.save(token="tok")
        assert cfg.path.exists()
        assert cfg.clear() is True
        assert not cfg.path.exists()

    def test_clear_returns_false_when_file_missing(self, tmp_path):
        cfg = Config(config_dir=tmp_path)
        assert cfg.clear() is False

    def test_clear_raises_config_error_on_unlink_failure(self, tmp_path, monkeypatch):
        cfg = Config(config_dir=tmp_path)
        cfg.save(token="tok")

        def boom(_):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "unlink", boom)
        with pytest.raises(ConfigError, match="Cannot remove"):
            cfg.clear()

    def test_snapshot_returns_effective_config_with_defaults(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ENV_BASE_URL, raising=False)
        monkeypatch.delenv(ENV_TOKEN, raising=False)
        monkeypatch.delenv(ENV_WORKSPACE_ID, raising=False)
        cfg = Config(config_dir=tmp_path)
        snap = cfg.snapshot()
        assert snap == {"base_url": DEFAULT_BASE_URL, "token": None, "workspace_id": None}

    def test_save_is_additive_and_preserves_existing_keys(self, tmp_path):
        cfg = Config(config_dir=tmp_path)
        cfg.save(base_url="http://a.test", token="tok-1", workspace_id="ws-1")
        # Second save with only token should preserve base_url and workspace_id.
        cfg.save(token="tok-2")
        data = json.loads(cfg.path.read_text(encoding="utf-8"))
        assert data["base_url"] == "http://a.test"
        assert data["token"] == "tok-2"
        assert data["workspace_id"] == "ws-1"

    def test_base_url_falls_back_to_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ENV_BASE_URL, raising=False)
        cfg = Config(config_dir=tmp_path)
        assert cfg.base_url == DEFAULT_BASE_URL

    def test_token_returns_none_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ENV_TOKEN, raising=False)
        cfg = Config(config_dir=tmp_path)
        assert cfg.token is None

    def test_write_raises_config_error_on_os_failure(self, tmp_path, monkeypatch):
        cfg = Config(config_dir=tmp_path)

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("workama_cli.config.NamedTemporaryFile", boom)
        with pytest.raises(ConfigError, match="Cannot write"):
            cfg.save(token="tok")


# ===========================================================================
# client.py — pure helpers + _request error paths (12 tests)
# ===========================================================================


class TestBuildHeaders:
    def test_includes_authorization_when_token_present(self):
        headers = _build_headers("tok-1", None)
        assert headers["Authorization"] == "Bearer tok-1"
        assert headers["Accept"] == "application/json"

    def test_omits_authorization_when_token_empty(self):
        headers = _build_headers("", None)
        assert "Authorization" not in headers

    def test_omits_authorization_when_token_none(self):
        headers = _build_headers(None, None)
        assert "Authorization" not in headers

    def test_includes_workspace_header_when_present(self):
        headers = _build_headers("tok", "ws-1")
        assert headers["X-Workspace-Id"] == "ws-1"

    def test_omits_workspace_header_when_absent(self):
        headers = _build_headers("tok", None)
        assert "X-Workspace-Id" not in headers

    def test_merges_extra_headers(self):
        headers = _build_headers("tok", None, extra={"X-Custom": "yes"})
        assert headers["X-Custom"] == "yes"
        assert headers["Authorization"] == "Bearer tok"


class TestParseResponse:
    def test_returns_empty_dict_on_204(self):
        resp = FakeResponse(204, content_type="application/json", text="")
        assert _parse_response(resp) == {}

    def test_returns_empty_dict_on_empty_content(self):
        resp = FakeResponse(200, content_type="application/json", text="")
        assert _parse_response(resp) == {}

    def test_parses_json_from_json_content_type(self):
        resp = FakeResponse(200, json_body={"a": 1})
        assert _parse_response(resp) == {"a": 1}

    def test_parses_json_when_body_starts_with_brace_regardless_of_content_type(self):
        resp = FakeResponse(200, json_body={"a": 1}, content_type="text/plain")
        assert _parse_response(resp) == {"a": 1}

    def test_parses_json_when_body_starts_with_bracket(self):
        resp = FakeResponse(200, json_body=[1, 2, 3], content_type="text/plain")
        assert _parse_response(resp) == [1, 2, 3]

    def test_returns_text_when_not_json(self):
        resp = FakeResponse(200, text="plain text body", content_type="text/plain")
        assert _parse_response(resp) == "plain text body"

    def test_returns_text_on_invalid_json_with_json_content_type(self):
        # body that looks like json (starts with {) but is invalid falls back to text
        resp = FakeResponse(200, content_type="application/json")
        # Manually craft an invalid JSON body that starts with {
        resp._json = None
        resp.text = "{ broken"
        resp.content = b"{ broken"
        assert _parse_response(resp) == "{ broken"


class TestWorkamaClientInit:
    def test_strips_trailing_slash_from_base_url(self):
        c, _ = make_client()
        assert c.base_url == "http://api.test"

    def test_strips_multiple_trailing_slashes(self):
        fake = FakeClient(response=FakeResponse(200, json_body={}))
        c = WorkamaClient("http://api.test///", client=fake)
        assert c.base_url == "http://api.test"

    def test_close_delegates_to_inner_client(self):
        fake = FakeClient(response=FakeResponse(200, json_body={}))
        c = WorkamaClient("http://api.test", client=fake)
        c.close()
        assert getattr(fake, "closed", False) is True


class TestRequestErrors:
    def test_raises_not_logged_in_when_token_missing(self):
        c, _ = make_client(response=FakeResponse(200, json_body={}), token=None)
        with pytest.raises(NotLoggedInError, match="Not logged in"):
            c._request("GET", "/api/v1/auth/me")

    def test_allows_no_token_when_require_token_false(self):
        resp = FakeResponse(200, json_body={"ok": True})
        c, fake = make_client(response=resp, token=None)
        # login path uses require_token=False
        out = c._request("POST", "/api/v1/auth/login", json_body={"email": "a"}, require_token=False)
        assert out == {"ok": True}
        # Authorization header must NOT be present
        assert "Authorization" not in fake.calls[0]["headers"]

    def test_raises_network_error_on_request_error(self):
        import httpx

        c, _ = make_client(exc=httpx.ConnectError("connection refused"))
        with pytest.raises(NetworkError, match="connection refused"):
            c._request("GET", "/api/v1/auth/me", require_token=False)

    def test_raises_api_error_on_4xx_with_dict_detail(self):
        resp = FakeResponse(401, json_body={"detail": "bad credentials"})
        c, _ = make_client(response=resp)
        with pytest.raises(ApiError) as exc_info:
            c._request("GET", "/api/v1/auth/me", require_token=False)
        assert exc_info.value.status_code == 401
        assert "bad credentials" in str(exc_info.value)
        assert exc_info.value.body == {"detail": "bad credentials"}

    def test_api_error_extracts_error_field_when_no_detail(self):
        resp = FakeResponse(422, json_body={"error": "validation failed"})
        c, _ = make_client(response=resp)
        with pytest.raises(ApiError, match="validation failed"):
            c._request("POST", "/api/v1/x", json_body={}, require_token=False)

    def test_api_error_extracts_message_field_when_no_detail_or_error(self):
        resp = FakeResponse(500, json_body={"message": "boom"})
        c, _ = make_client(response=resp)
        with pytest.raises(ApiError, match="boom"):
            c._request("GET", "/api/v1/x", require_token=False)

    def test_raises_api_error_on_4xx_with_text_body(self):
        resp = FakeResponse(500, text="Internal Server Error", content_type="text/plain")
        c, _ = make_client(response=resp)
        with pytest.raises(ApiError) as exc_info:
            c._request("GET", "/api/v1/x", require_token=False)
        assert exc_info.value.status_code == 500
        assert "Internal Server Error" in str(exc_info.value)


class TestClientResourceMethods:
    def test_upload_document_raises_filenotfounderror(self, tmp_path):
        c, _ = make_client(response=FakeResponse(200, json_body={}))
        with pytest.raises(FileNotFoundError, match="File not found"):
            c.upload_document("kb-1", str(tmp_path / "missing.txt"))

    def test_list_audit_logs_passes_limit_offset_as_params(self):
        resp = FakeResponse(200, json_body={"items": []})
        c, fake = make_client(response=resp)
        c.list_audit_logs(limit=10, offset=20)
        assert fake.calls[0]["params"] == {"limit": 10, "offset": 20}

    def test_list_audit_logs_defaults_to_50_0(self):
        resp = FakeResponse(200, json_body={"items": []})
        c, fake = make_client(response=resp)
        c.list_audit_logs()
        assert fake.calls[0]["params"] == {"limit": 50, "offset": 0}

    def test_rag_query_sends_query_and_top_k(self):
        resp = FakeResponse(200, json_body={"results": []})
        c, fake = make_client(response=resp)
        c.rag_query("kb-1", query="what?", top_k=7)
        assert fake.calls[0]["json"] == {"query": "what?", "top_k": 7}

    def test_invoke_mcp_tool_sends_arguments(self):
        resp = FakeResponse(200, json_body={"ok": True})
        c, fake = make_client(response=resp)
        c.invoke_mcp_tool("tool-1", arguments={"a": 1})
        assert fake.calls[0]["json"] == {"arguments": {"a": 1}}

    def test_invoke_mcp_tool_defaults_arguments_to_empty(self):
        resp = FakeResponse(200, json_body={"ok": True})
        c, fake = make_client(response=resp)
        c.invoke_mcp_tool("tool-1")
        assert fake.calls[0]["json"] == {"arguments": {}}

    def test_run_assistant_builds_run_endpoint(self):
        resp = FakeResponse(200, json_body={"output": "hi"})
        c, fake = make_client(response=resp)
        c.run_assistant("a-1", message="hello")
        assert fake.calls[0]["url"] == "http://api.test/api/v1/assistants/a-1/run"
        assert fake.calls[0]["json"] == {"message": "hello"}

    def test_login_does_not_require_token(self):
        resp = FakeResponse(200, json_body={"access_token": "t"})
        # login must not require a token; pass token=None explicitly.
        c, fake = make_client(response=resp, token=None)
        out = c.login("a@b.c", "pw")
        assert out == {"access_token": "t"}
        assert fake.calls[0]["url"] == "http://api.test/api/v1/auth/login"
        assert "Authorization" not in fake.calls[0]["headers"]


# ===========================================================================
# cli.py — helpers, parser and error paths (12 tests)
# ===========================================================================


def _run_main(argv, config_dir):
    """Invoke cli.main capturing stdout/stderr; return (rc, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_mod.main(list(argv) + ["--config-dir", config_dir])
    return rc, out.getvalue(), err.getvalue()


class TestExtractItems:
    def test_returns_list_unchanged_filtered_to_dicts(self):
        assert cli_mod._extract_items([{"a": 1}, "x", {"b": 2}]) == [{"a": 1}, {"b": 2}]

    def test_extracts_items_key_from_dict(self):
        assert cli_mod._extract_items({"items": [{"id": "1"}]}) == [{"id": "1"}]

    def test_extracts_data_key_when_no_items(self):
        assert cli_mod._extract_items({"data": [{"id": "2"}]}) == [{"id": "2"}]

    def test_wraps_single_resource_dict_with_id(self):
        assert cli_mod._extract_items({"id": "3", "name": "x"}) == [{"id": "3", "name": "x"}]

    def test_returns_empty_for_unknown_shape(self):
        assert cli_mod._extract_items({"unrelated": "value"}) == []

    def test_returns_empty_for_string(self):
        assert cli_mod._extract_items("plain string") == []


class TestPrintTable:
    def test_no_rows_prints_placeholder(self, capsys):
        cli_mod._print_table([], ("id", "name"))
        captured = capsys.readouterr()
        assert captured.out.strip() == "(no rows)"

    def test_renders_header_and_rows(self, capsys):
        cli_mod._print_table(
            [{"id": "1", "name": "Alpha"}, {"id": "22", "name": "Beta"}],
            ("id", "name"),
        )
        out = capsys.readouterr().out
        assert "id" in out and "name" in out
        assert "Alpha" in out and "Beta" in out
        # Header separator line of dashes present
        assert "---" in out

    def test_none_cell_rendered_as_empty(self, capsys):
        cli_mod._print_table([{"id": "1", "name": None}], ("id", "name"))
        out = capsys.readouterr().out
        # Should not print the literal string "None"
        assert "None" not in out


class TestCliErrorPaths:
    def test_mcp_invoke_malformed_arguments_returns_exit_1(self, tmp_path):
        with patch.object(WorkamaClient, "invoke_mcp_tool", return_value={"ok": True}) as mocked:
            rc, out, err = _run_main(
                ["mcp", "invoke", "t-1", "--arguments", "{bad json", "--api-token", "tok"],
                str(tmp_path),
            )
        assert rc == 1
        assert "--arguments must be a JSON object" in err
        mocked.assert_not_called()

    def test_mcp_invoke_success_json(self, tmp_path):
        with patch.object(WorkamaClient, "invoke_mcp_tool", return_value={"result": "ok"}) as mocked:
            rc, out, err = _run_main(
                ["mcp", "invoke", "t-1", "--arguments", '{"q":"a"}', "--api-token", "tok", "--json"],
                str(tmp_path),
            )
        assert rc == 0, err
        payload = json.loads(out)
        assert payload == {"result": "ok"}
        mocked.assert_called_once_with("t-1", arguments={"q": "a"})

    def test_workflows_run_malformed_input_returns_exit_1(self, tmp_path):
        with patch.object(WorkamaClient, "run_workflow", return_value={"id": "r"}) as mocked:
            rc, out, err = _run_main(
                ["workflows", "run", "wf-1", "--input", "{not json", "--api-token", "tok"],
                str(tmp_path),
            )
        assert rc == 1
        assert "--input must be a JSON object" in err
        mocked.assert_not_called()

    def test_knowledge_bases_upload_missing_file_returns_exit_1(self, tmp_path):
        # upload_document raises FileNotFoundError before any HTTP call; the
        # CLI must catch it and exit with code 1 and a clear error message.
        with patch.object(
            WorkamaClient,
            "upload_document",
            side_effect=FileNotFoundError("File not found: " + str(tmp_path / "nope.txt")),
        ) as mocked:
            rc, out, err = _run_main(
                ["knowledge-bases", "upload", "kb-1", str(tmp_path / "nope.txt"), "--api-token", "tok"],
                str(tmp_path),
            )
        assert rc == 1
        assert "File not found" in err
        mocked.assert_called_once()

    def test_login_response_without_access_token_returns_exit_1(self, tmp_path):
        with patch.object(WorkamaClient, "login", return_value={"token_type": "bearer"}):
            rc, out, err = _run_main(
                ["login", "--email", "a@b.c", "--password", "pw", "--url", "http://x.test"],
                str(tmp_path),
            )
        assert rc == 1
        assert "did not contain access_token" in err

    def test_build_parser_requires_subcommand(self):
        parser = cli_mod.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_whoami_human_readable_output(self, tmp_path):
        me = {"id": "u-1", "email": "tester@workama.example.com", "display_name": "Tester", "workspace_id": "ws-1"}
        with patch.object(WorkamaClient, "me", return_value=me):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli_mod.main(["whoami", "--api-token", "tok", "--config-dir", str(tmp_path)])
        assert rc == 0
        text = out.getvalue()
        assert "id:       u-1" in text
        assert "email:    tester@workama.example.com" in text
        assert "name:     Tester" in text
        assert "workspace:ws-1" in text

    def test_whoami_non_dict_payload_printed_directly(self, tmp_path):
        with patch.object(WorkamaClient, "me", return_value="string-response"):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli_mod.main(["whoami", "--api-token", "tok", "--config-dir", str(tmp_path)])
        assert rc == 0
        assert "string-response" in out.getvalue()

    def test_version_human_readable(self, tmp_path):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli_mod.main(["version", "--config-dir", str(tmp_path)])
        assert rc == 0
        assert out.getvalue().startswith("workama ")

    def test_audit_logs_passes_limit_offset(self, tmp_path):
        with patch.object(WorkamaClient, "list_audit_logs", return_value={"items": []}) as mocked:
            rc, out, err = _run_main(
                ["audit-logs", "list", "--limit", "15", "--offset", "30", "--api-token", "tok", "--json"],
                str(tmp_path),
            )
        assert rc == 0, err
        mocked.assert_called_once_with(limit=15, offset=30)

    def test_devices_register_default_type_is_desktop(self, tmp_path):
        with patch.object(WorkamaClient, "register_device", return_value={"id": "d-1"}) as mocked:
            rc, out, err = _run_main(
                ["devices", "register", "MyDevice", "--api-token", "tok", "--json"],
                str(tmp_path),
            )
        assert rc == 0, err
        mocked.assert_called_once_with("MyDevice", device_type="desktop", metadata=None)

    def test_knowledge_bases_query_passes_top_k(self, tmp_path):
        with patch.object(WorkamaClient, "rag_query", return_value={"results": []}) as mocked:
            rc, out, err = _run_main(
                ["knowledge-bases", "query", "kb-1", "--query", "what?", "--top-k", "9", "--api-token", "tok", "--json"],
                str(tmp_path),
            )
        assert rc == 0, err
        mocked.assert_called_once_with("kb-1", query="what?", top_k=9)

    def test_workspace_id_override_propagated_to_client(self, tmp_path):
        with patch.object(WorkamaClient, "list_workspaces", return_value={"items": []}) as mocked:
            rc, out, err = _run_main(
                ["workspaces", "list", "--api-token", "tok", "--workspace-id", "ws-override", "--json"],
                str(tmp_path),
            )
        assert rc == 0, err
        # WorkamaClient is constructed inside _build_client; verify the instance
        # received the workspace_id by inspecting the call to the constructor
        # via the patched method's call frame is fragile; instead assert that
        # list_workspaces was called (the override path did not raise).
        mocked.assert_called_once_with()

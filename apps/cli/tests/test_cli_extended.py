"""Extended CLI tests for the WorkAMA command line client.

Covers config boundaries, auth flows, chat/run/code commands, redaction,
HTTP transport errors, ConfigStore file handling, and parser discovery.

These tests complement the existing test_cli.py suite without modifying
any CLI source code.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from workama.cli import (
    CliError,
    _redact_headers,
    _redact_sensitive,
    _redact_text,
    build_parser,
    command_auth_login,
)
from workama.config import ConfigError, ConfigStore
from workama.transport import ApiError, TransportError, endpoint, HttpClient


CLI_DIR = Path(__file__).resolve().parents[1]
CLI_SCRIPT = CLI_DIR / "workama.py"


def _run_cli(*args: str, config_dir: str) -> subprocess.CompletedProcess:
    """Invoke the workama CLI script in an isolated config directory.

    input="" sets stdin to a closed pipe so that sys.stdin.isatty()
    returns False inside the subprocess, matching non-interactive usage.
    (On Windows, subprocess.DEVNULL still reports isatty() == True.)
    """
    env = os.environ.copy()
    env["WORKAMA_CONFIG_DIR"] = config_dir
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        input="",
    )


class HelpAndVersionTests(unittest.TestCase):
    """Verify --version and --help output."""

    def test_version_output_contains_program_name(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli("--version", config_dir=config_dir)
            self.assertEqual(result.returncode, 0)
            self.assertIn("workama", result.stdout)

    def test_help_lists_all_top_level_subcommands(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli("--help", config_dir=config_dir)
            self.assertEqual(result.returncode, 0)
            for cmd in ("auth", "config", "chat", "run", "code", "event", "workspace"):
                self.assertIn(cmd, result.stdout)

    def test_subcommand_help_exits_zero(self):
        with tempfile.TemporaryDirectory() as config_dir:
            for cmd in ("auth", "config", "chat", "run", "code", "event", "workspace"):
                result = _run_cli(cmd, "--help", config_dir=config_dir)
                self.assertEqual(result.returncode, 0, f"{cmd} --help failed: {result.stderr}")


class ConfigCommandTests(unittest.TestCase):
    """Config set/get/use/profiles boundaries."""

    def test_config_get_unset_key_returns_error(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli("config", "get", "workspace_id", config_dir=config_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("not set", result.stderr)

    def test_config_get_all_redacts_api_key_by_default(self):
        with tempfile.TemporaryDirectory() as config_dir:
            _run_cli("config", "set", "api_key", "super-secret", config_dir=config_dir)
            result = _run_cli("config", "get", "--json", config_dir=config_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["api_key"], "<configured>")
            self.assertNotIn("super-secret", result.stdout)

    def test_config_get_show_secrets_reveals_values(self):
        with tempfile.TemporaryDirectory() as config_dir:
            _run_cli("config", "set", "api_key", "super-secret", config_dir=config_dir)
            result = _run_cli(
                "config", "get", "--show-secrets", "--json", config_dir=config_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["api_key"], "super-secret")

    def test_config_set_and_get_with_named_profile(self):
        with tempfile.TemporaryDirectory() as config_dir:
            set_result = _run_cli(
                "config", "set", "model", "staging-model",
                "--profile", "staging",
                "--json",
                config_dir=config_dir,
            )
            self.assertEqual(set_result.returncode, 0, set_result.stderr)
            get_result = _run_cli(
                "config", "get", "model",
                "--profile", "staging",
                config_dir=config_dir,
            )
            self.assertEqual(get_result.returncode, 0, get_result.stderr)
            self.assertEqual(get_result.stdout.strip(), "staging-model")

    def test_config_set_secret_key_shows_configured_in_json(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli(
                "config", "set", "api_key", "my-secret", "--json", config_dir=config_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["value"], "<configured>")
            self.assertNotIn("my-secret", result.stdout)


class AuthCommandTests(unittest.TestCase):
    """Auth login flows and token persistence."""

    def test_auth_login_access_token_is_persisted(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli(
                "auth",
                "login",
                "--access-token",
                "plat-token-secret",
                "--platform-url",
                "http://platform.example.test",
                "--json",
                config_dir=config_dir,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["access_token"], "<configured>")
            self.assertNotIn("plat-token-secret", result.stdout)
            get_result = _run_cli("config", "get", "access_token", config_dir=config_dir)
            self.assertEqual(get_result.stdout.strip(), "plat-token-secret")

    def test_auth_login_without_credentials_raises_cli_error(self):
        """auth login without --api-key, --access-token, or email/password raises CliError."""
        args = argparse.Namespace(
            api_key=None,
            access_token=None,
            email=None,
            password=None,
            mfa_code=None,
            profile=None,
            json=False,
            timeout=30.0,
            platform_url=None,
            gateway_url=None,
        )
        store = ConfigStore(path=Path(tempfile.mkdtemp()) / "config.json")
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(CliError) as ctx:
                command_auth_login(args, store)
        self.assertIn("requires", str(ctx.exception).lower())


class ChatCommandTests(unittest.TestCase):
    """Chat prompt validation and payload construction."""

    def test_chat_without_prompt_errors_in_non_interactive_mode(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli("chat", config_dir=config_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("prompt", result.stderr.lower())

    def test_chat_dry_run_includes_optional_parameters(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli(
                "chat",
                "hello",
                "--system",
                "You are helpful.",
                "--temperature",
                "0.5",
                "--max-tokens",
                "42",
                "--dry-run",
                "--json",
                config_dir=config_dir,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            body = json.loads(result.stdout)["request"]["body"]
            self.assertEqual(
                body["messages"][0], {"role": "system", "content": "You are helpful."}
            )
            self.assertEqual(
                body["messages"][1], {"role": "user", "content": "hello"}
            )
            self.assertEqual(body["temperature"], 0.5)
            self.assertEqual(body["max_tokens"], 42)


class RunCommandTests(unittest.TestCase):
    """Run command dry-run and parameter validation."""

    def test_run_dry_run_reflects_custom_limits(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli(
                "run",
                "inspect repo",
                "--model",
                "custom-model",
                "--max-steps",
                "5",
                "--max-credits",
                "100",
                "--max-duration",
                "600",
                "--dry-run",
                "--json",
                config_dir=config_dir,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            session_body = output["session"]["body"]
            self.assertEqual(session_body["model"], "custom-model")
            self.assertEqual(session_body["max_steps"], 5)
            self.assertEqual(session_body["max_credits"], 100)
            self.assertEqual(session_body["max_duration_seconds"], 600)


class CodeCommandTests(unittest.TestCase):
    """Code task/list/event validation and dry-run."""

    def test_code_task_dry_run_includes_repository_and_session(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli(
                "code",
                "task",
                "implement feature",
                "--api-base-url",
                "http://code.example.test",
                "--access-token",
                "tok",
                "--repository-id",
                "repo-123",
                "--session-id",
                "sess-456",
                "--title",
                "My Task",
                "--dry-run",
                "--json",
                config_dir=config_dir,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            body = json.loads(result.stdout)["request"]["body"]
            self.assertEqual(body["repository_id"], "repo-123")
            self.assertEqual(body["session_id"], "sess-456")
            self.assertEqual(body["title"], "My Task")
            self.assertEqual(body["branch"], "workama/task")

    def test_code_list_with_limit_below_one_errors(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli(
                "code",
                "list",
                "--api-base-url",
                "http://code.example.test",
                "--limit",
                "0",
                config_dir=config_dir,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("limit", result.stderr.lower())

    def test_code_event_with_negative_after_errors(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli(
                "code",
                "event",
                "task-1",
                "--api-base-url",
                "http://code.example.test",
                "--after",
                "-1",
                config_dir=config_dir,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("after", result.stderr.lower())

    def test_code_task_with_query_in_api_url_errors(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli(
                "code",
                "task",
                "prompt",
                "--api-base-url",
                "http://code.example.test?query=1",
                config_dir=config_dir,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("must not contain", result.stderr)

    def test_code_list_dry_run_includes_status_filter(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli(
                "code",
                "list",
                "--api-base-url",
                "http://code.example.test",
                "--status",
                "running",
                "--limit",
                "10",
                "--dry-run",
                "--json",
                config_dir=config_dir,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            url = json.loads(result.stdout)["request"]["url"]
            self.assertIn("status=running", url)
            self.assertIn("limit=10", url)


class RedactionUnitTests(unittest.TestCase):
    """Sensitive field redaction in dicts, lists, and strings."""

    def test_redact_sensitive_replaces_secret_keys(self):
        self.assertEqual(
            _redact_sensitive({"api_key": "secret"}), {"api_key": "<redacted>"}
        )
        self.assertEqual(
            _redact_sensitive({"access_token": "tok"}),
            {"access_token": "<redacted>"},
        )
        self.assertEqual(
            _redact_sensitive({"password": "pw"}), {"password": "<redacted>"}
        )
        self.assertEqual(
            _redact_sensitive({"refresh_token": "rt"}),
            {"refresh_token": "<redacted>"},
        )
        self.assertEqual(
            _redact_sensitive({"ws_ticket": "t"}), {"ws_ticket": "<redacted>"}
        )

    def test_redact_sensitive_handles_nested_structures(self):
        data = {
            "outer": {"inner": {"api_key": "nested-secret"}},
            "items": [{"token": "item-tok"}, {"name": "safe"}],
            "safe_key": "visible",
        }
        result = _redact_sensitive(data)
        self.assertEqual(result["outer"]["inner"]["api_key"], "<redacted>")
        self.assertEqual(result["items"][0]["token"], "<redacted>")
        self.assertEqual(result["items"][1]["name"], "safe")
        self.assertEqual(result["safe_key"], "visible")

    def test_redact_sensitive_redacts_bearer_authorization(self):
        result = _redact_sensitive({"authorization": "Bearer abc123"})
        self.assertEqual(result["authorization"], "Bearer <redacted>")

    def test_redact_text_replaces_bearer_and_key_patterns(self):
        self.assertEqual(_redact_text("Bearer abc123"), "Bearer <redacted>")
        self.assertEqual(_redact_text("api_key=secret123"), "api_key=<redacted>")
        self.assertEqual(_redact_text("password: pw123"), "password:<redacted>")
        self.assertEqual(_redact_text("safe text"), "safe text")

    def test_redact_headers_masks_authorization_only(self):
        result = _redact_headers(
            {"Authorization": "Bearer secret", "X-Other": "keep"}
        )
        self.assertEqual(result["Authorization"], "Bearer <redacted>")
        self.assertEqual(result["X-Other"], "keep")


class TransportUnitTests(unittest.TestCase):
    """endpoint() helper and HttpClient error handling."""

    def test_endpoint_strips_duplicate_prefixes(self):
        self.assertEqual(
            endpoint("http://x.test", "/v1/chat"), "http://x.test/v1/chat"
        )
        self.assertEqual(
            endpoint("http://x.test/v1", "/v1/chat"), "http://x.test/v1/chat"
        )
        self.assertEqual(
            endpoint("http://x.test/api/v1", "/api/v1/code"),
            "http://x.test/api/v1/code",
        )
        self.assertEqual(
            endpoint("http://x.test/", "/v1/chat"), "http://x.test/v1/chat"
        )

    def test_http_client_raises_api_error_on_http_error(self):
        client = HttpClient(timeout=1.0)
        error = HTTPError(
            "http://x.test",
            400,
            "Bad Request",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"detail": "bad request"}'),
        )
        with patch("workama.transport.urlopen", side_effect=error):
            with self.assertRaises(ApiError) as ctx:
                client.request("GET", "http://x.test")
            self.assertEqual(ctx.exception.status, 400)
            self.assertIn("bad request", str(ctx.exception))

    def test_http_client_raises_transport_error_on_url_error(self):
        client = HttpClient(timeout=1.0)
        with patch(
            "workama.transport.urlopen", side_effect=URLError("connection refused")
        ):
            with self.assertRaises(TransportError):
                client.request("GET", "http://x.test")


class ConfigStoreUnitTests(unittest.TestCase):
    """ConfigStore file handling, defaults, and environment overrides."""

    def test_config_store_invalid_shape_raises_config_error(self):
        with tempfile.TemporaryDirectory() as config_dir:
            config_path = Path(config_dir) / "config.json"
            config_path.write_text('{"profiles": "not_a_dict"}', encoding="utf-8")
            store = ConfigStore(path=config_path)
            with self.assertRaises(ConfigError):
                store.values()

    def test_config_store_display_values_redacts_by_default(self):
        with tempfile.TemporaryDirectory() as config_dir:
            store = ConfigStore(path=Path(config_dir) / "config.json")
            store.set("api_key", "hidden-secret")
            display = store.display_values()
            self.assertEqual(display["api_key"], "<configured>")
            display_full = store.display_values(show_secrets=True)
            self.assertEqual(display_full["api_key"], "hidden-secret")

    def test_config_store_environment_override(self):
        with tempfile.TemporaryDirectory() as config_dir:
            store = ConfigStore(path=Path(config_dir) / "config.json")
            with patch.dict(os.environ, {"WORKAMA_MODEL": "env-model"}):
                self.assertEqual(store.get("model"), "env-model")

    def test_config_store_defaults_when_file_missing(self):
        with tempfile.TemporaryDirectory() as config_dir:
            store = ConfigStore(path=Path(config_dir) / "nonexistent.json")
            values = store.values()
            self.assertEqual(values["gateway_url"], "http://localhost:20202")
            self.assertEqual(values["model"], "workama-chat")


class ParserRegistryTests(unittest.TestCase):
    """Verify all subcommands are discoverable via the parser."""

    def test_build_parser_help_contains_all_top_level_commands(self):
        parser = build_parser()
        help_text = parser.format_help()
        for cmd in ("auth", "config", "chat", "run", "code", "event", "workspace"):
            self.assertIn(cmd, help_text)

    def test_build_parser_code_subcommands_parse(self):
        parser = build_parser()
        args = parser.parse_args(["code", "task", "do-stuff"])
        self.assertEqual(args.code_action, "task")
        args = parser.parse_args(["code", "list"])
        self.assertEqual(args.code_action, "list")
        args = parser.parse_args(["code", "event", "task-1"])
        self.assertEqual(args.code_action, "event")
        args = parser.parse_args(["code", "status", "task-1"])
        self.assertEqual(args.code_action, "status")
        args = parser.parse_args(["code", "artifact", "--task", "task-1"])
        self.assertEqual(args.code_action, "artifact")
        args = parser.parse_args(["code", "artifact", "--download", "art_1"])
        self.assertEqual(args.code_action, "artifact")

    def test_build_parser_event_subcommands_parse(self):
        parser = build_parser()
        args = parser.parse_args(["event", "tail", "--session", "sess_1"])
        self.assertEqual(args.event_action, "tail")
        self.assertEqual(args.session, "sess_1")

    def test_build_parser_workspace_subcommands_parse(self):
        parser = build_parser()
        args = parser.parse_args(["workspace", "switch", "ws_1"])
        self.assertEqual(args.workspace_action, "switch")
        self.assertEqual(args.workspace_id, "ws_1")

    def test_build_parser_auth_subcommands_parse(self):
        parser = build_parser()
        args = parser.parse_args(["auth", "logout"])
        self.assertEqual(args.auth_action, "logout")
        args = parser.parse_args(["auth", "status"])
        self.assertEqual(args.auth_action, "status")

    def test_build_parser_chat_flags_parse(self):
        parser = build_parser()
        args = parser.parse_args(["chat", "--list"])
        self.assertTrue(args.list)
        args = parser.parse_args(["chat", "--resume", "sess_1"])
        self.assertEqual(args.resume, "sess_1")
        args = parser.parse_args(["chat", "hello"])
        self.assertEqual(args.prompt, "hello")


class AuthStatusHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_GET(self):  # noqa: N802 - stdlib handler API
        AuthStatusHandler.requests.append(
            {"method": "GET", "path": self.path, "authorization": self.headers.get("Authorization")}
        )
        if self.path == "/api/v1/auth/me":
            body = {"id": "user_status", "email": "status@example.test", "workspace_id": "ws_status"}
        else:
            self.send_error(404)
            return
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else None
        AuthStatusHandler.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        if self.path == "/api/v1/auth/logout":
            self.send_response(204)
            self.end_headers()
        else:
            response = {"ok": True}
            raw = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    def log_message(self, *_args):
        return


class AuthStatusLogoutTests(unittest.TestCase):
    """Auth status and logout against a mock Platform API."""

    def _start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def test_auth_status_shows_current_user_and_redacts_token(self):
        AuthStatusHandler.requests = []
        server = self._start_server(AuthStatusHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                result = _run_cli(
                    "auth",
                    "status",
                    "--access-token",
                    "secret-token",
                    "--platform-url",
                    f"http://127.0.0.1:{server.server_port}",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["user"]["email"], "status@example.test")
                self.assertEqual(AuthStatusHandler.requests[-1]["authorization"], "Bearer secret-token")
                self.assertNotIn("secret-token", result.stdout)
        finally:
            server.shutdown()
            server.server_close()

    def test_auth_logout_calls_endpoint_and_clears_local_tokens(self):
        AuthStatusHandler.requests = []
        server = self._start_server(AuthStatusHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                _run_cli("auth", "login", "--access-token", "secret-token", config_dir=config_dir)
                _run_cli("config", "set", "api_key", "secret-key", config_dir=config_dir)
                result = _run_cli(
                    "auth",
                    "logout",
                    "--platform-url",
                    f"http://127.0.0.1:{server.server_port}",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertTrue(data["logged_out"])
                self.assertIn("access_token", data["deleted_keys"])
                self.assertIn("api_key", data["deleted_keys"])
                self.assertEqual(AuthStatusHandler.requests[0]["path"], "/api/v1/auth/logout")
                self.assertEqual(AuthStatusHandler.requests[0]["authorization"], "Bearer secret-token")
                get_result = _run_cli("config", "get", "access_token", config_dir=config_dir)
                self.assertEqual(get_result.returncode, 2)
        finally:
            server.shutdown()
            server.server_close()


class ChatSessionHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib handler API
        if self.path == "/api/v1/sessions":
            body = {"items": [{"id": "sess_1", "title": "Hello session", "status": "active"}]}
        elif self.path.startswith("/api/v1/sessions/") and "/events" in self.path:
            body = {
                "items": [
                    {
                        "id": "evt_1",
                        "type": "agent.message.completed",
                        "seq": 1,
                        "payload": {"content": "Hi there"},
                    }
                ]
            }
        else:
            self.send_error(404)
            return
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


class ChatListResumeTests(unittest.TestCase):
    """Chat list and resume against a mock Platform API."""

    def _start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def test_chat_list_outputs_sessions(self):
        server = self._start_server(ChatSessionHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                result = _run_cli(
                    "chat",
                    "--list",
                    "--access-token",
                    "tok",
                    "--platform-url",
                    f"http://127.0.0.1:{server.server_port}",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["items"][0]["id"], "sess_1")
        finally:
            server.shutdown()
            server.server_close()

    def test_chat_resume_prints_assistant_content(self):
        server = self._start_server(ChatSessionHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                result = _run_cli(
                    "chat",
                    "--resume",
                    "sess_1",
                    "--access-token",
                    "tok",
                    "--platform-url",
                    f"http://127.0.0.1:{server.server_port}",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Hi there", result.stdout)
        finally:
            server.shutdown()
            server.server_close()


class ConfigWorkspaceTests(unittest.TestCase):
    """Config use-workspace and unset actions."""

    def test_config_use_workspace_sets_workspace_id(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli("config", "use-workspace", "ws_42", "--json", config_dir=config_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["workspace_id"], "ws_42")
            get_result = _run_cli("config", "get", "workspace_id", config_dir=config_dir)
            self.assertEqual(get_result.stdout.strip(), "ws_42")

    def test_config_unset_removes_key(self):
        with tempfile.TemporaryDirectory() as config_dir:
            _run_cli("config", "set", "custom_key", "x", config_dir=config_dir)
            result = _run_cli("config", "unset", "custom_key", "--json", config_dir=config_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertTrue(data["existed"])
            get_result = _run_cli("config", "get", "custom_key", config_dir=config_dir)
            self.assertEqual(get_result.returncode, 2)


class CodeStatusHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_GET(self):  # noqa: N802 - stdlib handler API
        CodeStatusHandler.requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
        if self.path.startswith("/api/v1/code/tasks/") and "/events" in self.path:
            body = {
                "items": [
                    {"id": "evt_1", "task_id": "ctask_status", "seq": 1, "type": "terminal.output", "payload": {}}
                ]
            }
        elif self.path.startswith("/api/v1/code/repositories/"):
            body = {"id": "repo_1", "name": "workama", "provider": "github"}
        elif self.path.startswith("/api/v1/code/tasks/"):
            body = {"id": "ctask_status", "title": "feature work", "status": "running", "repository_id": "repo_1"}
        elif self.path.startswith("/api/v1/code/tasks"):
            body = {"items": [{"id": "ctask_status", "title": "feature work", "status": "running"}]}
        else:
            self.send_error(404)
            return
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


class CodeStatusTests(unittest.TestCase):
    """Code task status command."""

    def _start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def test_code_status_dry_run_and_live(self):
        CodeStatusHandler.requests = []
        server = self._start_server(CodeStatusHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                base_url = f"http://127.0.0.1:{server.server_port}"
                dry = _run_cli(
                    "code",
                    "status",
                    "ctask_status",
                    "--api-base-url",
                    base_url,
                    "--access-token",
                    "tok",
                    "--dry-run",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(dry.returncode, 0, dry.stderr)
                self.assertIn("/api/v1/code/tasks/ctask_status", json.loads(dry.stdout)["request"]["url"])
                live = _run_cli(
                    "code",
                    "status",
                    "ctask_status",
                    "--api-base-url",
                    base_url,
                    "--access-token",
                    "tok",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(live.returncode, 0, live.stderr)
                data = json.loads(live.stdout)
                self.assertEqual(data["task"]["id"], "ctask_status")
                self.assertEqual(data["task"]["status"], "running")
                self.assertEqual(data["repository"]["name"], "workama")
                self.assertEqual(data["recent_events"][0]["id"], "evt_1")
                self.assertTrue(any("/api/v1/code/tasks/ctask_status/events" in r["path"] for r in CodeStatusHandler.requests))
        finally:
            server.shutdown()
            server.server_close()

    def test_code_status_without_task_id_lists_recent_tasks(self):
        CodeStatusHandler.requests = []
        server = self._start_server(CodeStatusHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                base_url = f"http://127.0.0.1:{server.server_port}"
                result = _run_cli(
                    "code",
                    "status",
                    "--api-base-url",
                    base_url,
                    "--access-token",
                    "tok",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["items"][0]["id"], "ctask_status")
                self.assertTrue(any(r["path"].startswith("/api/v1/code/tasks") for r in CodeStatusHandler.requests))
        finally:
            server.shutdown()
            server.server_close()


class EventTailHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    events_after: int = 0

    def do_GET(self):  # noqa: N802 - stdlib handler API
        EventTailHandler.requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
        if self.path.startswith("/api/v1/sessions/sess_tail/events"):
            after = self.path.split("after=", 1)[1].split("&")[0]
            EventTailHandler.events_after = int(after)
            body = {
                "items": [
                    {"id": "evt_tail_1", "seq": int(after) + 1, "type": "agent.message.delta", "payload": {"delta": "hi"}}
                ]
            }
        else:
            self.send_error(404)
            return
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


class EventTailTests(unittest.TestCase):
    """event tail command."""

    def _start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def test_event_tail_outputs_events(self):
        EventTailHandler.requests = []
        EventTailHandler.events_after = 0
        server = self._start_server(EventTailHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                base_url = f"http://127.0.0.1:{server.server_port}"
                result = _run_cli(
                    "event",
                    "tail",
                    "--session",
                    "sess_tail",
                    "--after",
                    "3",
                    "--platform-url",
                    base_url,
                    "--access-token",
                    "tok",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["id"], "evt_tail_1")
                self.assertEqual(data["seq"], 4)
                self.assertEqual(EventTailHandler.events_after, 3)
                self.assertEqual(EventTailHandler.requests[-1]["authorization"], "Bearer tok")
        finally:
            server.shutdown()
            server.server_close()

    def test_event_tail_dry_run(self):
        EventTailHandler.requests = []
        server = self._start_server(EventTailHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                base_url = f"http://127.0.0.1:{server.server_port}"
                result = _run_cli(
                    "event",
                    "tail",
                    "--session",
                    "sess_tail",
                    "--after",
                    "3",
                    "--platform-url",
                    base_url,
                    "--access-token",
                    "tok",
                    "--dry-run",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("/api/v1/sessions/sess_tail/events?after=3", json.loads(result.stdout)["request"]["url"])
                self.assertEqual(EventTailHandler.requests, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_event_tail_without_token_errors(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli(
                "event",
                "tail",
                "--session",
                "sess_tail",
                config_dir=config_dir,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("access token", result.stderr.lower())


class ErrorHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib handler API
        self.send_error(401, "Unauthorized")

    def do_POST(self):  # noqa: N802 - stdlib handler API
        self.send_error(401, "Unauthorized")

    def log_message(self, *_args):
        return


class WorkspaceSwitchHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else None
        WorkspaceSwitchHandler.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": body})
        if self.path == "/api/v1/workspaces/ws_switch/switch":
            response = {
                "workspace": {"id": "ws_switch", "name": "Switched Workspace"},
                "workspace_token": "ws-token-secret",
                "token_type": "workspace_context",
            }
        else:
            self.send_error(404)
            return
        raw = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


class WorkspaceSwitchTests(unittest.TestCase):
    """workspace switch command."""

    def _start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def test_workspace_switch_calls_endpoint_and_saves_default(self):
        WorkspaceSwitchHandler.requests = []
        server = self._start_server(WorkspaceSwitchHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                base_url = f"http://127.0.0.1:{server.server_port}"
                result = _run_cli(
                    "workspace",
                    "switch",
                    "ws_switch",
                    "--set-default",
                    "--platform-url",
                    base_url,
                    "--access-token",
                    "tok",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["workspace"]["id"], "ws_switch")
                self.assertNotIn("ws-token-secret", result.stdout)
                self.assertEqual(WorkspaceSwitchHandler.requests[0]["path"], "/api/v1/workspaces/ws_switch/switch")
                self.assertEqual(WorkspaceSwitchHandler.requests[0]["authorization"], "Bearer tok")
                get_result = _run_cli("config", "get", "workspace_id", config_dir=config_dir)
                self.assertEqual(get_result.stdout.strip(), "ws_switch")
        finally:
            server.shutdown()
            server.server_close()

    def test_workspace_switch_dry_run(self):
        WorkspaceSwitchHandler.requests = []
        server = self._start_server(WorkspaceSwitchHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                base_url = f"http://127.0.0.1:{server.server_port}"
                result = _run_cli(
                    "workspace",
                    "switch",
                    "ws_switch",
                    "--platform-url",
                    base_url,
                    "--access-token",
                    "tok",
                    "--dry-run",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("/api/v1/workspaces/ws_switch/switch", json.loads(result.stdout)["request"]["url"])
                self.assertEqual(WorkspaceSwitchHandler.requests, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_workspace_switch_http_error_exits_two(self):
        server = self._start_server(ErrorHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                base_url = f"http://127.0.0.1:{server.server_port}"
                result = _run_cli(
                    "workspace",
                    "switch",
                    "ws_switch",
                    "--platform-url",
                    base_url,
                    "--access-token",
                    "tok",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("401", result.stderr)
        finally:
            server.shutdown()
            server.server_close()


class CodeArtifactHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_GET(self):  # noqa: N802 - stdlib handler API
        CodeArtifactHandler.requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
        if self.path.startswith("/api/v1/code/tasks/ctask_art"):
            body = {"id": "ctask_art", "title": "art task", "status": "succeeded", "session_id": "sess_art"}
        elif self.path == "/api/v1/sessions/sess_art/artifacts":
            body = {
                "items": [
                    {"id": "art_1", "name": "README.md", "kind": "file", "content_type": "text/plain", "size_bytes": 12}
                ]
            }
        elif self.path == "/api/v1/artifacts/art_1/download":
            body = b"hello world\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        else:
            self.send_error(404)
            return
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


class CodeArtifactTests(unittest.TestCase):
    """code artifact command."""

    def _start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def test_code_artifact_lists_task_artifacts(self):
        CodeArtifactHandler.requests = []
        server = self._start_server(CodeArtifactHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                base_url = f"http://127.0.0.1:{server.server_port}"
                result = _run_cli(
                    "code",
                    "artifact",
                    "--task",
                    "ctask_art",
                    "--api-base-url",
                    base_url,
                    "--access-token",
                    "tok",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["items"][0]["id"], "art_1")
                self.assertTrue(any(r["path"] == "/api/v1/sessions/sess_art/artifacts" for r in CodeArtifactHandler.requests))
        finally:
            server.shutdown()
            server.server_close()

    def test_code_artifact_downloads_artifact_to_file(self):
        CodeArtifactHandler.requests = []
        server = self._start_server(CodeArtifactHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                output_path = Path(config_dir) / "README.md"
                base_url = f"http://127.0.0.1:{server.server_port}"
                result = _run_cli(
                    "code",
                    "artifact",
                    "--download",
                    "art_1",
                    "--output",
                    str(output_path),
                    "--api-base-url",
                    base_url,
                    "--access-token",
                    "tok",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Downloaded artifact art_1", result.stdout)
                self.assertEqual(output_path.read_bytes(), b"hello world\n")
                self.assertTrue(any(r["path"] == "/api/v1/artifacts/art_1/download" for r in CodeArtifactHandler.requests))
        finally:
            server.shutdown()
            server.server_close()

    def test_code_artifact_without_task_or_download_errors(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = _run_cli(
                "code",
                "artifact",
                "--api-base-url",
                "http://code.example.test",
                config_dir=config_dir,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("requires", result.stderr.lower())

    def test_code_artifact_download_http_error_exits_two(self):
        server = self._start_server(ErrorHandler)
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                base_url = f"http://127.0.0.1:{server.server_port}"
                result = _run_cli(
                    "code",
                    "artifact",
                    "--download",
                    "art_missing",
                    "--api-base-url",
                    base_url,
                    "--access-token",
                    "tok",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("401", result.stderr)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

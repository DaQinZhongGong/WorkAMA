from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


CLI_DIR = Path(__file__).resolve().parents[1]
CLI_SCRIPT = CLI_DIR / "workama.py"


class ChatHandler(BaseHTTPRequestHandler):
    received: dict = {}

    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        ChatHandler.received = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(self.rfile.read(length)),
        }
        payload = {
            "id": "chat_test",
            "object": "chat.completion",
            "model": "workama-chat",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
        }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


class CodeHandler(BaseHTTPRequestHandler):
    authorization = None
    event_after = None
    created = None

    def do_POST(self):  # noqa: N802 - stdlib handler API
        CodeHandler.authorization = self.headers.get("Authorization")
        length = int(self.headers.get("Content-Length", "0"))
        CodeHandler.created = json.loads(self.rfile.read(length))
        payload = {
            "id": "ctask_created",
            "title": CodeHandler.created["title"],
            "prompt": CodeHandler.created["prompt"],
            "branch": CodeHandler.created["branch"],
            "status": "queued",
        }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802 - stdlib handler API
        CodeHandler.authorization = self.headers.get("Authorization")
        if self.path.startswith("/api/v1/code/tasks/") and "/events" in self.path:
            CodeHandler.event_after = self.path.split("after=", 1)[1]
            payload = {
                "items": [
                    {
                        "id": "evt_code_1",
                        "task_id": "ctask_code",
                        "seq": 4,
                        "type": "terminal.output",
                        "payload": {"authorization": "Bearer event-secret", "status": "succeeded"},
                    }
                ]
            }
        else:
            payload = {
                "items": [
                    {"id": "ctask_code", "title": "add tests", "status": "queued"},
                    {"id": "ctask_other", "title": "refactor", "status": "succeeded"},
                ]
            }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


class MfaHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        MfaHandler.requests.append({"path": self.path, "body": payload})
        if self.path == "/api/v1/auth/login":
            response = {"mfa_required": True, "mfa_ticket": "mfa-ticket-secret"}
        elif self.path == "/api/v1/auth/mfa/challenge":
            response = {
                "access_token": "access-token-secret",
                "refresh_token": "refresh-token-secret",
                "token_type": "bearer",
                "user": {"id": "user_1", "email": "mfa@example.test", "workspace_id": "ws_1"},
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


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str, config_dir: str) -> subprocess.CompletedProcess:
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

    def test_help_and_config_round_trip(self):
        with tempfile.TemporaryDirectory() as config_dir:
            help_result = self.run_cli("--help", config_dir=config_dir)
            self.assertEqual(help_result.returncode, 0)
            self.assertIn("chat", help_result.stdout)
            self.assertIn("run", help_result.stdout)
            self.assertIn("code", help_result.stdout)

            set_result = self.run_cli("config", "set", "model", "test-model", config_dir=config_dir)
            self.assertEqual(set_result.returncode, 0, set_result.stderr)
            get_result = self.run_cli("config", "get", "model", config_dir=config_dir)
            self.assertEqual(get_result.stdout.strip(), "test-model")

    def test_api_key_login_and_chat_dry_run(self):
        with tempfile.TemporaryDirectory() as config_dir:
            login = self.run_cli("auth", "login", "--api-key", "secret-key", config_dir=config_dir)
            self.assertEqual(login.returncode, 0, login.stderr)
            dry_run = self.run_cli("chat", "hello", "--dry-run", "--json", config_dir=config_dir)
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            output = json.loads(dry_run.stdout)
            self.assertTrue(output["dry_run"])
            self.assertEqual(output["request"]["body"]["messages"][0]["content"], "hello")
            self.assertEqual(output["request"]["headers"]["Authorization"], "Bearer <redacted>")
            self.assertNotIn("secret-key", dry_run.stdout)

    def test_chat_http_request(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ChatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                result = self.run_cli(
                    "chat",
                    "hello",
                    "--gateway-url",
                    f"http://127.0.0.1:{server.server_port}",
                    "--api-key",
                    "secret-key",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["choices"][0]["message"]["content"], "pong")
                self.assertEqual(ChatHandler.received["path"], "/v1/chat/completions")
                self.assertEqual(ChatHandler.received["authorization"], "Bearer secret-key")
        finally:
            server.shutdown()
            server.server_close()

    def test_run_dry_run_contains_agent_contract(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = self.run_cli("run", "inspect repo", "--dry-run", "--json", config_dir=config_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertTrue(output["dry_run"])
            self.assertIn("/api/v1/sessions", output["session"]["url"])
            self.assertIn("/api/v1/sessions/<session-id>/ws-tickets", output["ticket"]["url"])
            self.assertEqual(output["message"]["type"], "message.create")

    def test_code_task_dry_run_uses_explicit_api_base_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as config_dir:
            result = self.run_cli(
                "code",
                "task",
                "inspect access_token=do-not-print",
                "--api-base-url",
                "http://code.example.test",
                "--access-token",
                "secret-token",
                "--dry-run",
                "--json",
                config_dir=config_dir,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertTrue(output["dry_run"])
            self.assertEqual(output["request"]["url"], "http://code.example.test/api/v1/code/tasks")
            self.assertEqual(output["request"]["headers"]["Authorization"], "Bearer <redacted>")
            self.assertNotIn("secret-token", result.stdout)
            self.assertNotIn("do-not-print", result.stdout)
            self.assertEqual(output["request"]["body"]["prompt"], "inspect access_token=<redacted>")

    def test_code_task_list_and_event_use_code_api_and_redact_payload(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), CodeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                base_url = f"http://127.0.0.1:{server.server_port}"
                created = self.run_cli(
                    "code",
                    "task",
                    "add tests for auth",
                    "--api-base-url",
                    base_url,
                    "--access-token",
                    "secret-token",
                    "--branch",
                    "workama/code-auth",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(created.returncode, 0, created.stderr)
                self.assertEqual(json.loads(created.stdout)["id"], "ctask_created")
                self.assertEqual(CodeHandler.created["branch"], "workama/code-auth")
                self.assertEqual(CodeHandler.authorization, "Bearer secret-token")
                self.assertNotIn("secret-token", created.stdout)

                listed = self.run_cli(
                    "code",
                    "list",
                    "--api-base-url",
                    base_url,
                    "--access-token",
                    "secret-token",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(listed.returncode, 0, listed.stderr)
                list_output = json.loads(listed.stdout)
                self.assertEqual([item["id"] for item in list_output["items"]], ["ctask_code", "ctask_other"])
                self.assertEqual(CodeHandler.authorization, "Bearer secret-token")
                self.assertNotIn("secret-token", listed.stdout)

                events = self.run_cli(
                    "code",
                    "event",
                    "ctask_code",
                    "--after",
                    "3",
                    "--api-base-url",
                    base_url,
                    "--access-token",
                    "secret-token",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(events.returncode, 0, events.stderr)
                event_output = json.loads(events.stdout)
                self.assertEqual(event_output["items"][0]["id"], "evt_code_1")
                self.assertEqual(event_output["items"][0]["payload"]["authorization"], "Bearer <redacted>")
                self.assertEqual(CodeHandler.event_after, "3&limit=500")
                self.assertNotIn("event-secret", events.stdout)
        finally:
            server.shutdown()
            server.server_close()

    def test_password_login_completes_mfa_challenge_without_printing_secrets(self):
        MfaHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), MfaHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                result = self.run_cli(
                    "auth",
                    "login",
                    "--email",
                    "mfa@example.test",
                    "--password",
                    "password-value",
                    "--mfa-code",
                    "123456",
                    "--platform-url",
                    f"http://127.0.0.1:{server.server_port}",
                    "--json",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([item["path"] for item in MfaHandler.requests], [
                    "/api/v1/auth/login",
                    "/api/v1/auth/mfa/challenge",
                ])
                self.assertEqual(MfaHandler.requests[1]["body"], {"ticket": "mfa-ticket-secret", "code": "123456"})
                self.assertNotIn("access-token-secret", result.stdout)
                self.assertNotIn("refresh-token-secret", result.stdout)
                self.assertNotIn("123456", result.stdout)
        finally:
            server.shutdown()
            server.server_close()

    def test_password_login_requires_mfa_code_in_non_interactive_mode(self):
        MfaHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), MfaHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as config_dir:
                result = self.run_cli(
                    "auth",
                    "login",
                    "--email",
                    "mfa@example.test",
                    "--password",
                    "password-value",
                    "--platform-url",
                    f"http://127.0.0.1:{server.server_port}",
                    config_dir=config_dir,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("MFA is required; provide --mfa-code", result.stderr)
                self.assertEqual([item["path"] for item in MfaHandler.requests], ["/api/v1/auth/login"])
        finally:
            server.shutdown()
            server.server_close()


# ===========================================================================
# WorkAMA CLI v2 tests — exercises the resource-oriented workama_cli package.
#
# These tests mock WorkamaClient methods via unittest.mock.patch so no real
# HTTP traffic is generated. They complement the original CliTests suite which
# covers the gateway-focused workama package above.
# ===========================================================================


class WorkamaCliV2Tests(unittest.TestCase):
    """Tests for apps/cli/workama_cli — the v2 resource CLI."""

    @classmethod
    def setUpClass(cls):
        # Ensure apps/cli/ is importable so ``workama_cli`` resolves.
        cli_root = str(Path(__file__).resolve().parents[1])
        if cli_root not in sys.path:
            sys.path.insert(0, cli_root)
        # Import the v2 package lazily so a missing module surfaces as a clear
        # error rather than a collection-time import failure for the whole file.
        from workama_cli.cli import main as v2_main  # type: ignore
        from workama_cli.client import (  # type: ignore
            ApiError,
            NetworkError,
            WorkamaClient,
        )
        from workama_cli.config import Config as V2Config  # type: ignore
        cls.v2_main = staticmethod(v2_main)
        cls.WorkamaClient = WorkamaClient
        cls.ApiError = ApiError
        cls.NetworkError = NetworkError
        cls.V2Config = V2Config

    # -- helpers -----------------------------------------------------------
    def _run(self, *argv, config_dir):
        """Invoke the v2 CLI capturing stdout/stderr and returning (rc, out, err)."""
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.v2_main(list(argv) + ["--config-dir", config_dir])
        return rc, out.getvalue(), err.getvalue()

    def _run_json(self, *argv, config_dir):
        rc, out, err = self._run(*argv, "--json", config_dir=config_dir)
        self.assertEqual(rc, 0, err)
        return json.loads(out), err

    # -- login (2) ---------------------------------------------------------
    def test_v2_login_success_saves_token(self):
        login_response = {
            "access_token": "tok-abc",
            "token_type": "bearer",
            "user": {"id": "u1", "email": "tester@workama.example.com", "workspace_id": "ws-1"},
        }
        with patch.object(self.WorkamaClient, "login", return_value=login_response):
            with tempfile.TemporaryDirectory() as d:
                rc, out, err = self._run(
                    "login",
                    "--email",
                    "tester@workama.example.com",
                    "--password",
                    "pw",
                    "--url",
                    "http://test.local",
                    config_dir=d,
                )
                self.assertEqual(rc, 0, err)
                self.assertIn("Logged in as tester@workama.example.com", out)
                # Token saved to credentials file.
                creds = json.loads((Path(d) / "credentials").read_text(encoding="utf-8"))
                self.assertEqual(creds["token"], "tok-abc")
                self.assertEqual(creds["base_url"], "http://test.local")
                self.assertEqual(creds["workspace_id"], "ws-1")
                # Raw token is never printed in human-readable mode.
                self.assertNotIn("tok-abc", out)

    def test_v2_login_failure_returns_nonzero(self):
        with patch.object(self.WorkamaClient, "login", side_effect=self.ApiError(401, "HTTP 401: bad credentials", {"detail": "bad credentials"})):
            with tempfile.TemporaryDirectory() as d:
                rc, out, err = self._run(
                    "login",
                    "--email",
                    "x@example.com",
                    "--password",
                    "wrong",
                    "--url",
                    "http://test.local",
                    config_dir=d,
                )
                self.assertEqual(rc, 1)
                self.assertIn("API error", err)
                self.assertNotIn("Logged in", out)

    # -- whoami (2) --------------------------------------------------------
    def test_v2_whoami_success(self):
        me = {"id": "u1", "email": "tester@workama.example.com", "display_name": "Tester", "workspace_id": "ws-1"}
        with patch.object(self.WorkamaClient, "me", return_value=me):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json(
                    "whoami",
                    "--api-token",
                    "tok",
                    "--api-url",
                    "http://test.local",
                    config_dir=d,
                )
                self.assertEqual(payload["email"], "tester@workama.example.com")
                self.assertEqual(payload["workspace_id"], "ws-1")

    def test_v2_whoami_not_logged_in(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, err = self._run("whoami", config_dir=d)
            self.assertEqual(rc, 1)
            self.assertIn("Not logged in", err)

    # -- workspaces (2) ----------------------------------------------------
    def test_v2_workspaces_list(self):
        resp = {"items": [{"id": "ws-1", "name": "Acme", "slug": "acme", "status": "active"}]}
        with patch.object(self.WorkamaClient, "list_workspaces", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("workspaces", "list", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["items"][0]["id"], "ws-1")

    def test_v2_workspaces_create(self):
        resp = {"id": "ws-2", "name": "New", "slug": "new", "status": "active"}
        with patch.object(self.WorkamaClient, "create_workspace", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("workspaces", "create", "New", "--slug", "new", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["id"], "ws-2")

    # -- assistants (3) ----------------------------------------------------
    def test_v2_assistants_list(self):
        resp = {"items": [{"id": "a-1", "name": "Helper", "model": "gpt-4", "status": "active"}]}
        with patch.object(self.WorkamaClient, "list_assistants", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("assistants", "list", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["items"][0]["id"], "a-1")

    def test_v2_assistants_create(self):
        resp = {"id": "a-2", "name": "Bot", "model": "workama-chat", "status": "active"}
        with patch.object(self.WorkamaClient, "create_assistant", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("assistants", "create", "Bot", "--model", "workama-chat", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["id"], "a-2")

    def test_v2_assistants_run(self):
        resp = {"id": "run-1", "output": "Hello!", "status": "succeeded"}
        with patch.object(self.WorkamaClient, "run_assistant", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("assistants", "run", "a-1", "--message", "hi", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["output"], "Hello!")

    # -- workflows (2) -----------------------------------------------------
    def test_v2_workflows_list(self):
        resp = {"items": [{"id": "wf-1", "name": "ETL", "status": "published", "version": 3}]}
        with patch.object(self.WorkamaClient, "list_workflows", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("workflows", "list", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["items"][0]["id"], "wf-1")

    def test_v2_workflows_run(self):
        resp = {"id": "run-9", "status": "queued"}
        with patch.object(self.WorkamaClient, "run_workflow", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("workflows", "run", "wf-1", "--input", '{"q":"a"}', "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["status"], "queued")

    # -- knowledge bases (4) ----------------------------------------------
    def test_v2_knowledge_bases_list(self):
        resp = {"items": [{"id": "kb-1", "name": "Docs", "status": "ready", "document_count": 5}]}
        with patch.object(self.WorkamaClient, "list_knowledge_bases", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("knowledge-bases", "list", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["items"][0]["id"], "kb-1")

    def test_v2_knowledge_bases_create(self):
        resp = {"id": "kb-2", "name": "New KB", "status": "ready"}
        with patch.object(self.WorkamaClient, "create_knowledge_base", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("knowledge-bases", "create", "New KB", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["id"], "kb-2")

    def test_v2_knowledge_bases_upload(self):
        resp = {"id": "doc-1", "status": "ready", "filename": "notes.txt"}
        with patch.object(self.WorkamaClient, "upload_document", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                # Create a temporary file to upload.
                f = Path(d) / "notes.txt"
                f.write_text("hello world", encoding="utf-8")
                payload, _ = self._run_json("knowledge-bases", "upload", "kb-1", str(f), "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["id"], "doc-1")

    def test_v2_knowledge_bases_query(self):
        resp = {"results": [{"document_id": "doc-1", "content": "answer", "score": 0.9}]}
        with patch.object(self.WorkamaClient, "rag_query", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("knowledge-bases", "query", "kb-1", "--query", "what?", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["results"][0]["score"], 0.9)

    # -- devices (2) -------------------------------------------------------
    def test_v2_devices_list(self):
        resp = {"items": [{"id": "dev-1", "name": "Laptop", "type": "desktop", "status": "online", "last_seen": "2026-07-27"}]}
        with patch.object(self.WorkamaClient, "list_devices", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("devices", "list", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["items"][0]["id"], "dev-1")

    def test_v2_devices_register(self):
        resp = {"id": "dev-2", "name": "Server", "type": "server", "status": "online"}
        with patch.object(self.WorkamaClient, "register_device", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("devices", "register", "Server", "--type", "server", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["id"], "dev-2")

    # -- billing (2) -------------------------------------------------------
    def test_v2_billing_plans(self):
        resp = {"items": [{"id": "plan-1", "name": "Free", "price": "0", "currency": "USD", "interval": "month"}]}
        with patch.object(self.WorkamaClient, "list_billing_plans", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("billing", "plans", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["items"][0]["id"], "plan-1")

    def test_v2_billing_usage(self):
        resp = {"items": [{"id": "u-1", "metric": "tokens", "quantity": 1000, "period": "2026-07"}]}
        with patch.object(self.WorkamaClient, "list_billing_usage", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("billing", "usage", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["items"][0]["metric"], "tokens")

    # -- free providers (2) -----------------------------------------------
    def test_v2_free_providers_list(self):
        resp = {"items": [{"key": "siliconflow", "name": "SiliconFlow", "enabled": True, "category": "llm"}]}
        with patch.object(self.WorkamaClient, "list_free_providers", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("free-providers", "list", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["items"][0]["key"], "siliconflow")

    def test_v2_free_providers_enable(self):
        resp = {"key": "siliconflow", "enabled": True}
        with patch.object(self.WorkamaClient, "enable_free_provider", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("free-providers", "enable", "siliconflow", "--api-token", "tok", config_dir=d)
                self.assertTrue(payload["enabled"])

    # -- config read/write (2) --------------------------------------------
    def test_v2_config_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.V2Config(config_dir=Path(d))
            cfg.save(base_url="http://cfg.local", token="tok-cfg", workspace_id="ws-cfg")
            self.assertEqual(cfg.base_url, "http://cfg.local")
            self.assertEqual(cfg.token, "tok-cfg")
            self.assertEqual(cfg.workspace_id, "ws-cfg")
            # Env vars override file.
            with patch.dict(os.environ, {"WORKAMA_API_URL": "http://env.local"}, clear=False):
                self.assertEqual(cfg.base_url, "http://env.local")

    def test_v2_config_env_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.V2Config(config_dir=Path(d))
            cfg.save(token="file-token")
            with patch.dict(os.environ, {"WORKAMA_API_TOKEN": "env-token"}, clear=False):
                self.assertEqual(cfg.token, "env-token")
            # Without env, falls back to file.
            self.assertEqual(cfg.token, "file-token")

    # -- error handling (2) -----------------------------------------------
    def test_v2_api_error_handled_gracefully(self):
        with patch.object(self.WorkamaClient, "list_workspaces", side_effect=self.ApiError(500, "HTTP 500: boom", {"detail": "boom"})):
            with tempfile.TemporaryDirectory() as d:
                rc, out, err = self._run("workspaces", "list", "--api-token", "tok", config_dir=d)
                self.assertEqual(rc, 1)
                self.assertIn("API error", err)

    def test_v2_network_error_handled_gracefully(self):
        with patch.object(self.WorkamaClient, "list_workspaces", side_effect=self.NetworkError("connection refused")):
            with tempfile.TemporaryDirectory() as d:
                rc, out, err = self._run("workspaces", "list", "--api-token", "tok", config_dir=d)
                self.assertEqual(rc, 1)
                self.assertIn("network error", err)

    # -- bonus: version + mcp + audit-logs (3) ----------------------------
    def test_v2_version(self):
        with tempfile.TemporaryDirectory() as d:
            payload, _ = self._run_json("version", config_dir=d)
            self.assertEqual(payload["name"], "workama")
            self.assertIn("version", payload)

    def test_v2_mcp_tools_list(self):
        resp = {"items": [{"id": "t-1", "name": "echo", "description": "echoes", "enabled": True}]}
        with patch.object(self.WorkamaClient, "list_mcp_tools", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("mcp", "tools", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["items"][0]["name"], "echo")

    def test_v2_audit_logs_list(self):
        resp = {"items": [{"id": "ev-1", "action": "login", "actor_id": "u-1", "created_at": "2026-07-27T10:00:00Z"}]}
        with patch.object(self.WorkamaClient, "list_audit_logs", return_value=resp):
            with tempfile.TemporaryDirectory() as d:
                payload, _ = self._run_json("audit-logs", "list", "--api-token", "tok", config_dir=d)
                self.assertEqual(payload["items"][0]["action"], "login")


if __name__ == "__main__":
    unittest.main()

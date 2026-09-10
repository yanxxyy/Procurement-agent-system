from __future__ import annotations

import json
import os
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main as main_module
from main import ConversationManager, ModelProviderConfigRequest, ModelProviderManager, OrderApplicationManager, USERS, app


class LangfuseStubHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802
        self.__class__.requests.append(("GET", self.path))
        if self.path == "/api/public/projects":
            body = {
                "data": [
                    {
                        "id": "project-test-01",
                        "name": "Harness Integration Test",
                        "metadata": {},
                        "organization": {"id": "org-test", "name": "Test Org"},
                    }
                ]
            }
            self._json(200, body)
            return
        if self.path.startswith("/api/public/health"):
            self._json(200, {"status": "OK"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        self.__class__.requests.append(("POST", self.path))
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        if self.path.endswith("/v1/traces"):
            self._json(200, {})
            return
        self._json(404, {"error": "not found"})

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args) -> None:
        return


class LangfuseBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        LangfuseStubHandler.requests.clear()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), LangfuseStubHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_connect_trace_status_and_disconnect(self) -> None:
        host, port = self.server.server_address
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "Admin@2026"},
            )
            self.assertEqual(login.status_code, 200, login.text)
            response = client.post(
                "/api/langfuse/connect",
                json={
                    "base_url": f"http://{host}:{port}",
                    "public_key": "pk-lf-integration-test",
                    "secret_key": "sk-lf-integration-test",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            connected = response.json()
            self.assertTrue(connected["connected"])
            self.assertEqual(connected["project_name"], "Harness Integration Test")
            self.assertTrue(connected["trace_id"])
            self.assertTrue(connected["trace_url"])

            status = client.get("/api/langfuse/status").json()
            self.assertTrue(status["connected"])
            self.assertNotIn("secret_key", status)

            trace_response = client.post("/api/langfuse/test-trace")
            self.assertEqual(trace_response.status_code, 200, trace_response.text)
            self.assertTrue(trace_response.json()["trace_id"])

            stream = client.get("/api/runs/demo/events?mode=tail")
            self.assertEqual(stream.status_code, 200, stream.text)
            self.assertIn('"connected": true', stream.text)
            self.assertIn("event: done", stream.text)

            approval = client.post("/api/runs/demo/approve")
            self.assertEqual(approval.status_code, 200, approval.text)
            self.assertTrue(approval.json()["langfuse"]["connected"])

            disconnected = client.delete("/api/langfuse/connection")
            self.assertEqual(disconnected.status_code, 200)
            self.assertFalse(client.get("/api/langfuse/status").json()["connected"])

        paths = [path for method, path in LangfuseStubHandler.requests if method == "POST"]
        self.assertTrue(any(path.endswith("/v1/traces") for path in paths), paths)


class AuthorizationAndChatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(self.temporary_directory.name) / "authorization.db"
        self.patchers = [
            patch.object(main_module, "order_applications", OrderApplicationManager(database)),
            patch.object(main_module, "chat_conversations", ConversationManager(database)),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def test_employee_and_admin_permissions(self) -> None:
        with TestClient(app) as client:
            anonymous = client.get("/", follow_redirects=False)
            self.assertEqual(anonymous.status_code, 303)
            self.assertTrue(anonymous.headers["location"].startswith("/login"))

            employee_login = client.post(
                "/api/auth/login",
                json={"username": "employee", "password": "Employee@2026"},
            )
            self.assertEqual(employee_login.status_code, 200, employee_login.text)
            self.assertEqual(employee_login.json()["role"], "employee")

            employee_snapshot = client.get("/api/runs/demo")
            self.assertEqual(employee_snapshot.status_code, 200)
            self.assertFalse(employee_snapshot.json()["permissions"]["can_approve_order"])
            self.assertTrue(all(not task["tools"] for task in employee_snapshot.json()["tasks"]))

            forbidden_approval = client.post("/api/runs/demo/approve")
            self.assertEqual(forbidden_approval.status_code, 403)
            forbidden_langfuse = client.get("/api/langfuse/status")
            self.assertEqual(forbidden_langfuse.status_code, 403)
            model_catalog = client.get("/api/models/catalog")
            self.assertEqual(model_catalog.status_code, 200)
            self.assertTrue(any(provider["id"] == "local" for provider in model_catalog.json()["providers"]))
            forbidden_model_config = client.put(
                "/api/models/providers/deepseek",
                json={"base_url": "https://api.deepseek.com", "api_key": "sk-test", "enabled_models": ["deepseek-v4-pro"]},
            )
            self.assertEqual(forbidden_model_config.status_code, 403)
            forbidden_model_page = client.get("/models", follow_redirects=False)
            self.assertEqual(forbidden_model_page.status_code, 303)

            application = client.post(
                "/api/orders/applications",
                json={
                    "item": "M8×30 A2-70 内六角螺栓",
                    "quantity": 12000,
                    "required_date": "2026-09-12",
                    "reason": "生产线补充库存",
                },
            )
            self.assertEqual(application.status_code, 200, application.text)
            self.assertTrue(application.json()["created"])
            application_id = application.json()["application"]["id"]
            employee_applications = client.get("/api/orders/applications")
            self.assertEqual(employee_applications.status_code, 200)
            employee_items = employee_applications.json()["applications"]
            self.assertTrue(any(item["id"] == application_id for item in employee_items))
            self.assertTrue(all(item["applicant_username"] == "employee" for item in employee_items))
            employee_detail = client.get(f"/api/orders/applications/{application_id}")
            self.assertEqual(employee_detail.status_code, 200)
            self.assertEqual(employee_detail.json()["application"]["events"][0]["type"], "submitted")
            employee_approval = client.post(f"/api/orders/applications/{application_id}/approve")
            self.assertEqual(employee_approval.status_code, 403)

            chat = client.post(
                "/api/chat/stream",
                json={"message": "采购 12000 件 M8 螺栓，需要准备什么？"},
            )
            self.assertEqual(chat.status_code, 200, chat.text)
            self.assertIn("event: delta", chat.text)
            self.assertIn("event: done", chat.text)
            self.assertNotIn("event: tool", chat.text)
            self.assertNotIn('"trace_url": "http', chat.text)

            client.post("/api/auth/logout")
            admin_login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "Admin@2026"},
            )
            self.assertEqual(admin_login.status_code, 200, admin_login.text)
            self.assertEqual(client.get("/models").status_code, 200)
            approval = client.post("/api/runs/demo/approve")
            self.assertEqual(approval.status_code, 200, approval.text)
            self.assertEqual(approval.json()["approved_by"], "系统管理员")
            admin_applications = client.get("/api/orders/applications")
            self.assertEqual(admin_applications.status_code, 200)
            self.assertTrue(any(item["id"] == application_id for item in admin_applications.json()["applications"]))
            application_approval = client.post(f"/api/orders/applications/{application_id}/approve")
            self.assertEqual(application_approval.status_code, 200, application_approval.text)
            approved_application = application_approval.json()["application"]
            self.assertEqual(approved_application["status"], "approved")
            self.assertEqual(approved_application["approved_by"], "系统管理员")
            self.assertTrue(approved_application["order_no"].startswith("PO-"))
            approval_events = approved_application["events"]
            self.assertEqual([event["type"] for event in approval_events], ["submitted", "approved"])
            second_approval = client.post(f"/api/orders/applications/{application_id}/approve")
            self.assertEqual(second_approval.status_code, 200)
            self.assertEqual(len(second_approval.json()["application"]["events"]), 2)


class ModelProviderPersistenceTest(unittest.TestCase):
    def test_vendor_key_is_encrypted_reused_and_not_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            root = Path(directory)
            manager = ModelProviderManager(root / "models.db", root / ".key")
            payload = ModelProviderConfigRequest(
                base_url="https://api.openai.com/v1",
                api_key="sk-model-secret-value",
                enabled_models=["gpt-6-astra", "gpt-5.6-terra"],
                enabled=True,
            )
            saved = manager.save("openai", payload, USERS["admin"])
            self.assertTrue(saved["configured"])
            self.assertNotIn("sk-model-secret-value", json.dumps(saved))
            runtime = manager.runtime("openai", "gpt-5.6-terra")
            self.assertEqual(runtime["api_key"], "sk-model-secret-value")

            updated = ModelProviderConfigRequest(
                base_url="https://api.openai.com/v1",
                api_key=None,
                enabled_models=["gpt-5.6-luna"],
                enabled=True,
            )
            manager.save("openai", updated, USERS["admin"])
            reused = manager.runtime("openai", "gpt-5.6-luna")
            self.assertEqual(reused["api_key"], "sk-model-secret-value")
            reloaded_manager = ModelProviderManager(root / "models.db", root / ".key")
            after_restart = reloaded_manager.runtime("openai", "gpt-5.6-luna")
            self.assertEqual(after_restart["api_key"], "sk-model-secret-value")
            public_catalog = manager.catalog(False)
            self.assertNotIn("api_key_masked", next(item for item in public_catalog if item["id"] == "openai"))

    def test_selected_vendor_and_model_drive_chat_stream(self) -> None:
        class ModelStubHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
                if self.path.endswith("/responses"):
                    body = (
                        'data: {"type":"response.output_text.delta","delta":"OpenAI 模型已响应"}\n\n'
                        'data: {"type":"response.completed","response":{"usage":{"input_tokens":11,"output_tokens":4,"total_tokens":15}}}\n\n'
                        "data: [DONE]\n\n"
                    ).encode()
                else:
                    body = (
                        'data: {"choices":[{"delta":{"content":"千问模型已响应"}}]}\n\n'
                        'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":4,"total_tokens":14}}\n\n'
                        "data: [DONE]\n\n"
                    ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), ModelStubHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manager = ModelProviderManager(root / "models.db", root / ".key")
                manager.save(
                    "qwen",
                    ModelProviderConfigRequest(
                        base_url=f"http://{host}:{port}",
                        api_key="sk-qwen-test",
                        enabled_models=["qwen3.7-plus"],
                        enabled=True,
                    ),
                    USERS["admin"],
                )
                manager.save(
                    "openai",
                    ModelProviderConfigRequest(
                        base_url=f"http://{host}:{port}",
                        api_key="sk-openai-test",
                        enabled_models=["gpt-5.6-terra"],
                        enabled=True,
                    ),
                    USERS["admin"],
                )
                conversation_manager = ConversationManager(root / "conversations.db")
                with patch.object(main_module, "model_providers", manager), patch.object(main_module, "chat_conversations", conversation_manager), TestClient(app) as client:
                    client.post("/api/auth/login", json={"username": "employee", "password": "Employee@2026"})
                    response = client.post(
                        "/api/chat/stream",
                        json={
                            "message": "测试模型选择",
                            "provider_id": "qwen",
                            "model_id": "qwen3.7-plus",
                        },
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn('"provider_id": "qwen"', response.text)
                    self.assertIn('"model": "qwen3.7-plus"', response.text)
                    self.assertIn("千问模型已响应", response.text)
                    self.assertNotIn("event: tool", response.text)
                    openai_response = client.post(
                        "/api/chat/stream",
                        json={
                            "message": "测试另一个厂商",
                            "provider_id": "openai",
                            "model_id": "gpt-5.6-terra",
                        },
                    )
                    self.assertEqual(openai_response.status_code, 200, openai_response.text)
                    self.assertIn('"provider_id": "openai"', openai_response.text)
                    self.assertIn("OpenAI 模型已响应", openai_response.text)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

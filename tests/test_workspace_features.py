from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import main as main_module
from main import ConversationManager, SandboxManager, SkillManager, app


class PersistentConversationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.conversation_patcher = patch.object(
            main_module,
            "chat_conversations",
            ConversationManager(Path(self.temporary_directory.name) / "api-conversations.db"),
        )
        self.conversation_patcher.start()

    def tearDown(self) -> None:
        self.conversation_patcher.stop()
        self.temporary_directory.cleanup()

    def test_conversations_survive_manager_reload_and_are_owner_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workspace.db"
            manager = ConversationManager(database)
            conversation = manager.create("employee")
            manager.append_exchange(
                conversation["id"],
                "employee",
                "采购一批 M8 螺栓",
                "请补充数量和交期。",
                "local",
                "local-procurement-engine",
            )

            reloaded = ConversationManager(database)
            detail = reloaded.get(conversation["id"], "employee")
            self.assertIsNotNone(detail)
            self.assertEqual(detail["title"], "采购一批 M8 螺栓")
            self.assertEqual(len(detail["messages"]), 2)
            self.assertIsNone(reloaded.get(conversation["id"], "li.ming"))
            self.assertFalse(reloaded.delete(conversation["id"], "li.ming"))
            self.assertTrue(reloaded.delete(conversation["id"], "employee"))
            self.assertIsNone(reloaded.get(conversation["id"], "employee"))

    def test_conversation_routes_support_new_history_resume_and_isolation(self) -> None:
        with TestClient(app) as client:
            client.post("/api/auth/login", json={"username": "employee", "password": "Employee@2026"})
            first = client.post("/api/chat/conversations")
            self.assertEqual(first.status_code, 200, first.text)
            first_id = first.json()["conversation"]["id"]
            stream = client.post(
                "/api/chat/stream",
                json={
                    "conversation_id": first_id,
                    "message": "比较三家供应商报价",
                    "provider_id": "local",
                    "model_id": "local-procurement-engine",
                },
            )
            self.assertEqual(stream.status_code, 200, stream.text)
            second_id = client.post("/api/chat/conversations").json()["conversation"]["id"]
            self.assertNotEqual(first_id, second_id)
            history = client.get("/api/chat/conversations").json()["conversations"]
            self.assertTrue(any(item["id"] == first_id and item["message_count"] >= 2 for item in history))
            self.assertEqual(len(client.get(f"/api/chat/conversations/{first_id}").json()["conversation"]["messages"]), 2)

            deleted = client.delete(f"/api/chat/conversations/{second_id}")
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertTrue(deleted.json()["deleted"])
            self.assertEqual(client.get(f"/api/chat/conversations/{second_id}").status_code, 404)

            client.post("/api/auth/logout")
            client.post("/api/auth/login", json={"username": "li.ming", "password": "User@2026"})
            self.assertEqual(client.get(f"/api/chat/conversations/{first_id}").status_code, 404)
            self.assertEqual(client.delete(f"/api/chat/conversations/{first_id}").status_code, 404)
            client.post("/api/auth/logout")
            client.post("/api/auth/login", json={"username": "employee", "password": "Employee@2026"})
            self.assertEqual(client.delete(f"/api/chat/conversations/{first_id}").status_code, 200)


class TechnicalControlPlaneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(self.temporary_directory.name) / "api-control.db"
        self.patchers = [
            patch.object(main_module, "skills", SkillManager(database)),
            patch.object(main_module, "sandboxes", SandboxManager(database)),
            patch.object(main_module, "chat_conversations", ConversationManager(database)),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def test_skill_controls_and_sandbox_hot_swap_persist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "control.db"
            skill_manager = SkillManager(database)
            self.assertEqual(len(skill_manager.list()), 5)
            disabled = skill_manager.toggle("web-crawler", False, "admin")
            self.assertFalse(disabled["enabled"])
            validated = skill_manager.validate("web-crawler", "admin")
            self.assertEqual(validated["checks"]["sandbox_policy"], "passed")
            self.assertFalse(SkillManager(database).get("web-crawler")["enabled"])

            sandbox_manager = SandboxManager(database)
            before = sandbox_manager.get()
            after = sandbox_manager.hot_swap(before["handle"], "admin")
            self.assertEqual(after["handle"], before["handle"])
            self.assertNotEqual(after["instance_id"], before["instance_id"])
            self.assertEqual(after["generation"], before["generation"] + 1)
            self.assertEqual(SandboxManager(database).get()["instance_id"], after["instance_id"])

    def test_admin_pages_and_apis_are_permission_protected(self) -> None:
        with TestClient(app) as client:
            client.post("/api/auth/login", json={"username": "employee", "password": "Employee@2026"})
            self.assertEqual(client.get("/api/skills").status_code, 403)
            self.assertEqual(client.get("/api/sandboxes").status_code, 403)
            self.assertEqual(client.get("/skills", follow_redirects=False).status_code, 303)
            self.assertEqual(client.get("/sandboxes", follow_redirects=False).status_code, 303)
            self.assertEqual(client.get("/runs").status_code, 200)
            self.assertEqual(client.get("/api/overview").status_code, 200)

            client.post("/api/auth/logout")
            client.post("/api/auth/login", json={"username": "admin", "password": "Admin@2026"})
            self.assertEqual(client.get("/skills").status_code, 200)
            self.assertEqual(client.get("/sandboxes").status_code, 200)
            self.assertEqual(client.post("/api/skills/report-generation/validate").status_code, 200)
            sandbox = client.get("/api/sandboxes").json()["sandboxes"][0]
            self.assertEqual(client.post(f"/api/sandboxes/{sandbox['handle']}/health-check").status_code, 200)

    def test_observability_failure_never_breaks_business_streams(self) -> None:
        class BrokenLangfuseClient:
            def create_trace_id(self, **_kwargs):
                raise RuntimeError("observability unavailable")

        broken_connection = SimpleNamespace(
            client=BrokenLangfuseClient(),
            last_trace_id=None,
            last_trace_url=None,
        )
        with patch.object(main_module.langfuse_connections, "get", return_value=broken_connection), TestClient(app) as client:
            client.post("/api/auth/login", json={"username": "admin", "password": "Admin@2026"})
            chat = client.post(
                "/api/chat/stream",
                json={
                    "message": "检查可观测性故障降级",
                    "provider_id": "local",
                    "model_id": "local-procurement-engine",
                },
            )
            self.assertEqual(chat.status_code, 200, chat.text)
            self.assertIn("event: done", chat.text)
            self.assertIn('"connected": false', chat.text)

            run = client.get("/api/runs/demo/events?mode=tail")
            self.assertEqual(run.status_code, 200, run.text)
            self.assertIn("event: done", run.text)
            for conversation in client.get("/api/chat/conversations").json()["conversations"]:
                if conversation["title"] == "检查可观测性故障降级":
                    client.delete(f"/api/chat/conversations/{conversation['id']}")


if __name__ == "__main__":
    unittest.main()

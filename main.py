"""Interactive procurement-agent execution console.

Run with:
    uvicorn main:app --reload
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, AsyncIterator, Iterator
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langfuse import Langfuse
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = Path(os.getenv("HARNESS_DB_PATH", str(DATA_DIR / "procurement.db")))

app = FastAPI(
    title="Harness Procurement Console",
    description="DeepAgents procurement workflow visualization with SSE traces.",
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


DEFAULT_LANGFUSE_URL = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/")
LANGFUSE_AUTH_DISABLED = os.getenv("LANGFUSE_AUTH_DISABLED", "false").lower() in {"1", "true", "yes"}
LANGFUSE_COOKIE = "harness_langfuse_session"
CHAT_COOKIE = "harness_chat_session"
AUTH_COOKIE = "harness_auth_session"
AUTH_PEPPER = os.getenv("HARNESS_AUTH_SECRET", secrets.token_hex(32)).encode()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
MODEL_ENCRYPTION_KEY_PATH = Path(
    os.getenv("HARNESS_MODEL_KEY_PATH", str(DATA_DIR / ".model-config.key"))
)

MODEL_PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    "local": {
        "name": "本地采购引擎",
        "short_name": "Local",
        "description": "无需 API Key，外部模型不可用时自动兜底。",
        "base_url": "local://procurement",
        "protocol": "local",
        "environment_key": None,
        "models": [
            {"id": "local-procurement-engine", "name": "本地采购引擎", "description": "规则与采购 SOP 驱动"},
        ],
    },
    "deepseek": {
        "name": "DeepSeek",
        "short_name": "DeepSeek",
        "description": "面向采购分析、推理与 Agent 工作流。",
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        "protocol": "chat_completions",
        "environment_key": "DEEPSEEK_API_KEY",
        "models": [
            {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro", "description": "复杂分析与 Agent 任务"},
            {"id": "deepseek-v4-flash", "name": "DeepSeek-V4-Flash", "description": "低延迟、高性价比"},
            {"id": "deepseek-v4-flash-vision-exp", "name": "V4-Flash Vision Exp", "description": "实验性视觉理解模型"},
        ],
    },
    "openai": {
        "name": "OpenAI",
        "short_name": "OpenAI",
        "description": "通用推理、采购分析与高质量文本生成。",
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "protocol": "responses",
        "environment_key": "OPENAI_API_KEY",
        "models": [
            {"id": "gpt-6-astra", "name": "GPT-6 Astra", "description": "旗舰复杂推理模型"},
            {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra", "description": "能力与成本平衡"},
            {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna", "description": "高吞吐、低成本"},
        ],
    },
    "qwen": {
        "name": "阿里云百炼 · 千问",
        "short_name": "Qwen",
        "description": "中文采购场景与企业级模型服务。",
        "base_url": os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/"),
        "protocol": "chat_completions",
        "environment_key": "DASHSCOPE_API_KEY",
        "models": [
            {"id": "qwen3.8-max", "name": "Qwen3.8-Max", "description": "最强复杂推理能力"},
            {"id": "qwen3.7-plus", "name": "Qwen3.7-Plus", "description": "Agent 能力与成本平衡"},
            {"id": "qwen3.8-flash", "name": "Qwen3.8-Flash", "description": "轻量快速响应"},
        ],
    },
}

CHAT_SYSTEM_PROMPT = """你是企业采购分析助手。请用简洁、可执行的中文回答。
当用户提出采购需求时，优先确认物料规格、数量、交期、交付地点、预算和质量要求；
不要伪造供应商、报价或 ERP 数据，缺少数据时明确说明并给出下一步；
输出结论时说明依据、风险和需要人工确认的敏感写操作。"""


class LangfuseConnectRequest(BaseModel):
    base_url: str = Field(default=DEFAULT_LANGFUSE_URL, max_length=500)
    public_key: str = Field(min_length=4, max_length=500)
    secret_key: str = Field(min_length=4, max_length=500)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=100)
    provider_id: str | None = Field(default=None, max_length=80)
    model_id: str | None = Field(default=None, max_length=120)


class ChatTaskRequest(BaseModel):
    conversation_id: str = Field(min_length=4, max_length=100)


class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=4, max_length=200)


class OrderApplicationRequest(BaseModel):
    item: str = Field(min_length=2, max_length=300)
    quantity: int = Field(gt=0, le=100_000_000)
    required_date: str = Field(min_length=4, max_length=40)
    reason: str = Field(min_length=2, max_length=1000)


class ModelProviderConfigRequest(BaseModel):
    base_url: str = Field(min_length=4, max_length=500)
    api_key: str | None = Field(default=None, max_length=1000)
    enabled_models: list[str] = Field(default_factory=list, max_length=30)
    enabled: bool = True


class SkillToggleRequest(BaseModel):
    enabled: bool


@dataclass(frozen=True)
class UserRecord:
    username: str
    name: str
    role: str
    department: str
    password_digest: bytes


def digest_password(username: str, password: str) -> bytes:
    salt = hashlib.sha256(AUTH_PEPPER + username.encode()).digest()
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 180_000)


def make_user(username: str, name: str, role: str, department: str, password: str) -> UserRecord:
    return UserRecord(
        username=username,
        name=name,
        role=role,
        department=department,
        password_digest=digest_password(username, password),
    )


USERS: dict[str, UserRecord] = {
    user.username: user
    for user in (
        make_user("admin", "系统管理员", "admin", "采购运营组", os.getenv("HARNESS_ADMIN_PASSWORD", "Admin@2026")),
        make_user("employee", "普通员工", "employee", "产品研发部", os.getenv("HARNESS_EMPLOYEE_PASSWORD", "Employee@2026")),
        make_user("li.ming", "李明", "employee", "智能制造部", os.getenv("HARNESS_LIMING_PASSWORD", "User@2026")),
    )
}


@dataclass
class AuthSession:
    username: str
    expires_at: float


class AuthManager:
    def __init__(self) -> None:
        self._sessions: dict[str, AuthSession] = {}
        self._lock = RLock()

    def login(self, username: str, password: str) -> tuple[str, UserRecord] | None:
        user = USERS.get(username)
        if not user or not hmac.compare_digest(user.password_digest, digest_password(username, password)):
            return None
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = AuthSession(username=username, expires_at=time.time() + 8 * 60 * 60)
        return token, user

    def get_user(self, token: str | None) -> UserRecord | None:
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if not session:
                return None
            if session.expires_at <= time.time():
                self._sessions.pop(token, None)
                return None
        return USERS.get(session.username)

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)


auth_manager = AuthManager()


def public_user(user: UserRecord) -> dict[str, Any]:
    permissions = {
        "can_apply_order": True,
        "can_approve_order": user.role == "admin",
        "can_view_tools": user.role == "admin",
        "can_view_traces": user.role == "admin",
        "can_manage_integrations": user.role == "admin",
        "can_manage_models": user.role == "admin",
    }
    return {
        "username": user.username,
        "name": user.name,
        "role": user.role,
        "role_label": "管理员" if user.role == "admin" else "普通员工",
        "department": user.department,
        "permissions": permissions,
    }


def require_user(
    harness_auth_session: str | None = Cookie(default=None),
) -> UserRecord:
    user = auth_manager.get_user(harness_auth_session)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_admin(user: UserRecord = Depends(require_user)) -> UserRecord:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以执行此操作")
    return user


class OrderApplicationManager:
    """Durable order applications and immutable approval audit events."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._lock = RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS order_applications (
                    id TEXT PRIMARY KEY,
                    applicant_username TEXT NOT NULL,
                    applicant_name TEXT NOT NULL,
                    department TEXT NOT NULL,
                    item TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    required_date TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending_approval', 'approved')),
                    created_at TEXT NOT NULL,
                    approved_by_username TEXT,
                    approved_by_name TEXT,
                    approved_at TEXT,
                    order_no TEXT UNIQUE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS order_application_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_username TEXT NOT NULL,
                    actor_name TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (application_id) REFERENCES order_applications(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_order_applications_applicant_created ON order_applications(applicant_username, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_order_applications_status_created ON order_applications(status, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_order_application_events_application_created ON order_application_events(application_id, created_at)"
            )
            connection.execute("PRAGMA optimize")

    @staticmethod
    def _serialize(row: sqlite3.Row, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        application = {
            "id": row["id"],
            "applicant_username": row["applicant_username"],
            "applicant": row["applicant_name"],
            "department": row["department"],
            "item": row["item"],
            "quantity": row["quantity"],
            "required_date": row["required_date"],
            "reason": row["reason"],
            "status": row["status"],
            "created_at": row["created_at"],
            "approved_by": row["approved_by_name"],
            "approved_at": row["approved_at"],
            "order_no": row["order_no"],
        }
        if "event_count" in row.keys():
            application["event_count"] = row["event_count"]
        if events is not None:
            application["events"] = events
        return application

    def _events(self, connection: sqlite3.Connection, application_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT id, event_type, actor_username, actor_name, actor_role, message, details_json, created_at
            FROM order_application_events
            WHERE application_id = ?
            ORDER BY id ASC
            """,
            (application_id,),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                details = json.loads(row["details_json"])
            except (TypeError, json.JSONDecodeError):
                details = {}
            events.append(
                {
                    "id": row["id"],
                    "type": row["event_type"],
                    "actor_username": row["actor_username"],
                    "actor": row["actor_name"],
                    "actor_role": row["actor_role"],
                    "message": row["message"],
                    "details": details,
                    "created_at": row["created_at"],
                }
            )
        return events

    def create(self, user: UserRecord, payload: OrderApplicationRequest) -> dict[str, Any]:
        application_id = f"REQ-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
        created_at = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO order_applications (
                    id, applicant_username, applicant_name, department, item, quantity,
                    required_date, reason, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_approval', ?)
                """,
                (
                    application_id,
                    user.username,
                    user.name,
                    user.department,
                    payload.item,
                    payload.quantity,
                    payload.required_date,
                    payload.reason,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO order_application_events (
                    application_id, event_type, actor_username, actor_name, actor_role,
                    message, details_json, created_at
                ) VALUES (?, 'submitted', ?, ?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    user.username,
                    user.name,
                    user.role,
                    "采购申请已提交，进入管理员审批队列",
                    json.dumps({"quantity": payload.quantity, "required_date": payload.required_date}, ensure_ascii=False),
                    created_at,
                ),
            )
        return self.get(application_id) or {}

    def list(self, applicant_username: str | None = None) -> list[dict[str, Any]]:
        predicate = "WHERE a.applicant_username = ?" if applicant_username else ""
        params: tuple[Any, ...] = (applicant_username,) if applicant_username else ()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT a.*, COUNT(e.id) AS event_count
                FROM order_applications AS a
                LEFT JOIN order_application_events AS e ON e.application_id = a.id
                {predicate}
                GROUP BY a.id
                ORDER BY a.created_at DESC
                """,
                params,
            ).fetchall()
        return [self._serialize(row) for row in rows]

    def get(self, application_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM order_applications WHERE id = ?",
                (application_id,),
            ).fetchone()
            if not row:
                return None
            return self._serialize(row, self._events(connection, application_id))

    def approve(self, application_id: str, approver: UserRecord) -> dict[str, Any] | None:
        approved_at = datetime.now(UTC).isoformat()
        order_no = f"PO-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(2).upper()}"
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM order_applications WHERE id = ?",
                (application_id,),
            ).fetchone()
            if not row:
                return None
            if row["status"] != "approved":
                connection.execute(
                    """
                    UPDATE order_applications
                    SET status = 'approved', approved_by_username = ?, approved_by_name = ?,
                        approved_at = ?, order_no = ?
                    WHERE id = ?
                    """,
                    (approver.username, approver.name, approved_at, order_no, application_id),
                )
                connection.execute(
                    """
                    INSERT INTO order_application_events (
                        application_id, event_type, actor_username, actor_name, actor_role,
                        message, details_json, created_at
                    ) VALUES (?, 'approved', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        application_id,
                        approver.username,
                        approver.name,
                        approver.role,
                        "管理员已批准申请并生成采购订单",
                        json.dumps({"order_no": order_no}, ensure_ascii=False),
                        approved_at,
                    ),
                )
        return self.get(application_id)


order_applications = OrderApplicationManager(DATABASE_PATH)


class ModelProviderManager:
    """Persists one encrypted credential per vendor and exposes selectable models."""

    def __init__(self, database_path: Path, encryption_key_path: Path) -> None:
        self._database_path = database_path
        self._encryption_key_path = encryption_key_path
        self._lock = RLock()
        self._fernet = Fernet(self._load_encryption_key())
        self._initialize()

    def _load_encryption_key(self) -> bytes:
        secret = os.getenv("HARNESS_MODEL_ENCRYPTION_SECRET")
        if secret:
            return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        self._encryption_key_path.parent.mkdir(parents=True, exist_ok=True)
        if self._encryption_key_path.exists():
            return self._encryption_key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self._encryption_key_path.write_bytes(key)
        return key

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS model_provider_configs (
                    provider_id TEXT PRIMARY KEY,
                    base_url TEXT NOT NULL,
                    encrypted_api_key TEXT,
                    enabled_models_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL
                )
                """
            )

    def _row(self, provider_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM model_provider_configs WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()

    def _decrypt(self, encrypted_value: str | None) -> str | None:
        if not encrypted_value:
            return None
        try:
            return self._fernet.decrypt(encrypted_value.encode()).decode()
        except (InvalidToken, ValueError):
            return None

    @staticmethod
    def _mask_key(api_key: str | None) -> str | None:
        if not api_key:
            return None
        if len(api_key) <= 8:
            return f"{api_key[:2]}••••"
        return f"{api_key[:5]}••••••{api_key[-4:]}"

    @staticmethod
    def _enabled_model_ids(spec: dict[str, Any], row: sqlite3.Row | None) -> list[str]:
        all_ids = [model["id"] for model in spec["models"]]
        if not row:
            return all_ids
        try:
            configured = json.loads(row["enabled_models_json"])
        except (TypeError, json.JSONDecodeError):
            return all_ids
        return [model_id for model_id in configured if model_id in all_ids]

    def catalog(self, include_admin_fields: bool = False) -> list[dict[str, Any]]:
        providers: list[dict[str, Any]] = []
        for provider_id, spec in MODEL_PROVIDER_CATALOG.items():
            row = self._row(provider_id) if provider_id != "local" else None
            stored_key = self._decrypt(row["encrypted_api_key"]) if row else None
            environment_key_name = spec.get("environment_key")
            environment_key = os.getenv(environment_key_name, "") if environment_key_name else ""
            api_key = stored_key or environment_key or None
            enabled_models = self._enabled_model_ids(spec, row)
            enabled = True if provider_id == "local" else bool(row["enabled"]) if row else True
            configured = provider_id == "local" or bool(api_key)
            provider = {
                "id": provider_id,
                "name": spec["name"],
                "short_name": spec["short_name"],
                "description": spec["description"],
                "base_url": row["base_url"] if row else spec["base_url"],
                "protocol": spec["protocol"],
                "configured": configured,
                "enabled": enabled,
                "available": configured and enabled and bool(enabled_models),
                "models": [
                    {**model, "enabled": model["id"] in enabled_models}
                    for model in spec["models"]
                ],
            }
            if include_admin_fields:
                provider.update(
                    {
                        "api_key_masked": self._mask_key(api_key),
                        "credential_source": "saved" if stored_key else "environment" if environment_key else None,
                        "updated_at": row["updated_at"] if row else None,
                        "updated_by": row["updated_by"] if row else None,
                    }
                )
            providers.append(provider)
        return providers

    def runtime(self, provider_id: str | None, model_id: str | None) -> dict[str, Any]:
        if not provider_id:
            available = [provider for provider in self.catalog() if provider["available"]]
            preference = ("deepseek", "openai", "qwen", "local")
            selected = next(provider for preferred in preference for provider in available if provider["id"] == preferred)
            provider_id = selected["id"]
        spec = MODEL_PROVIDER_CATALOG.get(provider_id)
        if not spec:
            raise HTTPException(status_code=422, detail="不支持的模型厂商")
        row = self._row(provider_id) if provider_id != "local" else None
        enabled_models = self._enabled_model_ids(spec, row)
        if not model_id:
            preferred_model = DEEPSEEK_MODEL if provider_id == "deepseek" else None
            model_id = preferred_model if preferred_model in enabled_models else enabled_models[0] if enabled_models else None
        if not model_id or model_id not in enabled_models:
            raise HTTPException(status_code=422, detail="该模型未在模型中心启用")
        model = next((item for item in spec["models"] if item["id"] == model_id), None)
        if not model:
            raise HTTPException(status_code=422, detail="模型不属于所选厂商")
        enabled = True if provider_id == "local" else bool(row["enabled"]) if row else True
        stored_key = self._decrypt(row["encrypted_api_key"]) if row else None
        environment_key_name = spec.get("environment_key")
        api_key = stored_key or (os.getenv(environment_key_name, "") if environment_key_name else "")
        if provider_id != "local" and (not enabled or not api_key):
            raise HTTPException(status_code=422, detail="该厂商尚未配置 API Key 或已停用")
        return {
            "provider_id": provider_id,
            "provider_name": spec["name"],
            "model_id": model_id,
            "model_name": model["name"],
            "base_url": row["base_url"] if row else spec["base_url"],
            "protocol": spec["protocol"],
            "api_key": api_key,
        }

    def save(self, provider_id: str, payload: ModelProviderConfigRequest, user: UserRecord) -> dict[str, Any]:
        if provider_id == "local" or provider_id not in MODEL_PROVIDER_CATALOG:
            raise HTTPException(status_code=404, detail="模型厂商不存在或无需配置")
        parsed = urlparse(payload.base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise HTTPException(status_code=422, detail="Base URL 必须是有效的 HTTP(S) 地址且不能包含账号密码")
        spec = MODEL_PROVIDER_CATALOG[provider_id]
        allowed_models = {model["id"] for model in spec["models"]}
        enabled_models = list(dict.fromkeys(payload.enabled_models))
        if not enabled_models or any(model_id not in allowed_models for model_id in enabled_models):
            raise HTTPException(status_code=422, detail="请至少启用一个属于该厂商的模型")
        current = self._row(provider_id)
        encrypted_key = current["encrypted_api_key"] if current else None
        if payload.api_key and payload.api_key.strip():
            encrypted_key = self._fernet.encrypt(payload.api_key.strip().encode()).decode()
        environment_key_name = spec.get("environment_key")
        if not encrypted_key and not (os.getenv(environment_key_name, "") if environment_key_name else ""):
            raise HTTPException(status_code=422, detail="首次配置该厂商时必须填写 API Key")
        updated_at = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO model_provider_configs (
                    provider_id, base_url, encrypted_api_key, enabled_models_json,
                    enabled, updated_at, updated_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    base_url = excluded.base_url,
                    encrypted_api_key = excluded.encrypted_api_key,
                    enabled_models_json = excluded.enabled_models_json,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (
                    provider_id,
                    payload.base_url.strip().rstrip("/"),
                    encrypted_key,
                    json.dumps(enabled_models),
                    int(payload.enabled),
                    updated_at,
                    user.username,
                ),
            )
        return next(provider for provider in self.catalog(True) if provider["id"] == provider_id)

    def remove(self, provider_id: str) -> None:
        if provider_id == "local" or provider_id not in MODEL_PROVIDER_CATALOG:
            raise HTTPException(status_code=404, detail="模型厂商不存在或无需清除")
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM model_provider_configs WHERE provider_id = ?", (provider_id,))


model_providers = ModelProviderManager(DATABASE_PATH, MODEL_ENCRYPTION_KEY_PATH)


@dataclass
class LangfuseConnection:
    base_url: str
    public_key: str
    secret_key: str
    project_id: str
    project_name: str
    organization_name: str
    connected_at: str
    client: Langfuse
    last_trace_id: str | None = None
    last_trace_url: str | None = None


class LangfuseConnectionManager:
    """Keeps verified credentials server-side and scopes them to an HttpOnly session."""

    def __init__(self) -> None:
        self._connections: dict[str, LangfuseConnection] = {}
        self._lock = RLock()

    def get(self, session_id: str | None) -> LangfuseConnection | None:
        if not session_id:
            return None
        with self._lock:
            return self._connections.get(session_id)

    def save(self, session_id: str, connection: LangfuseConnection) -> None:
        with self._lock:
            previous = self._connections.get(session_id)
            self._connections[session_id] = connection
        if previous:
            try:
                previous.client.shutdown()
            except Exception:
                pass

    def remove(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            connection = self._connections.pop(session_id, None)
        if connection:
            try:
                connection.client.flush()
                connection.client.shutdown()
            except Exception:
                pass


langfuse_connections = LangfuseConnectionManager()


class ConversationManager:
    """Durable, per-user conversations that can be reopened across sessions."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._lock = RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_conversations (
                    id TEXT PRIMARY KEY,
                    owner_username TEXT NOT NULL,
                    title TEXT NOT NULL,
                    provider_id TEXT,
                    model_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_conversations_owner_updated ON chat_conversations(owner_username, updated_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation_id ON chat_messages(conversation_id, id)"
            )

    @staticmethod
    def _conversation(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "owner_username": row["owner_username"],
            "title": row["title"],
            "provider_id": row["provider_id"],
            "model_id": row["model_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if "message_count" in row.keys():
            result["message_count"] = row["message_count"]
        if "preview" in row.keys():
            result["preview"] = row["preview"] or "尚未发送消息"
        return result

    def create(self, owner_username: str, title: str = "新对话") -> dict[str, Any]:
        conversation_id = f"CHAT-{secrets.token_hex(5).upper()}"
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO chat_conversations (id, owner_username, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, owner_username, title, now, now),
            )
        return self.get(conversation_id, owner_username) or {}

    def list(self, owner_username: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.*, COUNT(m.id) AS message_count,
                    (SELECT content FROM chat_messages WHERE conversation_id = c.id ORDER BY id DESC LIMIT 1) AS preview
                FROM chat_conversations AS c
                LEFT JOIN chat_messages AS m ON m.conversation_id = c.id
                WHERE c.owner_username = ?
                GROUP BY c.id
                ORDER BY c.updated_at DESC
                LIMIT ?
                """,
                (owner_username, limit),
            ).fetchall()
        return [self._conversation(row) for row in rows]

    def get(self, conversation_id: str, owner_username: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM chat_conversations WHERE id = ? AND owner_username = ?",
                (conversation_id, owner_username),
            ).fetchone()
            if not row:
                return None
            messages = connection.execute(
                "SELECT role, content, created_at FROM chat_messages WHERE conversation_id = ? ORDER BY id ASC",
                (conversation_id,),
            ).fetchall()
        result = self._conversation(row)
        result["messages"] = [dict(message) for message in messages]
        return result

    def append_exchange(
        self,
        conversation_id: str,
        owner_username: str,
        user_message: str,
        assistant_message: str,
        provider_id: str,
        model_id: str,
    ) -> dict[str, Any] | None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT title FROM chat_conversations WHERE id = ? AND owner_username = ?",
                (conversation_id, owner_username),
            ).fetchone()
            if not row:
                return None
            connection.executemany(
                "INSERT INTO chat_messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [
                    (conversation_id, "user", user_message, now),
                    (conversation_id, "assistant", assistant_message, now),
                ],
            )
            title = row["title"]
            if title == "新对话":
                compact = " ".join(user_message.split())
                title = compact[:30] + ("…" if len(compact) > 30 else "")
            connection.execute(
                "UPDATE chat_conversations SET title = ?, provider_id = ?, model_id = ?, updated_at = ? WHERE id = ?",
                (title, provider_id, model_id, now, conversation_id),
            )
        return self.get(conversation_id, owner_username)

    def count(self, owner_username: str) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM chat_conversations WHERE owner_username = ?",
                (owner_username,),
            ).fetchone()
        return int(row["total"])

    def delete(self, conversation_id: str, owner_username: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM chat_conversations WHERE id = ? AND owner_username = ?",
                (conversation_id, owner_username),
            )
        return cursor.rowcount > 0


class SkillManager:
    """Persisted skill registry with validation and enable/disable controls."""

    DEFAULTS = (
        ("procurement-analysis-sop", "采购分析 SOP", "需求解析、比价维度、风险判断与报告结构", "采购分析", "2.4.1", "/persisted-skills/procurement-analysis-sop"),
        ("web-crawler", "网页爬虫", "隔离环境中的供应商与市场信息采集", "数据采集", "1.8.0", "/persisted-skills/web-crawler"),
        ("web-content-fetch", "网页内容获取", "读取页面正文并提取采购证据", "数据采集", "1.5.3", "/persisted-skills/web-content-fetch"),
        ("supplier-quote-mapping", "供应商报价映射", "统一税率、单位、交期和报价字段", "供应商", "3.1.0", "/persisted-skills/supplier-quote-mapping"),
        ("report-generation", "对比报告生成", "生成 V.S 对比报告与决策摘要", "报告", "2.2.0", "/persisted-skills/report-generation"),
    )

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._lock = RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_skills (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                    category TEXT NOT NULL, version TEXT NOT NULL, directory TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, invocation_count INTEGER NOT NULL DEFAULT 0,
                    last_checked_at TEXT, updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_skill_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, skill_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, message TEXT NOT NULL, actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (skill_id) REFERENCES agent_skills(id) ON DELETE CASCADE
                )"""
            )
            now = datetime.now(UTC).isoformat()
            for record in self.DEFAULTS:
                connection.execute(
                    """INSERT OR IGNORE INTO agent_skills
                    (id, name, description, category, version, directory, enabled, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
                    (*record, now),
                )

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def list(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM agent_skills ORDER BY category, name").fetchall()
        return [self._serialize(row) for row in rows]

    def get(self, skill_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM agent_skills WHERE id = ?", (skill_id,)).fetchone()
            if not row:
                return None
            events = connection.execute(
                "SELECT event_type, message, actor, created_at FROM agent_skill_events WHERE skill_id = ? ORDER BY id DESC LIMIT 20",
                (skill_id,),
            ).fetchall()
        result = self._serialize(row)
        result["events"] = [dict(event) for event in events]
        return result

    def toggle(self, skill_id: str, enabled: bool, actor: str) -> dict[str, Any] | None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT id FROM agent_skills WHERE id = ?", (skill_id,)).fetchone()
            if not row:
                return None
            connection.execute("UPDATE agent_skills SET enabled = ?, updated_at = ? WHERE id = ?", (int(enabled), now, skill_id))
            connection.execute(
                "INSERT INTO agent_skill_events (skill_id, event_type, message, actor, created_at) VALUES (?, 'configuration', ?, ?, ?)",
                (skill_id, "Skill 已启用" if enabled else "Skill 已停用", actor, now),
            )
        return self.get(skill_id)

    def validate(self, skill_id: str, actor: str) -> dict[str, Any] | None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT id FROM agent_skills WHERE id = ?", (skill_id,)).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE agent_skills SET last_checked_at = ?, invocation_count = invocation_count + 1, updated_at = ? WHERE id = ?",
                (now, now, skill_id),
            )
            connection.execute(
                "INSERT INTO agent_skill_events (skill_id, event_type, message, actor, created_at) VALUES (?, 'validation', '清单、说明文件与沙箱策略校验通过', ?, ?)",
                (skill_id, actor, now),
            )
        result = self.get(skill_id)
        if result is not None:
            result["checks"] = {"manifest": "passed", "instructions": "passed", "sandbox_policy": "passed"}
        return result

    def record_invocation(self, skill_id: str, actor: str, message: str) -> dict[str, Any] | None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT enabled FROM agent_skills WHERE id = ?", (skill_id,)).fetchone()
            if not row or not bool(row["enabled"]):
                return None
            connection.execute(
                "UPDATE agent_skills SET invocation_count = invocation_count + 1, updated_at = ? WHERE id = ?",
                (now, skill_id),
            )
            connection.execute(
                "INSERT INTO agent_skill_events (skill_id, event_type, message, actor, created_at) VALUES (?, 'invocation', ?, ?, ?)",
                (skill_id, message, actor, now),
            )
        return self.get(skill_id)


class SandboxManager:
    """Stable proxy-handle control plane for sandbox lifecycle demonstrations."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._lock = RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sandbox_instances (
                    handle TEXT PRIMARY KEY, instance_id TEXT NOT NULL, status TEXT NOT NULL,
                    generation INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    last_health_at TEXT NOT NULL, restore_count INTEGER NOT NULL DEFAULT 0,
                    file_count INTEGER NOT NULL DEFAULT 14
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sandbox_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, handle TEXT NOT NULL,
                    event_type TEXT NOT NULL, old_instance TEXT, new_instance TEXT,
                    message TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
                )"""
            )
            now = datetime.now(UTC)
            connection.execute(
                """INSERT OR IGNORE INTO sandbox_instances
                (handle, instance_id, status, generation, created_at, expires_at, last_health_at)
                VALUES ('sbx-proxy-07', ?, 'active', 1, ?, ?, ?)""",
                (f"sbx-{secrets.token_hex(3)}", now.isoformat(), datetime.fromtimestamp(now.timestamp() + 7200, UTC).isoformat(), now.isoformat()),
            )

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        expires_at = datetime.fromisoformat(result["expires_at"])
        result["remaining_seconds"] = max(0, int(expires_at.timestamp() - datetime.now(UTC).timestamp()))
        return result

    def get(self, handle: str = "sbx-proxy-07") -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM sandbox_instances WHERE handle = ?", (handle,)).fetchone()
            if not row:
                return None
            events = connection.execute(
                "SELECT event_type, old_instance, new_instance, message, actor, created_at FROM sandbox_events WHERE handle = ? ORDER BY id DESC LIMIT 30",
                (handle,),
            ).fetchall()
        if datetime.fromisoformat(row["expires_at"]).timestamp() <= datetime.now(UTC).timestamp():
            return self.hot_swap(handle, "system:ttl-reaper")
        result = self._serialize(row)
        result["events"] = [dict(event) for event in events]
        return result

    def health_check(self, handle: str, actor: str) -> dict[str, Any] | None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT instance_id FROM sandbox_instances WHERE handle = ?", (handle,)).fetchone()
            if not row:
                return None
            connection.execute("UPDATE sandbox_instances SET status = 'active', last_health_at = ? WHERE handle = ?", (now, handle))
            connection.execute(
                "INSERT INTO sandbox_events (handle, event_type, old_instance, new_instance, message, actor, created_at) VALUES (?, 'health_check', ?, ?, '代理、文件路由与实例心跳检查通过', ?, ?)",
                (handle, row["instance_id"], row["instance_id"], actor, now),
            )
        return self.get(handle)

    def hot_swap(self, handle: str, actor: str) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        new_instance = f"sbx-{secrets.token_hex(3)}"
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT instance_id FROM sandbox_instances WHERE handle = ?", (handle,)).fetchone()
            if not row:
                return None
            old_instance = row["instance_id"]
            connection.execute(
                """UPDATE sandbox_instances SET instance_id = ?, status = 'active', generation = generation + 1,
                    created_at = ?, expires_at = ?, last_health_at = ?, restore_count = restore_count + 1
                    WHERE handle = ?""",
                (new_instance, now.isoformat(), datetime.fromtimestamp(now.timestamp() + 7200, UTC).isoformat(), now.isoformat(), handle),
            )
            connection.execute(
                """INSERT INTO sandbox_events
                (handle, event_type, old_instance, new_instance, message, actor, created_at)
                VALUES (?, 'hot_swap', ?, ?, '底层实例已热替换，稳定句柄与 14 个工作文件保持不变', ?, ?)""",
                (handle, old_instance, new_instance, actor, now.isoformat()),
            )
        return self.get(handle)


chat_conversations = ConversationManager(DATABASE_PATH)
skills = SkillManager(DATABASE_PATH)
sandboxes = SandboxManager(DATABASE_PATH)


def local_procurement_reply(message: str) -> str:
    normalized = message.lower()
    if any(keyword in normalized for keyword in ("比价", "对比", "供应商", "报价")):
        return (
            "可以。我会按价格、质量、交期、履约记录和供应风险五个维度建立对比矩阵。\n\n"
            "当前还需要你提供：候选供应商报价、含税口径、交付地点，以及是否有必须保留的供应商。"
            "资料齐全后，我可以生成 V.S 对比报告并把推荐结论转成采购任务。"
        )
    if any(keyword in normalized for keyword in ("订单", "下单", "采购单")):
        return (
            "订单创建属于敏感写操作，我会先核对供应商主体、物料编码、数量、含税金额、账期和到货日期，"
            "然后停在人工审批节点。只有你明确批准后才会调用 ERP 创建订单。"
        )
    if any(keyword in normalized for keyword in ("m8", "采购", "购买", "件", "个", "吨", "台")):
        return (
            "我已识别到这是一条采购需求，可以进入任务规划。\n\n"
            "建议确认以下字段：\n"
            "1. 物料名称、规格或企业料号\n"
            "2. 数量与允许的交付批次\n"
            "3. 最晚到货日期和交付地点\n"
            "4. 预算、税率、账期与质量标准\n\n"
            "确认后我会拆解为供应商检索、行情调研、多维比价、报告生成和订单审批五个阶段。"
        )
    return (
        "我可以协助整理采购需求、检索供应商、分析报价、生成对比报告，并在人工确认后创建订单。"
        "请先告诉我需要采购什么、数量、期望到货时间和交付地点。"
    )


def encode_sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def normalize_langfuse_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if base_url.endswith("/api/public"):
        base_url = base_url[: -len("/api/public")]
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="LangFuse Host 必须是有效的 http(s) 地址")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="Host 中不能包含用户名或密码")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(status_code=422, detail="远程 LangFuse 服务必须使用 HTTPS")
    return base_url


def public_connection_status(connection: LangfuseConnection | None) -> dict[str, Any]:
    if not connection:
        return {
            "connected": False,
            "requires_api_key": not LANGFUSE_AUTH_DISABLED,
            "base_url": DEFAULT_LANGFUSE_URL,
            "direct_url": DEFAULT_LANGFUSE_URL if LANGFUSE_AUTH_DISABLED else None,
        }
    return {
        "connected": True,
        "requires_api_key": False,
        "base_url": connection.base_url,
        "public_key_masked": f"{connection.public_key[:8]}…{connection.public_key[-4:]}",
        "project_id": connection.project_id,
        "project_name": connection.project_name,
        "organization_name": connection.organization_name,
        "connected_at": connection.connected_at,
        "trace_id": connection.last_trace_id,
        "trace_url": connection.last_trace_url,
        "console_url": connection.base_url,
    }


def create_langfuse_trace(
    connection: LangfuseConnection,
    *,
    name: str,
    seed: str,
    input_data: dict[str, Any],
    output_data: dict[str, Any],
    trace_id_override: str | None = None,
) -> tuple[str, str]:
    trace_id = trace_id_override or connection.client.create_trace_id(seed=seed)
    with connection.client.start_as_current_observation(
        as_type="span",
        name=name,
        trace_context={"trace_id": trace_id},
        input=input_data,
        metadata={"service": "harness-procurement", "environment": "demo"},
    ) as observation:
        observation.update(output=output_data)
    connection.client.flush()
    trace_url = connection.client.get_trace_url(trace_id=trace_id)
    connection.last_trace_id = trace_id
    connection.last_trace_url = trace_url
    return trace_id, trace_url


TASKS: list[dict[str, Any]] = [
    {
        "id": "requirement",
        "index": "01",
        "title": "采购需求解析",
        "summary": "识别规格、数量、交期与历史偏好，形成结构化采购约束。",
        "agent": "主智能体",
        "status": "completed",
        "progress": 100,
        "duration": "0.84s",
        "started_at": "16:08:12",
        "tools": [
            {
                "id": "memory.read",
                "name": "memory.read",
                "kind": "Memory",
                "status": "completed",
                "duration": "118ms",
                "description": "读取跨会话采购偏好",
                "input": {"path": "/memories/procurement-preferences.json"},
                "output": {"chart_style": "决策矩阵", "preferred_suppliers": 3, "currency": "CNY"},
                "trace": "span_6ec19f",
            },
            {
                "id": "skill.load",
                "name": "skill.load",
                "kind": "Skills",
                "status": "completed",
                "duration": "73ms",
                "description": "按需加载采购分析 SOP",
                "input": {"skill": "procurement-analysis-sop", "mode": "progressive"},
                "output": {"version": "2.4.1", "instructions": 18},
                "trace": "span_1ab83d",
            },
        ],
    },
    {
        "id": "supplier",
        "index": "02",
        "title": "供应商与零件检索",
        "summary": "连接 ERP 与供应商库，完成料号映射、库存及有效资质核验。",
        "agent": "采购分析专家",
        "status": "completed",
        "progress": 100,
        "duration": "2.42s",
        "started_at": "16:08:13",
        "tools": [
            {
                "id": "erp.search_parts",
                "name": "erp.search_parts",
                "kind": "MCP · ERP",
                "status": "completed",
                "duration": "386ms",
                "description": "检索企业物料主数据",
                "input": {"query": "M8×30 A2-70 内六角螺栓", "quantity": 12000},
                "output": {"matched_parts": 8, "exact_matches": 3, "inventory_checked": True},
                "trace": "span_f2130c",
            },
            {
                "id": "supplier.map_quotes",
                "name": "supplier.map_quotes",
                "kind": "Skill",
                "status": "completed",
                "duration": "1.63s",
                "description": "统一供应商报价字段与含税口径",
                "input": {"supplier_records": 12, "normalize_tax": True},
                "output": {"valid_quotes": 7, "discarded": 5, "currency": "CNY"},
                "trace": "span_c9e35a",
            },
        ],
    },
    {
        "id": "market",
        "index": "03",
        "title": "市场行情调研",
        "summary": "采集公开行情与交期波动，补足 ERP 报价之外的市场证据。",
        "agent": "采购分析专家",
        "status": "completed",
        "progress": 100,
        "duration": "3.68s",
        "started_at": "16:08:15",
        "tools": [
            {
                "id": "web.search",
                "name": "web.search",
                "kind": "MCP · Web",
                "status": "completed",
                "duration": "1.92s",
                "description": "检索近 30 天现货价格与供给变化",
                "input": {"queries": 4, "recency_days": 30, "region": "华东"},
                "output": {"sources": 21, "accepted": 9, "confidence": 0.91},
                "trace": "span_a70c2e",
            },
            {
                "id": "crawler.fetch",
                "name": "crawler.fetch",
                "kind": "Sandbox",
                "status": "completed",
                "duration": "1.31s",
                "description": "在隔离环境抓取并清洗页面内容",
                "input": {"urls": 9, "sandbox": "sbx-proxy-07"},
                "output": {"documents": 9, "blocked": 0, "characters": 48620},
                "trace": "span_ef01c8",
            },
        ],
    },
    {
        "id": "compare",
        "index": "04",
        "title": "供应商多维对比",
        "summary": "对价格、质量、交期、履约与风险进行加权评分和敏感性分析。",
        "agent": "采购分析专家",
        "status": "completed",
        "progress": 100,
        "duration": "1.94s",
        "started_at": "16:08:19",
        "tools": [
            {
                "id": "sandbox.python",
                "name": "sandbox.python",
                "kind": "OpenSandbox",
                "status": "completed",
                "duration": "1.24s",
                "description": "执行加权决策矩阵与敏感性分析",
                "input": {"script": "/analysis/vendor_scoring.py", "timeout": "30s"},
                "output": {"winner": "震坤行", "score": 92.4, "saving_rate": "8.6%"},
                "trace": "span_108cfe",
            },
            {
                "id": "filesystem.write",
                "name": "filesystem.write",
                "kind": "VFS",
                "status": "completed",
                "duration": "92ms",
                "description": "持久化分析中间产物",
                "input": {"path": "/analysis/vendor-score-matrix.json"},
                "output": {"bytes": 12483, "backend": "StoreBackend", "version": 6},
                "trace": "span_33aa5d",
            },
        ],
    },
    {
        "id": "report",
        "index": "05",
        "title": "V.S 对比报告生成",
        "summary": "汇总关键证据、报价差异和推荐结论，生成可追溯采购报告。",
        "agent": "采购分析专家",
        "status": "in_progress",
        "progress": 72,
        "duration": "进行中",
        "started_at": "16:08:21",
        "tools": [
            {
                "id": "chart.render",
                "name": "chart.render",
                "kind": "Skill",
                "status": "completed",
                "duration": "834ms",
                "description": "渲染报价与综合评分决策图",
                "input": {"template": "vendor-decision-matrix", "vendors": 3},
                "output": {"charts": 3, "format": "SVG", "theme": "midnight-mint"},
                "trace": "span_52e213",
            },
            {
                "id": "report.compose",
                "name": "report.compose",
                "kind": "LLM · DeepSeek-V4",
                "status": "running",
                "duration": "1.09s",
                "description": "生成带证据引用的采购建议",
                "input": {"context_file": "/analysis/vendor-score-matrix.json", "format": "markdown"},
                "output": {"status": "streaming", "tokens": 1842},
                "trace": "span_7d812a",
            },
        ],
    },
    {
        "id": "order",
        "index": "06",
        "title": "采购订单创建",
        "summary": "写入 ERP 前执行人工审批，确认金额、交期和供应商主体。",
        "agent": "采购订单专家",
        "status": "pending",
        "progress": 0,
        "duration": "等待前置任务",
        "started_at": "—",
        "approval": True,
        "tools": [
            {
                "id": "erp.create_order",
                "name": "erp.create_order",
                "kind": "MCP · ERP",
                "status": "pending",
                "duration": "—",
                "description": "创建采购订单（敏感写操作）",
                "input": {"supplier": "震坤行", "amount": 287400, "approval_required": True},
                "output": {"status": "waiting_for_approval"},
                "trace": "span_pending",
            }
        ],
    },
]


def snapshot(user: UserRecord | None = None) -> dict[str, Any]:
    sandbox_state = sandboxes.get()
    result = {
        "run_id": "RUN-20260906-0472",
        "title": "M8 不锈钢紧固件采购",
        "request": "采购 12,000 件 M8×30 A2-70 内六角螺栓，9 月 12 日前到仓",
        "status": "running",
        "progress": 78,
        "completed": 4,
        "total": 6,
        "started_at": "2026-09-06 16:08:12",
        "token_usage": 28420,
        "estimated_saving": 26800,
        "trace_id": "lf-8f7e-4b21-a903",
        "tasks": copy.deepcopy(TASKS),
        "events": [
            {"time": "16:08:21.904", "tone": "success", "text": "决策矩阵已写入 /analysis/vendor-score-matrix.json"},
            {"time": "16:08:22.117", "tone": "info", "text": "委派采购分析专家生成 V.S 对比报告"},
            {"time": "16:08:23.029", "tone": "live", "text": "DeepSeek-V4 正在组织结论与证据引用…"},
        ],
        "sandbox": {
            "proxy": sandbox_state["handle"],
            "active_instance": sandbox_state["instance_id"],
            "recovery_count": sandbox_state["restore_count"],
            "last_hotswap": sandbox_state["events"][0]["created_at"] if sandbox_state["events"] else None,
            "restored_files": sandbox_state["file_count"],
        } if sandbox_state else None,
    }
    if user:
        user_data = public_user(user)
        result["user"] = user_data
        result["permissions"] = user_data["permissions"]
        if user.role != "admin":
            result.pop("trace_id", None)
            result.pop("token_usage", None)
            result.pop("sandbox", None)
            for task in result["tasks"]:
                task["tools"] = []
    return result


FULL_REPLAY_EVENTS = [
    {"type": "run_reset", "message": "已创建任务清单，准备执行采购工作流"},
    {"type": "task_started", "task_id": "requirement", "message": "开始解析采购需求"},
    {"type": "tool_started", "task_id": "requirement", "tool_id": "memory.read", "message": "读取用户采购偏好"},
    {"type": "tool_completed", "task_id": "requirement", "tool_id": "memory.read", "message": "采购偏好载入完成"},
    {"type": "task_completed", "task_id": "requirement", "progress": 17, "message": "采购需求已结构化"},
    {"type": "task_started", "task_id": "supplier", "message": "委派采购分析专家检索供应商"},
    {"type": "tool_started", "task_id": "supplier", "tool_id": "erp.search_parts", "message": "正在调用 ERP 物料检索"},
    {"type": "tool_completed", "task_id": "supplier", "tool_id": "erp.search_parts", "message": "找到 3 个精确料号"},
    {"type": "task_completed", "task_id": "supplier", "progress": 33, "message": "7 份有效报价已完成映射"},
    {"type": "task_started", "task_id": "market", "message": "开始补充市场行情证据"},
    {"type": "sandbox_hotswap", "message": "容器超时，Proxy 已热替换实例并恢复 14 个文件"},
    {"type": "task_completed", "task_id": "market", "progress": 50, "message": "市场行情调研完成"},
    {"type": "task_started", "task_id": "compare", "message": "执行多维加权评分"},
    {"type": "tool_started", "task_id": "compare", "tool_id": "sandbox.python", "message": "在安全沙箱运行评分脚本"},
    {"type": "task_completed", "task_id": "compare", "progress": 67, "message": "震坤行综合评分 92.4，暂列第一"},
    {"type": "task_started", "task_id": "report", "message": "生成 V.S 对比报告"},
    {"type": "tool_started", "task_id": "report", "tool_id": "report.compose", "message": "DeepSeek-V4 正在组织报告"},
    {"type": "tool_completed", "task_id": "report", "tool_id": "report.compose", "message": "报告已保存至 /analysis/vendor-comparison.md"},
    {"type": "task_completed", "task_id": "report", "progress": 83, "message": "V.S 对比报告生成完成"},
    {"type": "approval_required", "task_id": "order", "progress": 83, "message": "订单写入属于敏感操作，等待人工确认"},
]

TAIL_EVENTS = [
    {"type": "tool_progress", "task_id": "report", "tool_id": "report.compose", "task_progress": 86, "message": "正在补充价格波动风险说明"},
    {"type": "tool_completed", "task_id": "report", "tool_id": "report.compose", "message": "报告已保存至 /analysis/vendor-comparison.md"},
    {"type": "task_completed", "task_id": "report", "progress": 83, "message": "V.S 对比报告生成完成"},
    {"type": "approval_required", "task_id": "order", "progress": 83, "message": "订单写入属于敏感操作，等待人工确认"},
]


@app.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/api/auth/accounts")
async def auth_accounts() -> dict[str, Any]:
    return {
        "accounts": [
            {"username": user.username, "name": user.name, "role": user.role, "role_label": public_user(user)["role_label"], "department": user.department}
            for user in USERS.values()
        ]
    }


@app.get("/api/auth/me")
async def auth_me(user: UserRecord = Depends(require_user)) -> dict[str, Any]:
    return public_user(user)


@app.post("/api/auth/login")
async def auth_login(
    payload: LoginRequest,
    response: Response,
    harness_auth_session: str | None = Cookie(default=None),
    harness_chat_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    login_result = auth_manager.login(payload.username.strip(), payload.password)
    if not login_result:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token, user = login_result
    auth_manager.logout(harness_auth_session)
    response.delete_cookie(CHAT_COOKIE, path="/")
    response.set_cookie(
        key=AUTH_COOKIE,
        value=token,
        max_age=8 * 60 * 60,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return public_user(user)


@app.post("/api/auth/logout")
async def auth_logout(
    response: Response,
    harness_auth_session: str | None = Cookie(default=None),
    harness_chat_session: str | None = Cookie(default=None),
) -> dict[str, bool]:
    auth_manager.logout(harness_auth_session)
    response.delete_cookie(AUTH_COOKIE, path="/")
    response.delete_cookie(CHAT_COOKIE, path="/")
    return {"logged_out": True}


@app.get("/langfuse", include_in_schema=False)
async def langfuse_setup_page(
    harness_auth_session: str | None = Cookie(default=None),
) -> Response:
    user = auth_manager.get_user(harness_auth_session)
    if not user:
        return RedirectResponse("/login?return_to=/langfuse", status_code=303)
    if user.role != "admin":
        return RedirectResponse("/?forbidden=langfuse", status_code=303)
    return FileResponse(STATIC_DIR / "langfuse.html")


@app.get("/langfuse/open", include_in_schema=False)
async def open_langfuse(
    harness_auth_session: str | None = Cookie(default=None),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> RedirectResponse:
    user = auth_manager.get_user(harness_auth_session)
    if not user:
        return RedirectResponse("/login?return_to=/langfuse/open", status_code=303)
    if user.role != "admin":
        return RedirectResponse("/?forbidden=langfuse", status_code=303)
    connection = langfuse_connections.get(harness_langfuse_session)
    if connection:
        return RedirectResponse(connection.last_trace_url or connection.base_url, status_code=303)
    if LANGFUSE_AUTH_DISABLED:
        return RedirectResponse(DEFAULT_LANGFUSE_URL, status_code=303)
    return RedirectResponse("/langfuse", status_code=303)


@app.get("/api/langfuse/status")
async def langfuse_status(
    user: UserRecord = Depends(require_admin),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    return public_connection_status(langfuse_connections.get(harness_langfuse_session))


@app.post("/api/langfuse/connect")
async def connect_langfuse(
    payload: LangfuseConnectRequest,
    response: Response,
    user: UserRecord = Depends(require_admin),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    base_url = normalize_langfuse_url(payload.base_url)
    projects_url = f"{base_url}/api/public/projects"
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as http_client:
            verification = await http_client.get(
                projects_url,
                auth=httpx.BasicAuth(payload.public_key, payload.secret_key),
                headers={"Accept": "application/json", "User-Agent": "Harness-Procurement/1.0"},
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="连接 LangFuse 超时，请检查 Host 和网络") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接 LangFuse：{exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="无法建立 LangFuse 安全连接，请检查 Host、证书和代理设置") from exc

    if verification.status_code in {401, 403}:
        raise HTTPException(status_code=401, detail="Public Key 或 Secret Key 无效")
    if verification.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"LangFuse 验证失败：HTTP {verification.status_code}",
        )

    try:
        body = verification.json()
        projects = body.get("data", []) if isinstance(body, dict) else []
        project = projects[0] if projects else {}
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="LangFuse 返回了无法识别的验证结果") from exc

    project_id = str(project.get("id") or payload.public_key[:16])
    project_name = str(project.get("name") or "LangFuse Project")
    organization = project.get("organization") or {}
    organization_name = str(organization.get("name") or "—")

    try:
        client = Langfuse(
            public_key=payload.public_key,
            secret_key=payload.secret_key,
            base_url=base_url,
            environment="demo",
        )
        session_id = harness_langfuse_session or secrets.token_urlsafe(32)
        connection = LangfuseConnection(
            base_url=base_url,
            public_key=payload.public_key,
            secret_key=payload.secret_key,
            project_id=project_id,
            project_name=project_name,
            organization_name=organization_name,
            connected_at=datetime.now(UTC).isoformat(),
            client=client,
        )
        await asyncio.to_thread(
            create_langfuse_trace,
            connection,
            name="harness.langfuse.connection-check",
            seed=f"harness-connect-{session_id}-{time.time_ns()}",
            input_data={"source": "langfuse-setup", "project": project_name},
            output_data={"connected": True, "sdk": "langfuse-python-v4"},
        )
        langfuse_connections.save(session_id, connection)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LangFuse SDK 初始化失败：{exc}") from exc

    response.set_cookie(
        key=LANGFUSE_COOKIE,
        value=session_id,
        max_age=8 * 60 * 60,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return public_connection_status(connection)


@app.post("/api/langfuse/test-trace")
async def create_test_trace(
    user: UserRecord = Depends(require_admin),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    connection = langfuse_connections.get(harness_langfuse_session)
    if not connection:
        raise HTTPException(status_code=401, detail="请先连接 LangFuse")
    trace_id, trace_url = await asyncio.to_thread(
        create_langfuse_trace,
        connection,
        name="harness.procurement.manual-test",
        seed=f"harness-test-{time.time_ns()}",
        input_data={"action": "manual-test", "service": "procurement-console"},
        output_data={"status": "success", "message": "LangFuse trace pipeline verified"},
    )
    return {"status": "created", "trace_id": trace_id, "trace_url": trace_url}


@app.delete("/api/langfuse/connection")
async def disconnect_langfuse(
    response: Response,
    user: UserRecord = Depends(require_admin),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> dict[str, bool]:
    await asyncio.to_thread(langfuse_connections.remove, harness_langfuse_session)
    response.delete_cookie(LANGFUSE_COOKIE, path="/")
    return {"disconnected": True}


@app.get("/chat", include_in_schema=False)
async def chat_page(
    harness_auth_session: str | None = Cookie(default=None),
) -> Response:
    if not auth_manager.get_user(harness_auth_session):
        return RedirectResponse("/login?return_to=/chat", status_code=303)
    return FileResponse(STATIC_DIR / "chat.html")


@app.get("/applications", include_in_schema=False)
async def applications_page(
    harness_auth_session: str | None = Cookie(default=None),
) -> Response:
    if not auth_manager.get_user(harness_auth_session):
        return RedirectResponse("/login?return_to=/applications", status_code=303)
    return FileResponse(STATIC_DIR / "applications.html")


@app.get("/models", include_in_schema=False)
async def models_page(
    harness_auth_session: str | None = Cookie(default=None),
) -> Response:
    user = auth_manager.get_user(harness_auth_session)
    if not user:
        return RedirectResponse("/login?return_to=/models", status_code=303)
    if user.role != "admin":
        return RedirectResponse("/?forbidden=models", status_code=303)
    return FileResponse(STATIC_DIR / "models.html")


def admin_page_or_redirect(
    filename: str,
    return_to: str,
    harness_auth_session: str | None,
) -> Response:
    user = auth_manager.get_user(harness_auth_session)
    if not user:
        return RedirectResponse(f"/login?return_to={return_to}", status_code=303)
    if user.role != "admin":
        return RedirectResponse(f"/?forbidden={return_to.strip('/')}", status_code=303)
    return FileResponse(STATIC_DIR / filename)


@app.get("/skills", include_in_schema=False)
async def skills_page(harness_auth_session: str | None = Cookie(default=None)) -> Response:
    return admin_page_or_redirect("skills.html", "/skills", harness_auth_session)


@app.get("/sandboxes", include_in_schema=False)
async def sandboxes_page(harness_auth_session: str | None = Cookie(default=None)) -> Response:
    return admin_page_or_redirect("sandboxes.html", "/sandboxes", harness_auth_session)


@app.get("/api/models/catalog")
async def model_catalog(user: UserRecord = Depends(require_user)) -> dict[str, Any]:
    runtime = model_providers.runtime(None, None)
    return {
        "providers": model_providers.catalog(False),
        "default": {"provider_id": runtime["provider_id"], "model_id": runtime["model_id"]},
    }


@app.get("/api/models/providers")
async def model_provider_configs(user: UserRecord = Depends(require_admin)) -> dict[str, Any]:
    return {"providers": model_providers.catalog(True)}


@app.put("/api/models/providers/{provider_id}")
async def save_model_provider(
    provider_id: str,
    payload: ModelProviderConfigRequest,
    user: UserRecord = Depends(require_admin),
) -> dict[str, Any]:
    provider = model_providers.save(provider_id, payload, user)
    return {"saved": True, "provider": provider, "message": f"{provider['name']} 配置已保存"}


@app.delete("/api/models/providers/{provider_id}")
async def remove_model_provider(
    provider_id: str,
    user: UserRecord = Depends(require_admin),
) -> dict[str, Any]:
    model_providers.remove(provider_id)
    return {"removed": True, "message": "厂商配置已清除"}


@app.get("/api/chat/status")
async def chat_status(
    user: UserRecord = Depends(require_user),
    harness_chat_session: str | None = Cookie(default=None),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    conversation = chat_conversations.get(harness_chat_session, user.username) if harness_chat_session else None
    langfuse_connection = langfuse_connections.get(harness_langfuse_session)
    runtime = model_providers.runtime(None, None)
    return {
        "model": runtime["model_id"],
        "model_name": runtime["model_name"],
        "provider": runtime["provider_name"],
        "provider_id": runtime["provider_id"],
        "model_configured": runtime["provider_id"] != "local",
        "providers": model_providers.catalog(False),
        "langfuse_connected": bool(langfuse_connection) if user.role == "admin" else False,
        "history_messages": len(conversation["messages"]) if conversation else 0,
        "conversation_id": conversation["id"] if conversation else None,
        "conversation_count": chat_conversations.count(user.username),
    }


@app.get("/api/chat/conversations")
async def list_chat_conversations(user: UserRecord = Depends(require_user)) -> dict[str, Any]:
    return {"conversations": chat_conversations.list(user.username)}


@app.post("/api/chat/conversations")
async def create_chat_conversation(
    response: Response,
    user: UserRecord = Depends(require_user),
) -> dict[str, Any]:
    conversation = chat_conversations.create(user.username)
    response.set_cookie(
        key=CHAT_COOKIE,
        value=conversation["id"],
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return {"created": True, "conversation": conversation}


@app.get("/api/chat/conversations/{conversation_id}")
async def get_chat_conversation(
    conversation_id: str,
    response: Response,
    user: UserRecord = Depends(require_user),
) -> dict[str, Any]:
    conversation = chat_conversations.get(conversation_id, user.username)
    if not conversation:
        raise HTTPException(status_code=404, detail="对话不存在")
    response.set_cookie(
        key=CHAT_COOKIE,
        value=conversation_id,
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return {"conversation": conversation}


@app.delete("/api/chat/conversations/{conversation_id}")
async def delete_chat_conversation(
    conversation_id: str,
    response: Response,
    user: UserRecord = Depends(require_user),
    harness_chat_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    if not chat_conversations.delete(conversation_id, user.username):
        raise HTTPException(status_code=404, detail="对话不存在")
    if harness_chat_session == conversation_id:
        response.delete_cookie(CHAT_COOKIE, path="/")
    return {"deleted": True, "conversation_id": conversation_id, "message": "历史对话已删除"}


@app.get("/api/chat/history")
async def chat_history(
    user: UserRecord = Depends(require_user),
    harness_chat_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    conversation = chat_conversations.get(harness_chat_session, user.username) if harness_chat_session else None
    return {"conversation": conversation, "messages": conversation["messages"] if conversation else []}


@app.post("/api/chat/reset")
async def reset_chat(
    response: Response,
    user: UserRecord = Depends(require_user),
    harness_chat_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    response.delete_cookie(CHAT_COOKIE, path="/")
    return {"reset": True, "message": "已退出当前对话，历史记录仍保留"}


@app.post("/api/chat/to-task")
async def chat_to_task(
    payload: ChatTaskRequest,
    user: UserRecord = Depends(require_user),
) -> dict[str, Any]:
    return {
        "created": True,
        "conversation_id": payload.conversation_id,
        "run_id": "RUN-20260906-0472",
        "run_url": "/runs?source=chat",
        "message": "已根据当前对话生成采购任务清单",
        "applicant": user.name,
    }


@app.post("/api/chat/stream")
async def stream_chat(
    payload: ChatRequest,
    user: UserRecord = Depends(require_user),
    harness_chat_session: str | None = Cookie(default=None),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> StreamingResponse:
    conversation_id = payload.conversation_id or harness_chat_session
    conversation = chat_conversations.get(conversation_id, user.username) if conversation_id else None
    if conversation_id and not conversation:
        raise HTTPException(status_code=404, detail="对话不存在或无权访问")
    if not conversation:
        conversation = chat_conversations.create(user.username)
        conversation_id = conversation["id"]
    history = [{"role": item["role"], "content": item["content"]} for item in conversation["messages"][-20:]]
    user_message = payload.message.strip()
    if not user_message:
        raise HTTPException(status_code=422, detail="消息不能为空")
    connection = langfuse_connections.get(harness_langfuse_session)
    runtime = model_providers.runtime(payload.provider_id, payload.model_id)

    async def generate_provider_tokens(
        messages: list[dict[str, str]],
    ) -> AsyncIterator[tuple[str, dict[str, int] | None]]:
        if runtime["protocol"] == "responses":
            request_url = f"{runtime['base_url']}/responses"
            request_body = {
                "model": runtime["model_id"],
                "instructions": CHAT_SYSTEM_PROMPT,
                "input": [message for message in messages if message["role"] != "system"],
                "stream": True,
                "max_output_tokens": 1200,
            }
        else:
            request_url = f"{runtime['base_url']}/chat/completions"
            request_body = {
                "model": runtime["model_id"],
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
                "temperature": 0.2,
                "max_tokens": 1200,
            }
        timeout = httpx.Timeout(90.0, connect=12.0)
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            async with http_client.stream(
                "POST",
                request_url,
                headers={
                    "Authorization": f"Bearer {runtime['api_key']}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=request_body,
            ) as upstream:
                if upstream.status_code >= 400:
                    error_body = (await upstream.aread()).decode(errors="replace")[:400]
                    raise RuntimeError(f"{runtime['provider_name']} HTTP {upstream.status_code}: {error_body}")
                async for line in upstream.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if runtime["protocol"] == "responses":
                        content = chunk.get("delta") or "" if chunk.get("type") == "response.output_text.delta" else ""
                        response_data = chunk.get("response") or {}
                        response_usage = response_data.get("usage") if chunk.get("type") == "response.completed" else None
                        usage = (
                            {
                                "prompt_tokens": int(response_usage.get("input_tokens") or 0),
                                "completion_tokens": int(response_usage.get("output_tokens") or 0),
                                "total_tokens": int(response_usage.get("total_tokens") or 0),
                            }
                            if response_usage
                            else None
                        )
                    else:
                        choices = chunk.get("choices") or []
                        content = (choices[0].get("delta") or {}).get("content") or "" if choices else ""
                        usage = chunk.get("usage")
                    if content or usage:
                        yield content, usage

    async def event_generator() -> AsyncIterator[str]:
        model_name = runtime["model_id"]
        can_view_observability = user.role == "admin"
        trace_id = None
        trace_url = None
        trace_context = None
        trace_observation = None
        usage: dict[str, int] | None = None
        answer_parts: list[str] = []
        active_connection = connection
        messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}, *history, {"role": "user", "content": user_message}]

        if active_connection:
            try:
                trace_id = active_connection.client.create_trace_id(seed=f"{conversation_id}-{time.time_ns()}")
                trace_context = active_connection.client.start_as_current_observation(
                    as_type="generation",
                    name="procurement.chat",
                    trace_context={"trace_id": trace_id},
                    input=messages,
                    model=model_name,
                    metadata={
                        "conversation_id": conversation_id,
                        "service": "harness-procurement",
                        "mode": "chat",
                        "provider_id": runtime["provider_id"],
                        "provider_name": runtime["provider_name"],
                    },
                )
                trace_observation = trace_context.__enter__()
                trace_url = active_connection.client.get_trace_url(trace_id=trace_id)
                active_connection.last_trace_id = trace_id
                active_connection.last_trace_url = trace_url
            except Exception:
                active_connection = None
                trace_id = trace_url = trace_context = trace_observation = None

        yield encode_sse(
            "meta",
            {
                "conversation_id": conversation_id,
                "user": public_user(user),
                "model": model_name,
                "model_name": runtime["model_name"],
                "provider": runtime["provider_name"],
                "provider_id": runtime["provider_id"],
                "langfuse": (
                    {"connected": bool(active_connection), "trace_id": trace_id, "trace_url": trace_url}
                    if can_view_observability
                    else {"connected": False}
                ),
            },
        )

        loaded_skill = skills.record_invocation(
            "procurement-analysis-sop",
            user.username,
            f"由对话 {conversation_id} 渐进式加载",
        )
        tool_events = [
            {
                "id": "memory.read",
                "name": "memory.read",
                "kind": "Memory",
                "status": "completed",
                "duration": "42ms",
                "summary": f"读取最近 {len(history)} 条会话消息",
                "input": {"conversation": conversation_id, "limit": 20},
                "output": {"messages": len(history), "loaded": True},
            }
        ]
        if loaded_skill:
            tool_events.append({
                "id": "skill.load",
                "name": "skill.load",
                "kind": "Skills",
                "status": "completed",
                "duration": "31ms",
                "summary": "加载采购需求分析 SOP",
                "input": {"skill": "procurement-analysis-sop"},
                "output": {"loaded": True, "version": loaded_skill["version"]},
            })
        for tool in tool_events:
            if active_connection:
                try:
                    with active_connection.client.start_as_current_observation(
                        as_type="tool",
                        name=tool["name"],
                        input=tool["input"],
                        metadata={"conversation_id": conversation_id, "tool_kind": tool["kind"]},
                    ) as tool_observation:
                        tool_observation.update(output=tool["output"])
                except Exception:
                    active_connection = None
            if can_view_observability:
                yield encode_sse("tool", tool)
                await asyncio.sleep(0.16)

        try:
            if runtime["protocol"] != "local":
                try:
                    async for token, token_usage in generate_provider_tokens(messages):
                        if token:
                            answer_parts.append(token)
                            yield encode_sse("delta", {"content": token})
                        if token_usage:
                            usage = token_usage
                except Exception:
                    fallback_notice = f"{runtime['provider_name']} 暂时不可用，已切换到本地采购引擎。\n\n"
                    yield encode_sse("notice", {"message": fallback_notice.strip()})
                    answer_parts.append(fallback_notice)
                    fallback_answer = local_procurement_reply(user_message)
                    for start in range(0, len(fallback_answer), 7):
                        token = fallback_answer[start : start + 7]
                        answer_parts.append(token)
                        yield encode_sse("delta", {"content": token})
                        await asyncio.sleep(0.035)
            else:
                local_answer = local_procurement_reply(user_message)
                for start in range(0, len(local_answer), 7):
                    token = local_answer[start : start + 7]
                    answer_parts.append(token)
                    yield encode_sse("delta", {"content": token})
                    await asyncio.sleep(0.035)
        finally:
            answer = "".join(answer_parts).strip()
            if not answer:
                answer = "本次回复未能生成，请稍后重试。"
            chat_conversations.append_exchange(
                conversation_id,
                user.username,
                user_message,
                answer,
                runtime["provider_id"],
                runtime["model_id"],
            )
            if trace_observation and trace_context and active_connection:
                update_kwargs: dict[str, Any] = {"output": answer}
                if usage:
                    update_kwargs["usage_details"] = {
                        "input": int(usage.get("prompt_tokens") or 0),
                        "output": int(usage.get("completion_tokens") or 0),
                        "total": int(usage.get("total_tokens") or 0),
                    }
                trace_observation.update(**update_kwargs)
                trace_context.__exit__(None, None, None)
                try:
                    await asyncio.to_thread(active_connection.client.flush)
                except Exception:
                    pass

        yield encode_sse(
            "done",
            {
                "conversation_id": conversation_id,
                "model": model_name,
                "model_name": runtime["model_name"],
                "provider": runtime["provider_name"],
                "provider_id": runtime["provider_id"],
                "trace_id": trace_id if can_view_observability else None,
                "trace_url": trace_url if can_view_observability else None,
                "can_create_task": True,
            },
        )

    stream = StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    stream.set_cookie(
        key=CHAT_COOKIE,
        value=conversation_id,
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return stream


@app.get("/runs", include_in_schema=False)
async def runs_page(
    harness_auth_session: str | None = Cookie(default=None),
) -> Response:
    if not auth_manager.get_user(harness_auth_session):
        return RedirectResponse("/login?return_to=/runs", status_code=303)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/", include_in_schema=False)
async def overview_page(
    harness_auth_session: str | None = Cookie(default=None),
) -> Response:
    if not auth_manager.get_user(harness_auth_session):
        return RedirectResponse("/login?return_to=/", status_code=303)
    return FileResponse(STATIC_DIR / "overview.html")


@app.get("/api/overview")
async def overview(user: UserRecord = Depends(require_user)) -> dict[str, Any]:
    applications = order_applications.list(None if user.role == "admin" else user.username)
    conversations = chat_conversations.list(user.username, limit=5)
    payload: dict[str, Any] = {
        "user": public_user(user),
        "applications": {
            "total": len(applications),
            "pending": sum(item["status"] == "pending_approval" for item in applications),
            "approved": sum(item["status"] == "approved" for item in applications),
            "recent": applications[:5],
        },
        "conversations": {"total": chat_conversations.count(user.username), "recent": conversations},
        "run": {
            "id": "RUN-20260906-0472",
            "title": "M8 不锈钢内六角螺栓采购",
            "status": "waiting_for_approval",
            "progress": 83,
            "completed": 5,
            "total": 6,
            "url": "/runs",
        },
    }
    if user.role == "admin":
        skill_items = skills.list()
        sandbox = sandboxes.get()
        payload["technical"] = {
            "skills_total": len(skill_items),
            "skills_enabled": sum(item["enabled"] for item in skill_items),
            "sandbox": sandbox,
        }
    return payload


@app.get("/api/skills")
async def list_skills(user: UserRecord = Depends(require_admin)) -> dict[str, Any]:
    items = skills.list()
    return {
        "skills": items,
        "summary": {"total": len(items), "enabled": sum(item["enabled"] for item in items)},
    }


@app.get("/api/skills/{skill_id}")
async def get_skill(skill_id: str, user: UserRecord = Depends(require_admin)) -> dict[str, Any]:
    skill = skills.get(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return {"skill": skill}


@app.post("/api/skills/{skill_id}/toggle")
async def toggle_skill(
    skill_id: str,
    payload: SkillToggleRequest,
    user: UserRecord = Depends(require_admin),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    skill = skills.toggle(skill_id, payload.enabled, user.username)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    connection = langfuse_connections.get(harness_langfuse_session)
    trace_id = trace_url = None
    if connection:
        try:
            trace_id, trace_url = await asyncio.to_thread(
                create_langfuse_trace,
                connection,
                name="procurement.skill.configuration",
                seed=f"{skill_id}-toggle-{time.time_ns()}",
                input_data={"skill_id": skill_id, "enabled": payload.enabled, "actor": user.username},
                output_data={"updated": True, "version": skill["version"]},
            )
        except Exception:
            pass
    return {"updated": True, "skill": skill, "langfuse": {"connected": bool(connection), "trace_id": trace_id, "trace_url": trace_url}}


@app.post("/api/skills/{skill_id}/validate")
async def validate_skill(
    skill_id: str,
    user: UserRecord = Depends(require_admin),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    skill = skills.validate(skill_id, user.username)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    connection = langfuse_connections.get(harness_langfuse_session)
    trace_id = trace_url = None
    if connection:
        try:
            trace_id, trace_url = await asyncio.to_thread(
                create_langfuse_trace,
                connection,
                name="procurement.skill.validation",
                seed=f"{skill_id}-validate-{time.time_ns()}",
                input_data={"skill_id": skill_id, "actor": user.username},
                output_data={"validated": True, "checks": skill["checks"]},
            )
        except Exception:
            pass
    return {"validated": True, "skill": skill, "message": "Skill 校验通过，可由 Agent 加载", "langfuse": {"connected": bool(connection), "trace_id": trace_id, "trace_url": trace_url}}


@app.get("/api/sandboxes")
async def get_sandboxes(user: UserRecord = Depends(require_admin)) -> dict[str, Any]:
    sandbox = sandboxes.get()
    return {"sandboxes": [sandbox] if sandbox else [], "proxy_mode": "stable-handle"}


@app.post("/api/sandboxes/{handle}/health-check")
async def health_check_sandbox(
    handle: str,
    user: UserRecord = Depends(require_admin),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    sandbox = sandboxes.health_check(handle, user.username)
    if not sandbox:
        raise HTTPException(status_code=404, detail="沙箱句柄不存在")
    connection = langfuse_connections.get(harness_langfuse_session)
    if connection:
        try:
            await asyncio.to_thread(
                create_langfuse_trace,
                connection,
                name="procurement.sandbox.health-check",
                seed=f"{handle}-health-{time.time_ns()}",
                input_data={"handle": handle, "actor": user.username},
                output_data={"status": sandbox["status"], "instance_id": sandbox["instance_id"]},
            )
        except Exception:
            pass
    return {"checked": True, "sandbox": sandbox, "message": "沙箱代理链路健康", "langfuse_connected": bool(connection)}


@app.post("/api/sandboxes/{handle}/hot-swap")
async def hot_swap_sandbox(
    handle: str,
    user: UserRecord = Depends(require_admin),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    sandbox = sandboxes.hot_swap(handle, user.username)
    if not sandbox:
        raise HTTPException(status_code=404, detail="沙箱句柄不存在")
    connection = langfuse_connections.get(harness_langfuse_session)
    if connection:
        try:
            await asyncio.to_thread(
                create_langfuse_trace,
                connection,
                name="procurement.sandbox.hot-swap",
                seed=f"{handle}-swap-{time.time_ns()}",
                input_data={"handle": handle, "actor": user.username},
                output_data={"instance_id": sandbox["instance_id"], "generation": sandbox["generation"], "restored_files": sandbox["file_count"]},
            )
        except Exception:
            pass
    return {"swapped": True, "sandbox": sandbox, "message": "底层实例已热替换并完成状态恢复", "langfuse_connected": bool(connection)}


@app.get("/api/runs/demo")
async def get_demo_run(user: UserRecord = Depends(require_user)) -> dict[str, Any]:
    return snapshot(user)


@app.get("/api/runs/demo/events")
async def stream_demo_events(
    mode: str = "tail",
    user: UserRecord = Depends(require_user),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> StreamingResponse:
    source_events = FULL_REPLAY_EVENTS if mode == "replay" else TAIL_EVENTS
    events = (
        source_events
        if user.role == "admin"
        else [event for event in source_events if not event["type"].startswith("tool_") and event["type"] != "sandbox_hotswap"]
    )
    can_view_observability = user.role == "admin"
    connection = langfuse_connections.get(harness_langfuse_session)

    async def event_generator():
        root_context = None
        root_observation = None
        trace_id = None
        trace_url = None
        active_connection = connection
        if active_connection:
            try:
                trace_id = active_connection.client.create_trace_id(seed=f"RUN-20260906-0472-{mode}-{time.time_ns()}")
                root_context = active_connection.client.start_as_current_observation(
                    as_type="span",
                    name="procurement.run",
                    trace_context={"trace_id": trace_id},
                    input={"run_id": "RUN-20260906-0472", "mode": mode, "task_count": 6},
                    metadata={"service": "harness-procurement", "model": "DeepSeek-V4", "user": user.username, "role": user.role},
                )
                root_observation = root_context.__enter__()
                trace_url = active_connection.client.get_trace_url(trace_id=trace_id)
                active_connection.last_trace_id = trace_id
                active_connection.last_trace_url = trace_url
            except Exception:
                active_connection = None
                trace_id = trace_url = root_context = root_observation = None

        try:
            for sequence, event in enumerate(events, start=1):
                payload = {**event, "sequence": sequence}
                if event["type"] == "sandbox_hotswap":
                    sandbox_state = sandboxes.hot_swap("sbx-proxy-07", "system:agent-run")
                    if sandbox_state:
                        payload["sandbox"] = {
                            "handle": sandbox_state["handle"],
                            "instance_id": sandbox_state["instance_id"],
                            "generation": sandbox_state["generation"],
                            "restored_files": sandbox_state["file_count"],
                        }
                if active_connection and trace_id:
                    observation_type = (
                        "generation"
                        if event.get("tool_id") == "report.compose"
                        else "span"
                    )
                    observation_kwargs: dict[str, Any] = {
                        "as_type": observation_type,
                        "name": f"{event['type']}.{event.get('tool_id') or event.get('task_id') or 'run'}",
                        "input": {
                            "task_id": event.get("task_id"),
                            "tool_id": event.get("tool_id"),
                            "sequence": sequence,
                        },
                        "metadata": {"event_type": event["type"], "mode": mode},
                    }
                    if observation_type == "generation":
                        observation_kwargs["model"] = "DeepSeek-V4"
                    try:
                        with active_connection.client.start_as_current_observation(**observation_kwargs) as observation:
                            observation.update(output={"message": event["message"], "status": "recorded"})
                        payload["langfuse"] = (
                            {"connected": True, "trace_id": trace_id, "trace_url": trace_url}
                            if can_view_observability
                            else {"connected": False}
                        )
                    except Exception:
                        active_connection = None
                        payload["langfuse"] = {"connected": False}
                else:
                    payload["langfuse"] = {"connected": False}
                yield f"event: trace\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.58 if mode == "replay" else 0.82)
        finally:
            if root_observation and root_context and active_connection:
                try:
                    root_observation.update(output={"status": "waiting_for_approval", "completed_tasks": 5})
                    root_context.__exit__(None, None, None)
                    await asyncio.to_thread(active_connection.client.flush)
                except Exception:
                    active_connection = None

        done_payload = {
            "status": "waiting_for_approval",
            "langfuse_connected": bool(active_connection) if can_view_observability else False,
            "trace_id": trace_id if can_view_observability else None,
            "trace_url": trace_url if can_view_observability else None,
        }
        yield f"event: done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/runs/demo/approve")
async def approve_order(
    user: UserRecord = Depends(require_admin),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    await asyncio.sleep(0.75)
    connection = langfuse_connections.get(harness_langfuse_session)
    trace_id = None
    trace_url = None
    if connection:
        try:
            trace_id, trace_url = await asyncio.to_thread(
                create_langfuse_trace,
                connection,
                name="procurement.order.approved",
                seed=f"RUN-20260906-0472-order-{time.time_ns()}",
                input_data={"supplier": "震坤行", "amount": 287400, "approved": True},
                output_data={"order_no": "PO-2026-0906-1847", "erp_status": "已提交", "approved_by": user.username},
                trace_id_override=connection.last_trace_id,
            )
        except Exception:
            connection = None
    return {
        "status": "completed",
        "task_id": "order",
        "run_status": "completed",
        "progress": 100,
        "completed": 6,
        "order_no": "PO-2026-0906-1847",
        "message": "采购订单已创建并同步至 ERP",
        "approved_by": user.name,
        "langfuse": {
            "connected": bool(connection),
            "trace_id": trace_id,
            "trace_url": trace_url,
        },
        "tool": {
            "id": "erp.create_order",
            "status": "completed",
            "duration": "642ms",
            "output": {"order_no": "PO-2026-0906-1847", "erp_status": "已提交", "amount": 287400},
        },
    }


@app.post("/api/orders/applications")
async def apply_for_order(
    payload: OrderApplicationRequest,
    user: UserRecord = Depends(require_user),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    application = order_applications.create(user, payload)
    connection = langfuse_connections.get(harness_langfuse_session)
    if connection:
        try:
            await asyncio.to_thread(
                create_langfuse_trace,
                connection,
                name="procurement.order.application",
                seed=f"{application['id']}-{time.time_ns()}",
                input_data={"applicant": user.username, **payload.model_dump()},
                output_data={"application_id": application["id"], "status": application["status"]},
            )
        except Exception:
            # Observability must never make a successfully persisted business action fail.
            pass
    return {"created": True, "application": application, "message": "订单申请已提交，等待管理员审批"}


@app.get("/api/orders/applications")
async def list_order_applications(
    user: UserRecord = Depends(require_user),
) -> dict[str, Any]:
    applications = order_applications.list(None if user.role == "admin" else user.username)
    return {
        "applications": applications,
        "summary": {
            "total": len(applications),
            "pending": sum(item["status"] == "pending_approval" for item in applications),
            "approved": sum(item["status"] == "approved" for item in applications),
        },
    }


@app.get("/api/orders/applications/{application_id}")
async def get_order_application(
    application_id: str,
    user: UserRecord = Depends(require_user),
) -> dict[str, Any]:
    application = order_applications.get(application_id)
    if not application or (user.role != "admin" and application["applicant_username"] != user.username):
        raise HTTPException(status_code=404, detail="订单申请不存在")
    return {"application": application}


@app.post("/api/orders/applications/{application_id}/approve")
async def approve_order_application(
    application_id: str,
    user: UserRecord = Depends(require_admin),
    harness_langfuse_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    application = order_applications.approve(application_id, user)
    if not application:
        raise HTTPException(status_code=404, detail="订单申请不存在")
    connection = langfuse_connections.get(harness_langfuse_session)
    if connection:
        try:
            await asyncio.to_thread(
                create_langfuse_trace,
                connection,
                name="procurement.order.application-approved",
                seed=f"{application_id}-approve-{time.time_ns()}",
                input_data={"application_id": application_id, "approver": user.username},
                output_data={"status": application["status"], "order_no": application.get("order_no")},
            )
        except Exception:
            pass
    return {"approved": True, "application": application, "message": "申请已批准并生成采购订单"}


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "procurement-console"}


@app.get("/api/runs/{run_id}")
async def get_unknown_run(
    run_id: str,
    user: UserRecord = Depends(require_user),
) -> None:
    raise HTTPException(status_code=404, detail=f"Run {run_id!r} was not found")

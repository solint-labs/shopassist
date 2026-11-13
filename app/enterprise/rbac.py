"""API keys, roles and an append-only audit log.

Every operator action goes through an API key that carries a role and (optionally) a
tenant scope. Keys are random 256-bit secrets shown once; we store only a SHA-256 hash,
so a database leak doesn't leak working keys.

    role         can...
    ──────────── ─────────────────────────────────────────────────────────────────────
    admin        everything, including key management and erasure
    supervisor   agent rights + analytics, audit log, knowledge re-index, customer profiles
    agent        read queue/transcripts, reveal PII, reply, release
    analyst      analytics, transcripts with PII still pseudonymised
    auditor      audit log, transcripts with PII still pseudonymised

Every PII reveal, human reply, release, erasure, key change and re-index is written to
`audit_log` with who, what, which tenant and when. That's what a security review or a
DPA audit will ask you for first.

SSO: put the admin surface behind your identity-aware proxy (OIDC) and map groups to
roles; the API-key layer stays as the machine-to-machine path.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import JSON, DateTime, String, select
from sqlalchemy.orm import Mapped, mapped_column

from app.logging_config import log
from app.storage.db import Base, Database

ROLES: dict[str, set[str]] = {
    "admin": {"*"},
    "supervisor": {"conversations:read", "conversations:reveal_pii", "conversations:reply", "conversations:release",
                   "analytics:read", "audit:read", "kb:reindex", "profiles:write"},
    "agent": {"conversations:read", "conversations:reveal_pii", "conversations:reply", "conversations:release"},
    "analyst": {"analytics:read", "conversations:read"},
    "auditor": {"audit:read", "conversations:read"},
}


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(32))
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # None = all tenants
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(12))  # shown in UIs to identify a key
    created_by: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    actor: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(32))
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource: Mapped[str] = mapped_column(String(200))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    tenant_id: str | None  # None = all tenants

    def can(self, permission: str) -> bool:
        perms = ROLES.get(self.role, set())
        return "*" in perms or permission in perms

    def can_access_tenant(self, tenant_id: str) -> bool:
        return self.tenant_id is None or self.tenant_id == tenant_id


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class KeyStore:
    def __init__(self, db: Database):
        self.db = db

    async def create(self, name: str, role: str, tenant_id: str | None, created_by: str) -> tuple[str, ApiKey]:
        if role not in ROLES:
            raise ValueError(f"unknown role {role}")
        key = "sa_" + secrets.token_urlsafe(32)
        row = ApiKey(name=name, role=role, tenant_id=tenant_id, key_hash=hash_key(key), prefix=key[:10],
                     created_by=created_by)
        async with self.db.session() as s:
            s.add(row)
        return key, row

    async def lookup(self, key: str) -> ApiKey | None:
        async with self.db.session() as s:
            row = (await s.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(key)))).scalar_one_or_none()
        return row if row and row.revoked_at is None else None

    async def list(self) -> list[ApiKey]:
        async with self.db.session() as s:
            return list((await s.execute(select(ApiKey).order_by(ApiKey.created_at))).scalars())

    async def revoke(self, key_id: str) -> bool:
        async with self.db.session() as s:
            row = await s.get(ApiKey, key_id)
            if row is None or row.revoked_at:
                return False
            row.revoked_at = datetime.now(timezone.utc)
            return True


class Auditor:
    def __init__(self, db: Database):
        self.db = db

    async def record(self, who: Principal, action: str, resource: str, tenant_id: str | None = None,
                     **detail) -> None:
        async with self.db.session() as s:
            s.add(AuditLog(actor=who.name, role=who.role, tenant_id=tenant_id, action=action, resource=resource,
                           detail=detail))
        log.info("audit", actor=who.name, action=action, resource=resource, tenant=tenant_id)

    async def query(self, tenant_id: str | None = None, action: str | None = None, limit: int = 200) -> list[AuditLog]:
        async with self.db.session() as s:
            q = select(AuditLog).order_by(AuditLog.at.desc()).limit(limit)
            if tenant_id:
                q = q.where(AuditLog.tenant_id == tenant_id)
            if action:
                q = q.where(AuditLog.action == action)
            return list((await s.execute(q)).scalars())


async def current_principal(request: Request, authorization: str = Header(default=""),
                            x_agent_key: str = Header(default="")) -> Principal:
    """Bearer API key -> Principal. Also accepts the bootstrap admin key and Part 3's shared agent key."""
    settings = request.app.state.settings
    key = authorization.removeprefix("Bearer ").strip() if authorization else x_agent_key
    if not key:
        raise HTTPException(401, "missing API key")
    boot = settings.admin_bootstrap_key.get_secret_value()
    if boot and secrets.compare_digest(key, boot):
        return Principal("bootstrap-admin", "admin", None)
    legacy = settings.agent_api_key.get_secret_value()
    if legacy and secrets.compare_digest(key, legacy):
        return Principal("shared-agent-key", "agent", None)
    row = await request.app.state.keys.lookup(key)
    if row is None:
        raise HTTPException(401, "invalid API key")
    return Principal(row.name, row.role, row.tenant_id)


def require(permission: str):
    async def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.can(permission):
            raise HTTPException(403, f"role '{p.role}' lacks {permission}")
        return p

    return dep

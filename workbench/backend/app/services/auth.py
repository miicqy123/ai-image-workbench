"""身份与会话：服务端认证的唯一可信入口。

原则（对应施工图纸第 1 批）：
- 身份只能来自服务端会话，不接受任何客户端自报的 user_id / role / tenant_id；
- 未提供会话时**默认拒绝**（不再默认 super_admin）；
- 会话可在服务端撤销：退出登录、管理员禁用用户后原会话立即失效；
- 密码只保存 PBKDF2-HMAC-SHA256 强哈希，不保存明文；
- 本地开发放行必须显式开启 WB_DEV_AUTH=1，且生产环境（WB_ENV=production）禁止开启。
"""
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field

from fastapi import Header, HTTPException, Request

from .. import db
from . import rbac

SESSION_COOKIE = "wb_session"
SESSION_TTL_SECONDS = int(os.environ.get("WB_SESSION_TTL", str(12 * 3600)))
PBKDF2_ITERATIONS = int(os.environ.get("WB_PBKDF2_ITERATIONS", "200000"))
COOKIE_SECURE = os.environ.get("WB_COOKIE_SECURE", "0").lower() in ("1", "true", "yes")
COOKIE_SAMESITE = (os.environ.get("WB_COOKIE_SAMESITE", "lax").strip().lower() or "lax")
if COOKIE_SAMESITE not in ("lax", "strict", "none"):
    COOKIE_SAMESITE = "lax"

WB_ENV = (os.environ.get("WB_ENV") or "development").strip().lower()
DEV_AUTH_REQUESTED = os.environ.get("WB_DEV_AUTH", "0").strip().lower() in ("1", "true", "yes")
if DEV_AUTH_REQUESTED and WB_ENV == "production":
    raise RuntimeError("WB_DEV_AUTH 不允许在生产环境（WB_ENV=production）启用")
DEV_AUTH = DEV_AUTH_REQUESTED

DEV_USER_ID = os.environ.get("WB_DEV_USER", "usr_default")


@dataclass
class Actor:
    """服务端确认过的可信身份。"""
    user_id: str
    name: str = ""
    role: str = "viewer"
    organization_id: str | None = None
    workspace_id: str | None = None
    permissions: list = field(default_factory=list)
    org_ids: list = field(default_factory=list)
    session_id: str | None = None
    dev_mode: bool = False

    def has(self, permission: str) -> bool:
        return "*" in self.permissions or permission in self.permissions


# ---------------- 密码 ----------------

def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("密码至少 8 位")
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    try:
        algo, iters, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iters))
    return hmac.compare_digest(dk.hex(), digest)


def user_has_password(user_id: str) -> bool:
    u = db.query_one("SELECT password_hash FROM users WHERE id=?", (user_id,))
    return bool(u and u.get("password_hash"))


def any_password_configured() -> bool:
    return bool(db.query_one("SELECT id FROM users WHERE password_hash IS NOT NULL AND password_hash != '' LIMIT 1"))


def set_password(user_id: str, password: str) -> None:
    ph = hash_password(password)
    u = db.query_one("SELECT id FROM users WHERE id=?", (user_id,))
    if not u:
        raise ValueError(f"用户不存在：{user_id}")
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (ph, user_id))


# ---------------- 会话 ----------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: str, ip: str = "", user_agent: str = "", workspace_id: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    now = int(db.now())
    sid = db.gen_id("ses")
    db.execute("INSERT INTO sessions(id,user_id,token_hash,workspace_id,created_at,expires_at,ip,user_agent) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (sid, user_id, _hash_token(token), workspace_id, now, now + SESSION_TTL_SECONDS, ip[:64], (user_agent or "")[:200]))
    db.execute("UPDATE users SET last_login_at=? WHERE id=?", (now, user_id))
    return token


def revoke_session(token: str) -> bool:
    if not token:
        return False
    cur = db.execute_return("UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL RETURNING id",
                            (int(db.now()), _hash_token(token)))
    return bool(cur)


def revoke_all_sessions(user_id: str) -> None:
    db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (int(db.now()), user_id))


def _session_from_token(token: str) -> dict | None:
    if not token:
        return None
    s = db.query_one("SELECT * FROM sessions WHERE token_hash=?", (_hash_token(token),))
    if not s:
        return None
    now = int(db.now())
    if s.get("revoked_at") or int(s["expires_at"] or 0) <= now:
        return None
    return s


# ---------------- 身份解析 ----------------

def _memberships(user_id: str) -> list:
    return db.query("SELECT * FROM memberships WHERE user_id=? AND status='active' ORDER BY created_at ASC", (user_id,))


def _build_actor(user_id: str, session: dict | None = None, requested_workspace: str | None = None,
                 dev_mode: bool = False) -> Actor | None:
    u = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not u or (u.get("status") or "active") != "active":
        return None
    ms = _memberships(user_id)
    chosen = None
    if requested_workspace:
        chosen = next((m for m in ms if m.get("workspace_id") == requested_workspace), None)
        if not chosen:
            return None
    if chosen is None:
        sid_ws = (session or {}).get("workspace_id")
        if sid_ws:
            chosen = next((m for m in ms if m.get("workspace_id") == sid_ws), None)
        chosen = chosen or (ms[0] if ms else None)
    role_source = (chosen or {}).get("role") or u.get("role")
    role = rbac.resolve_role(role_source)
    org_ids = []
    for m in ms:
        oid = m.get("organization_id")
        if oid and oid not in org_ids:
            org_ids.append(oid)
    if not org_ids and u.get("organization_id"):
        org_ids.append(u["organization_id"])
    return Actor(
        user_id=u["id"],
        name=u.get("name") or u["id"],
        role=role,
        organization_id=(chosen or {}).get("organization_id") or u.get("organization_id"),
        workspace_id=(chosen or {}).get("workspace_id"),
        permissions=rbac.permissions_for(role),
        org_ids=org_ids,
        session_id=(session or {}).get("id"),
        dev_mode=dev_mode,
    )


def actor_from_request(request: Request) -> Actor | None:
    """从会话 Cookie 解析可信身份；无有效会话时返回 None（除了显式开启的开发模式）。"""
    token = request.cookies.get(SESSION_COOKIE) or ""
    session = _session_from_token(token)
    if session:
        actor = _build_actor(session["user_id"], session=session,
                             requested_workspace=request.headers.get("X-WB-Workspace-Id"))
        if actor:
            return actor
        # 会话存在但用户被禁用 / 工作空间非法 → 视为未认证，并撤销该会话
        revoke_session(token)
        return None
    if DEV_AUTH:
        return _build_actor(DEV_USER_ID, dev_mode=True)
    return None


def require_actor(request: Request) -> Actor:
    actor = getattr(request.state, "actor", None) or actor_from_request(request)
    if not actor:
        raise HTTPException(401, "未登录或会话已失效")
    return actor


def require_permission(permission: str):
    """FastAPI 依赖：以服务端身份为准校验权限（不再读取任何客户端角色头）。"""
    def dep(request: Request) -> Actor:
        actor = require_actor(request)
        if not actor.has(permission):
            raise HTTPException(403, f"当前角色（{actor.role}）没有 {permission} 权限")
        return actor
    return dep


SERVICE_USER_ID = "system:service"


def service_actor(request: Request, admin: bool = True) -> Actor:
    """机器凭据对应的系统身份（仅当配置了 WB_API_TOKEN / WB_ADMIN_TOKEN 时可达）。"""
    role = "super_admin" if admin else "viewer"
    return Actor(user_id=SERVICE_USER_ID, name="服务凭据", role=role,
                 organization_id=None, workspace_id=None,
                 permissions=rbac.permissions_for(role), org_ids=[], session_id=None, dev_mode=False)


def login(user_id: str, password: str, request: Request | None = None) -> str | None:
    u = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not u or (u.get("status") or "active") != "active":
        return None
    if not verify_password(password, u.get("password_hash")):
        return None
    ip = ""
    ua = ""
    if request is not None:
        ip = request.client.host if request.client else ""
        ua = request.headers.get("user-agent", "")
    return create_session(u["id"], ip=ip, user_agent=ua)


def me(actor: Actor) -> dict:
    ms = db.query("SELECT id,workspace_id,organization_id,role,status FROM memberships WHERE user_id=? AND status='active'",
                  (actor.user_id,))
    for m in ms:
        m["role"] = rbac.resolve_role(m.get("role"))
    org = db.query_one("SELECT id,name,plan FROM organizations WHERE id=?", (actor.organization_id,)) if actor.organization_id else None
    return {
        "user": {"id": actor.user_id, "name": actor.name, "status": "active"},
        "role": actor.role,
        "role_name": rbac.role_def(actor.role)["name"],
        "organization": org,
        "memberships": ms,
        "active_workspace_id": actor.workspace_id,
        "permissions": actor.permissions,
        "scope": rbac.role_scope(actor.role),
        "organizations": [{"id": oid, "name": (db.query_one("SELECT name FROM organizations WHERE id=?", (oid,)) or {}).get("name")}
                          for oid in (actor.org_ids or [])],
        "dev_mode": actor.dev_mode,
    }

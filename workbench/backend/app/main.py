"""FastAPI 主应用：企业营销生图工作台 MVP 后端。

实现 PRD v1/v2 的核心 API：项目/资产/图（强类型端口、DAG）/节点（编辑、AI 局部修改、应用候选、
运行）/运行进度 SSE/模型注册/导出（项目 JSON + 分层 PNG + 素材包）。版本乐观锁、幂等键、上游 stale 标记。
"""
import hashlib
import io
import json
import sqlite3
import os
import re
import urllib.request
import threading
import time
import zipfile
from collections import defaultdict, deque
from PIL import Image, ImageDraw
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse, Response
from pydantic import BaseModel
from typing import Optional, Union

from . import db, storage
from . import registry
from .generators import text as text_gen
from .generators import image as image_gen
from .services import canvas_renderer
from .services import model_router
from .services import prompt_compiler
from .services import metering
from .services import compliance
from .services import notify as notify_svc
from .services import rbac
from .services import auth
from .services import authz

app = FastAPI(title="AI 多节点产品营销生图工作台", version="0.1.0")
_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "WB_ALLOWED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000",
).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS, allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)

WB_API_TOKEN = os.environ.get("WB_API_TOKEN", "").strip()
WB_ADMIN_TOKEN = os.environ.get("WB_ADMIN_TOKEN", "").strip()
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

ADMIN_PATH_PREFIXES = ("/api/providers", "/api/models", "/api/admin")


def _is_admin_path(path: str) -> bool:
    return any(path.startswith(p) for p in ADMIN_PATH_PREFIXES)


PUBLIC_PATHS = ("/api/health", "/api/auth/login", "/api/auth/bootstrap", "/api/auth/bootstrap-state")


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS


def _service_token(request) -> str | None:
    """机器凭据识别：返回 'admin' / 'api' / None。

    这是**服务账号**，不是用户身份，只能由部署方配置的密钥获得；未配置时该通道关闭。
    """
    if not (WB_API_TOKEN or WB_ADMIN_TOKEN):
        return None
    got = request.headers.get("Authorization", "")
    if not got:
        return None
    if WB_ADMIN_TOKEN and got == f"Bearer {WB_ADMIN_TOKEN}":
        return "admin"
    if WB_API_TOKEN and got == f"Bearer {WB_API_TOKEN}":
        return "api"
    return None


def _protected(path: str) -> bool:
    return path.startswith("/api") or path.startswith("/files")


@app.middleware("http")
async def auth_middleware(request, call_next):
    """统一认证入口：/api 与 /files 必须持有有效会话（或显式开启的开发模式）。"""
    path = request.url.path
    if request.method == "OPTIONS" or _is_public(path) or not _protected(path):
        return await call_next(request)
    actor = auth.actor_from_request(request)
    if actor is None:
        kind = _service_token(request)
        if kind:
            request.state.actor = auth.service_actor(request, admin=(kind == "admin"))
            return await call_next(request)
        return JSONResponse({"detail": "未登录或会话已失效"}, status_code=401)
    request.state.actor = actor
    return await call_next(request)


try:
    _RATE_LIMIT = int(os.environ.get("WB_RATE_LIMIT", "600"))
except ValueError:
    _RATE_LIMIT = 600
_RATE_WINDOW = 60.0
_rate_hits = defaultdict(deque)
_rate_lock = threading.Lock()


# ---------------- CSRF 防护：同源校验 ----------------
# 说明：
# - 只有"环境凭据"（Cookie 会话）才会被 CSRF 利用；Bearer/机器凭据不属于环境凭据。
# - 判断顺序：Origin → Referer → 都没有时，带会话的修改请求一律拒绝。
# - 反向代理场景：仅当显式设置 WB_TRUST_PROXY=1 时才采信 X-Forwarded-Host / X-Forwarded-Proto，
#   否则一律用真实 Host，避免伪造转发头绕过校验。
# - WB_CSRF_ALLOW_NO_ORIGIN=1 仅供本地脚本调试，生产不要开启。
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_TRUST_PROXY = os.environ.get("WB_TRUST_PROXY", "0").strip().lower() in ("1", "true", "yes")
_CSRF_ALLOW_NO_ORIGIN = os.environ.get("WB_CSRF_ALLOW_NO_ORIGIN", "0").strip().lower() in ("1", "true", "yes")
TRUSTED_ORIGINS = {o.strip().lower().rstrip("/") for o in os.environ.get("WB_TRUSTED_ORIGINS", "").split(",") if o.strip()}


def _origin_only(value: str | None) -> str:
    if not value:
        return ""
    m = re.match(r"^(https?://[^/]+)", value.strip(), re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _expected_origin(request) -> str:
    if _TRUST_PROXY:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    else:
        host = request.headers.get("host") or ""
        proto = request.url.scheme
    return f"{proto}://{host}".lower() if host else ""


@app.middleware("http")
async def csrf_middleware(request, call_next):
    path = request.url.path
    if request.method in _CSRF_SAFE_METHODS or not _protected(path) or _is_public(path):
        return await call_next(request)
    if _CSRF_ALLOW_NO_ORIGIN:
        return await call_next(request)
    origin = _origin_only(request.headers.get("origin")) or _origin_only(request.headers.get("referer"))
    expected = _expected_origin(request)
    if origin:
        if origin == expected or origin in TRUSTED_ORIGINS:
            return await call_next(request)
        return JSONResponse({"detail": "跨站请求被拒绝（CSRF 同源校验未通过）"}, status_code=403)
    # 无 Origin/Referer：只有非 Cookie 凭据（Bearer / 匿名）才放行
    if request.cookies.get(auth.SESSION_COOKIE):
        return JSONResponse({"detail": "缺少 Origin/Referer 的会话修改请求已被拒绝（CSRF 防护）"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def rate_limit_middleware(request, call_next):
    if request.url.path.startswith("/api") and request.url.path != "/api/health":
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        with _rate_lock:
            q = _rate_hits[ip]
            while q and now - q[0] > _RATE_WINDOW:
                q.popleft()
            if len(q) >= _RATE_LIMIT:
                return JSONResponse({"detail": "请求过于频繁，请稍后再试"}, status_code=429)
            q.append(now)
    return await call_next(request)


def _init():
    db.init_db()
    registry.seed_models()
    storage.ensure_dirs()


_init()


# ---------------- helpers ----------------
def json_loads(s, default=None):
    if s is None or s == "":
        return default if default is not None else {}
    return json.loads(s)


def get_current_version(node_id):
    row = db.query_one("SELECT * FROM node_versions WHERE node_id=? ORDER BY version DESC LIMIT 1", (node_id,))
    if not row:
        return None
    return json_loads(row["content_json"], {})


def set_node_version(node_id, content, locks=None, author_type="human", model_id=None, input_snapshot=None):
    with db.tx() as conn:
        cur = conn.execute("SELECT current_version FROM nodes WHERE id=?", (node_id,)).fetchone()
        nv = (cur["current_version"] if cur else 0) + 1
        conn.execute(
            "INSERT INTO node_versions(node_id,version,input_snapshot_json,content_json,locks_json,author_type,model_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (node_id, nv, json.dumps(input_snapshot or {}), json.dumps(content), json.dumps(locks or []),
             author_type, model_id, db.now()),
        )
        conn.execute("UPDATE nodes SET current_version=?, status='ready' WHERE id=?", (nv, node_id))
    return nv


def downstream_node_ids(node_id):
    """BFS 下游节点 id 列表（不含自身）。"""
    seen = []
    visited = {node_id}
    frontier = [node_id]
    while frontier:
        n = frontier.pop()
        edges = db.query("SELECT to_node FROM edges WHERE from_node=?", (n,))
        for e in edges:
            t = e["to_node"]
            if t not in visited:
                visited.add(t)
                seen.append(t)
                frontier.append(t)
    return seen


def mark_stale(node_id):
    for d in downstream_node_ids(node_id):
        db.execute("UPDATE nodes SET status='stale' WHERE id=? AND status<>'stale'", (d,))


# ---------------- projects ----------------
class ProjectCreate(BaseModel):
    name: str
    tenant_id: str = "tnt_default"
    template_id: Optional[str] = None


@app.get("/api/health")
def health():
    # inflight_unique_index：数据库级“同节点最多一条在途运行”约束是否已生效（跨进程防双跑的依据）
    index_ok = bool(getattr(db, "INFLIGHT_INDEX_OK", False))
    reason = "" if index_ok else (
        "数据库级“同一节点最多一条在途运行”约束未生效（uq_node_runs_one_inflight）："
        "服务端拒绝创建任何新的运行（单节点与下游均拒绝），历史数据仍可只读访问；"
        "不提供绕过开关，也不会自动清理冲突在途记录。原因：" + (getattr(db, "INFLIGHT_INDEX_ERROR", "") or "index_missing"))
    return {"status": "ok", "time": db.now(),
            "inflight_unique_index": index_ok,
            "inflight_index_error": getattr(db, "INFLIGHT_INDEX_ERROR", "") or "",
            "runnable": index_ok,
            "not_runnable_reason": reason}


# ---------------- 认证：唯一可信身份入口 ----------------
class LoginIn(BaseModel):
    user_id: str
    password: str


class BootstrapIn(BaseModel):
    user_id: str = "usr_default"
    password: str
    name: Optional[str] = None


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str


def _set_session_cookie(resp: JSONResponse, token: str) -> None:
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL_SECONDS, httponly=True,
                    samesite=auth.COOKIE_SAMESITE, secure=auth.COOKIE_SECURE, path="/")


@app.get("/api/auth/bootstrap-state")
def auth_bootstrap_state():
    """前端用来判断是否需要首次初始化管理员密码。"""
    return {"needs_bootstrap": not auth.any_password_configured(), "dev_mode": auth.DEV_AUTH,
            "env": auth.WB_ENV, "session_ttl": auth.SESSION_TTL_SECONDS}


@app.post("/api/auth/bootstrap", status_code=201)
def auth_bootstrap(body: BootstrapIn):
    """首次初始化：仅当系统内没有任何用户设置过密码时可用，之后永久关闭。"""
    if auth.any_password_configured():
        raise HTTPException(409, "系统已完成初始化，请联系管理员重置密码")
    if not db.query_one("SELECT id FROM users WHERE id=?", (body.user_id,)):
        now = int(db.now())
        db.execute("INSERT INTO users(id,tenant_id,role,name,email,status,organization_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (body.user_id, "tnt_default", "super_admin", body.name or body.user_id, "",
                    "active", "org_default", now))
    try:
        auth.set_password(body.user_id, body.password)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.audit(None, body.user_id, "auth.bootstrap", body.user_id)
    token = auth.create_session(body.user_id)
    resp = JSONResponse({"ok": True, "user_id": body.user_id, "role": "super_admin"}, status_code=201)
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request):
    token = auth.login(body.user_id, body.password, request)
    if not token:
        db.audit(None, body.user_id or "", "auth.login.failed", body.user_id or "")
        raise HTTPException(401, "用户名或密码错误，或账号已被禁用")
    actor = auth._build_actor(body.user_id)
    db.audit(None, body.user_id, "auth.login", body.user_id)
    resp = JSONResponse({"ok": True, "user_id": body.user_id, "role": actor.role if actor else None})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    auth.revoke_session(request.cookies.get(auth.SESSION_COOKIE) or "")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def auth_me(request: Request):
    return auth.me(auth.require_actor(request))


@app.post("/api/auth/change-password")
def auth_change_password(body: PasswordChangeIn, request: Request):
    actor = auth.require_actor(request)
    u = db.query_one("SELECT password_hash FROM users WHERE id=?", (actor.user_id,))
    if not auth.verify_password(body.old_password, (u or {}).get("password_hash")):
        raise HTTPException(401, "原密码不正确")
    try:
        auth.set_password(actor.user_id, body.new_password)
    except ValueError as e:
        raise HTTPException(422, str(e))
    auth.revoke_all_sessions(actor.user_id)
    db.audit(None, actor.user_id, "auth.password.change", actor.user_id)
    return {"ok": True, "message": "密码已更新，所有会话已失效，请重新登录"}


@app.post("/api/projects")
def create_project(body: ProjectCreate, request: Request):
    actor = auth.require_actor(request)
    pid = db.gen_id("prj")
    # 归属来自服务端身份，不再采信请求体里的 tenant_id / owner
    db.execute("INSERT INTO projects(id,tenant_id,owner_id,name,status,organization_id,template_id,created_at,updated_at) "
               "VALUES(?,?,?,?,?,?,?,?,?)",
               (pid, actor.organization_id or body.tenant_id, actor.user_id, body.name, "active",
                actor.organization_id or body.tenant_id, body.template_id, db.now(), db.now()))
    gid = db.gen_id("grf")
    db.execute("INSERT INTO graphs(id,project_id,current_version) VALUES(?,?,1)", (gid, pid))
    return {"id": pid, "name": body.name, "graph_id": gid}


@app.get("/api/projects")
def list_projects(request: Request):
    actor = auth.require_actor(request)
    clause, args = authz.org_scope_clause(actor, "organization_id")
    return db.query("SELECT id,name,status,created_at,organization_id FROM projects WHERE 1=1" + clause
                    + " ORDER BY created_at DESC", tuple(args))


@app.get("/api/projects/{pid}")
def get_project(pid: str, request: Request):
    actor = auth.require_actor(request)
    p = authz.require_project_permission(actor, pid, "project.read")
    p = dict(p)
    p["graph"] = db.query_one("SELECT * FROM graphs WHERE project_id=?", (pid,))
    return p


@app.delete("/api/projects/{pid}")
def delete_project(pid: str, request: Request):
    actor = auth.require_actor(request)
    authz.require_project_permission(actor, pid, "project.delete")
    file_keys = []
    # 与 _create_node_run 共用 BEGIN IMMEDIATE：检查在途与删除在同一事务内，互斥（跨进程）
    with db.tx(immediate=True) as conn:
        g0 = conn.execute("SELECT id FROM graphs WHERE project_id=?", (pid,)).fetchone()
        if g0:
            runs = _runs_of_graph(conn, g0["id"])
            if runs:
                raise _conflict_with_run(actor, runs[0], "project_has_inflight_run",
                                         "该项目存在进行中的运行，已拒绝删除项目（数据库与文件均未改动）。")
        asset_ids = [a["id"] for a in conn.execute("SELECT id FROM assets WHERE project_id=?", (pid,)).fetchall()]
        conn.execute("DELETE FROM canvas_layers WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM agent_plans WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM audit_events WHERE project_id=?", (pid,))
        for aid in asset_ids:
            conn.execute("DELETE FROM export_records WHERE image_asset_id=?", (aid,))
        g = conn.execute("SELECT id FROM graphs WHERE project_id=?", (pid,)).fetchone()
        if g:
            node_ids = [n["id"] for n in conn.execute("SELECT id FROM nodes WHERE graph_id=?", (g["id"],)).fetchall()]
            for nid in node_ids:
                conn.execute("DELETE FROM node_versions WHERE node_id=?", (nid,))
                conn.execute("DELETE FROM node_candidates WHERE node_id=?", (nid,))
                for r in conn.execute("SELECT id FROM node_runs WHERE node_id=?", (nid,)).fetchall():
                    conn.execute("DELETE FROM run_outputs WHERE run_id=?", (r["id"],))
                    conn.execute("DELETE FROM run_inputs WHERE run_id=?", (r["id"],))
                    conn.execute("DELETE FROM node_runs WHERE id=?", (r["id"],))
                conn.execute("DELETE FROM nodes WHERE id=?", (nid,))
            conn.execute("DELETE FROM edges WHERE graph_id=?", (g["id"],))
            conn.execute("DELETE FROM graphs WHERE id=?", (g["id"],))
        for a in conn.execute("SELECT id,object_key FROM assets WHERE project_id=?", (pid,)).fetchall():
            file_keys.append(a["object_key"])
            conn.execute("DELETE FROM assets WHERE id=?", (a["id"],))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    # 事务提交成功后才删除磁盘文件（拒绝/回滚时文件保持原样）
    for key in file_keys:
        try:
            storage.delete(key)
        except Exception:
            pass
    return {"ok": True}


class ProjectRename(BaseModel):
    name: str


@app.patch("/api/projects/{pid}")
def rename_project(pid: str, body: ProjectRename, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.write")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "项目名不能为空")
    db.execute("UPDATE projects SET name=?, updated_at=? WHERE id=?", (name, db.now(), pid))
    return {"ok": True, "name": name}


@app.get("/api/projects/{pid}/defaults")
def get_defaults(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.read")
    p = db.query_one("SELECT default_text_model, default_image_model FROM projects WHERE id=?", (pid,))
    return {"default_text_model": p["default_text_model"] or "", "default_image_model": p["default_image_model"] or ""}


class DefaultsUpdate(BaseModel):
    default_text_model: Optional[str] = None
    default_image_model: Optional[str] = None


@app.put("/api/projects/{pid}/defaults")
def put_defaults(pid: str, body: DefaultsUpdate, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.write")
    p = db.query_one("SELECT id FROM projects WHERE id=?", (pid,))
    fields = {}
    if body.default_text_model is not None:
        if body.default_text_model:
            m = registry.get_model(body.default_text_model)
            if not m or m["enabled"] != 1 or m["modality"] != "text":
                raise HTTPException(422, "默认文本模型必须是已启用的文本模型")
        fields["default_text_model"] = body.default_text_model
    if body.default_image_model is not None:
        if body.default_image_model:
            m = registry.get_model(body.default_image_model)
            if not m or m["enabled"] != 1 or m["modality"] != "image":
                raise HTTPException(422, "默认图片模型必须是已启用的图片模型")
        fields["default_image_model"] = body.default_image_model
    if fields:
        setc = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE projects SET {setc} WHERE id=?", tuple(fields.values()) + (pid,))
    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (db.now(), pid))
    return get_defaults(pid)


# ---------------- generation brief（唯一生图输入源） ----------------
class BriefIn(BaseModel):
    user_prompt: str = ""
    purpose: str = "marketing_poster"
    platform: str = "xiaohongshu"
    aspect_ratio: str = "3:4"
    image_count: int = 4
    selected_model_id: Optional[str] = None
    selected_prompt_template_id: Optional[str] = None
    reference_asset_ids: list = []
    product_asset_ids: list = []
    style_keywords: list = []
    brand_keywords: list = []


def _brief_dict(r):
    return {
        "id": r["id"], "project_id": r["project_id"], "user_prompt": r["user_prompt"],
        "purpose": r["purpose"], "platform": r["platform"], "aspect_ratio": r["aspect_ratio"],
        "image_count": r["image_count"], "selected_model_id": r["selected_model_id"],
        "selected_prompt_template_id": r["selected_prompt_template_id"],
        "reference_asset_ids": json_loads(r["reference_asset_ids_json"], []),
        "product_asset_ids": json_loads(r["product_asset_ids_json"], []),
        "style_keywords": json_loads(r["style_keywords_json"], []),
        "brand_keywords": json_loads(r["brand_keywords_json"], []),
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def _brief_vals(b: BriefIn):
    return (b.user_prompt, b.purpose, b.platform, b.aspect_ratio, b.image_count,
            b.selected_model_id, b.selected_prompt_template_id,
            json.dumps(b.reference_asset_ids), json.dumps(b.product_asset_ids),
            json.dumps(b.style_keywords), json.dumps(b.brand_keywords))


BRIEF_COLS = ("id,project_id,user_prompt,purpose,platform,aspect_ratio,image_count,selected_model_id,"
              "selected_prompt_template_id,reference_asset_ids_json,product_asset_ids_json,"
              "style_keywords_json,brand_keywords_json,created_at,updated_at")


@app.get("/api/projects/{pid}/brief")
def get_brief(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.read")
    r = db.query_one("SELECT * FROM generation_briefs WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,))
    if not r:
        raise HTTPException(404, "brief 不存在")
    return _brief_dict(r)


@app.post("/api/projects/{pid}/brief", status_code=201)
def create_brief(pid: str, body: BriefIn, request: Request):
    actor = auth.require_actor(request)
    authz.require_project_permission(actor, pid, "project.write")
    _guard_brief_inflight(pid, actor)
    authz.assert_assets_in_project(pid, list(body.product_asset_ids or []) + list(body.reference_asset_ids or []),
                                   field="generation_brief")
    bid = db.gen_id("brief"); now = int(db.now())
    db.execute("INSERT INTO generation_briefs(" + BRIEF_COLS + ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (bid, pid) + _brief_vals(body) + (now, now))
    return _brief_dict(db.query_one("SELECT * FROM generation_briefs WHERE id=?", (bid,)))


@app.put("/api/projects/{pid}/brief")
def upsert_brief(pid: str, body: BriefIn, request: Request):
    actor = auth.require_actor(request)
    authz.require_project_permission(actor, pid, "project.write")
    _guard_brief_inflight(pid, actor)
    authz.assert_assets_in_project(pid, list(body.product_asset_ids or []) + list(body.reference_asset_ids or []),
                                   field="generation_brief")
    now = int(db.now())
    ex = db.query_one("SELECT id FROM generation_briefs WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,))
    if ex:
        db.execute("UPDATE generation_briefs SET user_prompt=?,purpose=?,platform=?,aspect_ratio=?,image_count=?,"
                   "selected_model_id=?,selected_prompt_template_id=?,reference_asset_ids_json=?,product_asset_ids_json=?,"
                   "style_keywords_json=?,brand_keywords_json=?,updated_at=? WHERE id=?",
                   _brief_vals(body) + (now, ex["id"]))
        return _brief_dict(db.query_one("SELECT * FROM generation_briefs WHERE id=?", (ex["id"],)))
    bid = db.gen_id("brief")
    db.execute("INSERT INTO generation_briefs(" + BRIEF_COLS + ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (bid, pid) + _brief_vals(body) + (now, now))
    return _brief_dict(db.query_one("SELECT * FROM generation_briefs WHERE id=?", (bid,)))


# ---------------- 模板中心 / Prompt 管理 ----------------
SEED_TEMPLATES = [
    ("b1", "Logo / VI 设计", "品牌标识与视觉规范", "brand", "品牌｜Logo与VI", "illustration", "1:1", "#7C3AED"),
    ("b2", "品牌视觉系统", "色彩 / 字体 / 版式规范", "brand", "品牌｜视觉系统", "illustration", "4:3", "#6366F1"),
    ("m1", "品牌形象海报", "品牌主张 / 价值表达", "marketing", "营销｜品牌海报", "poster", "3:4", "#7C3AED"),
    ("m2", "产品卖点海报", "主卖点 + 场景 + 证明", "marketing", "营销｜产品海报", "poster", "1:1", "#3B82F6"),
    ("m3", "服务承诺海报", "风险反转 / 无醛承诺", "marketing", "营销｜服务海报", "poster", "3:4", "#10B981"),
    ("m4", "活动传播海报", "活动主题 + 节点 + 权益", "marketing", "营销｜活动海报", "poster", "3:4", "#EF4444"),
    ("m5", "促销转化海报", "算账 + 权益 + 限时", "marketing", "营销｜促销海报", "poster", "3:4", "#F59E0B"),
    ("m6", "信任背书海报", "检测 / 人物 / 案例", "marketing", "营销｜背书/案例海报", "poster", "3:4", "#6366F1"),
    ("m7", "系列节点海报", "发布会 / 倒计时 / 悬念", "marketing", "营销｜系列节点海报", "poster", "3:4", "#8B5CF6"),
    ("c1", "视频号 / 抖音封面", "竖版封面 · 9:16", "content", "内容｜视频封面", "cover", "9:16", "#F59E0B"),
    ("c2", "公众号封面", "大字标题 · 16:9", "content", "内容｜图文封面", "cover", "16:9", "#10B981"),
    ("c3", "小红书图文封面", "种草 / 攻略 · 3:4", "content", "内容｜图文封面", "cover", "3:4", "#EC4899"),
    ("c4", "朋友圈海报", "悬念 / 金句 · 3:4", "content", "内容｜图文封面", "cover", "3:4", "#3B82F6"),
    ("c5", "会后成果长图", "结论 + 过程 + 成果", "content", "内容｜传播长图", "long", "a4", "#3B82F6"),
    ("c6", "攻略 / 科普信息图", "误区 + 判断工具", "content", "内容｜信息图", "long", "a4", "#14B8A6"),
    ("c7", "社媒轮播图", "多张轮播 · 3:4", "content", "内容｜社媒轮播图", "detail", "3:4", "#EC4899"),
    ("e1", "电商主图套图", "全套主图 · 1:1", "ecommerce", "电商｜电商主图", "detail", "1:1", "#7C3AED"),
    ("e2", "商品详情页", "参数 / 卖点 / 场景", "ecommerce", "电商｜详情页", "detail", "a4", "#6366F1"),
    ("e3", "商品场景图", "场景适配 · 多尺寸", "ecommerce", "电商｜场景图", "poster", "3:4", "#EC4899"),
    ("e4", "细节 / 参数图", "特写 + 参数标注", "ecommerce", "电商｜细节/参数图", "poster", "1:1", "#14B8A6"),
    ("i1", "场景插图", "生活场景 / 使用示意", "illustration", "素材｜内容插图", "illustration", "1:1", "#8B5CF6"),
    ("i2", "编辑插图", "图文配图 / 栏目插图", "illustration", "素材｜内容插图", "illustration", "4:3", "#14B8A6"),
    ("i3", "角色 / IP 与吉祥物", "品牌 IP 形象", "illustration", "素材｜角色IP", "illustration", "1:1", "#F59E0B"),
]


def seed_templates():
    if db.query_one("SELECT id FROM templates LIMIT 1"):
        return
    now = int(db.now())
    for t in SEED_TEMPLATES:
        db.execute("INSERT INTO templates(id,name,subtitle,level1,level2,skeleton,aspect,accent,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,1,?)",
                   t + (now,))


@app.get("/api/templates")
def list_templates():
    seed_templates()
    return db.query("SELECT id,name,subtitle,level1,level2,skeleton,aspect,accent,enabled,require_review FROM templates WHERE enabled=1 ORDER BY created_at ASC")


class TemplateIn(BaseModel):
    id: Optional[str] = None
    name: str
    subtitle: str = ""
    level1: str = "marketing"
    level2: str = ""
    skeleton: str = "poster"
    aspect: str = "3:4"
    accent: str = "#7C3AED"
    enabled: int = 1


@app.get("/api/admin/templates")
def admin_list_templates():
    seed_templates()
    return db.query("SELECT * FROM templates ORDER BY created_at ASC")


@app.post("/api/admin/templates", status_code=201)
def admin_create_template(body: TemplateIn):
    tid = body.id or db.gen_id("tpl")
    if db.query_one("SELECT id FROM templates WHERE id=?", (tid,)):
        raise HTTPException(400, "模板 ID 已存在")
    db.execute("INSERT INTO templates(id,name,subtitle,level1,level2,skeleton,aspect,accent,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (tid, body.name, body.subtitle, body.level1, body.level2, body.skeleton, body.aspect, body.accent, int(body.enabled), int(db.now())))
    return db.query_one("SELECT * FROM templates WHERE id=?", (tid,))


@app.put("/api/admin/templates/{tid}")
def admin_update_template(tid: str, body: TemplateIn):
    if not db.query_one("SELECT id FROM templates WHERE id=?", (tid,)):
        raise HTTPException(404, "模板不存在")
    db.execute("UPDATE templates SET name=?,subtitle=?,level1=?,level2=?,skeleton=?,aspect=?,accent=?,enabled=? WHERE id=?",
               (body.name, body.subtitle, body.level1, body.level2, body.skeleton, body.aspect, body.accent, int(body.enabled), tid))
    return db.query_one("SELECT * FROM templates WHERE id=?", (tid,))


class TemplatePatch(BaseModel):
    name: Optional[str] = None
    subtitle: Optional[str] = None
    level1: Optional[str] = None
    level2: Optional[str] = None
    skeleton: Optional[str] = None
    aspect: Optional[str] = None
    accent: Optional[str] = None
    enabled: Optional[int] = None
    require_review: Optional[int] = None


@app.patch("/api/admin/templates/{tid}")
def admin_patch_template(tid: str, body: TemplatePatch):
    if not db.query_one("SELECT id FROM templates WHERE id=?", (tid,)):
        raise HTTPException(404, "模板不存在")
    cols = ("name", "subtitle", "level1", "level2", "skeleton", "aspect", "accent", "enabled", "require_review")
    fields, args = [], []
    for c in cols:
        v = getattr(body, c)
        if v is not None:
            fields.append(f"{c}=?"); args.append(int(v) if c in ("enabled", "require_review") else v)
    if fields:
        db.execute(f"UPDATE templates SET {','.join(fields)} WHERE id=?", tuple(args) + (tid,))
        db.audit(None, "usr_default", "template.update", tid)
    return db.query_one("SELECT * FROM templates WHERE id=?", (tid,))


@app.delete("/api/admin/templates/{tid}")
def admin_delete_template(tid: str):
    db.execute("DELETE FROM templates WHERE id=?", (tid,))
    return {"ok": True}


class PromptTemplateIn(BaseModel):
    id: Optional[str] = None
    name: str
    category: str = ""
    template_text: str
    model_hint: str = ""
    enabled: int = 1


SEED_PROMPT_TEMPLATES = [
    ("pt1", "夏日清爽产品图", "夏季营销", "生成「{user_prompt}」。夏日清爽风格，明亮正午自然光，产品居中，背景浅蓝与白色渐变，画面干净有气泡感。比例 {aspect_ratio}，适配 {platform}。", "local-poster-compositor"),
    ("pt2", "高端商拍产品图", "高端商拍", "生成「{user_prompt}」。高端商业产品摄影，柔光棚拍，低饱和高级配色，材质细节清晰，留白充足，适合电商主图。比例 {aspect_ratio}。", "local-poster-compositor"),
    ("pt3", "小红书种草封面", "社媒封面", "生成「{user_prompt}」。小红书种草风封面，大字标题留白区在顶部，暖色生活场景，突出卖点：{style_keywords}。比例 {aspect_ratio}。", "local-poster-compositor"),
]


def seed_prompt_templates():
    if db.query_one("SELECT id FROM prompt_templates LIMIT 1"):
        return
    now = int(db.now())
    for t in SEED_PROMPT_TEMPLATES:
        db.execute("INSERT INTO prompt_templates(id,name,category,template_text,model_hint,enabled,created_at) VALUES(?,?,?,?,?,1,?)", t + (now,))


@app.get("/api/prompt-templates")
def list_prompt_templates_public():
    seed_prompt_templates()
    return db.query("SELECT id,name,category,template_text,model_hint FROM prompt_templates WHERE enabled=1 ORDER BY created_at ASC")


@app.get("/api/admin/prompt-templates")
def admin_list_prompt_templates():
    return db.query("SELECT * FROM prompt_templates ORDER BY created_at DESC")


@app.post("/api/admin/prompt-templates", status_code=201)
def admin_create_prompt_template(body: PromptTemplateIn):
    pid = body.id or db.gen_id("ptpl")
    db.execute("INSERT INTO prompt_templates(id,name,category,template_text,model_hint,enabled,created_at) VALUES(?,?,?,?,?,?,?)",
               (pid, body.name, body.category, body.template_text, body.model_hint, int(body.enabled), int(db.now())))
    return db.query_one("SELECT * FROM prompt_templates WHERE id=?", (pid,))


@app.delete("/api/admin/prompt-templates/{pid}")
def admin_delete_prompt_template(pid: str, request: Request):
    actor = auth.require_actor(request)
    with db.tx(immediate=True) as conn:
        graphs = [r["graph_id"] for r in conn.execute(
            "SELECT DISTINCT n.graph_id AS graph_id FROM node_runs r JOIN nodes n ON n.id=r.node_id "
            "WHERE r.status IN ('queued','running')").fetchall()]
        for gid in graphs:
            for run in _runs_of_graph(conn, gid):
                for nid in ({run["node_id"]} | run["up"]):
                    c = _node_content_conn(conn, nid)
                    if c.get("prompt_template_id") == pid:
                        raise _conflict_with_run(actor, run, "template_in_use_inflight",
                                                 "该提示词模板正被进行中的运行使用，运行期间禁止删除。")
        conn.execute("DELETE FROM prompt_templates WHERE id=?", (pid,))
    return {"ok": True}
# ---------------- 前台工作台 ----------------
@app.get("/api/creator/dashboard")
def creator_dashboard(request: Request):
    actor = auth.require_actor(request)
    clause, args = authz.org_scope_clause(actor, "organization_id")
    projects = db.query("SELECT id,name,status,created_at,organization_id FROM projects WHERE 1=1" + clause
                        + " ORDER BY created_at DESC", tuple(args))
    visible = {p["id"] for p in projects}
    jobs = [j for j in db.query("SELECT project_id,status FROM generation_jobs") if j["project_id"] in visible]
    briefs = {b["project_id"] for b in db.query("SELECT DISTINCT project_id FROM generation_briefs")
              if b["project_id"] in visible}
    out = []
    for p in projects:
        pj = [j for j in jobs if j["project_id"] == p["id"]]
        if p["status"] == "archived":
            st = "archived"
        elif any(j["status"] in ("queued", "running") for j in pj):
            st = "generating"
        elif any(j["status"] == "succeeded" for j in pj):
            st = "completed"
        else:
            st = "draft"
        out.append({**p, "derived_status": st, "has_brief": p["id"] in briefs})
    by = {}
    for p in out:
        by[p["derived_status"]] = by.get(p["derived_status"], 0) + 1
    return {"count": len(out), "by_status": by, "recent": out[:8], "projects": out}


@app.post("/api/projects/{pid}/archive")
def archive_project(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.write")
    db.execute("UPDATE projects SET status='archived', updated_at=? WHERE id=?", (db.now(), pid))
    return {"ok": True}


@app.post("/api/projects/{pid}/restore")
def restore_project(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.write")
    db.execute("UPDATE projects SET status='active', updated_at=? WHERE id=?", (db.now(), pid))
    return {"ok": True}


@app.post("/api/projects/{pid}/duplicate", status_code=201)
def duplicate_project(pid: str, request: Request):
    actor = auth.require_actor(request)
    src = authz.require_project_permission(actor, pid, "project.create")
    npid = db.gen_id("prj")
    db.execute("INSERT INTO projects(id,tenant_id,owner_id,name,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
               (npid, src["tenant_id"], src["owner_id"], src["name"] + " 副本", "active", db.now(), db.now()))
    gid = db.gen_id("grf")
    db.execute("INSERT INTO graphs(id,project_id,current_version) VALUES(?,?,1)", (gid, npid))
    b = db.query_one("SELECT * FROM generation_briefs WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,))
    if b:
        nb = db.gen_id("brief")
        db.execute("INSERT INTO generation_briefs(id,project_id,user_prompt,purpose,platform,aspect_ratio,image_count,selected_model_id,selected_prompt_template_id,reference_asset_ids_json,product_asset_ids_json,style_keywords_json,brand_keywords_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (nb, npid, b["user_prompt"], b["purpose"], b["platform"], b["aspect_ratio"], b["image_count"],
                    b["selected_model_id"], b["selected_prompt_template_id"], b["reference_asset_ids_json"],
                    b["product_asset_ids_json"], b["style_keywords_json"], b["brand_keywords_json"], int(db.now()), int(db.now())))
    return {"id": npid, "graph_id": gid}


# ---------------- 生图任务（jobs / candidates） ----------------
class JobIn(BaseModel):
    model_id: Optional[str] = None
    task_type: str = "text_to_image"


@app.post("/api/projects/{pid}/generation-jobs", status_code=201)
def create_generation_job(pid: str, body: JobIn, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "generation.run")
    brief = db.query_one("SELECT * FROM generation_briefs WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,))
    if brief:
        authz.assert_assets_in_project(
            pid,
            json_loads(brief["product_asset_ids_json"], []) + json_loads(brief["reference_asset_ids_json"], []),
            field="generation_brief")
    if not brief:
        raise HTTPException(422, "请先保存 GenerationBrief")
    pv_no = (db.query_one("SELECT count(*) c FROM prompt_versions WHERE project_id=?", (pid,)) or {"c": 0})["c"] + 1
    pv_id = db.gen_id("pv")
    tpl = None
    if brief["selected_prompt_template_id"]:
        tpl = db.query_one("SELECT * FROM prompt_templates WHERE id=?", (brief["selected_prompt_template_id"],))
        if tpl is None:
            seed_prompt_templates()
            tpl = db.query_one("SELECT * FROM prompt_templates WHERE id=?", (brief["selected_prompt_template_id"],))
    compiled = prompt_compiler.compile_prompt((tpl or {}).get("template_text"), dict(brief))
    structured = {"user_prompt": brief["user_prompt"], "platform": brief["platform"], "aspect_ratio": brief["aspect_ratio"],
                  "template_id": brief["selected_prompt_template_id"], "compiled": compiled}
    model_id = body.model_id or brief["selected_model_id"] or "local-poster-compositor"
    product_ids = json_loads(brief["product_asset_ids_json"], [])
    ref_ids = json_loads(brief["reference_asset_ids_json"], [])
    try:
        m = model_router.validate_model_for_task(model_id, body.task_type, reference_count=len(product_ids) + len(ref_ids),
                                                 ratio=brief["aspect_ratio"], count=brief["image_count"])
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.execute("INSERT INTO prompt_versions(id,project_id,brief_id,source_type,version_no,structured_prompt_json,prompt,negative_prompt,model_id,model_params_json,created_at) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (pv_id, pid, brief["id"], "brief", pv_no, json.dumps(structured, ensure_ascii=False), compiled, "",
                model_id, json.dumps({"aspect_ratio": brief["aspect_ratio"], "count": brief["image_count"]}, ensure_ascii=False), int(db.now())))
    jid = db.gen_id("job")
    db.execute("INSERT INTO generation_jobs(id,project_id,brief_id,prompt_version_id,model_id,provider_id,task_type,status,progress,created_at) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)",
               (jid, pid, brief["id"], pv_id, model_id, m.get("provider_id"), body.task_type, "queued", 0, int(db.now())))
    return db.query_one("SELECT * FROM generation_jobs WHERE id=?", (jid,))


@app.get("/api/generation-jobs/{jid}")
def get_generation_job(jid: str, request: Request):
    return authz.require_job_permission(auth.require_actor(request), jid, "job.read")


@app.post("/api/generation-jobs/{jid}/cancel")
def cancel_generation_job(jid: str, request: Request):
    authz.require_job_permission(auth.require_actor(request), jid, "generation.run")
    db.execute("UPDATE generation_jobs SET status='canceled', finished_at=? WHERE id=? AND status IN ('queued','running')", (int(db.now()), jid))
    return {"ok": True}


@app.get("/api/projects/{pid}/candidates")
def list_candidates(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "job.read")
    return db.query("SELECT * FROM generation_candidates WHERE project_id=? ORDER BY created_at DESC", (pid,))


@app.post("/api/candidates/{cid}/select")
def select_candidate(cid: str, request: Request):
    c = authz.require_candidate_permission(auth.require_actor(request), cid, "project.write")
    db.execute("UPDATE generation_candidates SET is_selected=0 WHERE project_id=?", (c["project_id"],))
    db.execute("UPDATE generation_candidates SET is_selected=1 WHERE id=?", (cid,))
    return {"ok": True}


# ---------------- 画布文档（可保存 + 真实导出） ----------------
class CanvasIn(BaseModel):
    width: int = 768
    height: int = 1024
    document: dict = {}
    candidate_id: Optional[str] = None


@app.get("/api/projects/{pid}/canvas")
def get_canvas(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.read")
    r = db.query_one("SELECT * FROM canvas_documents WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,))
    if not r:
        raise HTTPException(404, "画布不存在")
    return {"id": r["id"], "project_id": r["project_id"], "width": r["width"], "height": r["height"],
            "document": json_loads(r["canvas_json"], {})}


@app.put("/api/projects/{pid}/canvas")
def put_canvas(pid: str, body: CanvasIn, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.write")
    # 画布图层引用的素材必须属于本项目，杜绝把 B 项目图片合成进 A 项目成品
    layer_asset_ids = [l.get("asset_id") for l in ((body.document or {}).get("layers") or []) if isinstance(l, dict)]
    authz.assert_assets_in_project(pid, layer_asset_ids, field="canvas_layer")
    now = int(db.now())
    ex = db.query_one("SELECT id FROM canvas_documents WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,))
    doc_json = json.dumps(body.document, ensure_ascii=False)
    if ex:
        db.execute("UPDATE canvas_documents SET width=?,height=?,canvas_json=?,candidate_id=COALESCE(?,candidate_id),updated_at=? WHERE id=?",
                   (body.width, body.height, doc_json, body.candidate_id, now, ex["id"]))
        cid = ex["id"]
    else:
        cid = db.gen_id("cvs")
        db.execute("INSERT INTO canvas_documents(id,project_id,candidate_id,width,height,canvas_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                   (cid, pid, body.candidate_id, body.width, body.height, doc_json, now, now))
    approved = db.query("SELECT id,title FROM reviews WHERE project_id=? AND status='approved'", (pid,))
    for rv in approved:
        db.execute("UPDATE reviews SET status='pending', reason=?, updated_at=? WHERE id=?",
                   ("成品已修改，需重新审核", int(db.now()), rv["id"]))
        notify("usr_default", "review", f"需重新审核：{rv['title']}", "画布成品在审核通过后被修改", ref_type="review", ref_id=rv["id"])
    return {"ok": True, "id": cid, "reopened_reviews": [r["id"] for r in approved]}


@app.post("/api/projects/{pid}/canvas/export")
def export_canvas(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.export")
    _review_gate(pid, raise_error=True)
    r = db.query_one("SELECT * FROM canvas_documents WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,))
    if not r:
        raise HTTPException(422, "请先保存画布")
    doc = json_loads(r["canvas_json"], {})

    def loader(aid):
        if not aid:
            return None
        a = db.query_one("SELECT object_key FROM assets WHERE id=? AND project_id=?", (aid, pid))
        if not a:
            return None
        try:
            return storage.read_pillow(a["object_key"])
        except Exception:
            return None

    png = canvas_renderer.render_canvas(doc, loader)
    aid = db.gen_id("ast")
    obj_key = f"{pid}/{aid}.png"
    storage.save_bytes(obj_key, png)
    db.execute("INSERT INTO assets(id,tenant_id,project_id,kind,role,object_key,sha256,mime,width,height,created_at,source,origin,usage_rights_status) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (aid, "tnt_default", pid, "image", "export", obj_key, "", "image/png",
                int(doc.get("width") or 768), int(doc.get("height") or 1024), db.now(), "canvas_export", "manual", "exported"))
    # 审核通过记录绑定成品 Asset，便于追溯「哪一版成品被谁在何时放行」
    ap = db.query_one("SELECT id FROM reviews WHERE project_id=? AND status='approved' ORDER BY decided_at DESC LIMIT 1", (pid,))
    if ap:
        db.execute("UPDATE reviews SET asset_id=?, target_ref=?, updated_at=? WHERE id=?",
                   (aid, aid, int(db.now()), ap["id"]))
    return {"asset_id": aid, "download_url": f"/files/{aid}", "review_id": (ap or {}).get("id")}


# ---------------- assets ----------------
@app.post("/api/projects/{pid}/assets")
def upload_asset(request: Request, pid: str, file: UploadFile = File(...), role: str = Form("product"),
                source: str = Form("upload"), origin: str = Form("")):
    authz.require_project_permission(auth.require_actor(request), pid, "asset.upload")
    data = file.file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件过大（上限 20MB）")
    try:
        Image.open(io.BytesIO(data)).verify()
    except Exception:
        raise HTTPException(400, "不是有效的图片文件")
    meta = storage.save_upload(pid, file.filename or "file", data)
    aid = db.gen_id("ast")
    db.execute(
        "INSERT INTO assets(id,tenant_id,project_id,kind,role,object_key,sha256,mime,width,height,created_at,source,origin,usage_rights_status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, "tnt_default", pid, "image", role, meta["object_key"], meta["sha256"], meta["mime"],
         meta["width"], meta["height"], db.now(), source, origin, "pending_check"),
    )
    return {"id": aid, "role": role, "width": meta["width"], "height": meta["height"],
            "mime": meta["mime"], "object_key": meta["object_key"]}


def _extract_text(filename: str, data: bytes) -> str:
    import re
    name = (filename or "").lower()
    if name.endswith((".txt", ".md", ".markdown", ".csv")):
        return data.decode("utf-8", errors="replace")
    if name.endswith(".json"):
        try:
            return json.dumps(json.loads(data.decode("utf-8", errors="replace")), ensure_ascii=False, indent=2)
        except Exception:
            return data.decode("utf-8", errors="replace")
    if name.endswith(".docx"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
            xml = re.sub(r"<w:p[ >]", "\n", xml)
            xml = re.sub(r"<[^>]+>", "", xml)
            return xml
        except Exception:
            raise HTTPException(400, "无法解析该 docx 文件")
    raise HTTPException(400, "仅支持 txt / md / json / csv / docx")


@app.post("/api/projects/{pid}/assets/batch")
def upload_assets_batch(request: Request, pid: str, files: list[UploadFile] = File(...), role: str = Form("product"), source: str = Form("upload")):
    authz.require_project_permission(auth.require_actor(request), pid, "asset.upload")
    results = []
    for f in files:
        data = f.file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{f.filename} 超过 20MB 上限")
        try:
            Image.open(io.BytesIO(data)).verify()
        except Exception:
            raise HTTPException(400, f"{f.filename} 不是有效的图片文件")
        meta = storage.save_upload(pid, f.filename or "file", data)
        aid = db.gen_id("ast")
        db.execute(
            "INSERT INTO assets(id,tenant_id,project_id,kind,role,object_key,sha256,mime,width,height,created_at,source,origin,usage_rights_status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, "tnt_default", pid, "image", role, meta["object_key"], meta["sha256"], meta["mime"],
             meta["width"], meta["height"], db.now(), source, "", "pending_check"),
        )
        results.append({"id": aid, "role": role, "width": meta["width"], "height": meta["height"], "mime": meta["mime"]})
    return {"assets": results}


@app.post("/api/projects/{pid}/files/import")
def import_text_file(request: Request, pid: str, file: UploadFile = File(...)):
    authz.require_project_permission(auth.require_actor(request), pid, "asset.upload")
    data = file.file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件过大")
    text = _extract_text(file.filename or "", data)
    return {"filename": file.filename, "text": text}


@app.get("/api/projects/{pid}/assets")
def list_assets(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "asset.read")
    return db.query("SELECT id,role,kind,mime,width,height,created_at,object_key FROM assets WHERE project_id=?", (pid,))


@app.get("/api/assets/{aid}/download")
def download_asset(aid: str, request: Request):
    a = authz.require_asset_permission(auth.require_actor(request), aid, "asset.read")
    data = storage.read_bytes(a["object_key"])
    return Response(content=data, media_type=a["mime"] or "image/png")


@app.get("/files/{asset_id}")
def file_content(asset_id: str, request: Request):
    a = authz.require_asset_permission(auth.require_actor(request), asset_id, "asset.read")
    data = storage.read_bytes(a["object_key"])
    return Response(content=data, media_type=a["mime"] or "image/png")


# ---------------- graph ----------------
@app.get("/api/projects/{pid}/graph")
def get_graph(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.read")
    g = db.query_one("SELECT * FROM graphs WHERE project_id=?", (pid,))
    if not g:
        raise HTTPException(404, "图不存在")
    nodes = db.query("SELECT id,graph_id,type,name,position_json,current_version,status FROM nodes WHERE graph_id=?", (g["id"],))
    edges = db.query("SELECT id,from_node,from_port,to_node,to_port,semantic FROM edges WHERE graph_id=?", (g["id"],))
    out_nodes = []
    for n in nodes:
        n = dict(n)
        pos = json_loads(n["position_json"], {"x": 0, "y": 0})
        content = get_current_version(n["id"]) or {}
        n["position"] = pos
        n["content"] = content
        out_nodes.append(n)
    return {"graph_id": g["id"], "version": g["current_version"], "nodes": out_nodes, "edges": edges}


class GraphPatch(BaseModel):
    expected_graph_version: int
    moves: list = []   # [{node_id, x, y}]
    add_edges: list = []   # [{from_node, from_port, to_node, to_port, semantic}]
    remove_edges: list = []  # [edge_id]


@app.patch("/api/projects/{pid}/graph")
def patch_graph(pid: str, body: GraphPatch, request: Request):
    actor = auth.require_actor(request)
    authz.require_project_permission(actor, pid, "project.write")
    with db.tx(immediate=True) as conn:
        g = conn.execute("SELECT * FROM graphs WHERE project_id=?", (pid,)).fetchone()
        if not g:
            raise HTTPException(404, "图不存在")
        if body.expected_graph_version != g["current_version"]:
            raise HTTPException(409, "图版本冲突，请刷新后重试")
        # 拓扑保护：新增/删除连线若触及在途运行的依赖或下游范围 -> 409；位置移动放行
        if body.add_edges or body.remove_edges:
            affected = set()
            for e in body.add_edges:
                affected.add(e.get("from_node")); affected.add(e.get("to_node"))
            for rid in body.remove_edges:
                row = conn.execute("SELECT from_node,to_node FROM edges WHERE id=?", (rid,)).fetchone()
                if row:
                    affected.add(row["from_node"]); affected.add(row["to_node"])
            hits = _affecting_runs(conn, g["id"], affected, include_down=True)
            if hits:
                raise _conflict_with_run(actor, hits[0], "graph_has_inflight_run",
                                         "本次连线改动会影响在途运行的依赖或下游范围，已拒绝（位置移动仍可用）。")
        for mv in body.moves:
            conn.execute("UPDATE nodes SET position_json=? WHERE id=?",
                         (json.dumps({"x": mv.get("x", 0), "y": mv.get("y", 0)}), mv["node_id"]))
        for e in body.add_edges:
            err = _validate_edge_conn(conn, e["from_node"], e["from_port"], e["to_node"], e["to_port"])
            if err:
                raise HTTPException(422, err)
            eid = db.gen_id("edge")
            conn.execute("INSERT INTO edges(id,graph_id,from_node,from_port,to_node,to_port,semantic) VALUES(?,?,?,?,?,?,?)",
                         (eid, g["id"], e["from_node"], e["from_port"], e["to_node"], e["to_port"], e.get("semantic", "")))
        for rid in body.remove_edges:
            conn.execute("DELETE FROM edges WHERE id=?", (rid,))
        conn.execute("UPDATE graphs SET current_version=current_version+1 WHERE id=?", (g["id"],))
        ng = conn.execute("SELECT current_version FROM graphs WHERE id=?", (g["id"],)).fetchone()
        return {"ok": True, "version": ng["current_version"]}
PORT_TYPES = {
    "facts": "facts", "visual_reference": "image_asset", "strategy": "strategy",
    "prompt": "prompt", "generated_asset": "generated_asset", "review": "review",
}


def validate_edge(from_node, from_port, to_node, to_port):
    fn = db.query_one("SELECT type FROM nodes WHERE id=?", (from_node,))
    tn = db.query_one("SELECT type FROM nodes WHERE id=?", (to_node,))
    if not fn or not tn:
        return "节点不存在"
    # 禁止成环
    if to_node in downstream_node_ids(from_node) or to_node == from_node:
        return "连接会形成环或不兼容"
    # 端口类型校验（简化：facts->strategy, visual_reference->image_prompt, strategy->image_prompt, prompt->image_generation, generated_asset->review, review->layout_export）
    rules = {
        ("product_facts", "facts", "strategy"): True,
        ("product_image", "visual_reference", "image_prompt"): True,
        ("strategy", "strategy", "image_prompt"): True,
        ("image_prompt", "prompt", "image_generation"): True,
        ("image_generation", "generated_asset", "review"): True,
        ("review", "review", "layout_export"): True,
    }
    key = (fn["type"], from_port, tn["type"])
    if key not in rules:
        return f"不兼容的连接：{fn['type']}.{from_port} -> {tn['type']}.{to_port}"
    return None


class InitWorkflow(BaseModel):
    skeleton: str = "poster"


WORKFLOW_SKELETONS = {
    "poster":       {"facts": True, "image": True, "prompts": 1, "review": True},
    "long":         {"facts": True, "image": True, "prompts": 1, "review": True},
    "detail":       {"facts": True, "image": True, "prompts": 3, "review": True},
    "cover":        {"facts": False, "image": True, "prompts": 1, "review": True},
    "illustration": {"facts": False, "image": False, "prompts": 1, "review": True},
}


@app.post("/api/projects/{pid}/graph/init-template")
def init_template(pid: str, request: Request, body: InitWorkflow | None = None):
    actor = auth.require_actor(request)
    authz.require_project_permission(actor, pid, "project.write")
    sk = WORKFLOW_SKELETONS.get((body.skeleton if body else "poster"), WORKFLOW_SKELETONS["poster"])
    with db.tx(immediate=True) as conn:
        g = conn.execute("SELECT * FROM graphs WHERE project_id=?", (pid,)).fetchone()
        if not g:
            raise HTTPException(404, "图不存在")
        runs = _runs_of_graph(conn, g["id"])
        if runs:
            raise _conflict_with_run(actor, runs[0], "graph_has_inflight_run",
                                     "该工作流存在进行中的运行，运行期间禁止重置模板（图、节点与历史运行均未改动）。")
        conn.execute("DELETE FROM nodes WHERE graph_id=?", (g["id"],))
        conn.execute("DELETE FROM edges WHERE graph_id=?", (g["id"],))

        def add(type_, x, y, content=None):
            nid = db.gen_id(type_[:4])
            conn.execute("INSERT INTO nodes(id,graph_id,type,name,position_json,current_version,status) VALUES(?,?,?,?,?,1,?)",
                         (nid, g["id"], type_, NODE_NAMES.get(type_, type_), json.dumps({"x": x, "y": y}), "draft"))
            if content is not None:
                _write_node_version_conn(conn, nid, content, author_type="system")
            return nid

        def edge(f, fp, t, tp, sem):
            eid = db.gen_id("edge")
            conn.execute("INSERT INTO edges(id,graph_id,from_node,from_port,to_node,to_port,semantic) VALUES(?,?,?,?,?,?,?)",
                         (eid, g["id"], f, fp, t, tp, sem))

        facts = add("product_facts", 40, 220, NODE_DEFAULTS["product_facts"]) if sk["facts"] else None
        img = add("product_image", 40, 440, NODE_DEFAULTS["product_image"]) if sk["image"] else None
        strat = add("strategy", 360, 220, NODE_DEFAULTS["strategy"])
        prompts, gens = [], []
        for i in range(sk["prompts"]):
            prompt_content = {**NODE_DEFAULTS["image_prompt"], "strategy_ref": "hero_" + str(i + 1)}
            np_ = add("image_prompt", 680, 80 + i * 180, prompt_content)
            ng_ = add("image_generation", 1000, 80 + i * 180, NODE_DEFAULTS["image_generation"])
            prompts.append(np_); gens.append(ng_)
            edge(strat, "strategy", np_, "strategy", "该图策略")
            if img:
                edge(img, "visual_reference", np_, "visual_reference", "产品参考图")
            edge(np_, "prompt", ng_, "prompt", "提示词")
        rev = add("review", 1320, 220, NODE_DEFAULTS["review"]) if sk["review"] else None
        lay = add("layout_export", 1640, 220, NODE_DEFAULTS["layout_export"])
        if facts:
            edge(facts, "facts", strat, "facts", "锁定产品事实")
        for ng_ in gens:
            if rev:
                edge(ng_, "generated_asset", rev, "generated_asset", "生成结果")
        if rev:
            edge(rev, "review", lay, "review", "审核通过")
        conn.execute("UPDATE graphs SET current_version=current_version+1 WHERE id=?", (g["id"],))
        return {"ok": True, "graph_id": g["id"], "nodes": {"facts": facts, "image": img, "strategy": strat,
                "prompts": prompts, "gens": gens, "review": rev, "layout": lay}}
# ---------------- 工作流节点自由增删 ----------------
NODE_NAMES = {
    "product_facts": "产品事实卡", "product_image": "产品素材", "strategy": "视觉策略",
    "image_prompt": "单图提示词", "image_generation": "生图节点", "review": "审核", "layout_export": "排版与 PNG 导出",
}

NODE_DEFAULTS = {
    "product_facts": {"product_id": "", "product_name": "", "activity_version": "", "confirmed_selling_points": [], "locked_appearance": [], "applicable_scenes": [], "forbidden_expressions": [], "policies": [], "recognition_notes": []},
    "product_image": {"asset_id": None, "asset_role": "product", "filename": ""},
    "strategy": {"strategies": [], "task_brief": {"goal": "", "channel": "", "audience": ""}},
    "image_prompt": {"prompt": "", "negative_prompt": "", "strategy_ref": "hero_1", "prompt_template_id": ""},
    "image_generation": {"model_id": "local-poster-compositor", "outputs": []},
    "review": {"conclusion": "", "issue_tags": []},
    "layout_export": {"title": "", "subtitle": "", "approved": False},
}

# image_prompt 运行时的字段白名单：
# - IMAGE_PROMPT_INPUT_FIELDS：输入侧配置，运行前内容里存在才保留（不凭空新增字段）
# - IMAGE_PROMPT_OUTPUT_FIELDS：本次生成输出，生成结果优先；旧 prompt / 旧生成结果不得覆盖
IMAGE_PROMPT_INPUT_FIELDS = ("strategy_ref", "prompt_template_id")
IMAGE_PROMPT_OUTPUT_FIELDS = ("prompt", "negative_prompt", "requested_aspect_ratio", "selling_point_id",
                              "product_id", "locked_visual_features", "bg_style",
                              "prompt_template_id", "prompt_template_name")


class NodeAdd(BaseModel):
    type: str
    x: float = 0
    y: float = 0


@app.post("/api/graphs/{gid}/nodes")
def add_node(gid: str, body: NodeAdd, request: Request):
    authz.require_graph_permission(auth.require_actor(request), gid, "project.write")
    g = db.query_one("SELECT id FROM graphs WHERE id=?", (gid,))
    if not g:
        raise HTTPException(404, "图不存在")
    if body.type not in NODE_DEFAULTS:
        raise HTTPException(422, "未知节点类型")
    nid = db.gen_id(body.type[:4])
    db.execute("INSERT INTO nodes(id,graph_id,type,name,position_json,current_version,status) VALUES(?,?,?,?,?,1,'draft')",
               (nid, gid, body.type, NODE_NAMES.get(body.type, body.type), json.dumps({"x": body.x, "y": body.y})))
    set_node_version(nid, NODE_DEFAULTS[body.type], author_type="human")
    return {"id": nid, "type": body.type}


@app.delete("/api/nodes/{nid}")
def delete_node(nid: str, request: Request):
    actor = auth.require_actor(request)
    authz.require_node_permission(actor, nid, "project.write")
    with db.tx(immediate=True) as conn:
        row = conn.execute("SELECT graph_id FROM nodes WHERE id=?", (nid,)).fetchone()
        if not row:
            raise HTTPException(404, "节点不存在")
        hits = _affecting_runs(conn, row["graph_id"], {nid}, include_down=True)
        if hits:
            raise _conflict_with_run(actor, hits[0], "node_busy",
                                     "该节点处于在途运行的依赖/下游范围内，运行期间禁止删除（数据未改动）。")
        conn.execute("DELETE FROM edges WHERE from_node=? OR to_node=?", (nid, nid))
        conn.execute("DELETE FROM node_versions WHERE node_id=?", (nid,))
        conn.execute("DELETE FROM node_candidates WHERE node_id=?", (nid,))
        conn.execute("DELETE FROM nodes WHERE id=?", (nid,))
    return {"ok": True}


# ---------------- nodes ----------------
@app.get("/api/nodes/{nid}")
def get_node(nid: str, request: Request):
    authz.require_node_permission(auth.require_actor(request), nid, "project.read")
    n = db.query_one("SELECT * FROM nodes WHERE id=?", (nid,))
    if not n:
        raise HTTPException(404, "节点不存在")
    n = dict(n)
    n["content"] = get_current_version(nid) or {}
    return n


class NodePatch(BaseModel):
    content: Optional[dict] = None
    position: Optional[dict] = None
    status: Optional[str] = None
    name: Optional[str] = None


@app.patch("/api/nodes/{nid}")
def patch_node(nid: str, body: NodePatch, request: Request):
    actor = auth.require_actor(request)
    authz.require_node_permission(actor, nid, "project.write")
    with db.tx(immediate=True) as conn:
        row = conn.execute("SELECT graph_id FROM nodes WHERE id=?", (nid,)).fetchone()
        if not row:
            raise HTTPException(404, "节点不存在")
        if body.content is not None:
            hits = _affecting_runs(conn, row["graph_id"], {nid})
            if hits:
                raise _conflict_with_run(actor, hits[0], "node_input_inflight",
                                         "该节点是在途运行的输入（其自身或上游），运行期间禁止修改内容；位置、名称与状态仍可修改。")
            _write_node_version_conn(conn, nid, body.content, author_type="human")
            _mark_stale_conn(conn, nid)
        if body.position is not None:
            conn.execute("UPDATE nodes SET position_json=? WHERE id=?", (json.dumps(body.position), nid))
        if body.status is not None:
            conn.execute("UPDATE nodes SET status=? WHERE id=?", (body.status, nid))
        if body.name is not None:
            conn.execute("UPDATE nodes SET name=? WHERE id=?", (body.name, nid))
        ver = conn.execute("SELECT current_version FROM nodes WHERE id=?", (nid,)).fetchone()["current_version"]
    return {"ok": True, "version": ver}
class AiEdit(BaseModel):
    instruction: str
    base_version: Optional[int] = None


@app.post("/api/nodes/{nid}/ai-edit")
def ai_edit(nid: str, body: AiEdit, request: Request):
    authz.require_node_permission(auth.require_actor(request), nid, "project.write")
    n = db.query_one("SELECT type FROM nodes WHERE id=?", (nid,))
    if not n:
        raise HTTPException(404, "节点不存在")
    # 编辑类节点：直接基于当前版本产生候选，不自动应用
    base = get_current_version(nid) or {}
    cand_content, summary = text_gen.text_edit_prompt(n["type"], base, body.instruction)
    cid = db.gen_id("cand")
    ver = db.query_one("SELECT current_version FROM nodes WHERE id=?", (nid,))["current_version"]
    db.execute("INSERT INTO node_candidates(id,node_id,base_version,content_json,summary,created_at) VALUES(?,?,?,?,?,?)",
               (cid, nid, ver, json.dumps(cand_content), summary, db.now()))
    return {"candidate_id": cid, "content": cand_content, "summary": summary, "base_version": ver}


class ApplyCandidate(BaseModel):
    candidate_id: str
    base_version: int


@app.post("/api/nodes/{nid}/apply-candidate")
def apply_candidate(nid: str, body: ApplyCandidate, request: Request):
    actor = auth.require_actor(request)
    authz.require_node_permission(actor, nid, "project.write")
    with db.tx(immediate=True) as conn:
        c = conn.execute("SELECT * FROM node_candidates WHERE id=? AND node_id=?",
                         (body.candidate_id, nid)).fetchone()
        if not c:
            raise HTTPException(404, "候选不存在")
        cur = conn.execute("SELECT current_version, graph_id FROM nodes WHERE id=?", (nid,)).fetchone()
        if not cur:
            raise HTTPException(404, "节点不存在")
        if c["base_version"] != cur["current_version"]:
            raise HTTPException(409, "节点已更新，候选基于的版本已过期，请重新生成")
        hits = _affecting_runs(conn, cur["graph_id"], {nid})
        if hits:
            raise _conflict_with_run(actor, hits[0], "node_input_inflight",
                                     "该节点是在途运行的输入（其自身或上游），运行期间禁止应用候选内容。")
        content = json_loads(c["content_json"])
        before = cur["current_version"]
        after = _write_node_version_conn(conn, nid, content, author_type="ai")
        _mark_stale_conn(conn, nid)
    db.audit(get_project_of_node(nid), actor.user_id, "apply_candidate", nid, before, after)
    return {"ok": True, "version": after}
def get_project_of_node(nid):
    row = db.query_one("SELECT g.project_id AS pid FROM nodes n JOIN graphs g ON g.id=n.graph_id WHERE n.id=?", (nid,))
    return row["pid"] if row else "tnt_default"


# ---------------- runs ----------------
class RunReq(BaseModel):
    node_version: Optional[int] = None
    model_id: Optional[str] = None
    params: dict = {}
    idempotency_key: Optional[str] = None
    reference_asset_ids: list = []


def _run_node_thread(run_id, node_id, model_id, params, ref_asset_ids, idem, accounting=None):
    started = db.now()
    proj = get_project_of_node(node_id)
    ntype = (db.query_one("SELECT type FROM nodes WHERE id=?", (node_id,)) or {}).get("type") or ""
    try:
        db.execute("UPDATE node_runs SET status='running', started_at=? WHERE id=?", (db.now(), run_id))
        n = db.query_one("SELECT type,graph_id FROM nodes WHERE id=?", (node_id,))
        g = db.query_one("SELECT current_version FROM graphs WHERE id=?", (n["graph_id"],))
        result = execute_node(n["type"], node_id, model_id, params, ref_asset_ids, g["current_version"], run_id)
        db.execute("UPDATE node_runs SET status='succeeded', ended_at=?, usage_json=?, provider_task_id=? WHERE id=?",
                   (db.now(), json.dumps(result.get("usage", {})), result.get("provider_task_id"), run_id))
        for out in result.get("outputs", []):
            db.execute("INSERT INTO run_outputs(run_id,asset_id,output_json) VALUES(?,?,?)",
                       (run_id, out["asset_id"], json.dumps(out)))
        mark_stale(node_id)
        if n["type"] in ("image_generation", "image_prompt", "strategy"):
            outs = result.get("outputs", []) or []
            w = outs[0]["width"] if outs else None
            h = outs[0]["height"] if outs else None
            metering.record_usage(project_id=proj, model_id=model_id, task_type=n["type"],
                                  output_images=len(outs), width=w, height=h,
                                  duration_ms=int((db.now() - started) * 1000),
                                  status="succeeded", run_id=run_id,
                                  organization_id=(accounting or {}).get("organization_id"),
                                  user_id=(accounting or {}).get("user_id"))
    except Exception as e:
        db.execute("UPDATE node_runs SET status='failed', ended_at=?, error_code=? WHERE id=?",
                   (db.now(), str(e)[:200], run_id))
        try:
            metering.record_usage(project_id=proj, model_id=model_id, task_type=ntype,
                                  output_images=0, duration_ms=int((db.now() - started) * 1000),
                                  status="failed", run_id=run_id,
                                  organization_id=(accounting or {}).get("organization_id"),
                                  user_id=(accounting or {}).get("user_id"))
        except Exception:
            pass


def _persist_run_input_snapshot(run_id, snapshot, prompt_ref=None, image_ref=None, refs=None):
    """持久化本次 image_generation 的输入快照（不新增表/字段）。

    - run_inputs：上游引用行 —— 提示词节点一行（记录其版本），每个参考资产一行（按实际传入顺序）
    - node_runs.parameters_json：在原有请求参数上附加 input_snapshot 快照键（不覆盖原有键）
    只写白名单字段，不含任何 API Key / Cookie / 服务商凭据。
    """
    if not run_id:
        return
    prompt_ref = prompt_ref or {}
    image_ref = image_ref or {}
    if prompt_ref.get("node_id"):
        db.execute("INSERT INTO run_inputs(run_id,upstream_node_id,upstream_version,asset_id) VALUES(?,?,?,?)",
                   (run_id, prompt_ref["node_id"], prompt_ref.get("version"), None))
    for aid in (refs or []):
        db.execute("INSERT INTO run_inputs(run_id,upstream_node_id,upstream_version,asset_id) VALUES(?,?,?,?)",
                   (run_id, image_ref.get("node_id"), image_ref.get("version"), aid))
    row = db.query_one("SELECT parameters_json FROM node_runs WHERE id=?", (run_id,)) or {}
    params = json_loads(row.get("parameters_json"), {}) or {}
    if not isinstance(params, dict):
        params = {}
    params["input_snapshot"] = snapshot
    db.execute("UPDATE node_runs SET parameters_json=? WHERE id=?",
               (json.dumps(params, ensure_ascii=False), run_id))


def execute_node(ntype, node_id, model_id, params, ref_asset_ids, graph_version, run_id=None):
    """同步执行节点，返回 {outputs:[{asset_id,object_key,width,height}], usage, provider_task_id}。"""
    if ntype == "strategy":
        facts = get_upstream_facts(node_id)
        brief = get_current_version(node_id).get("task_brief", {}) or {}
        prov = registry.resolve_provider(model_id or "rule-based-planner")
        if prov:
            try:
                strat = text_gen.generate_strategy_llm(model_id, prov, facts, brief)
            except Exception:
                strat = text_gen.generate_strategy(facts, brief)
        else:
            strat = text_gen.generate_strategy(facts, brief)
        set_node_version(node_id, strat, author_type="ai", model_id=model_id or "rule-based-planner")
        return {"outputs": [], "usage": {"model": model_id or "rule-based-planner"}, "provider_task_id": "strat"}
    if ntype == "image_prompt":
        facts = get_upstream_facts(node_id)
        strat = get_upstream_strategy(node_id)
        baseline = (strat or {}).get("visual_baseline", {})
        cur_ver = get_current_version(node_id) or {}
        # 运行前快照输入侧配置：生成结果不得覆盖这些用户配置
        input_config = {k: cur_ver[k] for k in IMAGE_PROMPT_INPUT_FIELDS if k in cur_ver}
        sref = cur_ver.get("strategy_ref", "hero_1")
        item = next((s for s in strat.get("strategies", []) if s["id"] == sref), strat.get("strategies", [{}])[0] if strat.get("strategies") else {})
        prov = registry.resolve_provider(model_id or "rule-based-planner")
        if prov:
            try:
                prompt = text_gen.generate_prompt_llm(model_id, prov, item, facts, baseline=baseline)
            except Exception:
                prompt = text_gen.generate_prompt(item, facts, baseline=baseline)
        else:
            prompt = text_gen.generate_prompt(item, facts, baseline=baseline)
        # Prompt 模板接入：命中的后台 Prompt 模板会把生成结果作为 {user_prompt} 重新编译
        tpl_id = cur_ver.get("prompt_template_id") or params.get("prompt_template_id") or ""
        used_tpl_id = ""
        if tpl_id:
            seed_prompt_templates()
            tpl = db.query_one("SELECT * FROM prompt_templates WHERE id=? AND enabled=1", (tpl_id,))
            if not tpl:
                raise HTTPException(422, "所选 Prompt 模板不存在或已停用")
            br = db.query_one("SELECT * FROM generation_briefs WHERE project_id=? ORDER BY updated_at DESC LIMIT 1",
                              (get_project_of_node(node_id),)) or {}
            brief_vars = dict(br)
            brief_vars["user_prompt"] = prompt.get("prompt", "")
            prompt = {**prompt,
                      "prompt": prompt_compiler.compile_prompt(tpl["template_text"], brief_vars),
                      "prompt_template_id": tpl["id"],
                      "prompt_template_name": tpl["name"]}
            used_tpl_id = tpl["id"]
        # 生成成功：按字段白名单组装新 content —— 输入配置在前，本次生成输出在后（生成输出优先，
        # 旧 prompt 与旧生成结果不会被合并回来）
        generated = {k: prompt[k] for k in IMAGE_PROMPT_OUTPUT_FIELDS if k in prompt}
        set_node_version(node_id, {**input_config, **generated},
                         author_type="ai", model_id=model_id or "rule-based-planner")
        return {"outputs": [], "usage": {"model": model_id or "rule-based-planner",
                                         "prompt_template_id": used_tpl_id}, "provider_task_id": "prompt"}
    if ntype == "image_generation":
        pid = get_project_of_node(node_id)
        prompt_content = get_current_version(node_id)
        # 解析上游：prompt 节点 + product image 节点（向上多跳追溯）
        prompt_node = upstream_of_type(node_id, "image_prompt")
        img_node = upstream_of_type(node_id, "product_image")
        pc = get_current_version(prompt_node) if prompt_node else {}
        prompt_text = pc.get("prompt", params.get("prompt", ""))
        neg = pc.get("negative_prompt", "")
        ar = params.get("aspect_ratio") or pc.get("requested_aspect_ratio", "1:1")
        refs = []
        # —— 切片1：m2 绑定的类型校验/去重/归属/可读性，必须早于任何列表拼接、读图与 dispatch ——
        _tpl = (db.query_one("SELECT template_id FROM projects WHERE id=?", (pid,)) or {}).get("template_id")
        _m2_bound = None
        if _tpl == "m2":
            if model_id != "local-poster-compositor":
                raise ValueError("model_cannot_use_product_image: m2 产品合成仅支持 local-poster-compositor；当前模型=%s" % model_id)
            _ic = get_current_version(img_node) or {}
            _aid = _ic.get("asset_id")
            _refs_raw = _ic.get("reference_asset_ids")
            if _refs_raw is None:
                _refs_raw = []
            if _aid is not None and not isinstance(_aid, str):
                raise ValueError("product_image_binding_invalid: product_image.asset_id 类型异常（%s）" % type(_aid).__name__)
            if not isinstance(_refs_raw, list):
                raise ValueError("product_image_binding_invalid: product_image.reference_asset_ids 应为列表（%s）" % type(_refs_raw).__name__)
            if any(not isinstance(x, str) for x in _refs_raw):
                raise ValueError("product_image_binding_invalid: product_image.reference_asset_ids 含非字符串项")
            _bound = []
            for _x in ([_aid] if _aid else []) + _refs_raw:
                if _x and _x not in _bound:
                    _bound.append(_x)
            if not _bound:
                raise ValueError("product_image_required: 上游产品素材节点未绑定产品图，请先在「素材库」上传并在该节点勾选绑定")
            for _a in _bound:
                _row = db.query_one("SELECT object_key, project_id FROM assets WHERE id=?", (_a,))
                if not _row or _row.get("project_id") != pid:
                    raise ValueError("product_image_cross_project: 绑定的产品图 %s 不存在或不属于当前项目" % _a)
                try:
                    storage.read_pillow(_row["object_key"])
                except Exception:
                    raise ValueError("product_image_unreadable: 绑定的产品图 %s 文件不可读取" % _a)
            _m2_bound = _bound
        if img_node:
            ic = get_current_version(img_node) or {}
            _cands = list(_m2_bound) if _m2_bound is not None else (([ic.get("asset_id")] if ic.get("asset_id") else []) + (ic.get("reference_asset_ids") or []))
            for ia in _cands:
                if ia and db.query_one("SELECT id FROM assets WHERE id=? AND project_id=?", (ia, pid)):
                    refs.append(ia)
        for rid in ref_asset_ids:
            if rid and db.query_one("SELECT id FROM assets WHERE id=? AND project_id=?", (rid, pid)):
                refs.append(rid)
        # —— 本次运行的输入快照（白名单字段）——
        # 在调用模型前落库，成功与失败都可追溯；取值与下面构造 req 时完全一致
        prompt_ver = (db.query_one("SELECT current_version FROM nodes WHERE id=?", (prompt_node,)) or {}).get("current_version") if prompt_node else None
        img_ver = (db.query_one("SELECT current_version FROM nodes WHERE id=?", (img_node,)) or {}).get("current_version") if img_node else None
        _node_row = db.query_one("SELECT graph_id FROM nodes WHERE id=?", (node_id,)) or {}
        input_snapshot = {
            "project_id": pid,
            "graph_id": _node_row.get("graph_id"),
            "node_id": node_id,
            "run_id": run_id,
            "model_id": model_id,
            # 本分支未设置 req["task_type"]，与 image_gen.dispatch 的默认值保持一致
            "task_type": "text_to_image",
            "prompt": prompt_text,
            "negative_prompt": neg,
            "prompt_node_id": prompt_node,
            "prompt_node_version": prompt_ver,
            "reference_asset_ids": list(refs),
            "aspect_ratio": ar,
            "count": params.get("count", 1),
            "resolution_tier": params.get("resolution_tier", "standard"),
            "seed": params.get("seed"),
            "params": {"title": params.get("title", ""), "subtitle": params.get("subtitle", "")},
        }
        _persist_run_input_snapshot(run_id, input_snapshot,
                                    prompt_ref={"node_id": prompt_node, "version": prompt_ver},
                                    image_ref={"node_id": img_node, "version": img_ver},
                                    refs=refs)
        # 校验模型能力
        errs = registry.validate_image_params(model_id, {"aspect_ratio": ar, "count": params.get("count", 1),
                                                          "reference_count": len(refs)})
        if errs:
            raise HTTPException(422, "；".join(errs))
        pil_imgs, image_roles = [], []
        for rid in refs:
            # role 一律从 assets 表读取（不信任客户端传入），并与实际加载成功的图片一一对应
            row = db.query_one("SELECT object_key, role FROM assets WHERE id=?", (rid,))
            try:
                pil_imgs.append(storage.read_pillow(row["object_key"]))
            except Exception:
                continue
            image_roles.append((row or {}).get("role") or "")
        req = {"model_id": model_id, "prompt": prompt_text, "negative_prompt": neg,
               "reference_asset_ids": refs, "aspect_ratio": ar,
               "resolution_tier": params.get("resolution_tier", "standard"),
               "count": params.get("count", 1), "seed": params.get("seed"),
               "params": {"title": params.get("title", ""), "subtitle": params.get("subtitle", "") }}
        prov_cfg = registry.resolve_provider(model_id)
        adapter = (registry.get_model(model_id) or {}).get("adapter")
        # 本地拼接器才接收 role（内部版式参数）；外部模型的请求体 req 与 payload 完全不变
        local_roles = image_roles if model_id in ("local-poster-compositor", "demo-poster-compositor") else None
        _m2_local = (_tpl == "m2" and model_id == "local-poster-compositor")
        res = image_gen.dispatch(model_id, req, pil_imgs, provider_cfg=prov_cfg, adapter=adapter, roles=local_roles,
                                 draw_final_copy=(not _m2_local))
        outputs = []
        for o in res["outputs"]:
            aid = db.gen_id("ast")
            obj_key = f"{pid}/{aid}.png"
            storage.save_bytes(obj_key, o["bytes"])
            db.execute("INSERT INTO assets(id,tenant_id,project_id,kind,role,object_key,sha256,mime,width,height,created_at,source,origin,usage_rights_status) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, "tnt_default", pid, "image", "generated", obj_key, "", "image/png",
                        o["width"], o["height"], db.now(), "generation", model_id, "generated"))
            _out = {"asset_id": aid, "object_key": obj_key, "width": o["width"], "height": o["height"]}
            if _m2_local:
                _out["no_final_copy"] = True
                _out["copy_layer"] = "none-v2"
            outputs.append(_out)
        # 记录图层（背景/产品/文字），支持可编辑排版导出
        record_layers(pid, node_id, run_id, outputs, params)
        # 把生成结果写回节点 content，供画布缩略图与版本追溯
        cur = get_current_version(node_id) or {}
        all_out = (cur.get("outputs", []) or []) + [o["asset_id"] for o in outputs]
        set_node_version(node_id, {**cur, "outputs": all_out, "model_id": model_id, "last_usage": res["usage"]}, author_type="ai")
        return {"outputs": outputs, "usage": res["usage"], "provider_task_id": res["provider_task_id"]}
    raise ValueError(f"节点类型 {ntype} 不支持运行")


def record_layers(project_id, node_id, run_id, outputs, params):
    # 为每个生成图建立分层记录：background(合成) + product(参考) + text(可编辑)
    z = 0
    for o in outputs:
        # background
        db.execute("INSERT INTO canvas_layers(id,project_id,canvas_id,kind,asset_id,text,style_json,transform_json,z_index,source_run_id) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (db.gen_id("lay"), project_id, node_id, "background_bitmap", o["asset_id"], "", "{}", "{}", z, run_id))
        z += 1
        db.execute("INSERT INTO canvas_layers(id,project_id,canvas_id,kind,asset_id,text,style_json,transform_json,z_index,source_run_id) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (db.gen_id("lay"), project_id, node_id, "text_layer", None,
                    json.dumps({"title": params.get("title", ""), "subtitle": params.get("subtitle", "")}),
                    "{}", "{}", z, run_id))
        z += 1


def upstream_of_type(node_id, type_):
    """沿入边向上多跳追溯，返回最近的指定类型节点；仅限同一 graph。"""
    root = db.query_one("SELECT graph_id FROM nodes WHERE id=?", (node_id,))
    if not root:
        return None
    gid = root["graph_id"]
    visited = {node_id}
    frontier = [node_id]
    while frontier:
        n = frontier.pop(0)
        edges = db.query("SELECT from_node FROM edges WHERE to_node=? AND graph_id=?", (n, gid))
        for e in edges:
            fn_id = e["from_node"]
            if fn_id in visited:
                continue
            visited.add(fn_id)
            fn = db.query_one("SELECT id,type FROM nodes WHERE id=? AND graph_id=?", (fn_id, gid))
            if fn and fn["type"] == type_:
                return fn["id"]
            frontier.append(fn_id)
    return None


def get_upstream_facts(node_id):
    n = db.query_one("SELECT graph_id FROM nodes WHERE id=?", (node_id,))
    if not n:
        return {}
    gid = n["graph_id"]
    # 沿直接 facts 边回溯
    edges = db.query("SELECT from_node FROM edges WHERE to_node=? AND graph_id=?", (node_id, gid))
    for e in edges:
        fn = db.query_one("SELECT id,type FROM nodes WHERE id=? AND graph_id=?", (e["from_node"], gid))
        if fn and fn["type"] == "product_facts":
            return get_current_version(fn["id"]) or {}
    # 退而求其次：同图内任一 product_facts（跨项目隔离）
    f = db.query_one("SELECT id FROM nodes WHERE graph_id=? AND type='product_facts' LIMIT 1", (gid,))
    return get_current_version(f["id"]) if f else {}


def get_upstream_strategy(node_id):
    sid = upstream_of_type(node_id, "strategy")
    return get_current_version(sid) if sid else {}


def _resolve_run_model(nid: str, ntype: str, model_id):
    """模型回退链：显式指定 -> 项目默认 -> 内置默认。

    按节点类型区分文本/图片模型，避免把图片模型传给文本节点。
    """
    if model_id:
        return model_id
    proj = get_project_of_node(nid)
    dflt = db.query_one("SELECT default_text_model, default_image_model FROM projects WHERE id=?", (proj,))
    if ntype == "image_generation":
        return (dflt["default_image_model"] if dflt and dflt["default_image_model"] else None) or "local-poster-compositor"
    return (dflt["default_text_model"] if dflt and dflt["default_text_model"] else None) or "rule-based-planner"


# ---------------- 运行防双跑 / 等待超时语义（阶段 C 补修第一批） ----------------
# 说明：不新增数据库字段；守卫信息存放在既有 node_runs.parameters_json 的保留键下。
RUN_GUARD_KEY = "__run_guard"
# 可自动执行节点的上游依赖（用于生成“输入快照”指纹，值取上游节点当前版本）
RUN_INPUT_UPSTREAMS = {
    "strategy": ("product_facts",),
    "image_prompt": ("strategy", "product_image"),
    "image_generation": ("image_prompt", "product_image"),
}
NODE_BUSY_HINT = ("只读定位方式：GET /api/runs 查看最近运行记录，再 GET /api/runs/{run_id} 查看单次运行状态；"
                  "本批不做按时长自动清理、自动重试或强制重跑。")


def _node_version_of(node_id):
    if not node_id:
        return None
    return (db.query_one("SELECT current_version FROM nodes WHERE id=?", (node_id,)) or {}).get("current_version")


def _run_requester_hash(actor) -> str:
    """操作者指纹：只落不可逆哈希，用于判断“是否同一操作者的重复请求”，不落明文用户/组织 ID。

    无法确定身份时返回空串：空串一律不视为“同一操作者”，避免把不同用户/未知来源的请求误判为重复。
    """
    if isinstance(actor, dict):
        uid = actor.get("user_id") or ""
        org = actor.get("organization_id") or ""
    else:
        uid = getattr(actor, "user_id", "") or ""
        org = getattr(actor, "organization_id", "") or ""
    if not uid:
        return ""
    return hashlib.sha256(("%s|%s" % (uid, org)).encode("utf-8")).hexdigest()[:32]


def _upstream_of_type_conn(conn, node_id, type_):
    root = conn.execute("SELECT graph_id FROM nodes WHERE id=?", (node_id,)).fetchone()
    if not root:
        return None
    gid = root["graph_id"]
    visited, frontier = {node_id}, [node_id]
    while frontier:
        n = frontier.pop(0)
        for e in conn.execute("SELECT from_node FROM edges WHERE to_node=? AND graph_id=?", (n, gid)).fetchall():
            fn_id = e["from_node"]
            if fn_id in visited:
                continue
            visited.add(fn_id)
            fn = conn.execute("SELECT id,type FROM nodes WHERE id=? AND graph_id=?", (fn_id, gid)).fetchone()
            if fn and fn["type"] == type_:
                return fn["id"]
            frontier.append(fn_id)
    return None


def _run_input_guard_conn(conn, nid, ntype, model_id, params, ref_asset_ids):
    """在事务内计算“本次运行实际读取的输入”指纹并返回冻结快照。

    覆盖：节点自身版本、实际上游版本、参考素材顺序、参数、项目 Brief、提示词模板（文本哈希）、模型/服务商配置。
    """
    def nv(x):
        return (conn.execute("SELECT current_version FROM nodes WHERE id=?", (x,)).fetchone() or {"current_version": None})["current_version"]
    deps = {up: nv(_upstream_of_type_conn(conn, nid, up)) for up in RUN_INPUT_UPSTREAMS.get(ntype, ())}
    grow = conn.execute("SELECT graph_id FROM nodes WHERE id=?", (nid,)).fetchone()
    gid = grow["graph_id"] if grow else None
    prow = conn.execute("SELECT project_id FROM graphs WHERE id=?", (gid,)).fetchone() if gid else None
    pid = prow["project_id"] if prow else None
    org = (conn.execute("SELECT organization_id FROM projects WHERE id=?", (pid,)).fetchone() or {"organization_id": None})["organization_id"] if pid else None
    br = conn.execute("SELECT id, updated_at FROM generation_briefs WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,)).fetchone() if pid else None
    br = dict(br) if br else None
    cur = _node_content_conn(conn, nid)
    tpl_id = cur.get("prompt_template_id") or (params or {}).get("prompt_template_id") or ""
    tpl = conn.execute("SELECT id, template_text, enabled FROM prompt_templates WHERE id=?", (tpl_id,)).fetchone() if tpl_id else None
    tpl = dict(tpl) if tpl else None
    mrow = conn.execute("SELECT model_id, provider_id, adapter, enabled, capabilities_json FROM model_registry WHERE model_id=?",
                        (model_id or "",)).fetchone()
    mrow = dict(mrow) if mrow else None
    frozen = {
        "project_id": pid,
        "organization_id": org,
        "node_version": nv(nid),
        "upstream_versions": deps,
        "ref_asset_ids": [x for x in (ref_asset_ids or [])],
        "brief_id": (br or {}).get("id") if br else None,
        "brief_updated_at": (br or {}).get("updated_at") if br else None,
        "prompt_template_id": (tpl or {}).get("id") if tpl else (tpl_id or None),
        "prompt_template_hash": hashlib.sha256(((tpl or {}).get("template_text") or "").encode("utf-8")).hexdigest()[:16] if tpl else None,
        "model_id": (mrow or {}).get("model_id") if mrow else (model_id or None),
        "provider_id": (mrow or {}).get("provider_id") if mrow else None,
        "adapter": (mrow or {}).get("adapter") if mrow else None,
        "model_enabled": (mrow or {}).get("enabled") if mrow else None,
    }
    clean_params = dict(params or {})
    clean_params.pop(RUN_GUARD_KEY, None)
    payload = {"type": ntype, "params": clean_params, "inputs": frozen}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest(), frozen


def _run_input_digest(nid: str, ntype: str, model_id, params, ref_asset_ids) -> str:
    """本次运行“输入快照 + 参数”指纹：自身当前版本 + 实际读取的上游版本 + 参数 + 参考资产顺序。"""
    deps = {}
    for up_type in RUN_INPUT_UPSTREAMS.get(ntype, ()):
        up_id = upstream_of_type(nid, up_type)
        deps[up_type] = _node_version_of(up_id)
    clean_params = dict(params or {})
    clean_params.pop(RUN_GUARD_KEY, None)
    payload = {
        "node_id": nid,
        "type": ntype,
        "node_version": _node_version_of(nid),
        "model_id": model_id or "",
        "params": clean_params,
        "ref_asset_ids": [x for x in (ref_asset_ids or [])],
        "upstream_versions": deps,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _guard_prev_meta(busy) -> dict:
    """读取既有在途运行里保存的守卫信息（历史运行没有该键 → 返回空 → 一律冲突，不复用）。"""
    try:
        pj = json.loads((busy or {}).get("parameters_json") or "{}")
        return (pj or {}).get(RUN_GUARD_KEY) or {}
    except Exception:
        return {}


def _reuse_or_conflict(nid: str, busy: dict, guard: dict) -> dict:
    """在途运行与本次请求比对：能证明同一操作者+同一输入/参数则复用，否则 409 冲突（不泄露 run_id/操作者）。"""
    prev = _guard_prev_meta(busy)
    prev_hash = prev.get("requester_hash") or ""
    same_actor = bool(prev_hash) and bool(guard["requester_hash"]) and prev_hash == guard["requester_hash"]
    same_input = bool(prev.get("digest")) and prev.get("digest") == guard["digest"]
    if same_actor and same_input:
        return {"run_id": busy["id"], "idempotent": True, "reused": True, "status": busy.get("status")}
    raise _busy_run_conflict(nid, busy)


def _db_busy_error() -> HTTPException:
    """数据库繁忙/跨进程写入争抢：明确是服务端瞬时状态，不是网络错误，也不是运行失败。"""
    return HTTPException(503, {"code": "db_busy",
                               "message": "服务端数据库繁忙（其他请求正在写入运行记录），本次未创建运行，请稍后重试。",
                               "retryable": True, "manual_check_required": False})


# ---------------- 在途保护（阶段C补修第三批） ----------------
INFLIGHT_STATES = ("queued", "running")


def _require_runnable():
    """fail-closed：数据库级“同节点最多一条在途运行”约束未生效时，拒绝创建任何新运行。

    历史项目/节点/运行记录仍可只读访问；不提供任何绕过开关，也不清理/改写冲突的历史在途记录。
    """
    if not getattr(db, "INFLIGHT_INDEX_OK", False):
        raise HTTPException(503, {
            "code": "inflight_guard_unavailable",
            "message": ("服务端未启用数据库级“同一节点最多一条在途运行”约束"
                        "（uq_node_runs_one_inflight 未生效），为避免跨进程双跑，已拒绝创建新的运行；"
                        "既有项目、节点与历史运行仍可只读访问。"),
            "reason": (getattr(db, "INFLIGHT_INDEX_ERROR", "") or "index_missing"),
            "manual_check_required": True,
            "retryable": False,
        })


def _guard_scope_has_type(conn, graph_id, type_name):
    """在途运行范围（自身∪上游∪下游）内是否存在指定类型节点。"""
    for run in _runs_of_graph(conn, graph_id):
        for nid in ({run["node_id"]} | run["up"] | run["down"]):
            row = conn.execute("SELECT type FROM nodes WHERE id=?", (nid,)).fetchone()
            if row and row["type"] == type_name:
                return run
    return None


def _guard_brief_inflight(pid, actor):
    """项目 Brief 是在途 image_prompt 的实际输入：命中即 409（无关项目不受影响）。"""
    with db.tx(immediate=True) as conn:
        for g in conn.execute("SELECT id FROM graphs WHERE project_id=?", (pid,)).fetchall():
            hit = _guard_scope_has_type(conn, g["id"], "image_prompt")
            if hit:
                raise _conflict_with_run(actor, hit, "brief_in_use_by_inflight_run",
                                         "该项目的 Brief 正被进行中的提示词运行使用，运行期间禁止修改 Brief。")


def _guard_model_inflight(model_id, actor):
    """模型配置（启用状态/adapter/provider/capabilities）是运行时的实际执行输入：命中在途 run 即 409。"""
    with db.tx(immediate=True) as conn:
        for r in conn.execute("SELECT id,node_id,status,started_at,parameters_json FROM node_runs "
                              "WHERE status IN ('queued','running')").fetchall():
            meta = (_guard_prev_meta(dict(r)) or {}).get("inputs") or {}
            if meta.get("model_id") and meta.get("model_id") == model_id:
                _row = conn.execute("SELECT graph_id FROM nodes WHERE id=?", (r["node_id"],)).fetchone()
                gid = dict(_row)["graph_id"] if _row else None
                run = {k: dict(r)[k] for k in ("id", "node_id", "status", "started_at")}
                run.update({"run_id": r["id"], "up": set(), "down": set(),
                            "guard_hash": (_guard_prev_meta(dict(r)) or {}).get("requester_hash") or ""})
                raise _conflict_with_run(actor, run, "model_in_use_by_inflight_run",
                                         "该模型正被进行中的运行使用，运行期间禁止修改/停用该模型配置。")


def _guard_provider_inflight(provider_id, actor):
    with db.tx(immediate=True) as conn:
        for r in conn.execute("SELECT id,node_id,status,started_at,parameters_json FROM node_runs "
                              "WHERE status IN ('queued','running')").fetchall():
            meta = (_guard_prev_meta(dict(r)) or {}).get("inputs") or {}
            if meta.get("provider_id") and meta.get("provider_id") == provider_id:
                run = {"run_id": r["id"], "node_id": r["node_id"], "status": r["status"],
                       "started_at": r["started_at"], "up": set(), "down": set(),
                       "guard_hash": (_guard_prev_meta(dict(r)) or {}).get("requester_hash") or ""}
                raise _conflict_with_run(actor, run, "provider_in_use_by_inflight_run",
                                         "该服务商正被进行中的运行使用，运行期间禁止修改/删除该服务商配置。")


def _inflight_rows(conn, graph_id=None):
    sql = ("SELECT r.id,r.node_id,r.status,r.started_at,r.parameters_json, n.graph_id AS graph_id "
           "FROM node_runs r LEFT JOIN nodes n ON n.id=r.node_id WHERE r.status IN ('queued','running')")
    args = ()
    if graph_id:
        sql += " AND n.graph_id=?"
        args = (graph_id,)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _reachable_conn(conn, start, direction):
    """direction='up' 沿入边向上游；'down' 沿出边向下游；返回集合（不含 start）。"""
    seen, frontier = set(), [start]
    while frontier:
        cur = frontier.pop()
        if direction == "up":
            rows = conn.execute("SELECT from_node AS other FROM edges WHERE to_node=?", (cur,)).fetchall()
        else:
            rows = conn.execute("SELECT to_node AS other FROM edges WHERE from_node=?", (cur,)).fetchall()
        for r in rows:
            o = r["other"]
            if o not in seen:
                seen.add(o)
                frontier.append(o)
    return seen


def _inflight_scopes(conn, graph_id):
    """{run_id: {run_id,node_id,status,started_at,guard_hash,up,down}}，up/down 为该在途节点的上下游可达集合。"""
    out = {}
    for r in _inflight_rows(conn, graph_id):
        guard = _guard_prev_meta(r)
        out[r["id"]] = {"run_id": r["id"], "node_id": r["node_id"], "status": r["status"],
                        "started_at": r["started_at"], "guard_hash": guard.get("requester_hash") or "",
                        "up": _reachable_conn(conn, r["node_id"], "up"),
                        "down": _reachable_conn(conn, r["node_id"], "down")}
    return out


def _affecting_runs(conn, graph_id, node_ids, include_down=False):
    """返回“自身/上游(可选下游)”覆盖到给定节点集合的在途运行。"""
    ids = {x for x in node_ids if x}
    hits = []
    for run in _inflight_scopes(conn, graph_id).values():
        touched = {run["node_id"]} | run["up"] | (run["down"] if include_down else set())
        if touched & ids:
            hits.append(run)
    return hits


def _runs_of_graph(conn, graph_id):
    return list(_inflight_scopes(conn, graph_id).values())


def _conflict_with_run(actor, run, code, message):
    """安全的 409：只给状态信息；run_id 仅当请求者与在途运行的操作者一致时才返回。"""
    detail = {"code": code, "message": message, "manual_check_required": True,
              "busy_status": run.get("status"), "busy_started_at": run.get("started_at")}
    if run.get("guard_hash") and run["guard_hash"] == _run_requester_hash(actor):
        detail["run_id"] = run.get("run_id")
    return HTTPException(409, detail)


def _write_node_version_conn(conn, node_id, content, author_type="human", model_id=None, input_snapshot=None):
    """与 set_node_version 等价，但在给定连接/事务内执行（避免嵌套事务与锁重入）。"""
    row = conn.execute("SELECT current_version FROM nodes WHERE id=?", (node_id,)).fetchone()
    if not row:
        raise HTTPException(404, "节点不存在")
    nv = (row["current_version"] or 0) + 1
    conn.execute("INSERT INTO node_versions(node_id,version,input_snapshot_json,content_json,locks_json,author_type,model_id,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?)",
                 (node_id, nv, json.dumps(input_snapshot or {}), json.dumps(content), json.dumps([]),
                  author_type, model_id, db.now()))
    conn.execute("UPDATE nodes SET current_version=?, status='ready' WHERE id=?", (nv, node_id))
    return nv


def _mark_stale_conn(conn, node_id):
    for d in _reachable_conn(conn, node_id, "down"):
        conn.execute("UPDATE nodes SET status='stale' WHERE id=? AND status<>'stale'", (d,))


def _validate_edge_conn(conn, from_node, from_port, to_node, to_port):
    fn = conn.execute("SELECT type FROM nodes WHERE id=?", (from_node,)).fetchone()
    tn = conn.execute("SELECT type FROM nodes WHERE id=?", (to_node,)).fetchone()
    if not fn or not tn:
        return "节点不存在"
    if to_node in _reachable_conn(conn, from_node, "down") or to_node == from_node:
        return "该连接会形成环，不予添加"
    rules = {
        ("product_facts", "facts", "strategy"): True,
        ("product_image", "visual_reference", "image_prompt"): True,
        ("strategy", "strategy", "image_prompt"): True,
        ("image_prompt", "prompt", "image_generation"): True,
        ("image_generation", "generated_asset", "review"): True,
        ("review", "review", "layout_export"): True,
    }
    if (fn["type"], from_port, tn["type"]) not in rules:
        return "不支持的连接：%s.%s -> %s.%s" % (fn["type"], from_port, tn["type"], to_port)
    return None


def _node_content_conn(conn, node_id):
    row = conn.execute("SELECT content_json FROM node_versions WHERE node_id=? "
                       "ORDER BY version DESC LIMIT 1", (node_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["content_json"] or "{}")
    except Exception:
        return {}


def _asset_inflight_guard(conn, aid, project_id):
    """该素材是否被在途运行引用（run_inputs 或 在途范围内的节点内容）。返回 run 或 None。"""
    rows = conn.execute("SELECT r.id,r.node_id,r.status,r.started_at,r.parameters_json FROM run_inputs ri "
                        "JOIN node_runs r ON r.id=ri.run_id WHERE ri.asset_id=? "
                        "AND r.status IN ('queued','running')", (aid,)).fetchall()
    for r in rows:
        r = dict(r)
        guard = _guard_prev_meta(r)
        return {"run_id": r["id"], "node_id": r["node_id"], "status": r["status"],
                "started_at": r["started_at"], "guard_hash": guard.get("requester_hash") or ""}
    g = conn.execute("SELECT id FROM graphs WHERE project_id=?", (project_id,)).fetchone()
    if not g:
        return None
    for run in _runs_of_graph(conn, g["id"]):
        for nid in ({run["node_id"]} | run["up"] | run["down"]):
            c = _node_content_conn(conn, nid)
            refs = [c.get("asset_id")] + list(c.get("reference_asset_ids") or [])
            if aid in refs:
                return run
    return None


def _busy_run_conflict(nid: str, busy: dict) -> HTTPException:
    """同节点已有进行中运行时的冲突响应：只给安全状态信息，不泄露 run_id / 操作者 / 组织细节。"""
    started = busy.get("started_at")
    return HTTPException(409, {
        "code": "node_busy",
        "message": ("该节点已有未结束的运行（状态 %s，开始于 %s），且无法证明本次请求与其输入/参数/操作者一致，"
                    "因此未新建运行：需人工排查。" % (busy.get("status"), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)) if started else "未知时间")),
        "node_id": nid,
        "busy_status": busy.get("status"),
        "busy_started_at": started,
        "manual_check_required": True,
        "hint": NODE_BUSY_HINT,
    })


def _create_node_run(nid: str, model_id, params: dict, ref_asset_ids=None, idempotency_key=None,
                     node_version: int = 0, actor=None) -> dict:
    """创建一次节点运行并启动执行线程（HTTP 路由与「重跑下游」共用同一套逻辑）。

    actor 必须是调用方已经通过鉴权的身份：本函数不自行放行，缺少 actor 直接拒绝。
    运行记录、执行线程、版本写入与 stale 传播全部沿用既有路径。
    """
    if actor is None:
        raise HTTPException(500, "内部错误：缺少已认证身份")
    _require_runnable()
    params = params or {}
    n = db.query_one("SELECT type, current_version FROM nodes WHERE id=?", (nid,))
    if not n:
        raise HTTPException(404, "节点不存在")
    # 幂等：相同 idempotency_key 已成功则直接返回（既有行为，保持不变）
    if idempotency_key:
        ex = db.query_one("SELECT * FROM node_runs WHERE idempotency_key=? AND status='succeeded' LIMIT 1", (idempotency_key,))
        if ex:
            return {"run_id": ex["id"], "idempotent": True, "reused": True, "status": "succeeded"}
    model_id = _resolve_run_model(nid, n["type"], model_id)
    gid = db.query_one("SELECT graph_id FROM nodes WHERE id=?", (nid,))["graph_id"]
    gv = db.query_one("SELECT current_version FROM graphs WHERE id=?", (gid,))["current_version"]
    run_id = db.gen_id("run")
    requester_hash = _run_requester_hash(actor)
    # 事务边界内做“同节点进行中检查 + 插入”；跨进程由数据库部分唯一索引兜底
    inflight_sql = ("SELECT id,status,started_at,parameters_json FROM node_runs "
                    "WHERE node_id=? AND status IN ('queued','running') "
                    "ORDER BY started_at DESC LIMIT 1")
    params_for_store = dict(params or {})
    for attempt in (1, 2):
        try:
            with db.tx(immediate=True) as conn:
                if not conn.execute("SELECT id FROM nodes WHERE id=?", (nid,)).fetchone():
                    # 与“删除节点/重置图”互斥：节点已在本事务开始前被删除 → 不产生孤儿运行
                    raise HTTPException(404, "节点不存在或已在创建运行前被删除")
                # 输入指纹必须在同一事务内计算：与 INSERT 原子，避免“先算指纹后入库”导致与实际执行输入不一致
                digest, frozen_inputs = _run_input_guard_conn(conn, nid, n["type"], model_id, params, ref_asset_ids)
                guard = {"requester_hash": requester_hash,
                         "requester_user_id": getattr(actor, "user_id", "") or "",
                         "organization_id": frozen_inputs.get("organization_id"),
                         "digest": digest,
                         "inputs": frozen_inputs}
                params_for_store[RUN_GUARD_KEY] = guard
                accounting = {"organization_id": frozen_inputs.get("organization_id"),
                              "user_id": getattr(actor, "user_id", "") or ""}
                busy = conn.execute(inflight_sql, (nid,)).fetchone()
                if busy:
                    # 能证明是同一操作者的同一输入/参数重复请求 → 复用；否则明确冲突
                    return _reuse_or_conflict(nid, dict(busy), guard)
                conn.execute("INSERT INTO node_runs(id,node_id,node_version,graph_version,status,model_id,parameters_json,idempotency_key,started_at) "
                             "VALUES(?,?,?,?,?,?,?,?,?)",
                             (run_id, nid, node_version or 0, gv, "queued", model_id,
                              json.dumps(params_for_store, ensure_ascii=False), idempotency_key, db.now()))
            break
        except sqlite3.IntegrityError:
            # 跨进程并发：另一进程在同一瞬间已插入在途运行（被部分唯一索引拦下）→ 按同一规则重新判定
            busy2 = db.query_one(inflight_sql, (nid,))
            if busy2:
                return _reuse_or_conflict(nid, busy2, guard)
            if attempt == 2:
                raise _db_busy_error()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                raise _db_busy_error()
            raise
    t = threading.Thread(target=_run_node_thread,
                         args=(run_id, nid, model_id, params, ref_asset_ids or [], idempotency_key, accounting))
    t.daemon = True
    t.start()
    return {"run_id": run_id, "model_id": model_id}


@app.post("/api/nodes/{nid}/runs")
def run_node(nid: str, body: RunReq, request: Request):
    actor = auth.require_actor(request)
    authz.require_node_permission(actor, nid, "generation.run")
    created = _create_node_run(nid, body.model_id, body.params, body.reference_asset_ids,
                               body.idempotency_key, node_version=body.node_version or 0, actor=actor)
    if created.pop("idempotent", False):
        return {"run_id": created["run_id"], "idempotent": True,
                "reused": bool(created.get("reused")), "status": created.get("status")}
    return {"run_id": created["run_id"], "reused": False, "status": "queued"}


@app.get("/api/runs/{rid}")
def get_run(rid: str, request: Request):
    r = authz.require_run_permission(auth.require_actor(request), rid, "job.read")
    if not r:
        raise HTTPException(404, "运行不存在")
    outs = db.query("SELECT asset_id, output_json FROM run_outputs WHERE run_id=?", (rid,))
    params = json_loads(r.get("parameters_json"), {}) or {}
    snapshot = params.get("input_snapshot") if isinstance(params, dict) else None
    out = dict(r)
    if isinstance(params, dict) and RUN_GUARD_KEY in params:
        # 内部守卫数据（含操作者/组织快照）不下发普通接口，避免暴露其他操作者身份
        params = {k: v for k, v in params.items() if k != RUN_GUARD_KEY}
        out["parameters_json"] = json.dumps(params, ensure_ascii=False)
    return {**out, "outputs": outs, "input_snapshot": snapshot}


@app.get("/api/runs/{rid}/events")
def run_events(rid: str, request: Request):
    authz.require_run_permission(auth.require_actor(request), rid, "job.read")

    def gen():
        deadline = time.time() + 60
        last = None
        while time.time() < deadline:
            r = db.query_one("SELECT status,error_code,usage_json FROM node_runs WHERE id=?", (rid,))
            if not r:
                yield "data: " + json.dumps({"event": "error", "msg": "run missing"}) + "\n\n"
                return
            if r["status"] != last:
                outs = db.query("SELECT asset_id FROM run_outputs WHERE run_id=?", (rid,))
                payload = {"event": r["status"], "status": r["status"],
                           "outputs": [o["asset_id"] for o in outs], "usage": json_loads(r["usage_json"]),
                           "error_code": r["error_code"]}
                yield "data: " + json.dumps(payload) + "\n\n"
                last = r["status"]
            if r["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.3)
        yield "data: " + json.dumps({"event": "done"}) + "\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# 「重跑下游」可自动执行的节点类型；其余类型（产品事实/参考图/审核/排版导出）为人工或输入节点，
# 会显式列入 skipped 并给出原因，不静默跳过。
RUN_DOWNSTREAM_TYPES = ("strategy", "image_prompt", "image_generation")
RUN_DOWNSTREAM_TIMEOUT = float(os.environ.get("WB_RUN_DOWNSTREAM_TIMEOUT", "180"))


def topological_downstream(graph_id: str, from_node_id: str) -> list:
    """从起点可达的下游节点拓扑序（不含起点自身）；发现环时报错。"""
    node_ids = [n["id"] for n in db.query("SELECT id FROM nodes WHERE graph_id=?", (graph_id,))]
    children = defaultdict(list)
    for e in db.query("SELECT from_node, to_node FROM edges WHERE graph_id=?", (graph_id,)):
        if e["from_node"] in node_ids and e["to_node"] in node_ids:
            children[e["from_node"]].append(e["to_node"])
    # 可达子图（只跟出边，起点自身不计入）
    reachable, seen, frontier = [], {from_node_id}, [from_node_id]
    while frontier:
        cur = frontier.pop()
        for child in children.get(cur, []):
            if child not in seen:
                seen.add(child)
                reachable.append(child)
                frontier.append(child)
    # Kahn 拓扑排序（入度只统计可达子图内部）
    indeg = {nid: 0 for nid in reachable}
    for nid in reachable:
        for child in children.get(nid, []):
            if child in indeg:
                indeg[child] += 1
    queue = deque([nid for nid in reachable if indeg[nid] == 0])
    order = []
    while queue:
        nid = queue.popleft()
        order.append(nid)
        for child in children.get(nid, []):
            if child in indeg:
                indeg[child] -= 1
                if indeg[child] == 0:
                    queue.append(child)
    if len(order) != len(reachable):
        raise HTTPException(409, "工作流存在环，无法按依赖顺序重跑下游")
    return order


def _downstream_require_inputs(nid: str, ntype: str) -> None:
    """执行前校验必要上游输入；缺失时给出明确错误，不静默跳过。"""
    if ntype == "image_prompt":
        strat_id = upstream_of_type(nid, "strategy")
        strat = get_current_version(strat_id) if strat_id else {}
        if not (strat or {}).get("strategies"):
            raise HTTPException(422, f"提示词节点 {nid} 缺少上游策略内容（策略为空或策略节点未执行）")
    elif ntype == "image_generation":
        pnode = upstream_of_type(nid, "image_prompt")
        pc = get_current_version(pnode) if pnode else {}
        if not str(pc.get("prompt") or "").strip():
            raise HTTPException(422, f"生图节点 {nid} 缺少上游提示词（提示词为空或提示词节点未执行）")


def _wait_node_run(run_id: str, timeout: float) -> dict:
    """等待一次运行结束（成功/失败/取消），返回最终 node_runs 行。"""
    deadline = time.time() + timeout
    row = {}
    while time.time() < deadline:
        row = db.query_one("SELECT status, error_code FROM node_runs WHERE id=?", (run_id,)) or {}
        if row.get("status") in ("succeeded", "failed", "canceled"):
            return row
        time.sleep(0.2)
    return row


@app.post("/api/graphs/{gid}/run-downstream")
def run_downstream(gid: str, body: dict, request: Request):
    """按 DAG 依赖顺序重跑从起点可达的下游节点（策略 → 提示词 → 生图）。

    - 只执行从起点可达、类型受支持的节点；未连接的分支不会被执行
    - 每个节点等待其运行结束后才继续，失败立即停止后续依赖执行
    - 返回：order（拓扑序）、ran（已完成）、skipped（不自动执行）、failed、not_executed
    """
    actor = auth.require_actor(request)
    authz.require_graph_permission(actor, gid, "generation.run")
    from_node = (body or {}).get("from_node_id")
    start = db.query_one("SELECT id,type FROM nodes WHERE id=? AND graph_id=?", (from_node, gid))
    if not start:
        raise HTTPException(404, "起始节点不存在或不属于该图")

    order = topological_downstream(gid, from_node)
    ran, skipped, pending = [], [], []
    pending_run_ids = []
    failed = None
    not_executed = []
    final_status = "ok"

    def _type_of(node_id):
        return (db.query_one("SELECT type FROM nodes WHERE id=?", (node_id,)) or {}).get("type")

    for idx, nid in enumerate(order):
        ntype = _type_of(nid)
        if ntype not in RUN_DOWNSTREAM_TYPES:
            skipped.append({"node_id": nid, "type": ntype, "reason": "该类型不由「重跑下游」自动执行"})
            continue
        try:
            _downstream_require_inputs(nid, ntype)
            v_before = (db.query_one("SELECT current_version FROM nodes WHERE id=?", (nid,)) or {}).get("current_version")
            created = _create_node_run(nid, None, {}, [], None, actor=actor)
            run_id = created["run_id"]
            result = _wait_node_run(run_id, RUN_DOWNSTREAM_TIMEOUT)
            v_after = (db.query_one("SELECT current_version FROM nodes WHERE id=?", (nid,)) or {}).get("current_version")
            entry = {"node_id": nid, "type": ntype, "run_id": run_id, "model_id": created.get("model_id"),
                     "status": result.get("status"), "version_before": v_before, "version_after": v_after}
            if created.get("reused"):
                entry["reused"] = True
            st = result.get("status")
            if st not in ("succeeded", "failed", "canceled"):
                # 等待超时但运行仍是 queued/running：不判失败、不起第二个 run、不启动后续节点
                entry["status"] = "running"
                entry["message"] = "等待超时，该节点仍在运行；未启动后续节点，也未重试"
                pending.append(entry)
                pending_run_ids.append(run_id)
                pending += [{"node_id": x, "type": _type_of(x), "status": "pending",
                             "reason": "等待上游运行结束"} for x in order[idx + 1:]]
                final_status = "running"
                break
            if st != "succeeded":
                entry["error"] = result.get("error_code") or "运行失败"
                failed = entry
                not_executed = [{"node_id": x, "type": _type_of(x)} for x in order[idx + 1:]]
                final_status = "failed"
                break
            ran.append(entry)
        except HTTPException as e:
            detail = e.detail
            code = detail.get("code") if isinstance(detail, dict) else None
            msg = (detail.get("message") if isinstance(detail, dict) and detail.get("message")
                   else (detail if isinstance(detail, str) else "服务端拒绝本次运行"))
            entry = {"node_id": nid, "type": ntype,
                     "status": "blocked" if code == "node_busy" else "rejected",
                     "error": msg, "status_code": e.status_code}
            if isinstance(detail, dict):
                if detail.get("busy_status"):
                    entry["busy_status"] = detail["busy_status"]
                if detail.get("busy_started_at"):
                    entry["busy_started_at"] = detail["busy_started_at"]
                if detail.get("manual_check_required"):
                    entry["manual_check_required"] = True
                if detail.get("hint"):
                    entry["hint"] = detail["hint"]
            failed = entry
            not_executed = [{"node_id": x, "type": _type_of(x)} for x in order[idx + 1:]]
            final_status = "blocked" if code == "node_busy" else "failed"
            break

    return {"graph_id": gid, "from_node_id": from_node, "order": order,
            "ran": ran, "skipped": skipped, "failed": failed, "not_executed": not_executed,
            "pending": pending, "pending_run_ids": pending_run_ids, "status": final_status}


# ---------------- models ----------------
@app.get("/api/models")
def models(request: Request):
    # 只读清单：任何已登录用户都需要（创作者要选模型）
    authz.require_capability(auth.require_actor(request), "model.read")
    return registry.get_models()


class ModelCreate(BaseModel):
    model_id: str
    provider: str = "local"
    modality: str = "image"
    capabilities_json: Union[dict, str] = {}
    parameter_schema: Union[dict, str] = {}
    enabled: int = 1
    cost_policy: str = "free_local"
    workflow_version: str = "v1"
    provider_id: Optional[str] = None
    adapter: Optional[str] = None


class ModelUpdate(BaseModel):
    provider: Optional[str] = None
    modality: Optional[str] = None
    capabilities_json: Optional[Union[dict, str]] = None
    parameter_schema: Optional[Union[dict, str]] = None
    enabled: Optional[int] = None
    cost_policy: Optional[str] = None
    workflow_version: Optional[str] = None
    provider_id: Optional[str] = None
    adapter: Optional[str] = None


@app.post("/api/models", status_code=201)
def create_model_api(m: ModelCreate, request: Request):
    authz.require_platform(auth.require_actor(request), "model.manage")
    try:
        return registry.create_model(m.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/models/{model_id}")
def get_model_api(model_id: str, request: Request):
    authz.require_capability(auth.require_actor(request), "model.read")
    m = registry.get_model(model_id)
    if not m:
        raise HTTPException(404, "模型不存在")
    return m


@app.put("/api/models/{model_id}")
def update_model_api(model_id: str, patch: ModelUpdate, request: Request):
    _guard_model_inflight(model_id, auth.require_actor(request))
    authz.require_platform(auth.require_actor(request), "model.manage")
    try:
        m = registry.update_model(model_id, patch.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not m:
        raise HTTPException(404, "模型不存在")
    return m


@app.delete("/api/models/{model_id}")
def delete_model_api(model_id: str, request: Request):
    _guard_model_inflight(model_id, auth.require_actor(request))
    authz.require_platform(auth.require_actor(request), "model.manage")
    registry.delete_model(model_id)
    return {"ok": True}


# ---------------- providers ---------------- 
class ProviderCreate(BaseModel):
    id: Optional[str] = None
    name: str
    base_url: str = ""
    api_key: str = ""
    enabled: int = 1


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    enabled: Optional[int] = None


@app.get("/api/providers")
def list_providers_api(request: Request):
    authz.require_platform(auth.require_actor(request), "provider.manage")
    return [{"id": p["id"], "name": p["name"], "base_url": p["base_url"], "enabled": p["enabled"],
             "has_api_key": bool(p.get("api_key"))} for p in registry.list_providers()]


@app.post("/api/providers", status_code=201)
def create_provider_api(p: ProviderCreate, request: Request):
    authz.require_platform(auth.require_actor(request), "provider.manage")
    try:
        return registry.create_provider(p.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/providers/{pid}")
def update_provider_api(pid: str, patch: ProviderUpdate, request: Request):
    _guard_provider_inflight(pid, auth.require_actor(request))
    authz.require_platform(auth.require_actor(request), "provider.manage")
    prov = registry.update_provider(pid, patch.model_dump(exclude_unset=True))
    if not prov:
        raise HTTPException(404, "服务商不存在")
    return prov


@app.delete("/api/providers/{pid}")
def delete_provider_api(pid: str, request: Request):
    _guard_provider_inflight(pid, auth.require_actor(request))
    authz.require_platform(auth.require_actor(request), "provider.manage")
    registry.delete_provider(pid)
    return {"ok": True}


@app.post("/api/providers/{pid}/test")
def test_provider_api(pid: str, request: Request):
    authz.require_platform(auth.require_actor(request), "provider.manage")
    p = registry.get_provider(pid)
    if not p:
        raise HTTPException(404, "服务商不存在")
    base = (p["base_url"] or "").rstrip("/")
    key = p["api_key"] or ""
    if not base:
        return {"success": False, "error": "未配置 Base URL"}
    if not key:
        return {"success": False, "error": "未配置 API Key"}
    try:
        req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode("utf-8"))
        ids = [m.get("id") for m in body.get("data", []) if isinstance(m, dict)]
        return {"success": True, "provider": p["name"], "available_models": ids[:60]}
    except Exception as e:
        return {"success": False, "provider": p["name"], "error": str(e)[:200]}


# ---------------- image tools（节点悬浮工具栏意图） ----------------
@app.get("/api/image-tools/intents")
def image_tool_intents(request: Request):
    authz.require_capability(auth.require_actor(request), "model.read")
    """返回所有图片处理意图，以及每个意图当前是否可用（依赖 enabled 且含对应 editing_mode 的模型）。"""
    from .generators import image as image_gen
    import copy
    models = registry.get_models(enabled_only=True)
    # editing_mode -> 可用模型
    mode_models: dict = {}
    for m in models:
        cap = registry.capabilities(m["model_id"])
        for ed in cap.get("editing_modes", []) or []:
            mode_models.setdefault(ed, []).append(m["model_id"])
    # 额外：text_overlay 由本地合成器兜底的意图
    out = []
    for intent, info in image_gen.INTENT_MODE.items():
        if intent in ("替换",):
            out.append({"intent": intent, "available": True, "model_id": None, "note": "切换底图资产（无需模型）"})
            continue
        if intent in ("改字", "图文分层"):
            local_ok = any(mm["model_id"] == "local-poster-compositor" for mm in models)
            out.append({"intent": intent, "available": local_ok, "model_id": "local-poster-compositor" if local_ok else None,
                        "note": "本地合成器叠加文字层" if local_ok else "未接入：启用 local-poster-compositor"})
            continue
        avail = info["mode"] in mode_models
        out.append({"intent": intent, "available": avail,
                    "model_id": mode_models.get(info["mode"], [None])[0],
                    "note": f"需模型含 editing_mode={info['mode']}" if avail else f"未接入：需启用含 {info['mode']} 的模型"})
    return out


class ImageToolRun(BaseModel):
    node_id: str
    intent: str
    asset_id: Optional[str] = None
    model_id: Optional[str] = None
    title: str = ""
    subtitle: str = ""


@app.post("/api/image-tools/run")
def image_tool_run(body: ImageToolRun, request: Request):
    actor = auth.require_actor(request)
    node = authz.require_node_permission(actor, body.node_id, "generation.run")
    pid = (node.get("project") or {}).get("id")
    authz.assert_assets_in_project(pid, [body.asset_id] if body.asset_id else [], field="image_tool")
    n = db.query_one("SELECT id,type,graph_id FROM nodes WHERE id=?", (body.node_id,))
    if not n:
        raise HTTPException(404, "节点不存在")
    asset_ids = [body.asset_id] if body.asset_id else []
    if not asset_ids:
        cur = get_current_version(body.node_id) or {}
        asset_ids = (cur.get("outputs") or [])[:1]
    if not asset_ids:
        raise HTTPException(422, "缺少底图资产：请在节点上先生成，或指定 asset_id")
    pid = get_project_of_node(body.node_id)
    for aid in asset_ids:
        owned = db.query_one("SELECT id FROM assets WHERE id=? AND project_id=?", (aid, pid))
        if not owned:
            raise HTTPException(404, "底图资产不存在或不属于当前项目")
    try:
        res = image_gen.dispatch_tool(body.intent, asset_ids, body.title, body.subtitle, body.model_id)
    except ValueError as e:
        raise HTTPException(422, str(e))
    outputs, saved_keys = [], []
    for o in res.get("outputs", []):
        aid = db.gen_id("ast")
        obj_key = f"{pid}/{aid}.png"
        storage.save_bytes(obj_key, o["bytes"])
        saved_keys.append(obj_key)
        outputs.append({"asset_id": aid, "object_key": obj_key, "width": o["width"], "height": o["height"]})
    if not outputs:
        return {"outputs": [], "usage": res.get("usage", {}), "status": res.get("status", "succeeded")}
    try:
        with db.tx(immediate=True) as conn:
            row = conn.execute("SELECT graph_id FROM nodes WHERE id=?", (body.node_id,)).fetchone()
            if not row:
                raise HTTPException(404, "节点不存在")
            hits = _affecting_runs(conn, row["graph_id"], {body.node_id})
            if hits:
                raise _conflict_with_run(actor, hits[0], "node_input_inflight",
                                         "该节点处于在途运行的输入范围，运行期间禁止用图像工具覆盖其输出。")
            for o in outputs:
                conn.execute("INSERT INTO assets(id,tenant_id,project_id,kind,role,object_key,sha256,mime,width,height,created_at,source,origin,usage_rights_status) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (o["asset_id"], "tnt_default", pid, "image", "generated", o["object_key"], "", "image/png",
                              o["width"], o["height"], db.now(), "image_tool", body.intent, "generated"))
            cur = _node_content_conn(conn, body.node_id)
            all_out = (cur.get("outputs", []) or []) + [o["asset_id"] for o in outputs]
            _write_node_version_conn(conn, body.node_id, {**cur, "outputs": all_out, "last_tool": body.intent}, author_type="ai")
    except HTTPException:
        for k in saved_keys:
            try:
                storage.delete(k)
            except Exception:
                pass
        raise
    return {"outputs": outputs, "usage": res.get("usage", {}), "status": res.get("status", "succeeded")}
# ---------------- 最终文案排版安全（阶段D切片3A，仅 m2 使用） ----------------
COPY_MARGIN_X = 0.08             # 左右边距（占画布宽）：可见字形不得越过
COPY_TITLE_BAND = (0.72, 0.86)   # 标题安全带（占画布高）
COPY_SUB_BAND = (0.87, 0.96)     # 副标题安全带（占画布高）
COPY_PRODUCT_SAFE_BOTTOM = 0.72  # 产品主体安全区下界：文字不得进入 y < 0.72h
COPY_TITLE_SIZE, COPY_TITLE_SIZE_MIN = 0.070, 0.030
COPY_SUB_SIZE, COPY_SUB_SIZE_MIN = 0.038, 0.020
COPY_TITLE_MAX_LINES, COPY_SUB_MAX_LINES = 2, 2
COPY_LINE_SPACING = 1.12         # 行距倍数（相对字体行高）
COPY_MIN_GAP = 0.010             # 标题块与副标题块最小间隔（占画布高）
_COPY_MEASURE = ImageDraw.Draw(Image.new("RGB", (8, 8)))


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3000 <= o <= 0x303F or 0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x4DBF
            or 0x4E00 <= o <= 0x9FFF or 0xFF00 <= o <= 0xFFEF)


def _copy_tokens(text):
    """换行单位：CJK/全角逐字；拉丁/数字按连续词；空白作为显式 token（换行时丢弃行尾空格，不吞字符）。"""
    toks, buf = [], ""
    def flush():
        nonlocal buf
        if buf:
            toks.append(("w", buf)); buf = ""
    for ch in text:
        if ch.isspace():
            flush()
            if toks and toks[-1][0] != "s":
                toks.append(("s", " "))
        elif _is_cjk(ch):
            flush(); toks.append(("c", ch))
        else:
            buf += ch
    flush()
    return toks


def _copy_wrap(tokens, font, max_w):
    """按可见行宽（font.getlength）贪心换行；最小粒度仍超宽时返回 None（不截断）。"""
    lines, cur = [], ""
    for kind, tk in tokens:
        if kind == "s" and not cur:
            continue
        cand = cur + tk
        if font.getlength(cand) <= max_w:
            cur = cand
            continue
        if cur:
            lines.append(cur.rstrip()); cur = ""
            if kind == "s":
                continue
        if font.getlength(tk) <= max_w:
            cur = tk
            continue
        rest = tk
        while rest:
            n = 0
            while n < len(rest) and font.getlength(rest[:n + 1]) <= max_w:
                n += 1
            if n == 0:
                return None
            if n < len(rest):
                lines.append(rest[:n]); rest = rest[n:]
            else:
                cur = rest; rest = ""
    if cur:
        lines.append(cur.rstrip())
    return [x for x in lines if x]


def _copy_fit(text, w, h, band, size_hi, size_lo, max_lines):
    """在一个安全带内规划文字块：先试最大字号，放不下则逐级缩字号；返回 None 表示放不下。"""
    from .generators.image import _font
    max_w = w * (1 - 2 * COPY_MARGIN_X)
    band_top, band_bottom = int(h * band[0]), int(h * band[1])
    band_h = band_bottom - band_top
    size = max(1, int(h * size_hi))
    floor = max(8, int(h * size_lo))
    while size >= floor:
        font = _font(size)
        lines = _copy_wrap(_copy_tokens(text), font, max_w)
        if lines is not None and 0 < len(lines) <= max_lines:
            asc, desc = font.getmetrics()
            line_h = asc + desc
            step = int(round(line_h * COPY_LINE_SPACING))
            block_h = step * (len(lines) - 1) + line_h
            if block_h <= band_h:
                top = band_top + (band_h - block_h) // 2
                placed, x0, y0, x1, y1 = [], None, None, None, None
                for i, ln in enumerate(lines):
                    lx = int(round((w - font.getlength(ln)) / 2.0))
                    ly = top + i * step
                    bb = _COPY_MEASURE.textbbox((0, 0), ln, font=font)
                    ix0, iy0, ix1, iy1 = lx + bb[0], ly + bb[1], lx + bb[2], ly + bb[3]
                    placed.append((ln, lx, ly, (ix0, iy0, ix1, iy1)))
                    x0 = ix0 if x0 is None else min(x0, ix0)
                    y0 = iy0 if y0 is None else min(y0, iy0)
                    x1 = ix1 if x1 is None else max(x1, ix1)
                    y1 = iy1 if y1 is None else max(y1, iy1)
                if x0 < w * COPY_MARGIN_X or x1 > w * (1 - COPY_MARGIN_X):
                    return None
                if y0 < max(band_top, h * COPY_PRODUCT_SAFE_BOTTOM) or y1 > band_bottom:
                    return None
                return {"size": size, "font": font, "lines": placed, "ink": (x0, y0, x1, y1)}
        size -= 1
    return None


def _plan_copy_layout(w, h, title, subtitle):
    """m2 最终文案排版规划（在打开底图/创建文件之前完成）。返回计划；任意一块放不下则返回 None。"""
    plan = {"title": None, "subtitle": None}
    title = (title or "").strip()
    subtitle = (subtitle or "").strip()
    if w <= 0 or h <= 0:
        return None
    if title:
        plan["title"] = _copy_fit(title, w, h, COPY_TITLE_BAND, COPY_TITLE_SIZE, COPY_TITLE_SIZE_MIN, COPY_TITLE_MAX_LINES)
        if plan["title"] is None:
            return None
    if subtitle:
        plan["subtitle"] = _copy_fit(subtitle, w, h, COPY_SUB_BAND, COPY_SUB_SIZE, COPY_SUB_SIZE_MIN, COPY_SUB_MAX_LINES)
        if plan["subtitle"] is None:
            return None
    if plan["title"] and plan["subtitle"]:
        if plan["title"]["ink"][3] + h * COPY_MIN_GAP > plan["subtitle"]["ink"][1]:
            return None
    return plan


# ---------------- layout / export ----------------
@app.post("/api/projects/{pid}/layout/export")
def export_layout(pid: str, body: dict, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.export")
    authz.assert_assets_in_project(pid, [body.get("base_asset_id")], field="layout_export")
    """分层排版导出：基于生成图资产 + 可编辑文字层，重新合成最终 PNG。"""
    base_asset = body.get("base_asset_id")
    if not base_asset:
        raise HTTPException(422, "缺少 base_asset_id")
    _proj = db.query_one("SELECT id, COALESCE(template_id,'') AS tpl FROM projects WHERE id=?", (pid,))
    if not _proj:
        raise HTTPException(404, "项目不存在")
    _tpl = (_proj["tpl"] or "")
    if _tpl == "m2":
        # —— 切片2B：底图来源校验（在任何打开底图/绘字/写文件之前）——
        n_layout = db.query_one("SELECT n.id AS nid FROM nodes n JOIN graphs g ON g.id=n.graph_id "
                                "WHERE g.project_id=? AND n.type='layout_export'", (pid,))
        saved_base = ""
        if n_layout:
            _v = db.query_one("SELECT content_json FROM node_versions WHERE node_id=? ORDER BY version DESC LIMIT 1", (n_layout["nid"],))
            try:
                saved_base = (json_loads((_v or {}).get("content_json"), {}) or {}).get("base_asset_id") or ""
            except Exception:
                saved_base = ""
        if not saved_base or saved_base != base_asset:
            raise HTTPException(409, {"code": "base_not_saved", "message": "请先保存底图选择（当前节点已保存的底图与请求不一致）"})
        a = db.query_one("SELECT object_key,width,height FROM assets WHERE id=? AND project_id=?", (base_asset, pid))
        if not a:
            raise HTTPException(409, {"code": "base_cross_project", "message": "底图不存在或不属于当前项目"})
        prov_all = db.query("SELECT ro.run_id FROM run_outputs ro WHERE ro.asset_id=?", (base_asset,))
        if len(prov_all) == 0:
            raise HTTPException(409, {"code": "base_has_baked_text", "message": "该底图无“无最终文案”来源记录，请重新生成后再导出"})
        if len(prov_all) != 1:
            raise HTTPException(409, {"code": "base_provenance_ambiguous", "message": "该底图的来源记录不唯一，保守拒绝导出"})
        prov = db.query_one("SELECT r.status AS st, r.model_id AS mdl, n.type AS ntype, g.project_id AS gpid, p.template_id AS tpl,"
                            " json_type(ro.output_json, '$.no_final_copy') AS jt,"
                            " json_type(ro.output_json, '$.copy_layer') AS clt,"
                            " json_extract(ro.output_json, '$.copy_layer') AS clv"
                            " FROM run_outputs ro JOIN node_runs r ON r.id=ro.run_id JOIN nodes n ON n.id=r.node_id"
                            " JOIN graphs g ON g.id=n.graph_id JOIN projects p ON p.id=g.project_id WHERE ro.asset_id=?", (base_asset,))
        if not prov or prov["st"] != "succeeded" or prov["ntype"] != "image_generation" or prov["gpid"] != pid \
           or prov["tpl"] != "m2" or prov["mdl"] != "local-poster-compositor":
            raise HTTPException(409, {"code": "base_has_baked_text", "message": "该底图不是本项目 m2 本地拼接器的成功产物，请重新生成后再导出"})
        if prov["jt"] != "true":
            raise HTTPException(409, {"code": "base_has_baked_text", "message": "该底图未带服务端“无最终文案”标记，请重新生成后再导出"})
        if prov["clt"] != "text" or prov["clv"] != "none-v2":
            raise HTTPException(409, {"code": "base_has_baked_text", "message": "该底图缺少服务端“无字底图”版本标记（copy_layer=none-v2），请重新生成底图后再导出"})
        _plan = _plan_copy_layout(int(a["width"] or 0), int(a["height"] or 0), body.get("title"), body.get("subtitle"))
        if _plan is None:
            raise HTTPException(409, {"code": "copy_layout_unsafe", "message": "标题/副标题在安全排版区内放不下（已尝试换行与缩小字号），请缩短文案后重试。"})
    else:
        # legacy path for non-m2 / template_id=NULL (route-level permission and project-ownership guards already applied above)
        _plan = None
        a = db.query_one("SELECT object_key,width,height FROM assets WHERE id=? AND project_id=?", (base_asset, pid))
        if not a:
            raise HTTPException(404, "底图不存在或不属于当前项目")
    base_im = storage.read_pillow(a["object_key"]).convert("RGB")
    if _plan is not None and (base_im.width, base_im.height) != (int(a["width"] or 0), int(a["height"] or 0)):
        raise HTTPException(409, {"code": "copy_layout_unsafe", "message": "底图实际尺寸与记录不一致，无法安全排版，请重新生成底图。"})
    title = body.get("title", "")
    subtitle = body.get("subtitle", "")
    # 在底部叠加文字层（不重绘背景/产品，满足“改字不重生图”）
    draw = ImageDraw.Draw(base_im)
    from .generators.image import _font, _hex
    w, h = base_im.size
    if _plan is not None:
        # 3A：m2 仅在已校验的无字底图上按规划绘制一次最终文案（不截断、不涂盖、不改写底图）
        if _plan["title"]:
            for _ln, _lx, _ly, _ in _plan["title"]["lines"]:
                draw.text((_lx, _ly), _ln, font=_plan["title"]["font"], fill=(40, 40, 40, 255))
        if _plan["subtitle"]:
            for _ln, _lx, _ly, _ in _plan["subtitle"]["lines"]:
                draw.text((_lx, _ly), _ln, font=_plan["subtitle"]["font"], fill=(70, 70, 70, 255))
    else:
        if title:
            f = _font(int(h * 0.07))
            draw.text((w // 2, int(h * 0.78)), title, font=f, fill=(40, 40, 40, 255), anchor="mm")
        if subtitle:
            f = _font(int(h * 0.038))
            draw.text((w // 2, int(h * 0.90)), subtitle, font=f, fill=(70, 70, 70, 255), anchor="mm")
    buf = io.BytesIO()
    base_im.save(buf, format="PNG")
    aid = db.gen_id("ast")
    obj_key = f"{pid}/{aid}.png"
    storage.save_bytes(obj_key, buf.getvalue())
    db.execute("INSERT INTO assets(id,tenant_id,project_id,kind,role,object_key,sha256,mime,width,height,created_at,source,origin,usage_rights_status) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (aid, "tnt_default", pid, "image", "export", obj_key, "", "image/png", w, h, db.now(), "layout_export", "manual", "exported"))
    # 记录导出
    db.execute("INSERT INTO export_records(id,canvas_version,image_asset_id,format,width,height,approval_state) VALUES(?,?,?,?,?,?,?)",
               (db.gen_id("exp"), 1, aid, "png", w, h, "reviewing"))
    return {"asset_id": aid, "width": w, "height": h}


@app.get("/api/projects/{pid}/export")
def export_project(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.export")
    g = db.query_one("SELECT * FROM graphs WHERE project_id=?", (pid,))
    nodes = db.query("SELECT id,type,position_json,current_version,status FROM nodes WHERE graph_id=?", (g["id"],))
    edges = db.query("SELECT from_node,from_port,to_node,to_port,semantic FROM edges WHERE graph_id=?", (g["id"],))
    node_list = []
    for n in nodes:
        content = get_current_version(n["id"]) or {}
        node_list.append({"id": n["id"], "type": n["type"], "position": json_loads(n["position_json"]),
                          "version": n["current_version"], "status": n["status"], "content": content})
    payload = {
        "project_id": pid,
        "exported_at": db.now(),
        "graph_version": g["current_version"],
        "nodes": node_list,
        "edges": [dict(e) for e in edges],
        "asset_manifest": db.query("SELECT id,role,object_key,width,height FROM assets WHERE project_id=?", (pid,)),
    }
    return JSONResponse(content=payload)


@app.get("/api/projects/{pid}/export-package")
def export_package(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.export")
    g = db.query_one("SELECT * FROM graphs WHERE project_id=?", (pid,))
    nodes = db.query("SELECT id,type,position_json,current_version,status FROM nodes WHERE graph_id=?", (g["id"],))
    edges = db.query("SELECT from_node,from_port,to_node,to_port,semantic FROM edges WHERE graph_id=?", (g["id"],))
    node_list = [{"id": n["id"], "type": n["type"], "position": json_loads(n["position_json"]),
                  "version": n["current_version"], "status": n["status"], "content": get_current_version(n["id"]) or {}} for n in nodes]
    payload = {"project_id": pid, "graph_version": g["current_version"], "nodes": node_list,
               "edges": [dict(e) for e in edges]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project.json", json.dumps(payload, ensure_ascii=False, indent=2))
        assets = db.query("SELECT id,object_key FROM assets WHERE project_id=?", (pid,))
        for a in assets:
            try:
                data = storage.read_bytes(a["object_key"])
                z.writestr(f"assets/{a['id']}.png", data)
            except Exception:
                pass
        z.writestr("README.txt", "企业营销生图工作台导出包：project.json 为节点图与提示词；assets/ 为素材。导入时按 asset id 还原引用。")
    buf.seek(0)
    headers = {"Content-Disposition": f'attachment; filename="{pid}_export.zip"'}
    return Response(content=buf.getvalue(), media_type="application/zip", headers=headers)


# ---------------- agent plan (P1 草案，不自动执行) ----------------
class AgentPlanReq(BaseModel):
    instruction: str


@app.post("/api/projects/{pid}/agent/plan")
def agent_plan(pid: str, body: AgentPlanReq, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.write")
    g = db.query_one("SELECT current_version FROM graphs WHERE project_id=?", (pid,))
    plan = {
        "instruction": body.instruction,
        "proposed": ["为当前产品生成三套促销视觉（主图/场景/细节）", "对各生图节点应用选定模型", "产出后可进入分层排版"],
        "estimated_steps": 3,
        "cost_note": "需用户批准后才会调用付费图片模型",
    }
    pid_row = db.gen_id("plan")
    db.execute("INSERT INTO agent_plans(id,project_id,base_graph_version,proposed_operations_json,estimated_usage_json,status,created_at) "
               "VALUES(?,?,?,?,?,?,?)",
               (pid_row, pid, g["current_version"], json.dumps(plan["proposed"]), json.dumps({"estimated_steps": 3}), "draft", db.now()))
    return {"plan_id": pid_row, **plan, "status": "draft"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


# 生产便捷：若前端已构建 dist，则单端口托管（API 路由已在上方注册，优先匹配）
import os as _os
_DIST = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), "..", "..", "frontend", "dist"))
# ---------------- 后台管理 ----------------
@app.get("/api/admin/stats")
def admin_stats(request: Request):
    actor = auth.require_actor(request)
    authz.require_capability(actor, "usage.read")
    orgs = authz.visible_org_ids(actor)
    p_clause, p_args = authz.org_scope_clause(actor, "organization_id")

    def cnt(sql, params=()):
        r = db.query_one(sql, tuple(params))
        return r["c"] if r else 0

    return {
        "scope": "platform" if orgs is None else "organization",
        "projects": cnt("SELECT count(*) c FROM projects WHERE 1=1" + p_clause, p_args),
        "assets": cnt("SELECT count(*) c FROM assets a JOIN projects p ON p.id=a.project_id WHERE 1=1" + p_clause, p_args),
        "models": cnt("SELECT count(*) c FROM model_registry"),
        "providers": cnt("SELECT count(*) c FROM providers"),
        "runs": cnt("SELECT count(*) c FROM node_runs r JOIN nodes n ON n.id=r.node_id JOIN graphs g ON g.id=n.graph_id "
                    "JOIN projects p ON p.id=g.project_id WHERE 1=1" + p_clause, p_args),
        "audits": cnt("SELECT count(*) c FROM audit_events a LEFT JOIN projects p ON p.id=a.project_id WHERE 1=1" + p_clause, p_args),
        "usage_calls": cnt("SELECT count(*) c FROM usage_records WHERE 1=1" + p_clause, p_args),
        "total_cost": round(float((db.query_one("SELECT COALESCE(SUM(cost),0) c FROM usage_records WHERE 1=1" + p_clause, tuple(p_args)) or {"c": 0})["c"] or 0), 4),
    }


@app.get("/api/assets")
def list_all_assets(request: Request):
    actor = auth.require_actor(request)
    authz.require_admin_area(actor)
    authz.require_capability(actor, "asset.read")
    clause, args = authz.org_scope_clause(actor, "p.organization_id")
    return db.query("SELECT a.id, a.project_id, a.role, a.mime, a.width, a.height, a.created_at, a.source "
                    "FROM assets a JOIN projects p ON p.id=a.project_id WHERE 1=1" + clause
                    + " ORDER BY a.created_at DESC LIMIT 200", tuple(args))


@app.delete("/api/assets/{aid}")
def delete_asset(aid: str, request: Request):
    actor = auth.require_actor(request)
    asset = authz.require_asset_permission(actor, aid, "asset.delete")
    object_key = ""
    with db.tx(immediate=True) as conn:
        row = conn.execute("SELECT object_key, project_id FROM assets WHERE id=?", (aid,)).fetchone()
        if not row:
            raise HTTPException(404, "素材不存在")
        object_key = row["object_key"]
        hit = _asset_inflight_guard(conn, aid, row["project_id"])
        if hit:
            raise _conflict_with_run(actor, hit, "asset_in_use_by_inflight_run",
                                     "该素材正被进行中的运行引用，运行期间禁止删除（数据库与文件均未改动）。")
        conn.execute("DELETE FROM assets WHERE id=?", (aid,))
    try:
        storage.delete(object_key)
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/runs")
def list_runs(request: Request):
    actor = auth.require_actor(request)
    authz.require_capability(actor, "job.read")
    clause, args = authz.org_scope_clause(actor, "p.organization_id")
    return db.query("SELECT r.id, r.node_id, r.model_id, r.status, r.started_at, r.ended_at, r.error_code "
                    "FROM node_runs r JOIN nodes n ON n.id=r.node_id JOIN graphs g ON g.id=n.graph_id "
                    "JOIN projects p ON p.id=g.project_id WHERE 1=1" + clause
                    + " ORDER BY r.started_at DESC LIMIT 50", tuple(args))


@app.get("/api/audit")
def list_audit(request: Request):
    actor = auth.require_actor(request)
    authz.require_admin_area(actor)
    authz.require_capability(actor, "audit.read")
    clause, args = authz.org_scope_clause(actor, "p.organization_id")
    return db.query("SELECT a.* FROM audit_events a LEFT JOIN projects p ON p.id=a.project_id WHERE 1=1" + clause
                    + " ORDER BY a.time DESC LIMIT 100", tuple(args))


# ---------------- 额度、成本与账单（模块 K 后台 / 模块 I 前台额度） ----------------
class QuotaIn(BaseModel):
    plan: Optional[str] = None
    quota_total: Optional[float] = None
    daily_limit: Optional[float] = None
    credit_balance: Optional[float] = None


class CreditIn(BaseModel):
    organization_id: Optional[str] = None
    user_id: Optional[str] = None
    amount: float
    reason: str = ""


class PricingIn(BaseModel):
    unit_cost: float


def _scope_credit_org(actor, body) -> str:
    """额度操作的目标组织必须落在操作者作用域内；未指定时落到自己所属组织。"""
    oid = body.organization_id or actor.organization_id
    if not oid:
        authz.require_platform(actor, "org.quota.manage")
        oid = "org_default"
    authz.require_organization_permission(actor, oid, "org.quota.manage")
    body.organization_id = oid
    return oid


def _usage_filter(project_id=None, model_id=None, task_type=None, status=None, days=None, org_id=None):
    sql = "SELECT * FROM usage_records WHERE 1=1"
    args = []
    if project_id:
        sql += " AND project_id=?"; args.append(project_id)
    if model_id:
        sql += " AND model_id=?"; args.append(model_id)
    if task_type:
        sql += " AND task_type=?"; args.append(task_type)
    if status:
        sql += " AND status=?"; args.append(status)
    if org_id:
        sql += " AND organization_id=?"; args.append(org_id)
    if days:
        sql += " AND created_at>=?"; args.append(int(db.now()) - int(days) * 86400)
    return sql, args


@app.get("/api/admin/usage-records")
def admin_usage_records(request: Request, project_id: Optional[str] = None, model_id: Optional[str] = None,
                        task_type: Optional[str] = None, status: Optional[str] = None,
                        days: Optional[int] = None, limit: int = 200):
    actor = auth.require_actor(request)
    authz.require_capability(actor, "usage.read")
    sql, args = _usage_filter(project_id, model_id, task_type, status, days)
    clause, cargs = authz.org_scope_clause(actor, "organization_id")
    sql += clause
    args = list(args) + list(cargs)
    sql += " ORDER BY created_at DESC LIMIT ?"
    return db.query(sql, tuple(args) + (int(limit),))


@app.get("/api/admin/cost-summary")
def admin_cost_summary(request: Request, days: int = 30):
    actor = auth.require_actor(request)
    authz.require_capability(actor, "usage.read")
    since = int(db.now()) - int(days) * 86400
    clause, cargs = authz.org_scope_clause(actor, "organization_id")
    rows = db.query("SELECT * FROM usage_records WHERE created_at>=?" + clause, tuple([since] + list(cargs)))
    total_cost = round(sum(float(r["cost"] or 0) for r in rows), 4)
    succeeded = [r for r in rows if r["status"] == "succeeded"]
    failed = [r for r in rows if r["status"] != "succeeded"]
    by_model: dict = {}
    by_task: dict = {}
    by_day: dict = {}
    for r in rows:
        k = r["model_id"] or "unknown"
        m = by_model.setdefault(k, {"model_id": k, "calls": 0, "cost": 0.0, "images": 0, "failed": 0})
        m["calls"] += 1; m["cost"] = round(m["cost"] + float(r["cost"] or 0), 4)
        m["images"] += int(r["output_images"] or 0)
        if r["status"] != "succeeded":
            m["failed"] += 1
        t = by_task.setdefault(r["task_type"] or "unknown", {"task_type": r["task_type"] or "unknown", "calls": 0, "cost": 0.0})
        t["calls"] += 1; t["cost"] = round(t["cost"] + float(r["cost"] or 0), 4)
        import time as _t
        d = _t.strftime("%Y-%m-%d", _t.localtime(r["created_at"]))
        dd = by_day.setdefault(d, {"date": d, "calls": 0, "cost": 0.0, "images": 0})
        dd["calls"] += 1; dd["cost"] = round(dd["cost"] + float(r["cost"] or 0), 4)
        dd["images"] += int(r["output_images"] or 0)
    return {
        "days": days,
        "calls": len(rows), "succeeded": len(succeeded), "failed": len(failed),
        "total_cost": total_cost,
        "failed_cost": 0.0,
        "images": sum(int(r["output_images"] or 0) for r in rows),
        "avg_cost": round(total_cost / len(succeeded), 4) if succeeded else 0.0,
        "by_model": sorted(by_model.values(), key=lambda x: -x["cost"]),
        "by_task": sorted(by_task.values(), key=lambda x: -x["cost"]),
        "by_day": sorted(by_day.values(), key=lambda x: x["date"]),
    }


@app.get("/api/admin/organizations")
def admin_organizations(request: Request):
    actor = auth.require_actor(request)
    authz.require_capability(actor, "org.read")
    clause, args = authz.org_scope_clause(actor, "id")
    rows = db.query("SELECT * FROM organizations WHERE 1=1" + clause + " ORDER BY created_at ASC", tuple(args))
    if not actor.has("org.quota.manage"):
        for r in rows:
            r.pop("credit_balance", None)
            r.pop("quota_total", None)
            r.pop("daily_limit", None)
    return rows


@app.get("/api/admin/organizations/{oid}/quota")
def admin_org_quota(oid: str, request: Request):
    o = authz.require_organization_permission(auth.require_actor(request), oid, "org.quota.manage")
    used = db.query_one("SELECT COALESCE(SUM(cost),0) c FROM usage_records WHERE organization_id=? AND status='succeeded' AND created_at>=?",
                        (oid, int(o["period_start"] or 0)))["c"]
    return {**o, "used_this_period": round(float(used or 0), 4),
            "remaining": round(float(o["credit_balance"] or 0), 4)}


@app.put("/api/admin/organizations/{oid}/quota")
def admin_update_org_quota(oid: str, body: QuotaIn, request: Request,
                           _role: str = Depends(auth.require_permission("org.quota.manage"))):
    actor = auth.require_actor(request)
    o = authz.require_organization_permission(actor, oid, "org.quota.manage")
    fields, args = [], []
    for col in ("plan", "quota_total", "daily_limit", "credit_balance"):
        v = getattr(body, col)
        if v is not None:
            fields.append(f"{col}=?"); args.append(v)
    if not fields:
        return o
    db.execute(f"UPDATE organizations SET {','.join(fields)} WHERE id=?", tuple(args) + (oid,))
    db.audit(None, actor.user_id, "org.quota.update", oid)
    return db.query_one("SELECT * FROM organizations WHERE id=?", (oid,))


@app.get("/api/admin/credit-ledger")
def admin_credit_ledger(request: Request, limit: int = 200):
    actor = auth.require_actor(request)
    authz.require_capability(actor, "org.quota.manage")
    clause, args = authz.org_scope_clause(actor, "organization_id")
    return db.query("SELECT * FROM credit_ledger WHERE 1=1" + clause
                    + " ORDER BY created_at DESC LIMIT ?", tuple(list(args) + [int(limit)]))


@app.post("/api/admin/credits/grant", status_code=201)
def admin_credits_grant(body: CreditIn, request: Request,
                        _role: str = Depends(auth.require_permission('org.quota.manage'))):
    actor = auth.require_actor(request)
    _scope_credit_org(actor, body)
    if body.amount <= 0:
        raise HTTPException(422, "补发额度必须为正数")
    r = metering.apply_credit_change(body.organization_id, abs(body.amount),
                                     body.reason or "人工补发额度", operator_id=actor.user_id,
                                     ref_type="manual", ref_id="", user_id=body.user_id)
    if not r:
        raise HTTPException(404, "组织不存在")
    db.audit(None, actor.user_id, "credits.grant", r["id"])
    return r


@app.post("/api/admin/credits/deduct", status_code=201)
def admin_credits_deduct(body: CreditIn, request: Request,
                         _role: str = Depends(auth.require_permission('org.quota.manage'))):
    actor = auth.require_actor(request)
    _scope_credit_org(actor, body)
    if body.amount <= 0:
        raise HTTPException(422, "扣除额度必须为正数")
    r = metering.apply_credit_change(body.organization_id, -abs(body.amount),
                                     body.reason or "人工扣除额度", operator_id=actor.user_id,
                                     ref_type="manual", ref_id="", user_id=body.user_id)
    if not r:
        raise HTTPException(404, "组织不存在")
    db.audit(None, actor.user_id, "credits.deduct", r["id"])
    return r


@app.post("/api/admin/credits/refund", status_code=201)
def admin_credits_refund(body: CreditIn, request: Request,
                         _role: str = Depends(auth.require_permission('org.quota.manage'))):
    actor = auth.require_actor(request)
    _scope_credit_org(actor, body)
    if body.amount <= 0:
        raise HTTPException(422, "退款额度必须为正数")
    r = metering.apply_credit_change(body.organization_id, abs(body.amount),
                                     body.reason or "失败任务返还额度", operator_id=actor.user_id,
                                     ref_type="refund", ref_id="", user_id=body.user_id)
    if not r:
        raise HTTPException(404, "组织不存在")
    db.audit(None, actor.user_id, "credits.refund", r["id"])
    return r


@app.put("/api/admin/models/{model_id}/pricing")
def admin_model_pricing(model_id: str, body: PricingIn, request: Request):
    actor = auth.require_actor(request)
    authz.require_platform(actor, "model.manage")
    m = db.query_one("SELECT model_id FROM model_registry WHERE model_id=?", (model_id,))
    if not m:
        raise HTTPException(404, "模型不存在")
    db.execute("UPDATE model_registry SET unit_cost=? WHERE model_id=?", (float(body.unit_cost), model_id))
    db.audit(None, actor.user_id, "model.pricing.update", model_id)
    return db.query_one("SELECT * FROM model_registry WHERE model_id=?", (model_id,))


@app.get("/api/creator/usage-summary")
def creator_usage_summary(request: Request, days: int = 30):
    actor = auth.require_actor(request)
    o = metering.get_org(actor.organization_id) or {}
    since = int(db.now()) - int(days) * 86400
    # 个人额度视图：用量只统计本人，余额是所属组织的额度池
    rows = db.query("SELECT * FROM usage_records WHERE created_at>=? AND user_id=?", (since, actor.user_id))
    this_month = [r for r in rows if r["created_at"] >= int(o.get("period_start") or 0)]
    by_model: dict = {}
    for r in rows:
        k = r["model_id"] or "unknown"
        m = by_model.setdefault(k, {"model_id": k, "calls": 0, "cost": 0.0, "images": 0})
        m["calls"] += 1; m["cost"] = round(m["cost"] + float(r["cost"] or 0), 4)
        m["images"] += int(r["output_images"] or 0)
    return {
        "organization": {"id": o.get("id"), "name": o.get("name"), "plan": o.get("plan")},
        "balance": round(float(o.get("credit_balance") or 0), 4),
        "quota_total": round(float(o.get("quota_total") or 0), 4),
        "used_this_month": round(sum(float(r["cost"] or 0) for r in this_month), 4),
        "calls_this_month": len(this_month),
        "images_this_month": sum(int(r["output_images"] or 0) for r in this_month),
        "window_days": days,
        "usage_scope": "self",
        "window_cost": round(sum(float(r["cost"] or 0) for r in rows), 4),
        "by_model": sorted(by_model.values(), key=lambda x: -x["cost"]),
    }


@app.get("/api/creator/usage-records")
def creator_usage_records(request: Request, days: int = 30, limit: int = 100):
    actor = auth.require_actor(request)
    since = int(db.now()) - int(days) * 86400
    return db.query("SELECT id,project_id,model_id,task_type,output_images,cost,status,created_at "
                    "FROM usage_records WHERE created_at>=? AND user_id=? ORDER BY created_at DESC LIMIT ?",
                    (since, actor.user_id, int(limit)))


# ---------------- 审核与合规中心（模块 J）+ 站内通知 ----------------
REVIEW_STATUS = ["not_required", "pending", "in_review", "approved", "returned", "rejected"]


class ReviewIn(BaseModel):
    target_type: str = "canvas_output"
    asset_id: Optional[str] = None
    target_ref: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    note: Optional[str] = None


class ReviewDecisionIn(BaseModel):
    reason: str = ""
    assigned_to: Optional[str] = None


class ScanIn(BaseModel):
    text: Optional[str] = None
    texts: Optional[dict] = None


notify = notify_svc.notify


def _review_public(r: dict) -> dict:
    return {**r,
            "checklist": json_loads(r.get("checklist_json"), []),
            "hits": json_loads(r.get("hits_json"), []),
            "target_label": compliance.TARGET_LABELS.get(r["target_type"], r["target_type"])}


@app.get("/api/compliance/rules")
def compliance_rules():
    return {"rules": compliance.rules(), "target_types":
            [{"code": t, "label": compliance.TARGET_LABELS.get(t, t)} for t in compliance.TARGET_TYPES],
            "statuses": REVIEW_STATUS,
            "disclaimer": "规则初筛只针对文本关键词，不等价于合规结论，也不做图像识别；最终以人工审核为准。"}


@app.post("/api/compliance/scan")
def compliance_scan(body: ScanIn):
    if body.texts:
        return compliance.scan_many(body.texts)
    return compliance.scan_text(body.text or "")


def _review_gate(pid: str, raise_error: bool = True):
    """导出前审核门禁：未完成的审核或高要求模板未通过审核时拦截导出。"""
    pending = db.query_one("SELECT id,status FROM reviews WHERE project_id=? AND status IN ('pending','in_review') LIMIT 1", (pid,))
    if pending:
        if raise_error:
            raise HTTPException(409, "该项目有未完成的审核，请先到「审核中心」处理后再导出")
        return {"ok": False, "reason": "pending_review", "review_id": pending["id"]}
    proj = db.query_one("SELECT template_id FROM projects WHERE id=?", (pid,)) or {}
    if proj.get("template_id"):
        tpl = db.query_one("SELECT id,name,require_review FROM templates WHERE id=?", (proj["template_id"],)) or {}
        if tpl.get("require_review"):
            ok = db.query_one("SELECT id,decided_at FROM reviews WHERE project_id=? AND status='approved' ORDER BY decided_at DESC LIMIT 1", (pid,))
            if not ok:
                if raise_error:
                    raise HTTPException(409, "该模板要求导出前审核，请先提交审核并通过")
                return {"ok": False, "reason": "review_required", "template": tpl.get("name")}
            latest = db.query_one("SELECT updated_at FROM canvas_documents WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,))
            if latest and ok.get("decided_at") and latest["updated_at"] > ok["decided_at"]:
                if raise_error:
                    raise HTTPException(409, "成品在审核通过后又做了修改，需要重新提交审核")
                return {"ok": False, "reason": "stale_approval", "review_id": ok["id"]}
    return {"ok": True}


@app.get("/api/projects/{pid}/compliance-scan")
def project_compliance_scan(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.read")
    return compliance.scan_many(compliance.project_texts(pid, db))


@app.get("/api/projects/{pid}/review-gate")
def project_review_gate(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "project.read")
    return _review_gate(pid, raise_error=False)


@app.post("/api/projects/{pid}/reviews", status_code=201)
def create_review(pid: str, body: ReviewIn, request: Request):
    actor = auth.require_actor(request)
    authz.require_project_permission(actor, pid, "review.submit")
    if body.asset_id:
        authz.assert_assets_in_project(pid, [body.asset_id], field="review")
    if body.target_type not in compliance.TARGET_TYPES:
        raise HTTPException(422, f"不支持审核对象类型 {body.target_type}")
    scanned = compliance.scan_many(compliance.project_texts(pid, db))
    title = body.title or f"{(db.query_one('SELECT name FROM projects WHERE id=?', (pid,)) or {}).get('name') or pid} · {compliance.TARGET_LABELS.get(body.target_type, body.target_type)}"
    rid = db.gen_id("rev")
    now = int(db.now())
    db.execute("INSERT INTO reviews(id,project_id,target_type,asset_id,target_ref,title,summary,status,risk_level,"
               "checklist_json,hits_json,reason,submitted_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (rid, pid, body.target_type, body.asset_id, body.target_ref or "",
                title, body.summary or body.note or "", "pending", scanned["risk_level"],
                json.dumps(scanned["checklist"], ensure_ascii=False), json.dumps(scanned["hits"], ensure_ascii=False),
                "", "usr_default", now, now))
    db.audit(pid, "usr_default", "review.submit", rid)
    return _review_public(db.query_one("SELECT * FROM reviews WHERE id=?", (rid,)))


@app.get("/api/projects/{pid}/reviews")
def list_project_reviews(pid: str, request: Request):
    authz.require_project_permission(auth.require_actor(request), pid, "review.read")
    rows = db.query("SELECT * FROM reviews WHERE project_id=? ORDER BY created_at DESC", (pid,))
    return [_review_public(r) for r in rows]


@app.get("/api/admin/reviews")
def admin_reviews(request: Request, status: Optional[str] = None, project_id: Optional[str] = None):
    actor = auth.require_actor(request)
    authz.require_capability(actor, "review.queue")
    clause, cargs = authz.org_scope_clause(actor, "p.organization_id")
    sql = ("SELECT r.* FROM reviews r JOIN projects p ON p.id=r.project_id WHERE 1=1" + clause)
    args = list(cargs)
    if status:
        sql += " AND r.status=?"; args.append(status)
    if project_id:
        authz.require_project_permission(actor, project_id, "review.read")
        sql += " AND r.project_id=?"; args.append(project_id)
    sql += (" ORDER BY CASE r.status WHEN 'pending' THEN 0 WHEN 'in_review' THEN 1 ELSE 2 END, "
            "r.created_at DESC LIMIT 300")
    rows = db.query(sql, tuple(args))
    out = []
    for r in rows:
        d = _review_public(r)
        p = db.query_one("SELECT name FROM projects WHERE id=?", (r["project_id"],))
        d["project_name"] = (p or {}).get("name") or r["project_id"]
        out.append(d)
    return out


def _review_detail(rid: str) -> dict:
    """内部使用：不带授权检查，调用方必须先完成作用域校验。"""
    r = db.query_one("SELECT * FROM reviews WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "审核记录不存在")
    d = _review_public(r)
    p = db.query_one("SELECT name,organization_id FROM projects WHERE id=?", (r["project_id"],))
    d["project_name"] = (p or {}).get("name") or r["project_id"]
    d["project_texts"] = compliance.project_texts(r["project_id"], db)
    return d


@app.get("/api/admin/reviews/{rid}")
def admin_review_detail(rid: str, request: Request):
    authz.require_review_permission(auth.require_actor(request), rid, "review.read")
    return _review_detail(rid)


def _decide_review(rid: str, status: str, reason: str, assigned_to: Optional[str] = None, actor_id: str = "system"):
    r = db.query_one("SELECT * FROM reviews WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "审核记录不存在")
    if status == "in_review":
        db.execute("UPDATE reviews SET status=?, assigned_to=?, updated_at=? WHERE id=?",
                   (status, assigned_to or "usr_default", int(db.now()), rid))
    else:
        if status in ("returned", "rejected") and not (reason or "").strip():
            raise HTTPException(422, "退回或拒绝必须填写原因")
        db.execute("UPDATE reviews SET status=?, reason=?, decided_by=?, decided_at=?, assigned_to=COALESCE(?,assigned_to), updated_at=? WHERE id=?",
                   (status, reason or "", "usr_default", int(db.now()), assigned_to, int(db.now()), rid))
    label = {"approved": "审核通过", "returned": "审核退回", "rejected": "审核拒绝", "in_review": "已领取审核"}[status]
    submitter = r.get("submitted_by") or "usr_default"
    notify(submitter, "review", f"{label}：{r['title']}", (reason or "")[:200], ref_type="review", ref_id=rid)
    db.audit(r["project_id"], actor_id, f"review.{status}", rid)
    return _review_detail(rid)


@app.post("/api/admin/reviews/{rid}/claim")
def review_claim(rid: str, body: ReviewDecisionIn, request: Request):
    actor = auth.require_actor(request)
    authz.require_review_permission(actor, rid, "review.decide")
    return _decide_review(rid, "in_review", "", body.assigned_to, actor_id=actor.user_id)


@app.post("/api/admin/reviews/{rid}/approve")
def review_approve(rid: str, body: ReviewDecisionIn, request: Request,
                   _role: str = Depends(auth.require_permission("review.decide"))):
    actor = auth.require_actor(request)
    authz.require_review_permission(actor, rid, "review.decide")
    return _decide_review(rid, "approved", body.reason, body.assigned_to, actor_id=actor.user_id)


@app.post("/api/admin/reviews/{rid}/return")
def review_return(rid: str, body: ReviewDecisionIn, request: Request,
                  _role: str = Depends(auth.require_permission("review.decide"))):
    actor = auth.require_actor(request)
    authz.require_review_permission(actor, rid, "review.decide")
    return _decide_review(rid, "returned", body.reason, body.assigned_to, actor_id=actor.user_id)


@app.post("/api/admin/reviews/{rid}/reject")
def review_reject(rid: str, body: ReviewDecisionIn, request: Request,
                  _role: str = Depends(auth.require_permission("review.decide"))):
    actor = auth.require_actor(request)
    authz.require_review_permission(actor, rid, "review.decide")
    return _decide_review(rid, "rejected", body.reason, body.assigned_to, actor_id=actor.user_id)


# ---------------- 用户、组织与权限（模块 B 后台） ----------------
class OrganizationIn(BaseModel):
    id: Optional[str] = None
    name: str
    plan: str = "standard"
    quota_total: float = 0
    daily_limit: float = 0


class WorkspaceIn(BaseModel):
    id: Optional[str] = None
    name: str


class MemberIn(BaseModel):
    user_id: Optional[str] = None
    name: str = ""
    email: str = ""
    role: str = "editor"
    workspace_id: Optional[str] = None


class RoleAssignIn(BaseModel):
    role: str


class AssignOrgIn(BaseModel):
    organization_id: str
    reason: str = ""


@app.get("/api/admin/projects/unassigned")
def admin_unassigned_projects(request: Request):
    """归属不明的历史项目（仅平台级角色可见，组织级角色一律不可见）。"""
    actor = auth.require_actor(request)
    authz.require_platform(actor, "project.read")
    rows = db.query("SELECT id,name,owner_id,tenant_id,status,created_at FROM projects "
                    "WHERE organization_id IS NULL OR organization_id='' ORDER BY created_at DESC")
    owners = {}
    for r in rows:
        if r["owner_id"] and r["owner_id"] not in owners:
            u = db.query_one("SELECT id,name,organization_id FROM users WHERE id=?", (r["owner_id"],))
            ms = db.query("SELECT DISTINCT organization_id FROM memberships WHERE user_id=? AND organization_id IS NOT NULL",
                          (r["owner_id"],))
            owners[r["owner_id"]] = {
                "exists": bool(u), "name": (u or {}).get("name"),
                "user_organization_id": (u or {}).get("organization_id"),
                "member_organizations": [m["organization_id"] for m in ms],
            }
        r["owner"] = owners.get(r["owner_id"]) if r["owner_id"] else None
    mig = db.query("SELECT name,finished_at,scanned,resolved,unresolved,report_path FROM migrations "
                   "ORDER BY finished_at DESC LIMIT 1")
    return {"count": len(rows), "projects": rows, "last_migration": mig[0] if mig else None}


@app.put("/api/admin/projects/{pid}/organization")
def admin_assign_project_org(pid: str, body: AssignOrgIn, request: Request):
    """管理员为归属不明的项目指定组织；这是让它重新可被组织成员访问的唯一途径。"""
    actor = auth.require_actor(request)
    authz.require_platform(actor, "member.manage")
    if not db.query_one("SELECT id FROM projects WHERE id=?", (pid,)):
        raise HTTPException(404, "项目不存在")
    org = db.query_one("SELECT id FROM organizations WHERE id=?", (body.organization_id,))
    if not org:
        raise HTTPException(404, "组织不存在")
    before = (db.query_one("SELECT organization_id FROM projects WHERE id=?", (pid,)) or {}).get("organization_id")
    db.execute("UPDATE projects SET organization_id=?, updated_at=? WHERE id=?", (body.organization_id, db.now(), pid))
    db.audit(pid, actor.user_id, "project.organization.assign", pid)
    return {"ok": True, "project_id": pid, "before": before, "organization_id": body.organization_id,
            "reason": body.reason}


@app.get("/api/admin/roles")
def admin_roles():
    return {"roles": rbac.roles(), "permissions": rbac.PERMISSIONS}


@app.get("/api/admin/permissions")
def admin_permissions():
    return rbac.PERMISSIONS


@app.post("/api/admin/organizations", status_code=201)
def admin_create_organization(body: OrganizationIn, request: Request,
                              _role: str = Depends(auth.require_permission("member.manage"))):
    actor = auth.require_actor(request)
    authz.require_platform(actor, "member.manage")
    oid = body.id or db.gen_id("org")
    if db.query_one("SELECT id FROM organizations WHERE id=?", (oid,)):
        raise HTTPException(400, "组织 ID 已存在")
    now = int(db.now())
    db.execute("INSERT INTO organizations(id,name,plan,credit_balance,quota_total,daily_limit,period_start,created_at) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (oid, body.name, body.plan, float(body.quota_total), float(body.quota_total),
                float(body.daily_limit), now, now))
    db.execute("INSERT OR IGNORE INTO workspaces(id,organization_id,name,created_at) VALUES(?,?,?,?)",
               (db.gen_id("wsp"), oid, body.name + " 默认工作空间", now))
    db.audit(None, "usr_default", "org.create", oid)
    return db.query_one("SELECT * FROM organizations WHERE id=?", (oid,))


@app.put("/api/admin/organizations/{oid}")
def admin_update_organization(oid: str, body: OrganizationIn, request: Request,
                              _role: str = Depends(auth.require_permission("member.manage"))):
    actor = auth.require_actor(request)
    authz.require_organization_permission(actor, oid, "member.manage")
    db.execute("UPDATE organizations SET name=?, plan=? WHERE id=?", (body.name, body.plan, oid))
    db.audit(None, "usr_default", "org.update", oid)
    return db.query_one("SELECT * FROM organizations WHERE id=?", (oid,))


@app.get("/api/admin/workspaces")
def admin_workspaces(request: Request, organization_id: Optional[str] = None):
    actor = auth.require_actor(request)
    authz.require_capability(actor, "member.manage")
    sql, args = "SELECT * FROM workspaces WHERE 1=1", []
    if organization_id:
        authz.require_organization_permission(actor, organization_id, "member.manage")
        sql += " AND organization_id=?"; args.append(organization_id)
    else:
        clause, cargs = authz.org_scope_clause(actor, "organization_id")
        sql += clause; args += list(cargs)
    return db.query(sql + " ORDER BY created_at ASC", tuple(args))


@app.post("/api/admin/organizations/{oid}/workspaces", status_code=201)
def admin_create_workspace(oid: str, body: WorkspaceIn, request: Request,
                           _role: str = Depends(auth.require_permission("member.manage"))):
    authz.require_organization_permission(auth.require_actor(request), oid, "member.manage")
    wid = body.id or db.gen_id("wsp")
    db.execute("INSERT INTO workspaces(id,organization_id,name,created_at) VALUES(?,?,?,?)",
               (wid, oid, body.name, int(db.now())))
    return db.query_one("SELECT * FROM workspaces WHERE id=?", (wid,))


def _member_rows(actor=None) -> list:
    sql = ("SELECT DISTINCT u.* FROM users u LEFT JOIN memberships m ON m.user_id=u.id WHERE 1=1")
    args: list = []
    if actor is not None:
        orgs = authz.visible_org_ids(actor)
        if orgs is not None:
            if not orgs:
                sql += " AND 1=0"
            else:
                ph = ",".join(["?"] * len(orgs))
                sql += f" AND (u.organization_id IN ({ph}) OR m.organization_id IN ({ph}))"
                args = list(orgs) + list(orgs)
    users = db.query(sql + " ORDER BY u.created_at ASC, u.id ASC", tuple(args))
    usages = db.query("SELECT user_id, count(*) calls, COALESCE(SUM(cost),0) cost, COALESCE(SUM(output_images),0) images, "
                      "COALESCE(SUM(duration_ms),0) ms FROM usage_records GROUP BY user_id")
    umap = {u["user_id"]: u for u in usages}
    out = []
    for u in users:
        ms = db.query("SELECT * FROM memberships WHERE user_id=?", (u["id"],))
        m = ms[0] if ms else {}
        usage = umap.get(u["id"], {})
        org = db.query_one("SELECT name FROM organizations WHERE id=?", (m.get("organization_id") or u.get("organization_id"),))
        out.append({
            "user_id": u["id"], "name": u.get("name") or u["id"], "email": u.get("email") or "",
            "status": u.get("status") or "active", "tenant_id": u.get("tenant_id"),
            "membership_id": m.get("id"), "organization_id": m.get("organization_id") or u.get("organization_id"),
            "organization_name": (org or {}).get("name") or "",
            "workspace_id": m.get("workspace_id"),
            "role": rbac.resolve_role(m.get("role") or u.get("role")),
            "role_name": rbac.role_def(m.get("role") or u.get("role"))["name"],
            "membership_status": m.get("status") or "active",
            "calls": usage.get("calls", 0), "cost": round(float(usage.get("cost", 0) or 0), 4),
            "images": usage.get("images", 0), "duration_ms": usage.get("ms", 0),
        })
    return out


@app.get("/api/admin/users")
def admin_users(request: Request, _role: str = Depends(auth.require_permission("member.manage"))):
    return _member_rows(auth.require_actor(request))


@app.get("/api/admin/users/{uid}/usage")
def admin_user_usage(uid: str, request: Request, days: int = 30):
    authz.require_user_permission(auth.require_actor(request), uid, "member.manage")
    since = int(db.now()) - int(days) * 86400
    rows = db.query("SELECT * FROM usage_records WHERE user_id=? AND created_at>=? ORDER BY created_at DESC LIMIT 200",
                    (uid, since))
    by_model: dict = {}
    for r in rows:
        k = r["model_id"] or "unknown"
        m = by_model.setdefault(k, {"model_id": k, "calls": 0, "cost": 0.0, "images": 0})
        m["calls"] += 1; m["cost"] = round(m["cost"] + float(r["cost"] or 0), 4)
        m["images"] += int(r["output_images"] or 0)
    return {"user_id": uid, "days": days, "calls": len(rows),
            "cost": round(sum(float(r["cost"] or 0) for r in rows), 4),
            "images": sum(int(r["output_images"] or 0) for r in rows),
            "by_model": sorted(by_model.values(), key=lambda x: -x["cost"]), "records": rows[:100]}


@app.post("/api/admin/organizations/{oid}/members", status_code=201)
def admin_add_member(oid: str, body: MemberIn, request: Request,
                     _role: str = Depends(auth.require_permission("member.manage"))):
    actor = auth.require_actor(request)
    authz.require_organization_permission(actor, oid, "member.manage")
    known_roles = {r["id"] for r in rbac.roles()} | {al for r in rbac.roles() for al in r["aliases"]}
    if body.role not in known_roles:
        raise HTTPException(422, f"未知角色 {body.role}")
    uid = body.user_id or db.gen_id("usr")
    now = int(db.now())
    if not db.query_one("SELECT id FROM users WHERE id=?", (uid,)):
        db.execute("INSERT INTO users(id,tenant_id,role,name,email,status,organization_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (uid, "tnt_default", rbac.resolve_role(body.role), body.name or uid, body.email, "active", oid, now))
    else:
        db.execute("UPDATE users SET role=?, organization_id=? WHERE id=?", (rbac.resolve_role(body.role), oid, uid))
    mid = db.gen_id("mem")
    db.execute("INSERT INTO memberships(id,user_id,organization_id,workspace_id,role,status,created_at,updated_at) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (mid, uid, oid, body.workspace_id, rbac.resolve_role(body.role), "active", now, now))
    db.audit(None, "usr_default", "member.invite", uid)
    notify_svc.notify(uid, "member", "你已被加入组织", f"角色：{rbac.role_def(body.role)['name']}", ref_type="org", ref_id=oid)
    return {"membership_id": mid, "user_id": uid, "role": rbac.resolve_role(body.role)}


@app.put("/api/admin/memberships/{mid}/role")
def admin_set_member_role(mid: str, body: RoleAssignIn, request: Request,
                          _role: str = Depends(auth.require_permission("member.manage"))):
    actor = auth.require_actor(request)
    m = authz.require_membership_permission(actor, mid, "member.manage")
    # 不允许把自己所属组织的最后一名管理员降级（避免组织变成无人可管）
    if m["user_id"] == actor.user_id and rbac.resolve_role(body.role) != actor.role \
            and not rbac.is_platform_role(actor.role):
        others = db.query("SELECT id FROM memberships WHERE organization_id=? AND user_id!=? AND role IN "
                          "('super_admin','platform_admin','organization_admin')",
                          (m["organization_id"], actor.user_id))
        if not others:
            raise HTTPException(422, "本组织没有其他管理员，不能修改自己的角色")
    known = {r["id"] for r in rbac.roles()} | {a for r in rbac.roles() for a in r["aliases"]}
    if body.role not in known:
        raise HTTPException(422, f"未知角色 {body.role}")
    role = rbac.resolve_role(body.role)
    db.execute("UPDATE memberships SET role=?, updated_at=? WHERE id=?", (role, int(db.now()), mid))
    db.execute("UPDATE users SET role=? WHERE id=?", (role, m["user_id"]))
    db.audit(None, "usr_default", "member.role.change", m["user_id"])
    notify_svc.notify(m["user_id"], "member", f"你的角色已变更为 {rbac.role_def(role)['name']}", "", ref_type="membership", ref_id=mid)
    return {"membership_id": mid, "user_id": m["user_id"], "role": role}


class ResetPasswordIn(BaseModel):
    new_password: str


@app.post("/api/admin/users/{uid}/reset-password")
def admin_reset_password(uid: str, body: ResetPasswordIn, request: Request,
                         _actor=Depends(auth.require_permission("member.manage"))):
    """管理员重置成员密码；重置后该成员所有既有会话立即失效。"""
    scope_actor = auth.require_actor(request)
    authz.require_user_permission(scope_actor, uid, "member.manage")
    try:
        auth.set_password(uid, body.new_password)
    except ValueError as e:
        raise HTTPException(422, str(e))
    auth.revoke_all_sessions(uid)
    db.audit(None, _actor.user_id, "member.password.reset", uid)
    notify_svc.notify(uid, "member", "你的密码已被管理员重置", "请使用新密码登录", ref_type="user", ref_id=uid)
    return {"ok": True, "user_id": uid}


@app.post("/api/admin/users/{uid}/disable")
def admin_disable_user(uid: str, request: Request, _role: str = Depends(auth.require_permission("member.manage"))):
    actor = auth.require_actor(request)
    authz.require_user_permission(actor, uid, "member.manage")
    if uid == actor.user_id:
        raise HTTPException(422, "不能禁用自己")
    db.execute("UPDATE users SET status='disabled' WHERE id=?", (uid,))
    db.execute("UPDATE memberships SET status='disabled', updated_at=? WHERE user_id=?", (int(db.now()), uid))
    auth.revoke_all_sessions(uid)
    db.audit(None, actor.user_id, "member.disable", uid)
    return {"user_id": uid, "status": "disabled"}


@app.post("/api/admin/users/{uid}/enable")
def admin_enable_user(uid: str, request: Request, _role: str = Depends(auth.require_permission("member.manage"))):
    actor = auth.require_actor(request)
    authz.require_user_permission(actor, uid, "member.manage")
    db.execute("UPDATE users SET status='active' WHERE id=?", (uid,))
    db.execute("UPDATE memberships SET status='active', updated_at=? WHERE user_id=?", (int(db.now()), uid))
    db.audit(None, actor.user_id, "member.enable", uid)
    return {"user_id": uid, "status": "active"}


@app.get("/api/creator/notifications")
def creator_notifications(request: Request, unread_only: bool = False, limit: int = 50):
    actor = auth.require_actor(request)
    authz.require_capability(actor, "notification.read")
    sql = "SELECT * FROM notifications WHERE user_id=?"
    args: list = [actor.user_id]
    if unread_only:
        sql += " AND read=0"
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(int(limit))
    rows = db.query(sql, tuple(args))
    unread = (db.query_one("SELECT count(*) c FROM notifications WHERE read=0 AND user_id=?", (actor.user_id,)) or {"c": 0})["c"]
    return {"unread": unread, "items": rows}


@app.post("/api/creator/notifications/{nid}/read")
def read_notification(nid: str, request: Request):
    authz.require_notification_permission(auth.require_actor(request), nid, "notification.read")
    db.execute("UPDATE notifications SET read=1 WHERE id=?", (nid,))
    return {"ok": True}


@app.post("/api/creator/notifications/read-all")
def read_all_notifications(request: Request):
    actor = auth.require_actor(request)
    db.execute("UPDATE notifications SET read=1 WHERE read=0 AND user_id=?", (actor.user_id,))
    return {"ok": True}


if _os.path.isdir(_DIST):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="static")

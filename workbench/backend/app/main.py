"""FastAPI 主应用：企业营销生图工作台 MVP 后端。

实现 PRD v1/v2 的核心 API：项目/资产/图（强类型端口、DAG）/节点（编辑、AI 局部修改、应用候选、
运行）/运行进度 SSE/模型注册/导出（项目 JSON + 分层 PNG + 素材包）。版本乐观锁、幂等键、上游 stale 标记。
"""
import io
import json
import os
import threading
import time
import zipfile
from collections import defaultdict, deque
from PIL import Image, ImageDraw
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse, Response
from pydantic import BaseModel
from typing import Optional, Union

from . import db, storage
from . import registry
from .generators import text as text_gen
from .generators import image as image_gen
from .services import canvas_renderer

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


@app.middleware("http")
async def auth_middleware(request, call_next):
    path = request.url.path
    if path == "/api/health":
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    # 管理员接口：需 WB_ADMIN_TOKEN
    if WB_ADMIN_TOKEN and path.startswith("/api") and _is_admin_path(path):
        if auth != f"Bearer {WB_ADMIN_TOKEN}":
            return JSONResponse({"detail": "需要管理员 Token"}, status_code=401)
    # 普通接口：需 WB_API_TOKEN（管理员 Token 亦可）
    if WB_API_TOKEN and (path.startswith("/api") or path.startswith("/files")):
        if auth not in (f"Bearer {WB_API_TOKEN}", f"Bearer {WB_ADMIN_TOKEN}"):
            return JSONResponse({"detail": "未授权：请提供有效的 Bearer Token"}, status_code=401)
    return await call_next(request)


try:
    _RATE_LIMIT = int(os.environ.get("WB_RATE_LIMIT", "600"))
except ValueError:
    _RATE_LIMIT = 600
_RATE_WINDOW = 60.0
_rate_hits = defaultdict(deque)
_rate_lock = threading.Lock()


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


@app.get("/api/health")
def health():
    return {"status": "ok", "time": db.now()}


@app.post("/api/projects")
def create_project(body: ProjectCreate):
    pid = db.gen_id("prj")
    db.execute("INSERT INTO projects(id,tenant_id,owner_id,name,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
               (pid, body.tenant_id, "usr_default", body.name, "active", db.now(), db.now()))
    gid = db.gen_id("grf")
    db.execute("INSERT INTO graphs(id,project_id,current_version) VALUES(?,?,1)", (gid, pid))
    return {"id": pid, "name": body.name, "graph_id": gid}


@app.get("/api/projects")
def list_projects():
    return db.query("SELECT id,name,status,created_at FROM projects ORDER BY created_at DESC")


@app.get("/api/projects/{pid}")
def get_project(pid):
    p = db.query_one("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "项目不存在")
    p = dict(p)
    p["graph"] = db.query_one("SELECT * FROM graphs WHERE project_id=?", (pid,))
    return p


@app.delete("/api/projects/{pid}")
def delete_project(pid):
    if not db.query_one("SELECT id FROM projects WHERE id=?", (pid,)):
        raise HTTPException(404, "项目不存在")
    asset_ids = [a["id"] for a in db.query("SELECT id FROM assets WHERE project_id=?", (pid,))]
    db.execute("DELETE FROM canvas_layers WHERE project_id=?", (pid,))
    db.execute("DELETE FROM agent_plans WHERE project_id=?", (pid,))
    db.execute("DELETE FROM audit_events WHERE project_id=?", (pid,))
    for aid in asset_ids:
        db.execute("DELETE FROM export_records WHERE image_asset_id=?", (aid,))
    g = db.query_one("SELECT id FROM graphs WHERE project_id=?", (pid,))
    if g:
        node_ids = [n["id"] for n in db.query("SELECT id FROM nodes WHERE graph_id=?", (g["id"],))]
        for nid in node_ids:
            db.execute("DELETE FROM node_versions WHERE node_id=?", (nid,))
            db.execute("DELETE FROM node_candidates WHERE node_id=?", (nid,))
            for r in db.query("SELECT id FROM node_runs WHERE node_id=?", (nid,)):
                db.execute("DELETE FROM run_outputs WHERE run_id=?", (r["id"],))
                db.execute("DELETE FROM run_inputs WHERE run_id=?", (r["id"],))
                db.execute("DELETE FROM node_runs WHERE id=?", (r["id"],))
            db.execute("DELETE FROM nodes WHERE id=?", (nid,))
        db.execute("DELETE FROM edges WHERE graph_id=?", (g["id"],))
        db.execute("DELETE FROM graphs WHERE id=?", (g["id"],))
    for a in db.query("SELECT id,object_key FROM assets WHERE project_id=?", (pid,)):
        try:
            storage.delete(a["object_key"])
        except Exception:
            pass
        db.execute("DELETE FROM assets WHERE id=?", (a["id"],))
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    return {"ok": True}


class ProjectRename(BaseModel):
    name: str


@app.patch("/api/projects/{pid}")
def rename_project(pid: str, body: ProjectRename):
    if not db.query_one("SELECT id FROM projects WHERE id=?", (pid,)):
        raise HTTPException(404, "项目不存在")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "项目名不能为空")
    db.execute("UPDATE projects SET name=?, updated_at=? WHERE id=?", (name, db.now(), pid))
    return {"ok": True, "name": name}


@app.get("/api/projects/{pid}/defaults")
def get_defaults(pid: str):
    p = db.query_one("SELECT default_text_model, default_image_model FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "项目不存在")
    return {"default_text_model": p["default_text_model"] or "", "default_image_model": p["default_image_model"] or ""}


class DefaultsUpdate(BaseModel):
    default_text_model: Optional[str] = None
    default_image_model: Optional[str] = None


@app.put("/api/projects/{pid}/defaults")
def put_defaults(pid: str, body: DefaultsUpdate):
    p = db.query_one("SELECT id FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "项目不存在")
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
def get_brief(pid: str):
    r = db.query_one("SELECT * FROM generation_briefs WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,))
    if not r:
        raise HTTPException(404, "brief 不存在")
    return _brief_dict(r)


@app.post("/api/projects/{pid}/brief", status_code=201)
def create_brief(pid: str, body: BriefIn):
    if not db.query_one("SELECT id FROM projects WHERE id=?", (pid,)):
        raise HTTPException(404, "项目不存在")
    bid = db.gen_id("brief"); now = int(db.now())
    db.execute("INSERT INTO generation_briefs(" + BRIEF_COLS + ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (bid, pid) + _brief_vals(body) + (now, now))
    return _brief_dict(db.query_one("SELECT * FROM generation_briefs WHERE id=?", (bid,)))


@app.put("/api/projects/{pid}/brief")
def upsert_brief(pid: str, body: BriefIn):
    if not db.query_one("SELECT id FROM projects WHERE id=?", (pid,)):
        raise HTTPException(404, "项目不存在")
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


# ---------------- 生图任务（jobs / candidates） ----------------
class JobIn(BaseModel):
    model_id: Optional[str] = None
    task_type: str = "text_to_image"


@app.post("/api/projects/{pid}/generation-jobs", status_code=201)
def create_generation_job(pid: str, body: JobIn):
    brief = db.query_one("SELECT * FROM generation_briefs WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,))
    if not brief:
        raise HTTPException(422, "请先保存 GenerationBrief")
    pv_no = (db.query_one("SELECT count(*) c FROM prompt_versions WHERE project_id=?", (pid,)) or {"c": 0})["c"] + 1
    pv_id = db.gen_id("pv")
    structured = {"user_prompt": brief["user_prompt"], "platform": brief["platform"], "aspect_ratio": brief["aspect_ratio"]}
    model_id = body.model_id or brief["selected_model_id"] or "local-poster-compositor"
    m = registry.get_model(model_id) or {}
    db.execute("INSERT INTO prompt_versions(id,project_id,brief_id,source_type,version_no,structured_prompt_json,prompt,negative_prompt,model_id,model_params_json,created_at) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (pv_id, pid, brief["id"], "brief", pv_no, json.dumps(structured, ensure_ascii=False), brief["user_prompt"], "",
                model_id, json.dumps({"aspect_ratio": brief["aspect_ratio"], "count": brief["image_count"]}, ensure_ascii=False), int(db.now())))
    jid = db.gen_id("job")
    db.execute("INSERT INTO generation_jobs(id,project_id,brief_id,prompt_version_id,model_id,provider_id,task_type,status,progress,created_at) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)",
               (jid, pid, brief["id"], pv_id, model_id, m.get("provider_id"), body.task_type, "queued", 0, int(db.now())))
    return db.query_one("SELECT * FROM generation_jobs WHERE id=?", (jid,))


@app.get("/api/generation-jobs/{jid}")
def get_generation_job(jid: str):
    r = db.query_one("SELECT * FROM generation_jobs WHERE id=?", (jid,))
    if not r:
        raise HTTPException(404, "任务不存在")
    return r


@app.post("/api/generation-jobs/{jid}/cancel")
def cancel_generation_job(jid: str):
    db.execute("UPDATE generation_jobs SET status='canceled', finished_at=? WHERE id=? AND status IN ('queued','running')", (int(db.now()), jid))
    return {"ok": True}


@app.get("/api/projects/{pid}/candidates")
def list_candidates(pid: str):
    return db.query("SELECT * FROM generation_candidates WHERE project_id=? ORDER BY created_at DESC", (pid,))


@app.post("/api/candidates/{cid}/select")
def select_candidate(cid: str):
    c = db.query_one("SELECT project_id FROM generation_candidates WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404, "候选不存在")
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
def get_canvas(pid: str):
    r = db.query_one("SELECT * FROM canvas_documents WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,))
    if not r:
        raise HTTPException(404, "画布不存在")
    return {"id": r["id"], "project_id": r["project_id"], "width": r["width"], "height": r["height"],
            "document": json_loads(r["canvas_json"], {})}


@app.put("/api/projects/{pid}/canvas")
def put_canvas(pid: str, body: CanvasIn):
    if not db.query_one("SELECT id FROM projects WHERE id=?", (pid,)):
        raise HTTPException(404, "项目不存在")
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
    return {"ok": True, "id": cid}


@app.post("/api/projects/{pid}/canvas/export")
def export_canvas(pid: str):
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
    return {"asset_id": aid, "download_url": f"/files/{aid}"}


# ---------------- assets ----------------
@app.post("/api/projects/{pid}/assets")
def upload_asset(pid: str, file: UploadFile = File(...), role: str = Form("product"),
                source: str = Form("upload"), origin: str = Form("")):
    if not db.query_one("SELECT id FROM projects WHERE id=?", (pid,)):
        raise HTTPException(404, "项目不存在")
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
def upload_assets_batch(pid: str, files: list[UploadFile] = File(...), role: str = Form("product"), source: str = Form("upload")):
    if not db.query_one("SELECT id FROM projects WHERE id=?", (pid,)):
        raise HTTPException(404, "项目不存在")
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
def import_text_file(pid: str, file: UploadFile = File(...)):
    if not db.query_one("SELECT id FROM projects WHERE id=?", (pid,)):
        raise HTTPException(404, "项目不存在")
    data = file.file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件过大")
    text = _extract_text(file.filename or "", data)
    return {"filename": file.filename, "text": text}


@app.get("/api/projects/{pid}/assets")
def list_assets(pid: str):
    return db.query("SELECT id,role,kind,mime,width,height,created_at,object_key FROM assets WHERE project_id=?", (pid,))


@app.get("/api/assets/{aid}/download")
def download_asset(aid: str):
    a = db.query_one("SELECT * FROM assets WHERE id=?", (aid,))
    if not a:
        raise HTTPException(404, "资产不存在")
    if not db.query_one("SELECT id FROM projects WHERE id=?", (a["project_id"],)):
        raise HTTPException(404, "资产所属项目不存在，禁止访问")
    data = storage.read_bytes(a["object_key"])
    return Response(content=data, media_type=a["mime"] or "image/png")


@app.get("/files/{asset_id}")
def file_content(asset_id: str):
    a = db.query_one("SELECT * FROM assets WHERE id=?", (asset_id,))
    if not a:
        raise HTTPException(404, "资产不存在")
    data = storage.read_bytes(a["object_key"])
    return Response(content=data, media_type=a["mime"] or "image/png")


# ---------------- graph ----------------
@app.get("/api/projects/{pid}/graph")
def get_graph(pid: str):
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
def patch_graph(pid: str, body: GraphPatch):
    g = db.query_one("SELECT * FROM graphs WHERE project_id=?", (pid,))
    if not g:
        raise HTTPException(404, "图不存在")
    if body.expected_graph_version != g["current_version"]:
        raise HTTPException(409, "图版本冲突，请刷新后重试")
    for mv in body.moves:
        db.execute("UPDATE nodes SET position_json=? WHERE id=?",
                   (json.dumps({"x": mv.get("x", 0), "y": mv.get("y", 0)}), mv["node_id"]))
    for e in body.add_edges:
        # 端口类型校验
        err = validate_edge(e["from_node"], e["from_port"], e["to_node"], e["to_port"])
        if err:
            raise HTTPException(422, err)
        eid = db.gen_id("edge")
        db.execute("INSERT INTO edges(id,graph_id,from_node,from_port,to_node,to_port,semantic) VALUES(?,?,?,?,?,?,?)",
                   (eid, g["id"], e["from_node"], e["from_port"], e["to_node"], e["to_port"], e.get("semantic", "")))
    for rid in body.remove_edges:
        db.execute("DELETE FROM edges WHERE id=?", (rid,))
    db.execute("UPDATE graphs SET current_version=current_version+1 WHERE id=?", (g["id"],))
    ng = db.query_one("SELECT current_version FROM graphs WHERE id=?", (g["id"],))
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
def init_template(pid: str, body: InitWorkflow | None = None):
    g = db.query_one("SELECT * FROM graphs WHERE project_id=?", (pid,))
    if not g:
        raise HTTPException(404, "图不存在")
    sk = WORKFLOW_SKELETONS.get((body.skeleton if body else "poster"), WORKFLOW_SKELETONS["poster"])
    db.execute("DELETE FROM nodes WHERE graph_id=?", (g["id"],))
    db.execute("DELETE FROM edges WHERE graph_id=?", (g["id"],))

    def add(type_, x, y, content=None):
        nid = db.gen_id(type_[:4])
        db.execute("INSERT INTO nodes(id,graph_id,type,name,position_json,current_version,status) VALUES(?,?,?,?,?,1,?)",
                   (nid, g["id"], type_, NODE_NAMES.get(type_, type_), json.dumps({"x": x, "y": y}), "draft"))
        if content is not None:
            set_node_version(nid, content, author_type="system")
        return nid

    def edge(f, fp, t, tp, sem):
        eid = db.gen_id("edge")
        db.execute("INSERT INTO edges(id,graph_id,from_node,from_port,to_node,to_port,semantic) VALUES(?,?,?,?,?,?,?)",
                   (eid, g["id"], f, fp, t, tp, sem))

    facts = add("product_facts", 40, 220, NODE_DEFAULTS["product_facts"]) if sk["facts"] else None
    img = add("product_image", 40, 440, NODE_DEFAULTS["product_image"]) if sk["image"] else None
    strat = add("strategy", 360, 220, NODE_DEFAULTS["strategy"])
    prompts, gens = [], []
    for i in range(sk["prompts"]):
        prompt_content = {**NODE_DEFAULTS["image_prompt"], "strategy_ref": f"hero_{i+1}"}
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
    db.execute("UPDATE graphs SET current_version=current_version+1 WHERE id=?", (g["id"],))
    return {"ok": True, "graph_id": g["id"], "nodes": {"facts": facts, "image": img, "strategy": strat, "prompts": prompts, "gens": gens, "review": rev, "layout": lay}}


# ---------------- 工作流节点自由增删 ----------------
NODE_NAMES = {
    "product_facts": "产品事实卡", "product_image": "产品素材", "strategy": "视觉策略",
    "image_prompt": "单图提示词", "image_generation": "生图节点", "review": "审核", "layout_export": "分层排版导出",
}

NODE_DEFAULTS = {
    "product_facts": {"product_id": "", "product_name": "", "activity_version": "", "confirmed_selling_points": [], "locked_appearance": [], "applicable_scenes": [], "forbidden_expressions": [], "policies": [], "recognition_notes": []},
    "product_image": {"asset_id": None, "asset_role": "product", "filename": ""},
    "strategy": {"strategies": [], "task_brief": {"goal": "", "channel": "", "audience": ""}},
    "image_prompt": {"prompt": "", "negative_prompt": "", "strategy_ref": "hero_1"},
    "image_generation": {"model_id": "local-poster-compositor", "outputs": []},
    "review": {"conclusion": "", "issue_tags": []},
    "layout_export": {"title": "", "subtitle": "", "approved": False},
}


class NodeAdd(BaseModel):
    type: str
    x: float = 0
    y: float = 0


@app.post("/api/graphs/{gid}/nodes")
def add_node(gid: str, body: NodeAdd):
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
def delete_node(nid: str):
    db.execute("DELETE FROM edges WHERE from_node=? OR to_node=?", (nid, nid))
    db.execute("DELETE FROM node_versions WHERE node_id=?", (nid,))
    db.execute("DELETE FROM node_candidates WHERE node_id=?", (nid,))
    db.execute("DELETE FROM nodes WHERE id=?", (nid,))
    return {"ok": True}


# ---------------- nodes ----------------
@app.get("/api/nodes/{nid}")
def get_node(nid: str):
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
def patch_node(nid: str, body: NodePatch):
    if body.content is not None:
        set_node_version(nid, body.content, author_type="human")
        mark_stale(nid)
    if body.position is not None:
        db.execute("UPDATE nodes SET position_json=? WHERE id=?", (json.dumps(body.position), nid))
    if body.status is not None:
        db.execute("UPDATE nodes SET status=? WHERE id=?", (body.status, nid))
    if body.name is not None:
        db.execute("UPDATE nodes SET name=? WHERE id=?", (body.name, nid))
    return {"ok": True, "version": db.query_one("SELECT current_version FROM nodes WHERE id=?", (nid,))["current_version"]}


class AiEdit(BaseModel):
    instruction: str
    base_version: Optional[int] = None


@app.post("/api/nodes/{nid}/ai-edit")
def ai_edit(nid: str, body: AiEdit):
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
def apply_candidate(nid: str, body: ApplyCandidate):
    c = db.query_one("SELECT * FROM node_candidates WHERE id=? AND node_id=?", (body.candidate_id, nid))
    if not c:
        raise HTTPException(404, "候选不存在")
    cur = db.query_one("SELECT current_version FROM nodes WHERE id=?", (nid,))
    if not cur:
        raise HTTPException(404, "节点不存在")
    if c["base_version"] != cur["current_version"]:
        raise HTTPException(409, "节点已更新，候选基于的版本已过期，请重新生成")
    content = json_loads(c["content_json"])
    before = cur["current_version"]
    set_node_version(nid, content, author_type="ai")
    mark_stale(nid)
    after = db.query_one("SELECT current_version FROM nodes WHERE id=?", (nid,))["current_version"]
    db.audit(get_project_of_node(nid), "usr_default", "apply_candidate", nid, before, after)
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


def _run_node_thread(run_id, node_id, model_id, params, ref_asset_ids, idem):
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
    except Exception as e:
        db.execute("UPDATE node_runs SET status='failed', ended_at=?, error_code=? WHERE id=?",
                   (db.now(), str(e)[:200], run_id))


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
        sref = get_current_version(node_id).get("strategy_ref", "hero_1")
        item = next((s for s in strat.get("strategies", []) if s["id"] == sref), strat.get("strategies", [{}])[0] if strat.get("strategies") else {})
        prov = registry.resolve_provider(model_id or "rule-based-planner")
        if prov:
            try:
                prompt = text_gen.generate_prompt_llm(model_id, prov, item, facts, baseline=baseline)
            except Exception:
                prompt = text_gen.generate_prompt(item, facts, baseline=baseline)
        else:
            prompt = text_gen.generate_prompt(item, facts, baseline=baseline)
        set_node_version(node_id, prompt, author_type="ai", model_id=model_id or "rule-based-planner")
        return {"outputs": [], "usage": {"model": model_id or "rule-based-planner"}, "provider_task_id": "prompt"}
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
        if img_node:
            ic = get_current_version(img_node) or {}
            cands = ([ic.get("asset_id")] if ic.get("asset_id") else []) + (ic.get("reference_asset_ids") or [])
            for ia in cands:
                if ia and db.query_one("SELECT id FROM assets WHERE id=? AND project_id=?", (ia, pid)):
                    refs.append(ia)
        for rid in ref_asset_ids:
            if rid and db.query_one("SELECT id FROM assets WHERE id=? AND project_id=?", (rid, pid)):
                refs.append(rid)
        # 校验模型能力
        errs = registry.validate_image_params(model_id, {"aspect_ratio": ar, "count": params.get("count", 1),
                                                          "reference_count": len(refs)})
        if errs:
            raise HTTPException(422, "；".join(errs))
        pil_imgs = []
        for rid in refs:
            try:
                pil_imgs.append(storage.read_pillow(db.query_one("SELECT object_key FROM assets WHERE id=?", (rid,))["object_key"]))
            except Exception:
                pass
        req = {"model_id": model_id, "prompt": prompt_text, "negative_prompt": neg,
               "reference_asset_ids": refs, "aspect_ratio": ar,
               "resolution_tier": params.get("resolution_tier", "standard"),
               "count": params.get("count", 1), "seed": params.get("seed"),
               "params": {"title": params.get("title", ""), "subtitle": params.get("subtitle", "") }}
        prov_cfg = registry.resolve_provider(model_id)
        adapter = (registry.get_model(model_id) or {}).get("adapter")
        res = image_gen.dispatch(model_id, req, pil_imgs, provider_cfg=prov_cfg, adapter=adapter)
        outputs = []
        for o in res["outputs"]:
            aid = db.gen_id("ast")
            obj_key = f"{pid}/{aid}.png"
            storage.save_bytes(obj_key, o["bytes"])
            db.execute("INSERT INTO assets(id,tenant_id,project_id,kind,role,object_key,sha256,mime,width,height,created_at,source,origin,usage_rights_status) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, "tnt_default", pid, "image", "generated", obj_key, "", "image/png",
                        o["width"], o["height"], db.now(), "generation", model_id, "generated"))
            outputs.append({"asset_id": aid, "object_key": obj_key, "width": o["width"], "height": o["height"]})
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


@app.post("/api/nodes/{nid}/runs")
def run_node(nid: str, body: RunReq):
    n = db.query_one("SELECT type FROM nodes WHERE id=?", (nid,))
    if not n:
        raise HTTPException(404, "节点不存在")
    # 幂等：相同 idempotency_key 已成功则直接返回
    if body.idempotency_key:
        ex = db.query_one("SELECT * FROM node_runs WHERE idempotency_key=? AND status='succeeded' LIMIT 1", (body.idempotency_key,))
        if ex:
            return {"run_id": ex["id"], "idempotent": True}
    # 模型回退链：显式指定 -> 项目默认 -> 内置默认
    model_id = body.model_id
    if not model_id:
        proj = get_project_of_node(nid)
        dflt = db.query_one("SELECT default_text_model, default_image_model FROM projects WHERE id=?", (proj,))
        if n["type"] == "image_generation":
            model_id = (dflt["default_image_model"] if dflt and dflt["default_image_model"] else None) or "local-poster-compositor"
        else:
            model_id = (dflt["default_text_model"] if dflt and dflt["default_text_model"] else None) or "rule-based-planner"
    gid = db.query_one("SELECT graph_id FROM nodes WHERE id=?", (nid,))["graph_id"]
    gv = db.query_one("SELECT current_version FROM graphs WHERE id=?", (gid,))["current_version"]
    run_id = db.gen_id("run")
    db.execute("INSERT INTO node_runs(id,node_id,node_version,graph_version,status,model_id,parameters_json,idempotency_key,started_at) "
               "VALUES(?,?,?,?,?,?,?,?,?)",
               (run_id, nid, body.node_version or 0, gv, "queued", model_id, json.dumps(body.params),
                body.idempotency_key, db.now()))
    t = threading.Thread(target=_run_node_thread, args=(run_id, nid, model_id, body.params, body.reference_asset_ids, body.idempotency_key))
    t.daemon = True
    t.start()
    return {"run_id": run_id}


@app.get("/api/runs/{rid}")
def get_run(rid: str):
    r = db.query_one("SELECT * FROM node_runs WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "运行不存在")
    outs = db.query("SELECT asset_id, output_json FROM run_outputs WHERE run_id=?", (rid,))
    return {**dict(r), "outputs": outs}


@app.get("/api/runs/{rid}/events")
def run_events(rid: str):
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


@app.post("/api/graphs/{gid}/run-downstream")
def run_downstream(gid: str, body: dict):
    from_node = body.get("from_node_id")
    if not from_node or not db.query_one("SELECT id FROM nodes WHERE id=?", (from_node,)):
        raise HTTPException(404, "起始节点不存在")
    ds = downstream_node_ids(from_node)
    ran = []
    for d in ds:
        t = db.query_one("SELECT type FROM nodes WHERE id=?", (d,))["type"]
        if t == "image_generation":
            r = run_node(d, RunReq(model_id="local-poster-compositor", params={}))
            ran.append(r["run_id"])
    return {"ran": ran}


# ---------------- models ----------------
@app.get("/api/models")
def models():
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
def create_model_api(m: ModelCreate):
    try:
        return registry.create_model(m.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/models/{model_id}")
def get_model_api(model_id: str):
    m = registry.get_model(model_id)
    if not m:
        raise HTTPException(404, "模型不存在")
    return m


@app.put("/api/models/{model_id}")
def update_model_api(model_id: str, patch: ModelUpdate):
    try:
        m = registry.update_model(model_id, patch.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not m:
        raise HTTPException(404, "模型不存在")
    return m


@app.delete("/api/models/{model_id}")
def delete_model_api(model_id: str):
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
def list_providers_api():
    return [{"id": p["id"], "name": p["name"], "base_url": p["base_url"], "enabled": p["enabled"],
             "has_api_key": bool(p.get("api_key"))} for p in registry.list_providers()]


@app.post("/api/providers", status_code=201)
def create_provider_api(p: ProviderCreate):
    try:
        return registry.create_provider(p.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/providers/{pid}")
def update_provider_api(pid: str, patch: ProviderUpdate):
    prov = registry.update_provider(pid, patch.model_dump(exclude_unset=True))
    if not prov:
        raise HTTPException(404, "服务商不存在")
    return prov


@app.delete("/api/providers/{pid}")
def delete_provider_api(pid: str):
    registry.delete_provider(pid)
    return {"ok": True}


# ---------------- image tools（节点悬浮工具栏意图） ----------------
@app.get("/api/image-tools/intents")
def image_tool_intents():
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
def image_tool_run(body: ImageToolRun):
    n = db.query_one("SELECT id,type,graph_id FROM nodes WHERE id=?", (body.node_id,))
    if not n:
        raise HTTPException(404, "节点不存在")
    asset_ids = [body.asset_id] if body.asset_id else []
    # 默认取节点已生成的首张作为底图
    if not asset_ids:
        cur = get_current_version(body.node_id) or {}
        asset_ids = (cur.get("outputs") or [])[:1]
    if not asset_ids:
        raise HTTPException(422, "缺少底图资产：请在节点结果或请求中指定 asset_id")
    pid = get_project_of_node(body.node_id)
    # 禁止跨项目引用任意资产，必须在读取文件内容前校验
    for aid in asset_ids:
        owned = db.query_one("SELECT id FROM assets WHERE id=? AND project_id=?", (aid, pid))
        if not owned:
            raise HTTPException(404, "底图资产不存在或不属于当前项目")
    try:
        res = image_gen.dispatch_tool(body.intent, asset_ids, body.title, body.subtitle, body.model_id)
    except ValueError as e:
        raise HTTPException(422, str(e))
    outputs = []
    for o in res.get("outputs", []):
        aid = db.gen_id("ast")
        obj_key = f"{pid}/{aid}.png"
        storage.save_bytes(obj_key, o["bytes"])
        db.execute("INSERT INTO assets(id,tenant_id,project_id,kind,role,object_key,sha256,mime,width,height,created_at,source,origin,usage_rights_status) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (aid, "tnt_default", pid, "image", "generated", obj_key, "", "image/png",
                    o["width"], o["height"], db.now(), "image_tool", body.intent, "generated"))
        outputs.append({"asset_id": aid, "object_key": obj_key, "width": o["width"], "height": o["height"]})
    # 写回节点 outputs，供画布缩略图
    if outputs:
        cur = get_current_version(body.node_id) or {}
        all_out = (cur.get("outputs", []) or []) + [o["asset_id"] for o in outputs]
        set_node_version(body.node_id, {**cur, "outputs": all_out, "last_tool": body.intent}, author_type="ai")
    return {"outputs": outputs, "usage": res.get("usage", {}), "status": res.get("status", "succeeded")}


# ---------------- layout / export ----------------
@app.post("/api/projects/{pid}/layout/export")
def export_layout(pid: str, body: dict):
    """分层排版导出：基于生成图资产 + 可编辑文字层，重新合成最终 PNG。"""
    base_asset = body.get("base_asset_id")
    if not base_asset:
        raise HTTPException(422, "缺少 base_asset_id")
    if not db.query_one("SELECT id FROM projects WHERE id=?", (pid,)):
        raise HTTPException(404, "项目不存在")
    a = db.query_one("SELECT object_key,width,height FROM assets WHERE id=? AND project_id=?", (base_asset, pid))
    if not a:
        raise HTTPException(404, "底图不存在或不属于当前项目")
    base_im = storage.read_pillow(a["object_key"]).convert("RGB")
    title = body.get("title", "")
    subtitle = body.get("subtitle", "")
    # 在底部叠加文字层（不重绘背景/产品，满足“改字不重生图”）
    draw = ImageDraw.Draw(base_im)
    from .generators.image import _font, _hex
    w, h = base_im.size
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
def export_project(pid: str):
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
def export_package(pid: str):
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
def agent_plan(pid: str, body: AgentPlanReq):
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
def admin_stats():
    def cnt(sql):
        r = db.query_one(sql)
        return r["c"] if r else 0
    return {
        "projects": cnt("SELECT count(*) c FROM projects"),
        "assets": cnt("SELECT count(*) c FROM assets"),
        "models": cnt("SELECT count(*) c FROM model_registry"),
        "providers": cnt("SELECT count(*) c FROM providers"),
        "runs": cnt("SELECT count(*) c FROM node_runs"),
        "audits": cnt("SELECT count(*) c FROM audit_events"),
    }


@app.get("/api/assets")
def list_all_assets():
    return db.query("SELECT id, project_id, role, mime, width, height, created_at, source FROM assets ORDER BY created_at DESC LIMIT 200")


@app.delete("/api/assets/{aid}")
def delete_asset(aid: str):
    a = db.query_one("SELECT object_key FROM assets WHERE id=?", (aid,))
    if not a:
        raise HTTPException(404, "素材不存在")
    try:
        storage.delete(a["object_key"])
    except Exception:
        pass
    db.execute("DELETE FROM assets WHERE id=?", (aid,))
    return {"ok": True}


@app.get("/api/runs")
def list_runs():
    return db.query("SELECT id,node_id,model_id,status,started_at,ended_at,error_code FROM node_runs ORDER BY started_at DESC LIMIT 50")


@app.get("/api/audit")
def list_audit():
    return db.query("SELECT * FROM audit_events ORDER BY time DESC LIMIT 100")


if _os.path.isdir(_DIST):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="static")

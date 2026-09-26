"""SQLite 数据层：项目 / 资产 / 图 / 节点 / 版本 / 运行 / 模型 / 图层 / 审计。

遵循 PRD v1/v2 的数据模型（tenant / project / asset / graph / node / edge /
node_version / node_run / run_input / run_output / canvas_layer / agent_plan /
export_record / audit_event）。MVP 用 SQLite 落地，结构向后兼容 PostgreSQL。
"""
import sqlite3
import threading
import uuid
import time
import os
from contextlib import contextmanager

DB_PATH = os.environ.get("WB_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "db", "workbench.db"))
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY, name TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, tenant_id TEXT, role TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, tenant_id TEXT, owner_id TEXT, name TEXT,
    status TEXT DEFAULT 'active', created_at REAL, updated_at REAL,
    default_text_model TEXT, default_image_model TEXT
);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, kind TEXT, role TEXT,
    object_key TEXT, sha256 TEXT, mime TEXT, width INT, height INT,
    created_at REAL, source TEXT, origin TEXT, usage_rights_status TEXT
);
CREATE TABLE IF NOT EXISTS graphs (
    id TEXT PRIMARY KEY, project_id TEXT, current_version INT DEFAULT 1
);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, graph_id TEXT, type TEXT, name TEXT, position_json TEXT,
    current_version INT DEFAULT 1, status TEXT DEFAULT 'draft'
);
CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY, graph_id TEXT, from_node TEXT, from_port TEXT,
    to_node TEXT, to_port TEXT, semantic TEXT
);
CREATE TABLE IF NOT EXISTS node_versions (
    node_id TEXT, version INT, input_snapshot_json TEXT, content_json TEXT,
    locks_json TEXT, author_type TEXT, model_id TEXT, created_at REAL,
    PRIMARY KEY (node_id, version)
);
CREATE TABLE IF NOT EXISTS node_runs (
    id TEXT PRIMARY KEY, node_id TEXT, node_version INT, graph_version INT,
    status TEXT, model_id TEXT, parameters_json TEXT, idempotency_key TEXT,
    started_at REAL, ended_at REAL, usage_json TEXT, error_code TEXT,
    provider_task_id TEXT
);
CREATE TABLE IF NOT EXISTS run_inputs (
    run_id TEXT, upstream_node_id TEXT, upstream_version INT, asset_id TEXT
);
CREATE TABLE IF NOT EXISTS run_outputs (
    run_id TEXT, asset_id TEXT, output_json TEXT
);
CREATE TABLE IF NOT EXISTS node_candidates (
    id TEXT PRIMARY KEY, node_id TEXT, base_version INT, content_json TEXT,
    summary TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS canvas_layers (
    id TEXT PRIMARY KEY, project_id TEXT, canvas_id TEXT, kind TEXT,
    asset_id TEXT, text TEXT, style_json TEXT, transform_json TEXT,
    z_index INT, source_run_id TEXT
);
CREATE TABLE IF NOT EXISTS agent_plans (
    id TEXT PRIMARY KEY, project_id TEXT, base_graph_version INT,
    proposed_operations_json TEXT, estimated_usage_json TEXT,
    status TEXT DEFAULT 'draft', approved_by TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS export_records (
    id TEXT PRIMARY KEY, canvas_version INT, image_asset_id TEXT,
    format TEXT, width INT, height INT, approval_state TEXT
);
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY, name TEXT, base_url TEXT, api_key TEXT,
    enabled INT DEFAULT 1, created_at REAL
);
CREATE TABLE IF NOT EXISTS model_registry (
    model_id TEXT PRIMARY KEY, provider TEXT, modality TEXT,
    capabilities_json TEXT, parameter_schema TEXT, enabled INT DEFAULT 1,
    cost_policy TEXT, workflow_version TEXT, provider_id TEXT, adapter TEXT
);
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY, project_id TEXT, actor_id TEXT, action TEXT,
    entity_id TEXT, before_version INT, after_version INT, time REAL
);
CREATE TABLE IF NOT EXISTS generation_briefs (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_prompt TEXT NOT NULL,
    purpose TEXT NOT NULL, platform TEXT NOT NULL, aspect_ratio TEXT NOT NULL,
    image_count INTEGER NOT NULL, selected_model_id TEXT, selected_prompt_template_id TEXT,
    reference_asset_ids_json TEXT NOT NULL, product_asset_ids_json TEXT NOT NULL,
    style_keywords_json TEXT NOT NULL, brand_keywords_json TEXT NOT NULL,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_generation_briefs_project_id ON generation_briefs(project_id);
CREATE TABLE IF NOT EXISTS prompt_versions (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, brief_id TEXT NOT NULL,
    source_type TEXT NOT NULL, version_no INTEGER NOT NULL,
    structured_prompt_json TEXT NOT NULL, prompt TEXT NOT NULL, negative_prompt TEXT,
    model_id TEXT, model_params_json TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS generation_jobs (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, brief_id TEXT NOT NULL, prompt_version_id TEXT,
    model_id TEXT NOT NULL, provider_id TEXT, task_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', progress INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT, provider_task_id TEXT, error_code TEXT, error_message TEXT,
    created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_generation_jobs_status ON generation_jobs(status);
CREATE TABLE IF NOT EXISTS generation_candidates (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT NOT NULL, prompt_version_id TEXT,
    asset_id TEXT NOT NULL, model_id TEXT NOT NULL, provider_id TEXT, provider_task_id TEXT,
    width INTEGER, height INTEGER, seed INTEGER, status TEXT NOT NULL DEFAULT 'ready',
    score INTEGER, is_selected INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_generation_candidates_project ON generation_candidates(project_id);
CREATE TABLE IF NOT EXISTS canvas_documents (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, candidate_id TEXT,
    width INTEGER NOT NULL, height INTEGER NOT NULL, canvas_json TEXT NOT NULL,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_canvas_documents_project ON canvas_documents(project_id);
CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, subtitle TEXT, level1 TEXT, level2 TEXT,
    skeleton TEXT, aspect TEXT, accent TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_templates (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT, template_text TEXT NOT NULL,
    model_hint TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
);
"""


def gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    """单连接事务：在全局锁内复用同一连接，提交或回滚。"""
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    with _lock:
        conn = _connect()
        conn.executescript(SCHEMA)
        conn.commit()
        # 兼容已存在的库：补齐 projects 表新增列（SQLite 不支持 IF NOT COLUMN）
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(projects)")]
        for col, ctype in [("default_text_model", "TEXT"), ("default_image_model", "TEXT")]:
            if col not in cols:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {ctype}")
        mcols = [r["name"] for r in conn.execute("PRAGMA table_info(model_registry)")]
        for col in ("provider_id", "adapter"):
            if col not in mcols:
                conn.execute(f"ALTER TABLE model_registry ADD COLUMN {col} TEXT")
        ncols = [r["name"] for r in conn.execute("PRAGMA table_info(nodes)")]
        if "name" not in ncols:
            conn.execute("ALTER TABLE nodes ADD COLUMN name TEXT")
        mcols2 = [r["name"] for r in conn.execute("PRAGMA table_info(model_registry)")]
        for col in ("display_name", "task_types_json"):
            if col not in mcols2:
                conn.execute(f"ALTER TABLE model_registry ADD COLUMN {col} TEXT")
        conn.commit()
        # 默认租户，便于单机演示（生产应做真实鉴权与隔离）
        conn.execute("INSERT OR IGNORE INTO tenants(id,name) VALUES(?,?)", ("tnt_default", "默认企业租户"))
        conn.execute("INSERT OR IGNORE INTO users(id,tenant_id,role) VALUES(?,?,?)", ("usr_default", "tnt_default", "admin"))
        conn.commit()
        conn.close()


def execute(sql: str, params=()) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


def execute_return(sql: str, params=()):
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(sql, params)
            row = cur.fetchone()
            conn.commit()
            return row
        finally:
            conn.close()


def query(sql: str, params=()) -> list:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def query_one(sql: str, params=()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def now() -> float:
    return time.time()


def audit(project_id, actor_id, action, entity_id, before_version=None, after_version=None):
    execute(
        "INSERT INTO audit_events(id,project_id,actor_id,action,entity_id,before_version,after_version,time) VALUES(?,?,?,?,?,?,?,?)",
        (gen_id("aud"), project_id, actor_id, action, entity_id, before_version, after_version, now()),
    )

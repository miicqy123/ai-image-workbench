# AI 多节点产品营销生图工作台 — 代码审计报告

> **审计时间**：2026-09-26
> **审计范围**：`workbench/backend/app/`（`main.py` / `db.py` / `registry.py` / `storage.py` / `generators/` / `services/` / `workers/`）
> **当前状态**：结构清晰、可运行的 MVP，具备强类型端口、乐观锁、幂等键、上游 stale 标记、服务端二次校验模型能力等良好设计。以下问题按"上线前必须处理"到"可后续优化"排序。

---

## 已修复（相较第一轮审计）

| 项目 | 状态 |
|---|---|
| `allow_credentials=False`，CORS 不再矛盾 | ✅ 已修复 |
| 增加 `auth_middleware`，支持 Bearer Token 鉴权 | ✅ 已修复 |
| 增加 `rate_limit_middleware`，按 IP 限速 600 次/分钟 | ✅ 已修复 |
| `set_node_version` 改用 `db.tx()` 事务 | ✅ 已修复 |
| `downstream_node_ids` 改用 `set` 去重 | ✅ 已修复 |
| `delete_project` 手动级联删除所有关联表 | ✅ 已修复 |
| `_connect()` 每次执行 `PRAGMA foreign_keys = ON` | ✅ 已修复 |
| `db.tx()` 提供带回滚的事务上下文管理器 | ✅ 已修复 |

---

## 一、安全（上线前必修）

### 1. 鉴权环境变量未配置时自动跳过，等于无鉴权

**文件**：`main.py` `auth_middleware`

**现状**：
```python
if WB_API_TOKEN and path.startswith("/api"):  # Token 为空则跳过
    if auth not in (...):
        return JSONResponse(..., 401)
```
默认部署不设置 `WB_API_TOKEN` 时，所有接口完全开放。

**修改意见**：
```python
# 启动阶段检查，若未配置直接拒绝启动
_WB_DEV_MODE = os.environ.get("WB_DEV_MODE", "").lower() == "true"
if not WB_API_TOKEN and not _WB_DEV_MODE:
    raise RuntimeError(
        "必须设置 WB_API_TOKEN 环境变量。"
        "本地调试可设 WB_DEV_MODE=true，生产禁止。"
    )
```
同时在 README 中明确要求所有部署方式必须配置 Token。

---

### 2. `tenant_id` / `owner_id` 由客户端传入，未绑定认证身份

**文件**：`main.py` `ProjectCreate`

**现状**：
```python
class ProjectCreate(BaseModel):
    name: str
    tenant_id: str = "tnt_default"  # 客户端可任意传入
```

**修改意见**：
```python
class ProjectCreate(BaseModel):
    name: str
    # 删除 tenant_id 字段，从 Token 解析

@app.post("/api/projects")
def create_project(body: ProjectCreate, request: Request):
    tenant_id = _tenant_from_request(request)   # 从 Token / Header 解析
    owner_id  = _user_from_request(request)
    ...
```

---

### 3. `/files/{asset_id}` 无项目归属校验（IDOR）

**文件**：`main.py` `file_content`

**现状**：任何知道 `asset_id` 的请求方都可读取任意项目的文件。

**修改意见**：
```python
@app.get("/files/{asset_id}")
def file_content(asset_id: str, pid: str = Query(...)):
    row = db.query_one(
        "SELECT * FROM assets WHERE id=? AND project_id=?",
        (asset_id, pid)
    )
    if not row:
        raise HTTPException(404, "资产不存在或无权访问")
    ...
```

---

### 4. `export_layout` 底图资产无项目归属校验

**文件**：`main.py` `export_layout`

**现状**：
```python
base = db.query_one("SELECT * FROM assets WHERE id=?", (body.base_asset_id,))
```
可引用其他项目的资产做底图导出。

**修改意见**：
```python
base = db.query_one(
    "SELECT * FROM assets WHERE id=? AND project_id=?",
    (body.base_asset_id, pid)
)
if not base:
    raise HTTPException(403, "底图资产不属于当前项目")
```

---

### 5. 上传无字节大小、类型和尺寸限制（解压炸弹风险）

**文件**：`main.py` `upload_asset`

**现状**：直接 `file.file.read()` 全量读入内存，无任何校验。Pillow 虽有默认阈值，但未启用警告→异常转换，大尺寸合法图片仍可耗尽内存。

**修改意见**：
```python
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_PIXELS   = 4096 * 4096
import warnings

@app.post("/api/projects/{pid}/assets")
async def upload_asset(pid: str, file: UploadFile = File(...)):
    # 1. 分块读取，累计字节数
    chunks, size = [], 0
    async for chunk in file:
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "文件超过 20 MB 限制")
        chunks.append(chunk)
    data = b"".join(chunks)

    # 2. 校验图片格式与像素数
    warnings.filterwarnings("error", category=Image.DecompressionBombWarning)
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))   # verify 后必须重新 open
        if img.width * img.height > MAX_PIXELS:
            raise HTTPException(400, f"图片尺寸过大：{img.width}x{img.height}")
        if img.format and f"image/{img.format.lower()}" not in ALLOWED_MIME:
            raise HTTPException(400, f"不支持的图片格式：{img.format}")
    except Image.DecompressionBombWarning:
        raise HTTPException(400, "图片文件异常（解压炸弹）")
    ...
```

---

### 6. `pid` 落盘前无数据库归属校验

**文件**：`main.py` `upload_asset` / `export_layout`

`project_id` 直接拼入对象键路径，未先验证项目存在且属于当前用户。

**修改意见**：提取统一校验函数：
```python
def _require_project(pid: str) -> dict:
    """验证项目存在（后续加 owner_id 过滤）。"""
    p = db.query_one("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "项目不存在")
    return p
```
所有写入存储的接口在操作前先调用此函数。

---

## 二、功能主链

### 7. `upstream_of_type` 只查一跳，产品参考图永远为 None

**文件**：`main.py` 节点执行相关逻辑

**现状**：`image_generation` 的直接上游是 `image_prompt`，而非 `product_image`，导致产品图始终无法传递到合成器。

**修改意见**：改为 BFS 多跳向上追溯，并限定在同一 `graph_id`：
```python
def upstream_of_type(graph_id: str, start_node_id: str, node_type: str):
    """BFS 向上多跳查找最近的指定类型节点（限同一 graph）。"""
    visited = set()
    # 一次性预取所有边，避免 N+1
    parent_of = {
        e["to_node"]: e["from_node"]
        for e in db.query(
            "SELECT from_node, to_node FROM edges WHERE graph_id=?", (graph_id,)
        )
    }
    queue = deque([start_node_id])
    while queue:
        nid = queue.popleft()
        if nid in visited:
            continue
        visited.add(nid)
        parent = parent_of.get(nid)
        if not parent:
            continue
        row = db.query_one("SELECT * FROM nodes WHERE id=?", (parent,))
        if row and row["type"] == node_type:
            return row
        queue.append(parent)
    return None
```
所有调用处同步传入 `graph_id`。

---

### 8. `get_upstream_facts` 全库回退缺少 `graph_id` 过滤（数据串线）

**文件**：`main.py` `get_upstream_facts`

**现状**：回退查询 `SELECT ... WHERE type='product_facts' LIMIT 1` 无项目/graph 过滤，多项目并存时 B 项目会拿到 A 项目的事实卡。

**修改意见**：
```python
def get_upstream_facts(graph_id: str, node_id: str):
    direct = upstream_of_type(graph_id, node_id, "product_facts")
    if direct:
        return direct
    # 回退：仅在同一 graph 内查找
    return db.query_one(
        "SELECT * FROM nodes WHERE type='product_facts' AND graph_id=? "
        "ORDER BY created_at LIMIT 1",
        (graph_id,)
    )
```
`get_upstream_strategy` 及其他同类 helper 一并修改。

---

### 9. `apply_candidate` 和 `ai_edit` 的 `base_version` 未校验（乐观锁失效）

**文件**：`main.py`

**现状**：`base_version` 字段收了但未参与并发控制，并发编辑时会静默覆盖。

**修改意见**：
```python
@app.post("/api/nodes/{node_id}/apply_candidate")
def apply_candidate(node_id: str, body: ApplyCandidate):
    node = db.query_one("SELECT current_version FROM nodes WHERE id=?", (node_id,))
    if not node:
        raise HTTPException(404, "节点不存在")
    if node["current_version"] != body.base_version:
        raise HTTPException(
            409,
            f"版本冲突：当前 {node['current_version']}，提交基于 {body.base_version}"
        )
    ...
```
`ai_edit` 同理处理。

---

## 三、数据一致性

### 10. `execute_node` 写资产 → 写图层 → 写版本未包在同一事务

**文件**：`main.py` 节点执行逻辑

**现状**：三步独立执行，任意一步异常都会留下半写状态（资产已存、版本未更新）。

**修改意见**：将所有数据库写操作包入 `db.tx()`：
```python
with db.tx() as conn:
    conn.execute("INSERT INTO assets ...", ...)
    conn.execute("INSERT INTO canvas_layers ...", ...)
    conn.execute("INSERT INTO node_versions ...", ...)
    conn.execute("UPDATE nodes SET status='ready' ...", ...)
# 事务提交后再持久化文件（文件失败需补偿：删除临时文件）
```

---

### 11. `db.py` Schema 缺少外键声明，`ON DELETE CASCADE` 无效

**文件**：`db.py` `SCHEMA`

**现状**：`PRAGMA foreign_keys = ON` 已在每次连接中执行，但表定义中没有 `REFERENCES ... ON DELETE CASCADE`，级联删除依赖 `main.py` 中大量手写 DELETE。

**修改意见**：补齐外键（需迁移现有数据库）：
```sql
CREATE TABLE IF NOT EXISTS graphs (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    current_version INT DEFAULT 1
);
CREATE TABLE IF NOT EXISTS nodes (
    id       TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
    ...
);
CREATE TABLE IF NOT EXISTS node_versions (
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    version INT NOT NULL,
    ...
    PRIMARY KEY (node_id, version)
);
CREATE TABLE IF NOT EXISTS assets (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    ...
);
```
补齐外键后，`delete_project` 中大量手写 DELETE 可简化为单条 `DELETE FROM projects WHERE id=?`。

> **迁移注意**：现有数据库需先执行 `PRAGMA foreign_key_check` 检查孤儿数据，再重建表并迁移数据。

---

## 四、模型管理

### 12. `create_model_api` / `update_model_api` 使用裸 dict，无 Pydantic 校验

**文件**：`main.py`

**现状**：
```python
m = await request.json()   # 裸 dict
int(m.get("enabled", 1))   # 非数字字符串 → ValueError → 500
```

**修改意见**：
```python
class ModelIn(BaseModel):
    id: Optional[str] = None
    name: str
    provider_id: str
    model_code: str         # 发送给服务商的实际 model id
    modality: str           # "text" | "image"
    enabled: bool = True
    supports_ref_image: bool = False
    max_ref_images: int = 0
    supported_aspects: list[str] = []
    param_schema_json: dict = {}

@app.post("/api/models", status_code=201)
def create_model_api(body: ModelIn):
    ...
```

---

### 13. `providers` 表 `api_key` 字段明文存储，接口返回明文

**文件**：`db.py` `SCHEMA`，`main.py` Provider CRUD

**修改意见**：
- 数据库存储时使用 AES-256-GCM 加密，密钥来自 `WB_SECRET_KEY` 环境变量。
- 列表和详情接口只返回掩码，例如 `sk-****9F2A`。
- 更新接口：传空字符串 = 不修改；传新值 = 替换加密存储。
- 日志、异常信息和审计事件中不得包含明文 Key。

---

### 14. 服务商与模型耦合，每个模型重复存储 URL 和 Key

**文件**：`db.py` `model_registry`

**现状**：每行 `model_registry` 各自存 `provider` 字符串，`providers` 表有 `base_url`/`api_key`，但模型表又有 `provider` 字段，关联不清晰。

**修改意见**：建立清晰的 Provider → Model 一对多关系：
```sql
CREATE TABLE IF NOT EXISTS providers (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    protocol         TEXT NOT NULL,   -- openai_compat | dashscope | zhipu_native
    base_url         TEXT NOT NULL,
    api_key_enc      TEXT NOT NULL,   -- AES-256-GCM 密文
    region           TEXT DEFAULT '',
    workspace_id     TEXT DEFAULT '',
    max_concurrency  INTEGER DEFAULT 10,
    timeout_seconds  INTEGER DEFAULT 120,
    enabled          INTEGER DEFAULT 1,
    created_at       INTEGER,
    updated_at       INTEGER
);

-- model_registry 增加 provider_id 外键约束（字段已存在，补约束）
-- 前端添加模型时只需选择服务商，无需重填 URL/Key
```

---

## 五、性能

### 15. `_gradient` 逐行 `ImageDraw.line`，大图极慢

**文件**：`generators/image.py`

**修改意见**：
```python
import numpy as np

def _gradient(width: int, height: int, color1, color2) -> Image.Image:
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    for i, (c1, c2) in enumerate(zip(color1[:3], color2[:3])):
        arr[:, :, i] = np.linspace(c1, c2, height, dtype=np.uint8).reshape(-1, 1)
    return Image.fromarray(arr)
```
速度提升约 10x～100x（依图片尺寸）。

---

### 16. SSE 每 0.3 秒轮询数据库，并发连接多时压力大

**文件**：`main.py` SSE 接口

**短期**：将轮询间隔从 0.3 秒改为 1 秒。

**中期**：改为 `asyncio.Queue` 内存推送，由任务完成时主动通知，消除轮询：
```python
_sse_queues: dict[str, asyncio.Queue] = {}

async def _notify(project_id: str, event: dict):
    q = _sse_queues.get(project_id)
    if q:
        await q.put(event)
```

---

### 17. `get_upstream_facts` / `get_upstream_strategy` 存在 N+1 查询

每条边单独查库，节点多时呈线性增长。

**修改意见**：参考第 7 条，预取整个 graph 的节点和边，在内存中完成遍历。

---

## 六、可维护性

### 18. `main.py` 约 2000 行，路由/业务/helper 全部耦合

**修改意见**（修复高风险问题后再拆分）：
```
app/
├── api/
│   ├── projects.py      # 项目 CRUD + defaults
│   ├── assets.py        # 上传 / 文件读取
│   ├── workflows.py     # 节点 / 边 / 执行 / SSE
│   ├── briefs.py        # 生图简报
│   ├── models.py        # 模型管理（用户端）
│   └── admin.py         # 模板 / Provider / 模型后台
├── services/
│   ├── execution.py     # run_node / run_downstream 业务逻辑
│   ├── project_service.py
│   └── asset_service.py
├── domain/
│   └── errors.py        # 统一业务异常类型
└── main.py              # 只保留 app 创建 + 中间件 + router include
```

---

### 19. `run_downstream` 直接调用路由函数，绕过校验和依赖

**文件**：`main.py`

**修改意见**：路由函数和 `run_downstream` 共同调用 Service 层：
```python
# services/execution.py
async def execute_node_logic(node_id: str, opts: dict) -> dict:
    ...

# main.py 路由
@app.post("/api/nodes/{node_id}/run")
async def run_node_api(node_id: str, body: RunOpts):
    return await execution_service.execute_node_logic(node_id, body.dict())

# run_downstream
async def run_downstream(node_id: str):
    for nid in downstream_node_ids(node_id):
        await execution_service.execute_node_logic(nid, {})
```

---

### 20. Windows 字体路径硬编码，Linux/Docker 中文变框

**文件**：`generators/image.py`

**修改意见**：
```python
def _find_font(candidates: list[str]) -> str:
    for p in candidates:
        if os.path.exists(p):
            return p
    for name in ("NotoSansCJK-Regular.ttc", "wqy-zenhei.ttc", "DroidSansFallback.ttf"):
        linux_path = f"/usr/share/fonts/truetype/noto/{name}"
        if os.path.exists(linux_path):
            return linux_path
    return ""   # 回落到 Pillow 默认字体（中文会变框，需在 Docker 中安装字体）

FONT_PATH = os.environ.get("WB_FONT_PATH") or _find_font([
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
])
```
Dockerfile 中需添加：`RUN apt-get install -y fonts-noto-cjk`

---

## 七、新增功能需求（当前代码中缺失）

| 模块 | 说明 | 优先级 |
|---|---|---|
| 产品素材类型字段 | `assets.role` 增加枚举（主图/Logo/细节图/场景参考/资料文件） | P0 |
| 文档解析服务 | PDF/DOCX → 文字提取 → 调用文本模型结构化为产品事实 | P0 |
| 产品事实版本 | `product_fact_versions` 表，用户确认后才生效 | P0 |
| 创意方案 | `creative_plans` 表，含策略、文案、构图建议结构化字段 | P0 |
| Provider 管理后台 | 服务商配置 + 加密凭据 + 连接测试 + 模型同步 | P0 |
| 生成素材库增强 | 生成结果关联原始素材和任务，支持采用/废弃/重新生成 | P1 |
| 表单模板 | 行业扩展字段定义，项目创建时绑定版本 | P1 |
| 后台首页统计 | 项目数、任务数、失败率、调用量概览 | P1 |
| 审计日志查询 | 模型/Key/项目的操作记录 | P1 |

---

## 八、建议整改顺序

| 阶段 | 修改内容 | 验收标准 |
|---|---|---|
| **第 1 阶段**（安全）| 第 1～6 条 | 未授权和跨项目访问全部返回 401/403/404 |
| **第 2 阶段**（功能主链）| 第 7～9 条 | 产品图多跳传递正常，并发冲突返回 409 |
| **第 3 阶段**（数据一致性）| 第 10～11 条 | 故障注入后无半写和孤儿记录 |
| **第 4 阶段**（模型管理）| 第 12～14 条 | 一个服务商 Key 驱动多个模型，Key 不可明文读取 |
| **第 5 阶段**（工程化）| 第 15～20 条 | 路由层无业务逻辑，Linux 容器中文正常显示 |
| **第 6 阶段**（新功能）| 第七节 | 产品图→事实→策略→生图完整闭环通过测试 |

---

## 附录：关键文件清单

| 文件 | 大小 | 说明 |
|---|---|---|
| `app/main.py` | ~89 KB | 全部路由、业务逻辑（建议拆分） |
| `app/db.py` | ~12 KB | SQLite schema、事务、查询封装 |
| `app/registry.py` | ~14 KB | 模型注册与能力校验 |
| `app/storage.py` | ~2.5 KB | 本地文件存储封装 |
| `app/generators/image.py` | — | Pillow 图片合成（含渐变性能瓶颈） |
| `app/services/` | — | canvas_renderer / model_router / prompt_compiler / metering |
| `app/workers/` | — | 异步任务工作器 |

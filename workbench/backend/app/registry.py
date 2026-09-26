"""模型注册表：统一声明文本 / 图片模型的能力，供前端禁用不支持参数、服务端二次校验。

- 内置可用模型：local-poster-compositor（图片，本地合成器，免费，真实产出可见海报）
- 文本模型：rule-based-planner（策略/提示词规则生成，免费，离线）
- 占位适配器：comfyui-* / api-* 默认 disabled，结构兼容，仅记录“未接入”不捏造路由。
遵循 PRD 6.5：仅当模型具备能力才提供对应按钮；服务端再次校验。
"""
import json
import os
from . import db

SEED_PROVIDERS = [
    {"id": "prv_openrouter", "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "api_key": ""},
    {"id": "prv_dashscope", "name": "阿里云百炼（通义千问/万相）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": ""},
    {"id": "prv_zhipu", "name": "智谱AI（GLM/CogView）", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": ""},
]

SEED_EXTERNAL_MODELS = [
    {"model_id": "openai/gpt-4o-mini", "provider": "OpenRouter", "provider_id": "prv_openrouter", "adapter": "chat", "modality": "text", "capabilities_json": json.dumps({'image_input_limit': 0, 'aspect_ratios': [], 'resolutions': [], 'max_count': 0, 'editing_modes': [], 'tasks': ['strategy', 'image_prompt']}), "parameter_schema": json.dumps({}), "enabled": 0, "cost_policy": "per_token", "workflow_version": "chat-v1"},
    {"model_id": "qwen-max", "provider": "阿里云百炼", "provider_id": "prv_dashscope", "adapter": "chat", "modality": "text", "capabilities_json": json.dumps({'image_input_limit': 0, 'aspect_ratios': [], 'resolutions': [], 'max_count': 0, 'editing_modes': [], 'tasks': ['strategy', 'image_prompt']}), "parameter_schema": json.dumps({}), "enabled": 0, "cost_policy": "per_token", "workflow_version": "chat-v1"},
    {"model_id": "glm-4-flash", "provider": "智谱AI", "provider_id": "prv_zhipu", "adapter": "chat", "modality": "text", "capabilities_json": json.dumps({'image_input_limit': 0, 'aspect_ratios': [], 'resolutions': [], 'max_count': 0, 'editing_modes': [], 'tasks': ['strategy', 'image_prompt']}), "parameter_schema": json.dumps({}), "enabled": 0, "cost_policy": "per_token", "workflow_version": "chat-v1"},
    {"model_id": "wanx2.1-t2i-turbo", "provider": "阿里云百炼", "provider_id": "prv_dashscope", "adapter": "image_dashscope", "modality": "image", "capabilities_json": json.dumps({'image_input_limit': 4, 'aspect_ratios': ['1:1', '4:3', '3:4', '16:9'], 'resolutions': [{'tier': 'standard', 'min': 1024}], 'max_count': 1, 'editing_modes': [], 'tasks': ['image_generation']}), "parameter_schema": json.dumps({'aspect_ratio': {'type': 'enum', 'enum': ['1:1', '4:3', '3:4', '16:9'], 'default': '1:1'}, 'count': {'type': 'int', 'min': 1, 'max': 1, 'default': 1}}), "enabled": 0, "cost_policy": "per_image", "workflow_version": "wanx-v1"},
    {"model_id": "cogview-3-flash", "provider": "智谱AI", "provider_id": "prv_zhipu", "adapter": "image_openai", "modality": "image", "capabilities_json": json.dumps({'image_input_limit': 4, 'aspect_ratios': ['1:1', '4:3', '3:4', '16:9'], 'resolutions': [{'tier': 'standard', 'min': 1024}], 'max_count': 1, 'editing_modes': [], 'tasks': ['image_generation']}), "parameter_schema": json.dumps({'aspect_ratio': {'type': 'enum', 'enum': ['1:1', '4:3', '3:4', '16:9'], 'default': '1:1'}, 'count': {'type': 'int', 'min': 1, 'max': 1, 'default': 1}}), "enabled": 0, "cost_policy": "per_image", "workflow_version": "cogview-v1"},
    {"model_id": "openai/gpt-image-1", "provider": "OpenRouter", "provider_id": "prv_openrouter", "adapter": "image_openai", "modality": "image", "capabilities_json": json.dumps({'image_input_limit': 4, 'aspect_ratios': ['1:1', '4:3', '3:4', '16:9'], 'resolutions': [{'tier': 'standard', 'min': 1024}], 'max_count': 1, 'editing_modes': [], 'tasks': ['image_generation']}), "parameter_schema": json.dumps({'aspect_ratio': {'type': 'enum', 'enum': ['1:1', '4:3', '3:4', '16:9'], 'default': '1:1'}, 'count': {'type': 'int', 'min': 1, 'max': 1, 'default': 1}}), "enabled": 0, "cost_policy": "per_image", "workflow_version": "image-v1"}
]


SEED_MODELS = [
    {
        "model_id": "rule-based-planner",
        "provider": "local",
        "modality": "text",
        "capabilities_json": json.dumps({
            "image_input_limit": 0,
            "aspect_ratios": [],
            "resolutions": [],
            "max_count": 0,
            "editing_modes": [],
            "tasks": ["strategy", "image_prompt"],
        }),
        "parameter_schema": json.dumps({}),
        "enabled": 1,
        "cost_policy": "free_local",
        "workflow_version": "text-v1",
    },
    {
        "model_id": "local-poster-compositor",
        "provider": "local",
        "modality": "image",
        "capabilities_json": json.dumps({
            "image_input_limit": 4,
            "aspect_ratios": ["1:1", "4:3", "3:4", "16:9"],
            "resolutions": [{"tier": "standard", "min": 768}, {"tier": "hd", "min": 1024}],
            "max_count": 4,
            "editing_modes": ["text_overlay"],
            "tasks": ["image_generation"],
        }),
        "parameter_schema": json.dumps({
            "aspect_ratio": {"type": "enum", "enum": ["1:1", "4:3", "3:4", "16:9"], "default": "1:1"},
            "resolution_tier": {"type": "enum", "enum": ["standard", "hd"], "default": "standard"},
            "count": {"type": "int", "min": 1, "max": 4, "default": 1},
            "bg_style": {"type": "string", "default": "auto"},
            "seed": {"type": "int", "optional": True},
        }),
        "enabled": 1 if os.environ.get("WB_ENABLE_DEMO_COMPOSITOR", "true").lower() != "false" else 0,
        "cost_policy": "free_local",
        "workflow_version": "compositor-v1",
        "display_name": "演示拼贴生成器（非 AI 生图）",
        "task_types_json": '["text_to_image", "image_to_image", "product_composition"]',
    },
    {
        "model_id": "comfyui-product-edit",
        "provider": "comfyui",
        "modality": "image",
        "capabilities_json": json.dumps({
            "image_input_limit": 2,
            "aspect_ratios": ["1:1", "4:3", "3:4", "16:9"],
            "resolutions": [{"tier": "standard", "min": 768}, {"tier": "hd", "min": 1024}],
            "max_count": 4,
            "editing_modes": ["inpaint", "img2img"],
            "tasks": ["image_generation"],
        }),
        "parameter_schema": json.dumps({
            "aspect_ratio": {"type": "enum", "enum": ["1:1", "4:3", "3:4", "16:9"], "default": "1:1"},
            "resolution_tier": {"type": "enum", "enum": ["standard", "hd"], "default": "hd"},
            "count": {"type": "int", "min": 1, "max": 4, "default": 1},
        }),
        "enabled": 0,
        "cost_policy": "self_hosted_gpu",
        "workflow_version": "comfyui-api-v0",
    },
    {
        "model_id": "api-commercial-image",
        "provider": "commercial_api",
        "modality": "image",
        "capabilities_json": json.dumps({
            "image_input_limit": 1,
            "aspect_ratios": ["1:1", "16:9", "3:4"],
            "resolutions": [{"tier": "standard", "min": 1024}],
            "max_count": 2,
            "editing_modes": [],
            "tasks": ["image_generation"],
        }),
        "parameter_schema": json.dumps({
            "aspect_ratio": {"type": "enum", "enum": ["1:1", "16:9", "3:4"], "default": "1:1"},
            "resolution_tier": {"type": "enum", "enum": ["standard"], "default": "standard"},
            "count": {"type": "int", "min": 1, "max": 2, "default": 1},
        }),
        "enabled": 0,
        "cost_policy": "per_image",
        "workflow_version": "api-v0",
    },
]


def seed_providers():
    for p in SEED_PROVIDERS:
        if not db.query_one("SELECT id FROM providers WHERE id=?", (p["id"],)):
            db.execute("INSERT INTO providers(id,name,base_url,api_key,enabled,created_at) VALUES(?,?,?,?,1,?)",
                       (p["id"], p["name"], p["base_url"], p["api_key"], db.now()))


def seed_models():
    seed_providers()
    for m in SEED_MODELS + SEED_EXTERNAL_MODELS:
        exists = db.query_one("SELECT model_id FROM model_registry WHERE model_id=?", (m["model_id"],))
        if not exists:
            db.execute(
                "INSERT INTO model_registry(model_id,provider,modality,capabilities_json,parameter_schema,enabled,cost_policy,workflow_version,provider_id,adapter) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (m["model_id"], m["provider"], m["modality"], m["capabilities_json"], m["parameter_schema"],
                 m["enabled"], m["cost_policy"], m["workflow_version"], m.get("provider_id"), m.get("adapter")),
            )


def get_models(enabled_only=False) -> list:
    if enabled_only:
        return db.query("SELECT * FROM model_registry WHERE enabled=1")
    return db.query("SELECT * FROM model_registry")


def get_model(model_id: str) -> dict | None:
    return db.query_one("SELECT * FROM model_registry WHERE model_id=?", (model_id,))


def capabilities(model_id: str) -> dict:
    m = get_model(model_id)
    if not m:
        return {}
    return json.loads(m["capabilities_json"])


def validate_image_params(model_id: str, params: dict) -> list:
    """返回错误列表（空=通过）。服务端二次校验，前端已禁用不支持项。"""
    errs = []
    m = get_model(model_id)
    if not m:
        return ["未知模型"]
    if m["enabled"] != 1:
        return ["该模型当前未接入（disabled）"]
    cap = capabilities(model_id)
    schema = json.loads(m["parameter_schema"])
    # aspect_ratio
    ar = params.get("aspect_ratio", schema.get("aspect_ratio", {}).get("default"))
    if ar and ar not in cap.get("aspect_ratios", []):
        errs.append(f"模型不支持画幅 {ar}")
    # count
    count = _to_int(params.get("count", schema.get("count", {}).get("default", 1)), 1)
    if count < 1 or count > cap.get("max_count", 1):
        errs.append(f"张数超出模型范围 1..{cap.get('max_count',1)}")
    # reference images
    ref_n = _to_int(params.get("reference_count", 0), 0)
    if ref_n > cap.get("image_input_limit", 0):
        errs.append(f"参考图数量超出模型上限 {cap.get('image_input_limit',0)}")
    return errs


def _as_json_str(v, field_name: str) -> str:
    """接受 dict 或合法 JSON 字符串，统一返回 JSON 字符串；非法则抛错。"""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if v is None or v == "":
        return "{}"
    try:
        json.loads(v)
    except Exception:
        raise ValueError(f"{field_name} 不是合法 JSON")
    return v


def _to_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def create_model(m: dict) -> dict:
    model_id = (m.get("model_id") or "").strip()
    if not model_id:
        raise ValueError("model_id 必填")
    if get_model(model_id):
        raise ValueError(f"model_id 已存在：{model_id}")
    cap = _as_json_str(m.get("capabilities_json"), "capabilities_json")
    ps = _as_json_str(m.get("parameter_schema"), "parameter_schema")
    db.execute(
        "INSERT INTO model_registry(model_id,provider,modality,capabilities_json,parameter_schema,enabled,cost_policy,workflow_version,provider_id,adapter) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (model_id, m.get("provider", "local"), m.get("modality", "image"), cap, ps,
         _to_int(m.get("enabled", 1), 1), m.get("cost_policy", "free_local"), m.get("workflow_version", "v1"),
         m.get("provider_id"), m.get("adapter")),
    )
    return get_model(model_id)


def update_model(model_id: str, patch: dict) -> dict | None:
    cur = get_model(model_id)
    if not cur:
        return None
    fields: dict = {}
    for k in ("provider", "modality", "cost_policy", "workflow_version", "provider_id", "adapter"):
        if k in patch and patch[k] is not None:
            fields[k] = patch[k]
    if "enabled" in patch and patch["enabled"] is not None:
        fields["enabled"] = _to_int(patch["enabled"], 1)
    if "capabilities_json" in patch:
        fields["capabilities_json"] = _as_json_str(patch["capabilities_json"], "capabilities_json")
    if "parameter_schema" in patch:
        fields["parameter_schema"] = _as_json_str(patch["parameter_schema"], "parameter_schema")
    if not fields:
        return cur
    setclause = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE model_registry SET {setclause} WHERE model_id=?", tuple(fields.values()) + (model_id,))
    return get_model(model_id)


def delete_model(model_id: str) -> None:
    db.execute("DELETE FROM model_registry WHERE model_id=?", (model_id,))


def resolve_provider(model_id: str) -> dict | None:
    m = get_model(model_id)
    if not m:
        return None
    pid = m.get("provider_id")
    if not pid:
        return None
    p = db.query_one("SELECT * FROM providers WHERE id=?", (pid,))
    if not p:
        return None
    return {"provider_id": p["id"], "name": p["name"], "base_url": p["base_url"], "api_key": p["api_key"]}


def list_providers() -> list:
    return db.query("SELECT * FROM providers ORDER BY created_at ASC")


def get_provider(pid: str) -> dict | None:
    return db.query_one("SELECT * FROM providers WHERE id=?", (pid,))


def create_provider(p: dict) -> dict:
    name = (p.get("name") or "").strip()
    if not name:
        raise ValueError("服务商名称必填")
    pid = (p.get("id") or "").strip() or db.gen_id("prv")
    if get_provider(pid):
        raise ValueError(f"服务商已存在：{pid}")
    db.execute("INSERT INTO providers(id,name,base_url,api_key,enabled,created_at) VALUES(?,?,?,?,?,?)",
               (pid, name, p.get("base_url", ""), p.get("api_key", ""), _to_int(p.get("enabled", 1), 1), db.now()))
    return get_provider(pid)


def update_provider(pid: str, patch: dict) -> dict | None:
    if not get_provider(pid):
        return None
    fields = {}
    for k in ("name", "base_url", "api_key"):
        if k in patch and patch[k] is not None:
            fields[k] = patch[k]
    if "enabled" in patch and patch["enabled"] is not None:
        fields["enabled"] = _to_int(patch["enabled"], 1)
    if not fields:
        return get_provider(pid)
    setclause = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE providers SET {setclause} WHERE id=?", tuple(fields.values()) + (pid,))
    return get_provider(pid)


def delete_provider(pid: str) -> None:
    db.execute("DELETE FROM providers WHERE id=?", (pid,))
    db.execute("UPDATE model_registry SET provider_id=NULL WHERE provider_id=?", (pid,))


"""文本生成适配器（离线规则版）。

遵循 PRD 4.2 AI 行为契约：
- 事实卡：只抽取可见特征并打“识别推测”标签；不创造未提供事实。
- 策略：从确认卖点池选题，三图差异化而非同义改写。
- 提示词：只扩写当前策略为画面指令，输出带 selling_point_id / product_id / locked_visual_features。
- 生图/审核/全局 Agent 见各自模块。

接入真实 LLM：将下方 build_* 函数替换为对 LiteLLM/自建网关的调用即可，契约不变。
"""
import json
import re

ROLE_TEMPLATES = [
    {"role": "主图", "composition": "主体居中，留标题安全区", "scene": "纯色/轻场景棚拍", "aspect": "1:1"},
    {"role": "场景图", "composition": "多区域生活场景", "scene": "真实可用的家庭场景", "aspect": "4:3"},
    {"role": "细节图", "composition": "产品与部件特写", "scene": "纯色棚拍微距", "aspect": "1:1"},
]


def _palette_from_locked(locked: list) -> list:
    # 从锁定外观里粗取颜色词
    colors = []
    for w in locked:
        for c in ["暖木色", "奶油白", "原木色", "深灰", "黑色", "白色", "米色", "胡桃木", "浅灰"]:
            if c in w:
                colors.append(c)
    return list(dict.fromkeys(colors)) or ["暖木色", "奶油白"]


def _alt_scene(scene: str) -> str:
    """为场景搭配库提供一个差异化备选场景。"""
    table = {
        "纯色棚拍": "轻北欧客厅一角", "轻场景棚拍": "纯色渐变背景",
        "真实可用的家庭场景": "明亮厨房操作台", "纯色/轻场景棚拍": "柔光窗边",
        "客厅": "阳台", "卧室": "书房", "厨房": "餐厅", "浴室": "衣帽间",
    }
    return table.get(scene, "柔光纯色背景")


def generate_strategy(facts: dict, task_brief: dict) -> dict:
    """facts: 产品事实卡 content；task_brief: {goal, channel, audience}。

    返回结构化策略（对齐「策略方案」卡片：需求摘要 / 统一视觉基线 / 目标比例 /
    每图卖点证明 / 场景搭配库 / 产品位置 / 文字与信息模块）。
    """
    sps = facts.get("confirmed_selling_points", []) or []
    if not sps:
        sps = [{"id": "sp_auto", "text": "核心卖点（待填写）"}]
    locked = facts.get("locked_appearance", []) or []
    palette = _palette_from_locked(locked)
    product_name = facts.get("product_name", "产品") or "产品"
    audience = task_brief.get("audience") or "居家用户"
    channel = task_brief.get("channel") or "电商主图"
    goal = task_brief.get("goal") or "提升点击与转化"
    policies = facts.get("policies", []) or []

    requirement_summary = (
        f"为「{product_name}」面向{audience}，在{channel}渠道投放；核心目标：{goal}。"
        f"整体调性自然、低饱和，强调材质真实感与品牌一致性。"
    )
    material_light = "柔光漫射，突出哑光质感与材质纹理，避免硬阴影"
    imaging_mode = "棚拍主体 + 轻场景合成，统一成像语言"

    strategies = []
    for i, tpl in enumerate(ROLE_TEMPLATES):
        sp = sps[i % len(sps)]
        sp_id = sp.get("id") or f"sp_{i+1}"
        sp_text = sp.get("text", "")
        scene = sp.get("scene") or tpl["scene"]
        strategies.append({
            "id": f"hero_{i+1}",
            "role": tpl["role"],
            "selling_point_id": sp_id,
            "selling_point_text": sp_text,
            "audience": sp.get("audience") or audience,
            "scene": scene,
            "composition": tpl["composition"],
            "aspect_ratio": tpl["aspect"],
            # —— 新增：截图「策略方案」结构化字段 ——
            "proposition_proof": f"用特写与暖光呈现「{sp_text}」，让卖点在画面中被直观验证，而非仅靠文字说明",
            "scene_options": [scene, _alt_scene(scene)],
            "product_placement": "居中偏下" if tpl["role"] == "主图" else ("右侧三分线" if tpl["role"] == "场景图" else "居中特写"),
            "text_modules": {
                "headline": "主图标题（待定）",
                "selling_point": sp_text,
                "policy": policies[0] if policies else "",
                "badge": "品牌标识（待确认）",
            },
        })
    return {
        "product_id": facts.get("product_id", ""),
        "task_brief": {"goal": goal, "channel": channel, "audience": audience},
        "requirement_summary": requirement_summary,
        "visual_baseline": {
            "palette": palette,
            "material_light": material_light,
            "imaging_mode": imaging_mode,
            "locked_features": locked,
        },
        "target_aspect": ["1:1", "4:3"],
        "strategies": strategies,
    }


def generate_prompt(strategy_item: dict, facts: dict, global_constraints: dict | None = None, baseline: dict | None = None) -> dict:
    """把单张图策略扩写为画面提示词。baseline 来自策略节点的 visual_baseline。"""
    product_name = facts.get("product_name", "产品") or "产品"
    sp_text = strategy_item.get("selling_point_text", "")
    scene = strategy_item.get("scene", "")
    comp = strategy_item.get("composition", "")
    baseline = baseline or facts.get("visual_baseline", {}) or {}
    palette = baseline.get("palette") or _palette_from_locked(facts.get("locked_appearance", []) or [])
    material_light = baseline.get("material_light", "")
    imaging_mode = baseline.get("imaging_mode", "")
    forbidden = facts.get("forbidden_expressions", []) or []

    positive = (
        f"商业产品摄影，{product_name}，{strategy_item.get('role','主图')}。"
        f"场景：{scene}。构图：{comp}。"
        f"主卖点：{sp_text}。"
        f"卖点证明：{strategy_item.get('proposition_proof','')}。"
        f"视觉基调：{material_light}；成像方式：{imaging_mode}。"
        f"配色：{('、'.join(palette)) if palette else '柔和自然'}。"
        f"光线柔和、质感清晰、留白充足、适合电商主图。"
    )
    negative = "变形，多余文字水印，低分辨率，畸变，" + ("，".join(forbidden) if forbidden else "无关元素")
    return {
        "prompt": positive,
        "negative_prompt": negative,
        "requested_aspect_ratio": strategy_item.get("aspect_ratio", "1:1"),
        "selling_point_id": strategy_item.get("selling_point_id", ""),
        "product_id": facts.get("product_id", ""),
        "locked_visual_features": facts.get("locked_appearance", []) or [],
        "bg_style": "auto",
    }


def text_edit_prompt(node_type: str, base_content: dict, instruction: str) -> tuple[dict, str]:
    """节点级 AI 局部修改：仅修改该节点内容，返回(候选内容, 变更摘要)。不暗改其他节点。"""
    new = json.loads(json.dumps(base_content))
    summary = []
    ins = instruction.strip()

    if node_type == "image_prompt":
        pos = new.get("prompt", "")
        # 把指令作为风格/构图强化追加到正面提示词（不删除既有内容）
        new["prompt"] = f"{pos} {ins}".strip()
        # 简单意图识别：含“高级感/质感”等 → 提升提示词强度
        if any(k in ins for k in ["高级感", "质感", "精致", "高端"]):
            new["prompt"] = new["prompt"] + "，高级感，精致材质细节"
            summary.append("在正面提示词强化高级感/质感")
        if any(k in ins for k in ["更简洁", "留白", "极简"]):
            new["prompt"] = new["prompt"] + "，极简留白"
            summary.append("在正面提示词强化极简留白")
        summary.append("仅修改本节点提示词，未触动上游策略/事实")

    elif node_type == "strategy":
        # 修改策略节点：依据指令调整 copy/场景；不创造新卖点
        strat = new
        if "场景" in ins or "scene" in ins.lower():
            m = re.search(r"场景[是为：: ]*([^\s，。；]+)", ins)
            if m:
                for s in strat.get("strategies", []):
                    s["scene"] = m.group(1)
                summary.append(f"按指令调整策略场景为「{m.group(1)}」")
        if "文案" in ins or "标题" in ins:
            for s in strat.get("strategies", []):
                s["copy_text"] = "示例文案（已按指令调整，仅供排版审核）"
            summary.append("策略文案位按指令标记待排版")
        summary.append("仅修改策略节点，未触动事实卡")

    elif node_type == "product_facts":
        # 事实卡：仅允许补充“识别推测”字段，绝不删除已确认事实
        note = new.get("recognition_notes", [])
        note.append(f"[识别推测，需人工核对] {ins}")
        new["recognition_notes"] = note
        summary.append("已将指令作为“识别推测”追加，未改动任何已确认事实")

    else:
        new["_note"] = ins
        summary.append("已记录指令备注")

    return new, "；".join(summary) if summary else "已生成候选修改"


import urllib.request


def chat_completion(model_id: str, provider_cfg: dict, messages: list, temperature: float = 0.7, json_mode: bool = False) -> str:
    base = (provider_cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise ValueError("服务商未配置 Base URL")
    key = provider_cfg.get("api_key") or ""
    payload = {"model": model_id, "messages": messages, "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base + "/chat/completions",
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def _extract_json(s: str) -> dict:
    s = (s or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip("`")
    i = s.find("{"); j = s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    return json.loads(s)


def generate_strategy_llm(model_id: str, provider_cfg: dict, facts: dict, task_brief: dict) -> dict:
    sys = "你是资深电商营销视觉策划。只输出一个 JSON 对象，不要任何额外解释。"
    example = {"product_id": "", "task_brief": {"goal": "", "channel": "", "audience": ""},
               "requirement_summary": "", "visual_baseline": {"palette": [], "material_light": "", "imaging_mode": "", "locked_features": []},
               "target_aspect": ["1:1", "4:3"],
               "strategies": [{"id": "hero_1", "role": "主图", "selling_point_id": "", "selling_point_text": "", "audience": "", "scene": "", "composition": "", "aspect_ratio": "1:1", "proposition_proof": "", "scene_options": [], "product_placement": "", "text_modules": {"headline": "", "selling_point": "", "policy": "", "badge": ""}}]}
    user = ("请把下面的产品事实卡和任务 Brief 生成 3 套差异化视觉策略（strategies 数组固定 3 项，对应 主图/场景图/细节图），"
            "严格输出与示例同结构的 JSON：\n" + json.dumps({"facts": facts, "task_brief": task_brief}, ensure_ascii=False)
            + "\n示例结构：" + json.dumps(example, ensure_ascii=False))
    txt = chat_completion(model_id, provider_cfg, [{"role": "system", "content": sys}, {"role": "user", "content": user}], json_mode=True)
    data = _extract_json(txt)
    if not data.get("strategies"):
        data["strategies"] = generate_strategy(facts, task_brief)["strategies"]
    return data


def generate_prompt_llm(model_id: str, provider_cfg: dict, strategy_item: dict, facts: dict, baseline: dict | None = None) -> dict:
    sys = "你是商业产品摄影提示词专家。只输出一个 JSON 对象，不要任何额外解释。"
    user = ("请把下面这张图的策略扩写为中文画面提示词，输出 JSON：{\"prompt\":\"\",\"negative_prompt\":\"\",\"requested_aspect_ratio\":\"\"}。\n"
            + json.dumps({"strategy": strategy_item, "facts": facts, "baseline": baseline}, ensure_ascii=False))
    txt = chat_completion(model_id, provider_cfg, [{"role": "system", "content": sys}, {"role": "user", "content": user}], json_mode=True)
    data = _extract_json(txt)
    if not data.get("prompt"):
        raise ValueError("LLM 未返回有效提示词")
    data.setdefault("requested_aspect_ratio", strategy_item.get("aspect_ratio", "1:1"))
    return data


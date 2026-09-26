"""合规初筛：基于关键词规则的文本扫描。

说明：这是**规则初筛**，用于在提交审核前把高风险表述挑出来提示人，不等价于合规结论，
也不做图像内容识别。审核结论仍以人工审核为准（后台审核中心）。
"""
import json

RULE_SET = [
    {"code": "absolute_wording", "name": "绝对化用语", "risk": "high",
     "patterns": ["最好", "最佳", "最强", "第一", "唯一", "顶级", "国家级", "世界级", "绝无仅有", "史无前例", "极致", "永久", "最便宜"]},
    {"code": "exaggerated_efficacy", "name": "夸大功效", "risk": "high",
     "patterns": ["治愈", "根治", "药到病除", "百分百", "100%", "零风险", "彻底解决", "包治"]},
    {"code": "medical_claim", "name": "医疗健康暗示", "risk": "high",
     "patterns": ["消炎", "杀菌", "抗菌", "除菌", "治疗", "医用", "保健功效", "抗病毒"]},
    {"code": "eco_zero", "name": "环保/无醛/零甲醛表述", "risk": "high",
     "patterns": ["零甲醛", "无醛", "零污染", "无毒无害", "纯天然", "零 VOC", "无voc"]},
    {"code": "price_promo", "name": "价格与促销限时规则", "risk": "medium",
     "patterns": ["仅限今日", "最后一天", "全网最低", "亏本", "免费送", "0元", "1元", "限时秒杀"]},
    {"code": "portrait", "name": "人物肖像授权", "risk": "medium",
     "patterns": ["代言人", "明星同款", "网红同款", "本人出镜", "素人实拍"]},
    {"code": "trademark", "name": "Logo/商标与版权", "risk": "medium",
     "patterns": ["官方指定", "授权品牌", "独家代理", "驰名商标"]},
    {"code": "sensitive", "name": "敏感内容", "risk": "high",
     "patterns": ["赌博", "高额回报", "保本保收益", "一夜暴富"]},
    {"code": "qrcode", "name": "二维码与外链风险", "risk": "low",
     "patterns": ["扫码", "加微信", "私信领取", "点击链接", "限时进群"]},
]

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

TARGET_TYPES = ["user_input", "prompt", "reference_image", "generated_image",
                "canvas_output", "copywriting", "price_promo", "portrait",
                "logo", "qrcode", "case_data"]

TARGET_LABELS = {
    "user_input": "用户输入", "prompt": "Prompt", "reference_image": "参考图",
    "generated_image": "生成图片", "canvas_output": "画布最终成品", "copywriting": "标题与卖点文案",
    "price_promo": "价格与促销信息", "portrait": "人物肖像", "logo": "Logo",
    "qrcode": "二维码", "case_data": "案例和数据",
}


def rules() -> list:
    return [{"code": r["code"], "name": r["name"], "risk": r["risk"], "patterns": r["patterns"]} for r in RULE_SET]


def scan_text(text: str) -> dict:
    """返回 {risk_level, hits:[{code,name,risk,matched}]}。"""
    t = (text or "").lower()
    hits = []
    for r in RULE_SET:
        matched = [p for p in r["patterns"] if p.lower() in t]
        if matched:
            hits.append({"code": r["code"], "name": r["name"], "risk": r["risk"], "matched": matched})
    level = "low"
    for h in hits:
        if RISK_ORDER[h["risk"]] > RISK_ORDER[level]:
            level = h["risk"]
    return {"risk_level": level, "hits": hits, "text_length": len(text or "")}


def scan_many(texts: dict) -> dict:
    """对多个字段分别扫描，返回合并结果（用于画布 / 项目提交审核）。"""
    all_hits, level, per_field = [], "low", {}
    for field, text in (texts or {}).items():
        r = scan_text(text)
        per_field[field] = r
        if r["hits"]:
            all_hits.extend([{**h, "field": field} for h in r["hits"]])
        if RISK_ORDER[r["risk_level"]] > RISK_ORDER[level]:
            level = r["risk_level"]
    return {"risk_level": level, "hits": all_hits, "fields": per_field,
            "checklist": [{"code": r["code"], "name": r["name"], "risk": r["risk"],
                           "hit": any(h["code"] == r["code"] for h in all_hits)} for r in RULE_SET]}


def project_texts(pid: str, db) -> dict:
    """收集项目里需要过筛的文本：Brief、Prompt 版本、画布文本图层。"""
    texts = {}
    brief = db.query_one("SELECT * FROM generation_briefs WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,))
    if brief:
        texts["user_input"] = brief.get("user_prompt") or ""
        try:
            kw = json.loads(brief.get("style_keywords_json") or "[]") + json.loads(brief.get("brand_keywords_json") or "[]")
            if kw:
                texts["copywriting"] = "、".join(kw)
        except Exception:
            pass
    pv = db.query_one("SELECT prompt FROM prompt_versions WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (pid,))
    if pv:
        texts["prompt"] = pv.get("prompt") or ""
    doc = db.query_one("SELECT canvas_json FROM canvas_documents WHERE project_id=? ORDER BY updated_at DESC LIMIT 1", (pid,))
    if doc:
        try:
            layers = json.loads(doc["canvas_json"]).get("layers", [])
            t = " ".join(str(l.get("text") or "") for l in layers)
            if t.strip():
                texts["canvas_copy"] = t
        except Exception:
            pass
    return texts

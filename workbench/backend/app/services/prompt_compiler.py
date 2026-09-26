"""提示词编译器：把 Prompt 模板 + Brief 变量编译为最终提示词。"""
import json


def compile_prompt(template_text: str | None, brief: dict) -> str:
    user_prompt = (brief.get("user_prompt") or "").strip()
    if not template_text:
        return user_prompt
    def kw(field):
        try:
            return "、".join(json.loads(brief.get(field) or "[]"))
        except Exception:
            return ""
    mapping = {
        "user_prompt": user_prompt,
        "platform": brief.get("platform", ""),
        "aspect_ratio": brief.get("aspect_ratio", ""),
        "image_count": str(brief.get("image_count", "")),
        "purpose": brief.get("purpose", ""),
        "style_keywords": kw("style_keywords_json"),
        "brand_keywords": kw("brand_keywords_json"),
    }
    out = template_text
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", v)
    return out.strip()

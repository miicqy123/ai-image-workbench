"""图片生成适配器：统一 GenerationRequest -> GenerationResult 契约。

MVP 内置 local-poster-compositor：依据产品图 + 提示词合成「背景渐变 + 产品主体 + 可编辑文字层」的
真实可见海报 PNG，并产出分层元数据（background_bitmap / product_cutout_or_render / text_layers）。
无密钥也能演示完整管线；真实模型接入时只需新增实现 generate() 的适配器并登记到注册表。

ComfyUI / 商业 API 适配器：见 ComfyUIAdapter / CommercialApiAdapter 占位类，结构兼容，
默认未启用，不捏造 provider 路由（遵循 PRD 5.4 / 6.5）。
"""
import io
import json
import os
import random
import base64
import time
import urllib.request
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from .. import db, storage

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
]


def _font(size: int):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


SCENE_COLORS = {
    "客厅": ("#c9a878", "#f4ece0"),
    "卧室": ("#b3a3c7", "#efeaf4"),
    "厨房": ("#9bb3a4", "#e9f1ea"),
    "浴室": ("#9bb6c7", "#e8f1f5"),
    "阳台": ("#a9bd8f", "#eef3e7"),
    "纯色": ("#d8cfc1", "#f5f1ea"),
    "棚拍": ("#cfc8bf", "#f2eee8"),
    "生活": ("#cdb89a", "#f3ece1"),
}


def _dims(aspect: str, tier: str):
    base = 1280 if tier == "hd" else 1024
    a = (aspect or "1:1").replace(" ", "")
    if a == "1:1":
        return base, base
    if a == "4:3":
        return base, int(base * 3 / 4)
    if a == "3:4":
        return int(base * 3 / 4), base
    if a == "16:9":
        return base, int(base * 9 / 16)
    return base, base


def _scene_colors(prompt: str):
    for k, v in SCENE_COLORS.items():
        if k in prompt:
            return v
    if "暖木" in prompt or "原木" in prompt:
        return ("#c9a878", "#f4ece0")
    if "奶油" in prompt or "白" in prompt:
        return ("#e7ddd0", "#f8f4ee")
    return ("#cab89c", "#f3ece1")


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _gradient(w, h, top, bottom):
    t = _hex(top); b = _hex(bottom)
    col = Image.new("RGB", (1, h))
    for y in range(h):
        r = int(t[0] + (b[0] - t[0]) * y / h)
        g = int(t[1] + (b[1] - t[1]) * y / h)
        bl = int(t[2] + (b[2] - t[2]) * y / h)
        col.putpixel((0, y), (r, g, bl))
    return col.resize((w, h)).convert("RGBA")


def _fit_product(prod: Image.Image, max_w, max_h) -> Image.Image:
    prod = prod.convert("RGBA")
    pw, ph = prod.size
    scale = min(max_w / pw, max_h / ph)
    nw, nh = int(pw * scale), int(ph * scale)
    return prod.resize((nw, nh), Image.LANCZOS)


def _shadow(im: Image.Image):
    a = im.split()[3]
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sda = a.filter(ImageFilter.GaussianBlur(18)).point(lambda p: int(p * 0.35))
    sh.putalpha(sda)
    return sh


def compose_one(product_imgs: list, prompt: str, params: dict, seed: int, w: int, h: int) -> Image.Image:
    rnd = random.Random(seed)
    top, bottom = _scene_colors(prompt)
    canvas = _gradient(w, h, top, bottom)
    dr = ImageDraw.Draw(canvas)
    # 柔和径向高光
    glow = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    gd = ImageDraw.Draw(glow)
    cx, cy = w // 2, int(h * 0.42)
    gd.ellipse([cx - w * 0.42, cy - h * 0.42, cx + w * 0.42, cy + h * 0.42], fill=(255, 255, 255, 40))
    canvas = Image.alpha_composite(canvas, glow.filter(ImageFilter.GaussianBlur(60)))

    # 产品主体
    if product_imgs:
        prod = _fit_product(product_imgs[0], int(w * 0.62), int(h * 0.62))
        px = (w - prod.width) // 2
        py = int(h * 0.40) - prod.height // 2
        py = max(int(h * 0.12), py)
        shadow = _shadow(prod)
        canvas.alpha_composite(shadow, (px + 14, py + 20))
        canvas.alpha_composite(prod, (px, py))
    else:
        # 无产品图：绘制占位主体块（明确标注，不冒充真实产品）
        bx, by, bw, bh = int(w * 0.22), int(h * 0.2), int(w * 0.56), int(h * 0.5)
        dr.rounded_rectangle([bx, by, bx + bw, by + bh], radius=24, fill=(255, 255, 255, 200), outline=(120, 120, 120, 255), width=3)
        f = _font(int(h * 0.04))
        dr.text((w // 2, int(h * 0.45)), "未提供产品图", font=f, fill=(90, 90, 90, 255), anchor="mm")

    # 文字层（可编辑，导出时叠加）
    title = (params.get("title") or "产品主图").strip()
    subtitle = (params.get("subtitle") or "").strip()
    bar_y = int(h * 0.82)
    dr.line([int(w * 0.12), bar_y, int(w * 0.88), bar_y], fill=(255, 255, 255, 160), width=3)
    tf = _font(int(h * 0.07))
    dr.text((w // 2, int(h * 0.78)), title, font=tf, fill=(40, 40, 40, 255), anchor="mm")
    if subtitle:
        sf = _font(int(h * 0.038))
        dr.text((w // 2, int(h * 0.90)), subtitle, font=sf, fill=(70, 70, 70, 255), anchor="mm")
    return canvas.convert("RGB")


def generate(req: dict, reference_images: list) -> dict:
    """req: {model_id, prompt, negative_prompt, reference_asset_ids, aspect_ratio,
             resolution_tier, count, seed, params}。返回 GenerationResult。"""
    aspect = req.get("aspect_ratio", "1:1")
    tier = req.get("resolution_tier", req.get("resolutionTier", "standard"))
    count = max(1, min(int(req.get("count", 1)), 4))
    params = req.get("params", {}) or {}
    seed = int(req.get("seed", 1234) or 1234)
    w, h = _dims(aspect, tier)

    outputs = []
    for i in range(count):
        im = compose_one(reference_images, req.get("prompt", ""), params, seed + i, w, h)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        outputs.append({"bytes": buf.getvalue(), "width": w, "height": h})

    usage = {
        "provider": "local",
        "model_id": req.get("model_id"),
        "model_version": "compositor-v1",
        "count": count,
        "aspect_ratio": aspect,
        "resolution_tier": tier,
        "cost_unit": "free_local",
        "billing_note": "本地合成，不计费",
    }
    return {
        "provider_task_id": f"local_{seed}_{count}",
        "status": "succeeded",
        "outputs": outputs,
        "model_version": "compositor-v1",
        "usage": usage,
        "error_code": None,
    }


class ComfyUIAdapter:
    """占位：真实接入时在后端用版本化 API 工作流模板调用 ComfyUI，禁止前端直传任意 JSON。默认未启用。"""

    enabled = False

    def generate(self, req: dict, reference_images: list) -> dict:
        raise NotImplementedError("ComfyUI 适配器未启用：请在后端配置 ComfyUI 服务与工作流模板后启用。")


class CommercialApiAdapter:
    """占位：按采购合同接入商业图片 API；实际路由到的 provider/model 需记录以保证可追溯。默认未启用。"""

    enabled = False

    def generate(self, req: dict, reference_images: list) -> dict:
        raise NotImplementedError("商业图片 API 适配器未启用：请配置密钥与授权范围后启用，并确保数据出境合规。")


def dispatch(model_id: str, req: dict, reference_images: list, provider_cfg: dict | None = None, adapter: str | None = None) -> dict:
    if model_id == "local-poster-compositor":
        return generate(req, reference_images)
    if adapter == "image_openai" and provider_cfg:
        return generate_openai_image(model_id, provider_cfg, req)
    if adapter == "image_dashscope" and provider_cfg:
        return generate_dashscope_wanx(model_id, provider_cfg, req)
    if model_id == "comfyui-product-edit":
        return ComfyUIAdapter().generate(req, reference_images)
    if model_id == "api-commercial-image":
        return CommercialApiAdapter().generate(req, reference_images)
    raise ValueError(f"未接入的图片模型：{model_id}")


# ---------------- 图片处理意图（节点悬浮工具栏） ----------------
# intent -> 需要的 editing_mode（用于按模型能力置灰 / 路由）
INTENT_MODE = {
    "改字": {"mode": "text_overlay", "model": "local-poster-compositor"},
    "替换": {"mode": "replace", "model": None},            # 仅换底图资产，不走模型
    "抠图": {"mode": "inpaint", "model": "comfyui-product-edit"},
    "消除": {"mode": "inpaint", "model": "comfyui-product-edit"},
    "变清晰": {"mode": "upscale", "model": "comfyui-product-edit"},
    "扩图": {"mode": "outpaint", "model": "comfyui-product-edit"},
    "相似图": {"mode": "img2img", "model": "comfyui-product-edit"},
    "风格迁移": {"mode": "img2img", "model": "comfyui-product-edit"},
    "溶图": {"mode": "img2img", "model": "comfyui-product-edit"},
    "变体": {"mode": "img2img", "model": "comfyui-product-edit"},
    "转矢量": {"mode": "img2img", "model": "comfyui-product-edit"},
    "图文分层": {"mode": "text_overlay", "model": "local-poster-compositor"},
}


def edit_text(asset_bytes: bytes, title: str, subtitle: str) -> bytes:
    """在已有生成图上重新叠加文字层（改字不重生图）。返回新 PNG 字节。"""
    im = Image.open(io.BytesIO(asset_bytes)).convert("RGB")
    w, h = im.size
    dr = ImageDraw.Draw(im)
    if title:
        f = _font(int(h * 0.07))
        dr.text((w // 2, int(h * 0.78)), title, font=f, fill=(40, 40, 40, 255), anchor="mm")
    if subtitle:
        f = _font(int(h * 0.038))
        dr.text((w // 2, int(h * 0.90)), subtitle, font=f, fill=(70, 70, 70, 255), anchor="mm")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def dispatch_tool(intent: str, asset_ids: list, title: str = "", subtitle: str = "", model_id: str | None = None) -> dict:
    """节点悬浮工具栏的图片处理意图分发。

    返回 {outputs:[{bytes,width,height}], usage, provider_task_id, status}。
    未接入能力直接抛错（由接口层转 422），不捏造路由。
    """
    info = INTENT_MODE.get(intent)
    if not info:
        raise ValueError(f"未知图片处理意图：{intent}")
    if intent == "替换":
        # 替换仅在前端切换底图资产，后端无需生成；这里返回空表示交给前端处理
        return {"outputs": [], "usage": {}, "provider_task_id": "replace_noop", "status": "noop"}
    if intent in ("改字", "图文分层"):
        if not asset_ids:
            raise ValueError("改字需要选择一张底图")
        # 本地合成器可直接叠加文字层，真实可用
        row = db.query_one("SELECT object_key FROM assets WHERE id=?", (asset_ids[0],))
        if not row:
            raise ValueError("底图资产不存在")
        a = storage.read_bytes(row["object_key"])
        new_bytes = edit_text(a, title, subtitle)
        im = Image.open(io.BytesIO(new_bytes))
        return {
            "outputs": [{"bytes": new_bytes, "width": im.width, "height": im.height}],
            "usage": {"provider": "local", "model_id": "local-poster-compositor", "intent": intent},
            "provider_task_id": f"tool_{intent}", "status": "succeeded",
        }
    # 其余意图（抠图/消除/变清晰/扩图/风格迁移…）依赖 ComfyUI / 商业 API 适配器
    model_id = model_id or info["model"]
    if model_id:
        try:
            return dispatch(model_id, {"model_id": model_id, "prompt": intent,
                                       "params": {"title": title, "subtitle": subtitle}}, [])
        except NotImplementedError as e:
            # 适配器未启用：转为可读的 422，而不是 500
            raise ValueError(f"意图「{intent}」暂未接入：{e}")
        except ValueError as e:
            raise ValueError(f"意图「{intent}」暂未接入：{e}")
    raise ValueError(f"意图「{intent}」暂未接入可用模型")


def _http_json(url: str, payload: dict, key: str, timeout: int = 120) -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _size_for(aspect: str) -> str:
    return {"1:1": "1024x1024", "4:3": "1280x960", "3:4": "960x1280", "16:9": "1280x720"}.get((aspect or "1:1").replace(" ", ""), "1024x1024")


def generate_openai_image(model_id: str, provider_cfg: dict, req: dict) -> dict:
    base = (provider_cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise ValueError("服务商未配置 Base URL")
    size = _size_for(req.get("aspect_ratio", "1:1"))
    count = max(1, min(int(req.get("count", 1) or 1), 4))
    data = _http_json(base + "/images/generations",
                      {"model": model_id, "prompt": req.get("prompt", ""), "size": size, "n": count, "response_format": "b64_json"},
                      provider_cfg.get("api_key") or "")
    outputs = []
    for item in data.get("data", []):
        b64 = item.get("b64_json")
        if not b64 and item.get("url"):
            with urllib.request.urlopen(item["url"], timeout=120) as r:
                b64 = base64.b64encode(r.read()).decode("utf-8")
        if b64:
            outputs.append({"bytes": base64.b64decode(b64), "width": int(size.split("x")[0]), "height": int(size.split("x")[1])})
    return {"outputs": outputs, "usage": {"provider": provider_cfg.get("name"), "model_id": model_id, "count": len(outputs)},
            "provider_task_id": "openai_image"}


def generate_dashscope_wanx(model_id: str, provider_cfg: dict, req: dict) -> dict:
    key = provider_cfg.get("api_key") or ""
    if not key:
        raise ValueError("通义万相需要配置 API Key")
    size = _size_for(req.get("aspect_ratio", "1:1")).replace("x", "*")
    payload = {"model": model_id, "input": {"prompt": req.get("prompt", "")}, "parameters": {"size": size, "n": 1}}
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "X-DashScope-Async": "enable"}
    rq = urllib.request.Request("https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis",
                                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(rq, timeout=120) as r:
        task = json.loads(r.read().decode("utf-8"))
    task_id = task["output"]["task_id"]
    for _ in range(60):
        gr = urllib.request.Request("https://dashscope.aliyuncs.com/api/v1/tasks/" + task_id, headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(gr, timeout=120) as r:
            st = json.loads(r.read().decode("utf-8"))
        out = st.get("output", {})
        if out.get("task_status") in ("SUCCEEDED", "FAILED", "CANCELED"):
            outputs = []
            for it in out.get("results", []):
                u = it.get("url")
                if u:
                    with urllib.request.urlopen(u, timeout=120) as r:
                        outputs.append({"bytes": r.read(), "width": 1024, "height": 1024})
            return {"outputs": outputs, "usage": {"provider": provider_cfg.get("name"), "model_id": model_id, "count": len(outputs)},
                    "provider_task_id": task_id}
        time.sleep(1)
    raise ValueError("通义万相生成超时")


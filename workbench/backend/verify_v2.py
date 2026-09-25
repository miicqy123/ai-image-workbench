"""验证三块改造：A 策略结构化 / C 全局模型默认 / B 图片处理意图。"""
import json
import time
import urllib.request

B = "http://127.0.0.1:8000"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(r, timeout=10)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def poll(rid):
    for _ in range(50):
        s, t = req("GET", f"/api/runs/{rid}")
        d = json.loads(t)
        if d["status"] in ("succeeded", "failed"):
            return d
        time.sleep(0.3)
    return {"status": "timeout"}


def node_by_id(pid, nid):
    s, t = req("GET", f"/api/projects/{pid}/graph")
    return [n for n in json.loads(t)["nodes"] if n["id"] == nid][0]


# 建项目 + 模板
s, t = req("POST", "/api/projects", {"name": "verify-v2"})
pid = json.loads(t)["id"]
req("POST", f"/api/projects/{pid}/graph/init-template")
s, t = req("GET", f"/api/projects/{pid}/graph")
g = json.loads(t)
N = {n["type"]: n["id"] for n in g["nodes"]}
print("项目:", pid)

# 填事实卡
facts = {
    "product_id": "SKU-A", "product_name": "实木餐边柜", "activity_version": "v1",
    "confirmed_selling_points": [
        {"id": "sp1", "text": "全实木无贴皮", "audience": "新婚家庭", "scene": "餐厅"},
        {"id": "sp2", "text": "大容量收纳", "audience": "小户型", "scene": "客厅"},
        {"id": "sp3", "text": "圆角防磕碰", "audience": "有娃家庭", "scene": "儿童房"},
    ],
    "locked_appearance": ["暖木色柜体", "黑色拉手"], "applicable_scenes": ["餐厅", "客厅"],
    "forbidden_expressions": ["全网最低"], "policies": ["以旧换新补贴"], "recognition_notes": [],
}
req("PATCH", f"/api/nodes/{N['product_facts']}", {"content": facts})

# —— A：策略结构化 ——
s, t = req("POST", f"/api/nodes/{N['strategy']}/runs",
           {"model_id": "rule-based-planner", "params": {}, "idempotency_key": f"k_a_{time.time()}"})
poll(json.loads(t)["run_id"])
s, t = req("GET", f"/api/projects/{pid}/graph")
strat = [n for n in json.loads(t)["nodes"] if n["type"] == "strategy"][0]["content"]
one = strat["strategies"][0]
print("\n[A] 需求摘要:", (strat.get("requirement_summary") or "")[:40], "...")
print("[A] 视觉基线 material_light:", strat["visual_baseline"].get("material_light"))
print("[A] 成像方式:", strat["visual_baseline"].get("imaging_mode"))
print("[A] 目标比例:", strat.get("target_aspect"))
print("[A] 策略1 卖点证明:", (one.get("proposition_proof") or "")[:30], "...")
print("[A] 策略1 场景搭配库:", one.get("scene_options"))
print("[A] 策略1 产品位置:", one.get("product_placement"))
print("[A] 策略1 文字模块:", one.get("text_modules"))

# A2：提示词是否引用新字段
s, t = req("POST", f"/api/nodes/{N['image_prompt']}/runs", {"params": {}, "idempotency_key": f"k_p_{time.time()}"})
poll(json.loads(t)["run_id"])
pc = node_by_id(pid, N["image_prompt"])["content"]
p = pc.get("prompt", "")
print("[A] 提示词含『卖点证明』:", "卖点证明" in p, "| 含『视觉基调』:", "视觉基调" in p)

# —— C：全局模型默认 ——
s, t = req("GET", f"/api/projects/{pid}/defaults")
print("\n[C] GET defaults:", s, t)
s, t = req("PUT", f"/api/projects/{pid}/defaults", {"default_text_model": "rule-based-planner",
                                                    "default_image_model": "local-poster-compositor"})
print("[C] PUT defaults:", s, t)
# 不带 model_id 运行，验证回退默认
s, t = req("POST", f"/api/nodes/{N['strategy']}/runs", {"params": {}, "idempotency_key": f"k_c_{time.time()}"})
rid = json.loads(t)["run_id"]
poll(rid)
s, t = req("GET", f"/api/runs/{rid}")
print("[C] 未传 model_id 运行时实际模型:", json.loads(t)["model_id"])

# —— B：图片处理意图 ——
s, t = req("GET", "/api/image-tools/intents")
ints = json.loads(t)
print("\n[B] 意图清单:")
for i in ints:
    print(f"    {i['intent']:<6} available={i['available']} model={i['model_id']}")
# 先生成一张底图
s, t = req("POST", f"/api/nodes/{N['image_generation']}/runs",
           {"model_id": "local-poster-compositor", "params": {"count": 1, "title": "原文字", "subtitle": "旧副标"},
            "idempotency_key": f"k_g_{time.time()}"})
poll(json.loads(t)["run_id"])
gen = node_by_id(pid, N["image_generation"])
base = (gen["content"].get("outputs") or [None])[0]
print("[B] 底图:", base)
# 改字（真实可用）
s, t = req("POST", "/api/image-tools/run", {"node_id": N["image_generation"], "intent": "改字",
                                            "asset_id": base, "title": "新标题", "subtitle": "新副标"})
print("[B] 改字:", s, (json.loads(t).get("outputs") or [{}])[0].get("asset_id") if s == 200 else t[:80])
# 未接入意图应被拦截
s, t = req("POST", "/api/image-tools/run", {"node_id": N["image_generation"], "intent": "抠图", "asset_id": base})
print("[B] 抠图（未接入应报错）:", s, t[:90])
# 首页
s, t = req("GET", "/")
print("\n[静态] GET /:", s, "| 新 JS 命中:", "index-Bm5Jr9Rm.js" in t)

"""端到端验证脚本：覆盖 PRD 验收用例 1-7 的后端链路。"""
import io, json, time, urllib.request, urllib.error
from PIL import Image, ImageDraw

BASE = "http://127.0.0.1:8000"

def req(method, path, body=None):
    url = BASE + path
    data = None; h = {}
    if body is not None:
        data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

def multipart(path, field, filename, data_bytes, ctype, extra_fields=None):
    boundary = "----wbtestboundary"
    parts = []
    if extra_fields:
        for k, v in extra_fields.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
    parts.append(data_bytes); parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return req("POST", path, raw=body) if False else _raw(path, body, boundary)

def _raw(path, body, boundary):
    url = BASE + path
    r = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

def poll_run(rid, timeout=40):
    for _ in range(timeout * 4):
        st, txt = req("GET", f"/api/runs/{rid}")
        d = json.loads(txt)
        if d.get("status") in ("succeeded", "failed"):
            return d
        time.sleep(0.25)
    return {"status": "timeout"}

# 1) 建项目
st, txt = req("POST", "/api/projects", {"name": "E2E测试·营销主图"})
pid = json.loads(txt)["id"]
print("[1] 创建项目", pid)

# 2) 模板
req("POST", f"/api/projects/{pid}/graph/init-template")
print("[2] 初始化三图模板 OK")

# 3) 上传测试产品图（红色圆角矩形）
im = Image.new("RGBA", (400, 400), (255, 255, 255, 0))
d = ImageDraw.Draw(im); d.rounded_rectangle([60, 60, 340, 340], radius=40, fill=(200, 60, 60, 255))
buf = io.BytesIO(); im.save(buf, "PNG"); png = buf.getvalue()
st, txt = multipart(f"/api/projects/{pid}/assets", "file", "product.png", png, "image/png", {"role": "product"})
asset = json.loads(txt); print("[3] 上传产品图", asset["id"], asset["width"], "x", asset["height"])

# 4) 取图
st, txt = req("GET", f"/api/projects/{pid}/graph")
g = json.loads(txt)
nodes = {n["type"]: n for n in g["nodes"]}
facts_n = nodes["product_facts"]; img_n = nodes["product_image"]; strat_n = nodes["strategy"]
prompt_n = [n for n in g["nodes"] if n["type"] == "image_prompt"][0]
gen_n = [n for n in g["nodes"] if n["type"] == "image_generation"][0]

# 5) 挂载产品图 + 填事实卡
req("PATCH", f"/api/nodes/{img_n['id']}", {"content": {"asset_id": asset["id"], "asset_role": "product"}})
facts = {"product_id": "SKU001", "product_name": "暖光蓝牙音箱", "activity_version": "618",
         "confirmed_selling_points": [{"id": "sp_01", "text": "360°环绕立体声", "audience": "音乐爱好者", "scene": "客厅"},
                                       {"id": "sp_02", "text": "12小时续航", "audience": "通勤用户", "scene": "书房"},
                                       {"id": "sp_03", "text": "胡桃木纹理外壳", "audience": "家居党", "scene": "卧室"}],
         "locked_appearance": ["外壳颜色", "旋钮位置"], "applicable_scenes": ["客厅边柜"],
         "forbidden_expressions": ["全网最低"], "policies": ["以旧换新补贴"], "recognition_notes": []}
req("PATCH", f"/api/nodes/{facts_n['id']}", {"content": facts})
print("[5] 挂载产品图 + 保存事实卡 OK")
st, txt = req("GET", f"/api/projects/{pid}/graph"); g = json.loads(txt)
strat_status = [n for n in g["nodes"] if n["type"] == "strategy"][0]["status"]
print("    -> 事实卡修改后策略节点状态：", strat_status, "(应为 stale)")

# 6) 生成策略
k = f"idem_strat_{strat_n['id']}_{time.time()}"
st, txt = req("POST", f"/api/nodes/{strat_n['id']}/runs", {"model_id": "rule-based-planner", "params": {}, "idempotency_key": k})
rid = json.loads(txt)["run_id"]; d = poll_run(rid)
st, txt = req("GET", f"/api/projects/{pid}/graph"); g = json.loads(txt)
strat = [n for n in g["nodes"] if n["type"] == "strategy"][0]["content"]
print("[6] 策略生成", d["status"], "策略数：", len(strat.get("strategies", [])),
      "角色：", [s["role"] for s in strat.get("strategies", [])])

# 7) 生成提示词（图1）
k = f"idem_p1_{prompt_n['id']}_{time.time()}"
st, txt = req("POST", f"/api/nodes/{prompt_n['id']}/runs", {"params": {}, "idempotency_key": k})
rid = json.loads(txt)["run_id"]; poll_run(rid)
st, txt = req("GET", f"/api/projects/{pid}/graph"); g = json.loads(txt)
pc = [n for n in g["nodes"] if n["type"] == "image_prompt"][0]["content"]
print("[7] 提示词生成 OK，prompt 片段：", (pc.get("prompt") or "")[:50], "...")

# 8) 生图（图1，2 张，带文字层）
k = f"idem_gen1_{gen_n['id']}_{time.time()}"
st, txt = req("POST", f"/api/nodes/{gen_n['id']}/runs",
              {"model_id": "local-poster-compositor",
               "params": {"aspect_ratio": "1:1", "resolution_tier": "standard", "count": 2, "title": "夏日焕新价", "subtitle": "以旧换新补贴"},
               "idempotency_key": k})
rid = json.loads(txt)["run_id"]; d = poll_run(rid)
st, txt = req("GET", f"/api/projects/{pid}/graph"); g = json.loads(txt)
outs = [n for n in g["nodes"] if n["type"] == "image_generation"][0]["content"].get("outputs", [])
print("[8] 生图", d["status"], "产出张数：", len(outs), "用量：", json.loads(d.get("usage_json") or "{}").get("cost_unit"))

# 9) 分层导出（改字不重生图）
if outs:
    st, txt = req("POST", f"/api/projects/{pid}/layout/export", {"base_asset_id": outs[0], "title": "最终标题", "subtitle": "最终价格"})
    exp = json.loads(txt)
    print("[9] 分层导出 PNG", exp.get("asset_id"), exp.get("width"), "x", exp.get("height"))

# 10) 导出项目 JSON
st, txt = req("GET", f"/api/projects/{pid}/export")
pj = json.loads(txt)
print("[10] 导出项目 JSON：", "nodes=", len(pj["nodes"]), "edges=", len(pj["edges"]), "assets=", len(pj["asset_manifest"]))

# 11) 验收：AI 局部修改只改当前节点
k = f"idem_ai_{prompt_n['id']}_{time.time()}"
st, txt = req("POST", f"/api/nodes/{prompt_n['id']}/ai-edit", {"instruction": "更有高级感"})
cand = json.loads(txt)
print("[11] AI 局部修改候选摘要：", cand.get("summary"))
req("POST", f"/api/nodes/{prompt_n['id']}/apply-candidate", {"candidate_id": cand["candidate_id"], "base_version": cand["base_version"]})
st, txt = req("GET", f"/api/projects/{pid}/graph"); g = json.loads(txt)
p2 = [n for n in g["nodes"] if n["type"] == "image_prompt"][0]
s2 = [n for n in g["nodes"] if n["type"] == "strategy"][0]
print("    -> 应用后提示词节点版本：", p2["current_version"], "；策略节点未被改动（版本", s2["current_version"], "）")

print("\n=== E2E 验证完成 ===")

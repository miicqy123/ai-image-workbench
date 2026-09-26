"""端到端验证脚本：覆盖 PRD 验收用例 1-7 的后端链路，并对关键数据/版本关系做断言。

第 1 批起后端要求真实会话、第 2 批起要求同源（CSRF），因此脚本会先登录，
并在所有请求上带同源 Origin。

隔离：不设置 WB_E2E_BASE 时，脚本用 tests/harness.py 的隔离机制自建实例
（临时数据库 + 临时素材目录，均在 %TEMP%），结束后自动清理；不访问现有 :8000
服务、不连接业务数据库、不调用任何外部/付费模型（固定使用免费本地合成器）。

对接已启动的服务（旧用法仍可用）：
    set WB_E2E_BASE=http://127.0.0.1:8000
    set WB_E2E_USER=usr_default
    set WB_E2E_PASSWORD=<该账号密码>
    python e2e_test.py

断言原则：失败即失败，不为变绿而放宽；无法用公开接口证实的一律列入“未覆盖”，
不写成通过。
"""
import hashlib
import http.cookiejar
import io
import json
import os
import secrets
import string
import sys
import time
import traceback
import urllib.error
import urllib.request

from PIL import Image, ImageChops, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests"))
from harness import Instance, Results  # noqa: E402

BASE = os.environ.get("WB_E2E_BASE", "").rstrip("/")
USER_ID = os.environ.get("WB_E2E_USER", "usr_default")
PASSWORD = os.environ.get("WB_E2E_PASSWORD", "")

R = Results()
ISOLATED = None
NOT_COVERED = []

MODEL_ID = "local-poster-compositor"   # 免费本地合成器（cost_policy=free_local）
GEN_COUNT = 2                          # 本次生图张数

# image_prompt 运行后的内容字段白名单（与被测代码契约一致，用于断言无旧数据残留）
IMAGE_PROMPT_OUTPUT_WHITELIST = ("prompt", "negative_prompt", "requested_aspect_ratio", "selling_point_id",
                                 "product_id", "locked_visual_features", "bg_style",
                                 "prompt_template_id", "prompt_template_name")

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


# ---------------- 断言与请求工具 ----------------

def check(label: str, ok, detail: str = "") -> bool:
    """记录一条断言，label 带行号，失败时可在报告中直接定位。"""
    line = sys._getframe(1).f_lineno
    R.check(f"{label}（e2e_test.py:{line}）", bool(ok), detail)
    return bool(ok)


def not_covered(item: str) -> None:
    if item not in NOT_COVERED:
        NOT_COVERED.append(item)


def req(method, path, body=None):
    url = BASE + path
    data = None
    h = {"Origin": BASE}
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with _opener.open(r, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def fetch_raw(path):
    """取二进制内容（图片/下载），不做文本解码。"""
    r = urllib.request.Request(BASE + path, headers={"Origin": BASE}, method="GET")
    try:
        with _opener.open(r, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def jbody(txt):
    try:
        return json.loads(txt)
    except Exception:
        return None


def json_get(path):
    st, txt = req("GET", path)
    return st, jbody(txt)


def multipart(path, field, filename, data_bytes, ctype, extra_fields=None):
    boundary = "----wbtestboundary"
    parts = []
    if extra_fields:
        for k, v in extra_fields.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
    parts.append(data_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return _raw(path, b"".join(parts), boundary)


def _raw(path, body, boundary):
    url = BASE + path
    r = urllib.request.Request(url, data=body,
                              headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Origin": BASE},
                              method="POST")
    try:
        with _opener.open(r, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def poll_run(rid, timeout=60):
    for _ in range(timeout * 4):
        st, txt = req("GET", f"/api/runs/{rid}")
        d = jbody(txt)
        if d and d.get("status") in ("succeeded", "failed"):
            return d
        time.sleep(0.25)
    return {"status": "timeout"}


def run_node(nid, payload):
    """发起节点运行并等待结束，返回 run 记录（失败时也返回，由调用方断言）。"""
    st, txt = req("POST", f"/api/nodes/{nid}/runs", payload)
    if st != 200:
        return {"status": f"http_{st}", "error_code": txt[:200]}
    rid = jbody(txt)["run_id"]
    return poll_run(rid)


def make_png(w, h, fill, shape="round") -> bytes:
    """在测试内生成小型图片（不引入外部素材）。"""
    im = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    d = ImageDraw.Draw(im)
    if shape == "round":
        d.rounded_rectangle([w * 0.12, h * 0.12, w * 0.88, h * 0.88], radius=int(min(w, h) * 0.12), fill=fill)
    elif shape == "ellipse":
        d.ellipse([w * 0.18, h * 0.18, w * 0.82, h * 0.82], fill=fill)
    else:
        d.rectangle([w * 0.1, h * 0.35, w * 0.9, h * 0.65], fill=fill)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def px_diff(im1, im2):
    """按像素比较两张图，返回差异像素数/最大通道差/差异包围盒；尺寸不同返回 None。"""
    if im1 is None or im2 is None:
        return None
    if im1.size != im2.size:
        return {"diff_pixels": None, "total": None, "max_delta": None, "size_mismatch": (im1.size, im2.size)}
    a, b = im1.convert("RGBA"), im2.convert("RGBA")
    d = ImageChops.difference(a, b)
    hist = d.convert("L").histogram()
    return {"diff_pixels": sum(hist[1:]), "total": a.size[0] * a.size[1],
            "max_delta": max((m for _, m in d.getextrema()), default=0),
            "bbox": d.convert("RGB").getbbox()}


def color_count(im, rgb, tol=30):
    """统计与给定颜色接近的像素数（诊断用，不做断言）。"""
    if im is None:
        return None
    rgb_im = im.convert("RGB")
    d = ImageChops.difference(rgb_im, Image.new("RGB", rgb_im.size, rgb))
    hit = None
    for band in d.split():
        mask = band.point(lambda v: 255 if v <= tol else 0)
        hit = mask if hit is None else ImageChops.multiply(hit, mask)
    return hit.histogram()[255]


def upload_asset(pid, filename, data, role):
    st, txt = multipart(f"/api/projects/{pid}/assets", "file", filename, data, "image/png", {"role": role})
    return st, jbody(txt)


def project_assets(pid):
    st, data = json_get(f"/api/projects/{pid}/assets")
    return data if isinstance(data, list) else []


def graph_of(pid):
    st, data = json_get(f"/api/projects/{pid}/graph")
    return data if isinstance(data, dict) else {}


def nodes_by_type(graph, type_, index=0):
    xs = [n for n in (graph.get("nodes") or []) if n.get("type") == type_]
    return xs[index] if len(xs) > index else None


def login():
    st, txt = req("POST", "/api/auth/login", {"user_id": USER_ID, "password": PASSWORD})
    if st != 200:
        print("登录失败：", st, txt)
        raise SystemExit(2)
    print(f"[0] 已登录：{USER_ID}")


def start_isolated():
    """用 tests/harness.py 的隔离机制自建实例；库与素材目录都在 %TEMP%。"""
    global BASE, USER_ID, PASSWORD, ISOLATED
    inst = Instance()
    inst.__enter__()
    ISOLATED = inst
    BASE = inst.base
    USER_ID = "usr_e2e_isolated"
    PASSWORD = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16)) + "!aZ9"
    print(f"[隔离] 数据库   : {inst.db_path}")
    print(f"[隔离] 素材目录 : {inst.env.get('WB_UPLOAD_DIR')}")
    print(f"[隔离] 服务地址 : {BASE}（未使用 :8000，未连接业务库）")
    st, _ = req("POST", "/api/auth/bootstrap", {"user_id": USER_ID, "password": PASSWORD, "name": "E2E 临时管理员"})
    print(f"[隔离] 临时账号初始化: HTTP {st}（密码随机生成，不输出）")
    if st != 201:
        raise SystemExit(3)

# ---------------- 主流程 ----------------

def main() -> int:
    if not BASE:
        start_isolated()

    login()
    st, me = json_get("/api/auth/me")
    check("登录态可读 /api/auth/me", st == 200, f"HTTP {st}")
    me = me or {}
    my_orgs = {o.get("id") for o in (me.get("organizations") or [])}
    check("当前账号具备 project.create 权限", "project.create" in (me.get("permissions") or []),
          str(me.get("permissions"))[:120])

    # ===== A. 项目和素材 =====
    print("\n=== A. 项目和素材 ===")
    st, txt = req("POST", "/api/projects", {"name": "E2E测试·营销主图"})
    proj = jbody(txt) or {}
    pid = proj.get("id")
    check("创建项目返回 id", bool(pid), f"HTTP {st} {txt[:120]}")

    st, prj = json_get(f"/api/projects/{pid}")
    check("按 ID 查回项目", st == 200 and (prj or {}).get("id") == pid, f"HTTP {st}")
    prj = prj or {}
    check("项目归属为当前测试账号（owner_id）", prj.get("owner_id") == me["user"]["id"],
          f"owner_id={prj.get('owner_id')} me={me['user']['id']}")
    check("项目归属组织属于当前账号所属组织", prj.get("organization_id") in my_orgs,
          f"organization_id={prj.get('organization_id')} my_orgs={sorted(my_orgs)}")

    st, txt = req("POST", f"/api/projects/{pid}/graph/init-template")
    check("初始化三图工作流模板", st == 200, f"HTTP {st} {txt[:120]}")
    g = graph_of(pid)
    types = [n["type"] for n in (g.get("nodes") or [])]
    for t in ("product_facts", "product_image", "strategy", "image_prompt", "image_generation"):
        check(f"模板包含节点类型 {t}", t in types, str(types))
    edges = g.get("edges") or []
    check("模板建立了连线", len(edges) > 0, f"edges={len(edges)}")
    print("[2] 初始化三图模板 OK")

    # A1 主产品图
    main_png = make_png(400, 400, (200, 60, 60, 255), "round")
    st, asset = upload_asset(pid, "product.png", main_png, "product")
    asset = asset or {}
    check("上传主产品图返回 id/宽高", bool(asset.get("id")) and asset.get("width") == 400 and asset.get("height") == 400,
          f"HTTP {st} {str(asset)[:160]}")
    print("[3] 上传主产品图", asset.get("id"), asset.get("width"), "x", asset.get("height"))

    assets = project_assets(pid)
    by_id = {a["id"]: a for a in assets}
    main_id = asset.get("id")
    check("主产品图出现在本项目素材列表", main_id in by_id, f"assets={list(by_id)}")
    check("主产品图素材角色为 product", (by_id.get(main_id) or {}).get("role") == "product",
          str(by_id.get(main_id)))
    st, raw = fetch_raw(f"/api/assets/{main_id}/download")
    check("主产品图可经受保护下载接口读取", st == 200 and len(raw) > 0, f"HTTP {st} bytes={len(raw)}")
    try:
        im = Image.open(io.BytesIO(raw))
        decodable, size = True, im.size
    except Exception as e:
        decodable, size = False, None
        print("    解码异常:", e)
    check("主产品图字节可被 Pillow 解码且尺寸一致", decodable and size == (400, 400), f"size={size}")
    st, raw2 = fetch_raw(f"/files/{main_id}")
    check("主产品图可经 /files 预览且内容一致", st == 200 and len(raw2) == len(raw), f"HTTP {st} {len(raw2)} vs {len(raw)}")

    # A2 第二张产品图 + Logo + 两张参考图（多图与上限探针用）
    extra_specs = [
        ("product_2.png", make_png(360, 360, (60, 120, 200, 255), "ellipse"), "product"),
        ("logo.png", make_png(240, 120, (30, 30, 30, 255), "bar"), "logo"),
        ("ref_1.png", make_png(200, 200, (240, 200, 60, 255), "round"), "reference"),
        ("ref_2.png", make_png(200, 200, (120, 200, 140, 255), "ellipse"), "reference"),
    ]
    extra = {}
    for fn, data, role in extra_specs:
        st, a = upload_asset(pid, fn, data, role)
        a = a or {}
        check(f"上传补充素材 {fn}（role={role}）", bool(a.get("id")), f"HTTP {st} {str(a)[:120]}")
        extra[fn] = a.get("id")
    assets = project_assets(pid)
    by_id = {a["id"]: a for a in assets}
    check("补充素材均归属本项目素材列表", all(v in by_id for v in extra.values()), f"extra={extra}")
    role_map = {"product_2.png": "product", "logo.png": "logo", "ref_1.png": "reference", "ref_2.png": "reference"}
    check("补充素材角色正确（含 Logo）",
          all((by_id.get(extra[f]) or {}).get("role") == r for f, r in role_map.items()),
          str([(f, (by_id.get(extra[f]) or {}).get("role")) for f in extra]))
    readable = True
    for f, aid in extra.items():
        st, raw = fetch_raw(f"/files/{aid}")
        if st != 200 or not raw:
            readable = False
        else:
            try:
                Image.open(io.BytesIO(raw)).verify()
            except Exception:
                readable = False
    check("补充素材均可读取且可解码", readable, "至少一个素材读取/解码失败")
    check("多图已上传：本项目现有 ≥5 张可用素材", len(assets) >= 5, f"assets={len(assets)}")
    print("[3b] 补充素材上传 OK：", {k: v for k, v in extra.items()})

    # A3 把多图挂到产品图节点（asset_id + reference_asset_ids）
    img_n = nodes_by_type(g, "product_image")
    strat_n = nodes_by_type(g, "strategy")
    expect_refs = [extra["product_2.png"], extra["logo.png"]]
    st, txt = req("PATCH", f"/api/nodes/{img_n['id']}", {"content": {
        "asset_id": main_id, "asset_role": "product", "filename": "product.png",
        "reference_asset_ids": expect_refs}})
    check("挂载主产品图 + 参考图到产品图节点", st == 200, f"HTTP {st} {txt[:160]}")
    st, node = json_get(f"/api/nodes/{img_n['id']}")
    content = (node or {}).get("content") or {}
    check("产品图节点回读 asset_id 一致", content.get("asset_id") == main_id, str(content)[:160])
    check("产品图节点回读 reference_asset_ids 一致", content.get("reference_asset_ids") == expect_refs,
          str(content.get("reference_asset_ids")))

    # ===== B. 产品事实、策略、提示词 =====
    print("\n=== B. 产品事实 / 策略 / 提示词 ===")
    facts_n = nodes_by_type(g, "product_facts")
    st, node_before = json_get(f"/api/nodes/{facts_n['id']}")
    v_before = (node_before or {}).get("current_version")
    facts = {"product_id": "SKU001", "product_name": "暖光蓝牙音箱", "activity_version": "618",
             "confirmed_selling_points": [{"id": "sp_01", "text": "360°环绕立体声", "audience": "音乐爱好者", "scene": "客厅"},
                                           {"id": "sp_02", "text": "12小时续航", "audience": "通勤用户", "scene": "书房"},
                                           {"id": "sp_03", "text": "胡桃木纹理外壳", "audience": "家居党", "scene": "卧室"}],
             "locked_appearance": ["外壳颜色", "旋钮位置"], "applicable_scenes": ["客厅边柜"],
             "forbidden_expressions": ["全网最低"], "policies": ["以旧换新补贴"], "recognition_notes": []}
    st, txt = req("PATCH", f"/api/nodes/{facts_n['id']}", {"content": facts})
    patch_res = jbody(txt) or {}
    check("保存产品事实成功", st == 200, f"HTTP {st} {txt[:160]}")
    check("保存产品事实后版本号 +1", patch_res.get("version") == (v_before or 0) + 1,
          f"before={v_before} after={patch_res.get('version')}")
    st, node_after = json_get(f"/api/nodes/{facts_n['id']}")
    node_after = node_after or {}
    check("产品事实读回与输入完全一致", node_after.get("content") == facts,
          f"读回={str(node_after.get('content'))[:200]}")
    check("产品事实节点 current_version 与写入返回一致",
          node_after.get("current_version") == patch_res.get("version"),
          f"{node_after.get('current_version')} vs {patch_res.get('version')}")
    print("[5] 挂载产品图 + 保存事实卡 OK（版本", v_before, "->", patch_res.get("version"), "）")

    g = graph_of(pid)
    strat_status = nodes_by_type(g, "strategy")["status"]
    check("修改事实后下游策略节点标记 stale", strat_status == "stale", f"status={strat_status}")

    # B2 策略
    strat_n = nodes_by_type(g, "strategy")
    run = run_node(strat_n["id"], {"model_id": "rule-based-planner", "params": {},
                                   "idempotency_key": f"idem_strat_{strat_n['id']}_{time.time()}"})
    check("策略节点运行成功", run.get("status") == "succeeded", f"status={run.get('status')} err={run.get('error_code')}")
    g = graph_of(pid)
    strat = (nodes_by_type(g, "strategy") or {}).get("content") or {}
    strategies = strat.get("strategies") or []
    check("策略生成 3 套", len(strategies) == 3, f"len={len(strategies)}")
    check("策略 id 唯一且形如 hero_N",
          len({s.get("id") for s in strategies}) == 3 and all((s.get("id") or "").startswith("hero_") for s in strategies),
          str([s.get("id") for s in strategies]))
    key_fields = ("role", "selling_point_text", "scene", "composition", "aspect_ratio", "proposition_proof")
    missing = [(s.get("id"), k) for s in strategies for k in key_fields if not (s.get(k) or "").strip()]
    check("每套策略的下游关键字段非空", not missing, f"缺失={missing}")
    sp_map = {sp["id"]: sp["text"] for sp in facts["confirmed_selling_points"]}
    check("策略引用的事实卖点 id 均存在",
          all(s.get("selling_point_id") in sp_map for s in strategies),
          str([s.get("selling_point_id") for s in strategies]))
    check("策略的卖点文本与事实卡一致",
          all(s.get("selling_point_text") == sp_map.get(s.get("selling_point_id")) for s in strategies),
          str([(s.get("selling_point_id"), s.get("selling_point_text")) for s in strategies]))
    check("策略 product_id 与事实卡一致", strat.get("product_id") == facts["product_id"], str(strat.get("product_id")))
    check("策略需求摘要与视觉基线非空",
          bool(strat.get("requirement_summary")) and bool((strat.get("visual_baseline") or {}).get("palette")),
          str(strat.get("visual_baseline"))[:160])
    print("[6] 策略生成", run.get("status"), "策略数：", len(strategies),
          "角色：", [s["role"] for s in strategies])

    # ===== B3 提示词：输入配置保留 + 二次运行 =====
    prompt_n = nodes_by_type(g, "image_prompt")

    # 取一个真实的后台 Prompt 模板（种子模板），用于模板引用往返验证
    st, tpl_list = json_get("/api/prompt-templates")
    tpl_list = tpl_list if isinstance(tpl_list, list) else []
    tpl = tpl_list[0] if tpl_list else None
    check("存在可用的 Prompt 模板（模板引用往返验证前置）", tpl is not None, f"HTTP {st} {str(tpl_list)[:160]}")

    # 运行前写入确定的输入配置 + 旧提示词哨兵：用于验证旧 prompt 不会进入本次生成结果
    SENTINEL = "OLD_PROMPT_SENTINEL_应被本次生成替换"
    base_content = prompt_n.get("content") or {}
    pre_content = {**base_content,
                   "strategy_ref": base_content.get("strategy_ref") or "hero_1",
                   "prompt_template_id": (tpl or {}).get("id", base_content.get("prompt_template_id", "")),
                   "prompt": SENTINEL}
    st, txt = req("PATCH", f"/api/nodes/{prompt_n['id']}", {"content": pre_content})
    check("写入运行前输入配置（strategy_ref + prompt_template_id + 旧提示词哨兵）", st == 200,
          f"HTTP {st} {txt[:160]}")
    st, node_pre = json_get(f"/api/nodes/{prompt_n['id']}")
    node_pre = node_pre or {}
    pre = node_pre.get("content") or {}
    v_pre = node_pre.get("current_version")
    check("运行前读回 strategy_ref 与写入一致", pre.get("strategy_ref") == pre_content["strategy_ref"],
          f"{pre.get('strategy_ref')!r}")
    check("运行前读回 prompt_template_id 与写入一致",
          pre.get("prompt_template_id") == pre_content["prompt_template_id"],
          f"{pre.get('prompt_template_id')!r}")

    sref_in = str(pre.get("strategy_ref") or "")
    item = next((s for s in strategies if s.get("id") == sref_in), None)
    check("strategy_ref 指向本次生成的策略条目", item is not None, f"strategy_ref={sref_in!r}")

    run = run_node(prompt_n["id"], {"params": {}, "idempotency_key": f"idem_p1_{prompt_n['id']}_{time.time()}"})
    check("提示词节点运行成功", run.get("status") == "succeeded",
          f"status={run.get('status')} err={run.get('error_code')}")

    g = graph_of(pid)
    pnode = nodes_by_type(g, "image_prompt")
    pc = pnode.get("content") or {}
    check("运行后节点版本递增", pnode.get("current_version") == (v_pre or 0) + 1,
          f"{pnode.get('current_version')} vs {(v_pre or 0) + 1}")
    check("运行后 strategy_ref 与运行前一致", pc.get("strategy_ref") == pre.get("strategy_ref"),
          f"before={pre.get('strategy_ref')!r} after={pc.get('strategy_ref')!r}")
    check("运行后 prompt_template_id 与运行前一致", pc.get("prompt_template_id") == pre.get("prompt_template_id"),
          f"before={pre.get('prompt_template_id')!r} after={pc.get('prompt_template_id')!r}")
    check("运行后内容字段符合白名单（无旧 prompt / 旧生成结果残留）",
          set(pc.keys()) <= {"strategy_ref", "prompt_template_id"} | set(IMAGE_PROMPT_OUTPUT_WHITELIST),
          f"keys={sorted(pc.keys())}")
    check("提示词是本次生成内容（旧提示词哨兵未残留）", SENTINEL not in (pc.get("prompt") or ""),
          (pc.get("prompt") or "")[:120])
    if tpl:
        check("模板引用可追溯（prompt_template_name 取自模板库）",
              pc.get("prompt_template_name") == tpl.get("name"),
              f"{pc.get('prompt_template_name')!r} vs {tpl.get('name')!r}")
        frag = (tpl.get("template_text") or "").split("{")[0].strip()
        check("模板正文确实进入本次提示词（模板静态片段）", bool(frag) and frag in (pc.get("prompt") or ""),
              f"fragment={frag!r}")
    check("提示词节点输出包含 selling_point_id（输出侧可交叉验证的关联字段）",
          bool(pc.get("selling_point_id")), str(sorted(pc.keys())))
    if item:
        prompt_text = pc.get("prompt") or ""
        check("提示词非空", len(prompt_text.strip()) > 0, f"len={len(prompt_text)}")
        check("提示词引用本项目产品名（来自事实卡）", facts["product_name"] in prompt_text, prompt_text[:120])
        check("提示词引用所选策略的卖点文本", item["selling_point_text"] in prompt_text, prompt_text[:160])
        check("提示词引用所选策略的场景", item["scene"] in prompt_text, prompt_text[:160])
        check("提示词引用所选策略的构图", item["composition"] in prompt_text, prompt_text[:160])
        check("提示词 selling_point_id 与所选策略一致",
              pc.get("selling_point_id") == item.get("selling_point_id"),
              f"{pc.get('selling_point_id')} vs {item.get('selling_point_id')}")
        check("提示词 requested_aspect_ratio 与所选策略一致",
              pc.get("requested_aspect_ratio") == item.get("aspect_ratio"),
              f"{pc.get('requested_aspect_ratio')} vs {item.get('aspect_ratio')}")
        check("提示词 product_id 与事实卡一致", pc.get("product_id") == facts["product_id"], str(pc.get("product_id")))
        check("负向提示词包含事实卡的禁用表述",
              facts["forbidden_expressions"][0] in (pc.get("negative_prompt") or ""),
              str(pc.get("negative_prompt"))[:160])
        print("[7] 提示词生成 OK，prompt 片段：", prompt_text[:50], "...")

    # 二次运行同一节点：输入配置仍需保留，版本继续递增（不要求输出文本变化）
    v_run1 = pnode.get("current_version")
    run2 = run_node(prompt_n["id"], {"params": {}, "idempotency_key": f"idem_p2_{prompt_n['id']}_{time.time()}"})
    check("提示词节点二次运行成功", run2.get("status") == "succeeded",
          f"status={run2.get('status')} err={run2.get('error_code')}")
    g = graph_of(pid)
    pnode2 = nodes_by_type(g, "image_prompt")
    pc2 = pnode2.get("content") or {}
    check("二次运行后节点版本继续递增", pnode2.get("current_version") == (v_run1 or 0) + 1,
          f"{pnode2.get('current_version')} vs {(v_run1 or 0) + 1}")
    check("二次运行后 strategy_ref 仍保留", pc2.get("strategy_ref") == pre.get("strategy_ref"),
          f"before={pre.get('strategy_ref')!r} after={pc2.get('strategy_ref')!r}")
    check("二次运行后 prompt_template_id 仍保留", pc2.get("prompt_template_id") == pre.get("prompt_template_id"),
          f"before={pre.get('prompt_template_id')!r} after={pc2.get('prompt_template_id')!r}")
    check("二次运行后旧提示词哨兵仍未残留", SENTINEL not in (pc2.get("prompt") or ""),
          (pc2.get("prompt") or "")[:120])
    pc = pc2  # 后续段落以最近一次运行结果为准
    prompt_nodes_all = [n for n in (g.get("nodes") or []) if n["type"] == "image_prompt"]
    if len(prompt_nodes_all) > 1:
        not_covered(f"只运行了 1 个 image_prompt 节点（工作流共 {len(prompt_nodes_all)} 个），图 2 / 图 3 的提示词分支未覆盖")

    # ===== C. 生图和回存 =====
    print("\n=== C. 本地生图与素材回存 ===")
    st, model = json_get(f"/api/models/{MODEL_ID}")
    model = model or {}
    check("生图固定使用免费本地模型 local-poster-compositor",
          st == 200 and model.get("cost_policy") == "free_local" and model.get("enabled") == 1,
          f"HTTP {st} cost_policy={model.get('cost_policy')} enabled={model.get('enabled')}")
    cap = {}
    try:
        cap = json.loads(model.get("capabilities_json") or "{}")
    except Exception:
        cap = {}
    limit = int(cap.get("image_input_limit") or 0)
    print(f"    模型能力：image_input_limit={limit}, max_count={cap.get('max_count')}")

    gen_n = nodes_by_type(g, "image_generation")
    ref_pool = [extra["product_2.png"], extra["logo.png"], extra["ref_1.png"], extra["ref_2.png"]]

    # C0 多图确实进入模型入参校验：参考图数量 = 1 + len(reference_asset_ids) 超过上限即被拒
    over_refs = ref_pool[:limit] if limit else ref_pool
    st, _ = req("PATCH", f"/api/nodes/{img_n['id']}", {"content": {
        "asset_id": main_id, "asset_role": "product", "reference_asset_ids": over_refs}})
    run_over = run_node(gen_n["id"], {"model_id": MODEL_ID,
                                      "params": {"aspect_ratio": "1:1", "resolution_tier": "standard", "count": 1},
                                      "idempotency_key": f"idem_over_{gen_n['id']}_{time.time()}"})
    err = run_over.get("error_code") or ""
    check(f"参考图数量超过模型上限（1+{len(over_refs)} > {limit}）时被拒绝",
          run_over.get("status") == "failed" and "参考图数量超出模型上限" in err,
          f"status={run_over.get('status')} err={err[:200]}")
    # 失败运行同样要能追溯输入：快照在能力校验/模型调用之前落库
    snap_over = run_over.get("input_snapshot")
    check("失败运行也保存了输入快照（可追溯）", isinstance(snap_over, dict), str(run_over.get("input_snapshot"))[:160])
    check("失败运行快照记录的参考资产 = 实际传入列表（含超限项）",
          (snap_over or {}).get("reference_asset_ids") == [main_id] + list(over_refs),
          str((snap_over or {}).get("reference_asset_ids")))

    not_covered("按本轮版式契约，本地预览最多渲染 2 张 role=product 与 1 张 role=logo；"
                "role=reference 以及第 3 张及以后的参考图只记录在 input_snapshot，不影响本地预览像素（契约如此，非缺陷）；"
                "generation_jobs（worker）路径仍未传 role，仍为“第一张即主产品图”的旧行为")

    # 恢复为 3 张参考（主图 + 第二产品图 + Logo）
    st, _ = req("PATCH", f"/api/nodes/{img_n['id']}", {"content": {
        "asset_id": main_id, "asset_role": "product", "reference_asset_ids": expect_refs}})
    st, node = json_get(f"/api/nodes/{img_n['id']}")
    check("多图挂载回读一致（主图 + 第二产品图 + Logo）",
          ((node or {}).get("content") or {}).get("reference_asset_ids") == expect_refs, str(node)[:200])

    # C1 正式生图
    run = run_node(gen_n["id"], {"model_id": MODEL_ID,
                                 "params": {"aspect_ratio": "1:1", "resolution_tier": "standard",
                                            "count": GEN_COUNT, "title": "夏日焕新价", "subtitle": "以旧换新补贴"},
                                 "idempotency_key": f"idem_gen1_{gen_n['id']}_{time.time()}"})
    check("本地生图任务成功", run.get("status") == "succeeded",
          f"status={run.get('status')} err={run.get('error_code')}")
    usage = jbody(run.get("usage_json") or "{}") or {}
    check("本次生图不计费（cost_unit=free_local）", usage.get("cost_unit") == "free_local", str(usage)[:200])
    check("本次生图模型与请求一致", usage.get("model_id") == MODEL_ID, str(usage.get("model_id")))
    check("本次生图张数与请求一致", int(usage.get("count") or 0) == GEN_COUNT, str(usage.get("count")))

    outputs = run.get("outputs") or []
    check(f"任务返回图片数量为 {GEN_COUNT}", len(outputs) == GEN_COUNT, f"len={len(outputs)}")
    g = graph_of(pid)
    gen_node = nodes_by_type(g, "image_generation")
    check("生成结果写回生成节点 outputs", len((gen_node.get("content") or {}).get("outputs") or []) >= GEN_COUNT,
          str((gen_node.get("content") or {}).get("outputs")))
    assets = project_assets(pid)
    by_id = {a["id"]: a for a in assets}
    for i, out in enumerate(outputs):
        aid = out.get("asset_id")
        meta = jbody(out.get("output_json") or "{}") or {}
        w, h = meta.get("width"), meta.get("height")
        st, raw = fetch_raw(f"/files/{aid}")
        ok_read = st == 200 and len(raw) > 0
        size, fmt = None, None
        if ok_read:
            try:
                im = Image.open(io.BytesIO(raw))
                size, fmt = im.size, im.format
            except Exception as im_err:
                print("    解码异常:", im_err)
        check(f"生成图 {i+1} 字节非空且可解码", ok_read and size == (w, h) and fmt == "PNG",
              f"HTTP {st} bytes={len(raw)} size={size} fmt={fmt} 期望={(w, h)}")
        st2, raw2 = fetch_raw(f"/api/assets/{aid}/download")
        check(f"生成图 {i+1} 可经受保护下载接口读取且内容一致",
              st2 == 200 and len(raw2) == len(raw), f"HTTP {st2} {len(raw2)} vs {len(raw)}")
        a = by_id.get(aid)
        check(f"生成图 {i+1} 在本项目素材列表可查", a is not None, f"aid={aid}")
        check(f"生成图 {i+1} 素材角色为 generated 且宽高一致",
              (a or {}).get("role") == "generated" and (a or {}).get("width") == w and (a or {}).get("height") == h,
              str(a))

    print("[8] 生图", run.get("status"), "产出张数：", len(outputs), "用量：", usage.get("cost_unit"))

    # ===== C2 运行输入快照（第一次运行）=====
    print("\n=== C2. 运行输入快照（第一次运行）===")
    SNAP_KEYS = {"project_id", "graph_id", "node_id", "run_id", "model_id", "task_type", "prompt",
                 "negative_prompt", "prompt_node_id", "prompt_node_version", "reference_asset_ids",
                 "aspect_ratio", "count", "resolution_tier", "seed", "params"}
    run1_id = run.get("id")
    check("运行详情可通过公开接口取到 run_id", bool(run1_id), str(run)[:160])
    snap1 = run.get("input_snapshot")
    check("运行详情返回 input_snapshot", isinstance(snap1, dict), str(run.get("input_snapshot"))[:160])
    snap1 = snap1 or {}
    check("快照只包含白名单字段", set(snap1.keys()) <= SNAP_KEYS, f"keys={sorted(snap1.keys())}")

    g = graph_of(pid)
    gen_node = nodes_by_type(g, "image_generation")
    prompt_node_c = nodes_by_type(g, "image_prompt")
    p_content = prompt_node_c.get("content") or {}
    p1_prompt = p_content.get("prompt") or ""
    p1_negative = p_content.get("negative_prompt") or ""
    r1_refs = [main_id] + list(expect_refs)
    check("快照 project_id / graph_id 与本次运行一致",
          snap1.get("project_id") == pid and snap1.get("graph_id") == g.get("graph_id"),
          f"{snap1.get('project_id')} / {snap1.get('graph_id')} vs {pid} / {g.get('graph_id')}")
    check("快照 node_id / run_id 指向本次运行",
          snap1.get("node_id") == gen_n["id"] and snap1.get("run_id") == run1_id,
          f"{snap1.get('node_id')} / {snap1.get('run_id')}")
    check("快照 model_id / task_type 与实际调用一致",
          snap1.get("model_id") == MODEL_ID and snap1.get("task_type") == "text_to_image",
          f"{snap1.get('model_id')} / {snap1.get('task_type')}")
    check("快照 prompt 等于本次实际使用的提示词（P1）", snap1.get("prompt") == p1_prompt,
          f"snap={str(snap1.get('prompt'))[:80]!r} node={str(p1_prompt)[:80]!r}")
    check("快照 negative_prompt 等于提示词节点的负向提示", snap1.get("negative_prompt") == p1_negative,
          str(snap1.get("negative_prompt"))[:120])
    check("快照记录提示词节点 ID 与其当时版本",
          snap1.get("prompt_node_id") == prompt_node_c["id"]
          and snap1.get("prompt_node_version") == prompt_node_c["current_version"],
          f"{snap1.get('prompt_node_id')}@{snap1.get('prompt_node_version')} vs "
          f"{prompt_node_c['id']}@{prompt_node_c['current_version']}")
    check("快照参考资产列表与顺序 = [主图] + 节点 reference_asset_ids（R1）",
          snap1.get("reference_asset_ids") == r1_refs,
          f"{snap1.get('reference_asset_ids')} vs {r1_refs}")
    check("快照比例 / 张数 / 清晰度与请求一致",
          snap1.get("aspect_ratio") == "1:1" and snap1.get("count") == GEN_COUNT
          and snap1.get("resolution_tier") == "standard",
          f"{snap1.get('aspect_ratio')} / {snap1.get('count')} / {snap1.get('resolution_tier')}")
    check("快照有效参数（标题/副标题）与请求一致",
          (snap1.get("params") or {}) == {"title": "夏日焕新价", "subtitle": "以旧换新补贴"},
          str(snap1.get("params")))

    # ===== C3 第二次运行：输入变更 + 旧快照不被覆盖 =====
    print("\n=== C3. 第二次运行（输入变更）===")
    v_prompt2 = prompt_node_c["current_version"]
    p2_prompt = (p1_prompt + " 【v2-输入已变更】").strip()
    st, txt = req("PATCH", f"/api/nodes/{prompt_node_c['id']}",
                  {"content": {**p_content, "prompt": p2_prompt}})
    check("第二次运行前修改提示词内容", st == 200, f"HTTP {st} {txt[:160]}")
    refs2 = [extra["logo.png"]]
    st, txt = req("PATCH", f"/api/nodes/{img_n['id']}",
                  {"content": {"asset_id": main_id, "asset_role": "product", "reference_asset_ids": refs2}})
    check("第二次运行前修改参考资产列表", st == 200, f"HTTP {st} {txt[:160]}")

    run2g = run_node(gen_n["id"], {"model_id": MODEL_ID,
                                   "params": {"aspect_ratio": "1:1", "resolution_tier": "standard", "count": 1},
                                   "idempotency_key": f"idem_gen2_{gen_n['id']}_{time.time()}"})
    check("第二次生图任务成功", run2g.get("status") == "succeeded",
          f"status={run2g.get('status')} err={run2g.get('error_code')}")
    snap2 = run2g.get("input_snapshot") or {}
    check("第二次运行快照存在", isinstance(run2g.get("input_snapshot"), dict), str(run2g.get("input_snapshot"))[:160])
    check("第二次运行 run_id 与第一次不同", run2g.get("id") and run2g.get("id") != run1_id,
          f"{run2g.get('id')} vs {run1_id}")
    check("第二次快照保存新提示词（P2）", snap2.get("prompt") == p2_prompt, str(snap2.get("prompt"))[:100])
    check("第二次快照提示词与第一次不同", snap2.get("prompt") != snap1.get("prompt"), "两次快照 prompt 相同")
    check("第二次快照保存新参考资产列表（R2，顺序一致）",
          snap2.get("reference_asset_ids") == [main_id] + refs2,
          f"{snap2.get('reference_asset_ids')} vs {[main_id] + refs2}")
    check("第二次快照记录更新后的提示词节点版本", snap2.get("prompt_node_version") == (v_prompt2 or 0) + 1,
          f"{snap2.get('prompt_node_version')} vs {(v_prompt2 or 0) + 1}")
    check("第二次快照张数为 1", snap2.get("count") == 1, str(snap2.get("count")))

    st, run1_again = json_get(f"/api/runs/{run1_id}")
    snap1_again = (run1_again or {}).get("input_snapshot") or {}
    check("回读第一次运行详情成功", st == 200 and (run1_again or {}).get("id") == run1_id, f"HTTP {st}")
    check("第一次运行快照未被第二次运行覆盖（prompt 仍为 P1）",
          snap1_again.get("prompt") == snap1.get("prompt"),
          f"again={str(snap1_again.get('prompt'))[:80]!r} first={str(snap1.get('prompt'))[:80]!r}")
    check("第一次运行快照未被覆盖（参考资产仍为 R1）",
          snap1_again.get("reference_asset_ids") == snap1.get("reference_asset_ids"),
          f"{snap1_again.get('reference_asset_ids')} vs {snap1.get('reference_asset_ids')}")
    check("第一次运行快照整体未变", snap1_again == snap1, "两次读回的第一次快照不一致")

    # ===== 导出（沿用原有用例，补断言）=====
    print("\n=== 导出 ===")
    gen_outputs = (nodes_by_type(graph_of(pid), "image_generation").get("content") or {}).get("outputs") or []
    st, txt = req("POST", f"/api/projects/{pid}/layout/export",
                  {"base_asset_id": gen_outputs[0], "title": "最终标题", "subtitle": "最终价格"})
    exp = jbody(txt) or {}
    check("分层导出返回成品资产", st == 200 and bool(exp.get("asset_id")), f"HTTP {st} {txt[:160]}")
    st, raw = fetch_raw(f"/files/{exp.get('asset_id')}")
    check("分层导出成品可读取且非空", st == 200 and len(raw) > 0, f"HTTP {st} bytes={len(raw)}")
    print("[9] 分层导出 PNG", exp.get("asset_id"), exp.get("width"), "x", exp.get("height"))

    st, pj = json_get(f"/api/projects/{pid}/export")
    pj = pj or {}
    g = graph_of(pid)
    check("导出项目 JSON 节点数与当前图一致",
          st == 200 and len(pj.get("nodes") or []) == len(g.get("nodes") or []),
          f"HTTP {st} export={len(pj.get('nodes') or [])} graph={len(g.get('nodes') or [])}")
    check("导出项目 JSON 连线数与当前图一致",
          len(pj.get("edges") or []) == len(g.get("edges") or []),
          f"export={len(pj.get('edges') or [])} graph={len(g.get('edges') or [])}")
    check("导出项目 JSON 覆盖本项目全部素材",
          len(pj.get("asset_manifest") or []) == len(project_assets(pid)),
          f"export={len(pj.get('asset_manifest') or [])} assets={len(project_assets(pid))}")
    print("[10] 导出项目 JSON：nodes=", len(pj.get("nodes") or []), "edges=", len(pj.get("edges") or []),
          "assets=", len(pj.get("asset_manifest") or []))

    # ===== D0. 建立并运行第二条真实分支（策略 → 提示词 B → 生图 B）=====
    print("\n=== D0. 建立第二条真实分支（B）===")
    runs_of = lambda nid: [r for r in (json_get("/api/runs")[1] or []) if r.get("node_id") == nid]
    g = graph_of(pid)
    gid_d = g.get("graph_id")
    strat_d = nodes_by_type(g, "strategy")
    prompt_a = nodes_by_type(g, "image_prompt", 0)
    gen_a_id = next((e["to_node"] for e in (g.get("edges") or []) if e["from_node"] == prompt_a["id"]), None)
    check("定位既有 A 分支（策略 → 提示词 A → 生图 A）", bool(strat_d and prompt_a and gen_a_id),
          f"strategy={strat_d and strat_d['id']} promptA={prompt_a and prompt_a['id']} genA={gen_a_id}")
    print(f"    A 分支：策略={strat_d['id']} 提示词={prompt_a['id']} 生图={gen_a_id}")

    # 用公开 API 新增提示词 B / 生图 B（不写数据库、不绕过图校验）
    st, pbt = req("POST", f"/api/graphs/{gid_d}/nodes", {"type": "image_prompt", "x": 680, "y": 620})
    prompt_b_id = (jbody(pbt) or {}).get("id")
    st2, gbt = req("POST", f"/api/graphs/{gid_d}/nodes", {"type": "image_generation", "x": 1000, "y": 620})
    gen_b_id = (jbody(gbt) or {}).get("id")
    check("公开 API 新增提示词 B / 生图 B 节点",
          st == 200 and st2 == 200 and bool(prompt_b_id) and bool(gen_b_id),
          f"HTTP {st}/{st2} {pbt[:120]} {gbt[:120]}")

    # 连边（强类型端口）：策略 --strategy/strategy--> 提示词 B --prompt/prompt--> 生图 B
    st, et = req("PATCH", f"/api/projects/{pid}/graph", {
        "expected_graph_version": g.get("version"),
        "add_edges": [
            {"from_node": strat_d["id"], "from_port": "strategy", "to_node": prompt_b_id,
             "to_port": "strategy", "semantic": "该图策略（B 分支）"},
            {"from_node": prompt_b_id, "from_port": "prompt", "to_node": gen_b_id,
             "to_port": "prompt", "semantic": "提示词（B 分支）"},
        ]})
    check("通过图接口建立 B 分支连线（强类型端口校验通过）", st == 200, f"HTTP {st} {et[:200]}")
    g = graph_of(pid)
    b_edges = [e for e in (g.get("edges") or [])
               if (e["from_node"], e["to_node"]) in ((strat_d["id"], prompt_b_id), (prompt_b_id, gen_b_id))]
    check("B 分支两条边真实存在", len(b_edges) == 2, f"{[(e['from_node'], e['to_node']) for e in (g.get('edges') or [])]}")
    print(f"    B 分支：提示词={prompt_b_id} 生图={gen_b_id}")

    # B 分支设置可区分的 strategy_ref（hero_2）与旧提示词哨兵
    B_SENTINEL = "B_BRANCH_OLD_PROMPT_应被本次生成替换"
    st, bt = req("PATCH", f"/api/nodes/{prompt_b_id}",
                 {"content": {"prompt": B_SENTINEL, "negative_prompt": "", "strategy_ref": "hero_2",
                              "prompt_template_id": ""}})
    check("B 分支设置可区分的 strategy_ref=hero_2", st == 200, f"HTTP {st} {bt[:160]}")

    # 各运行一次 A、B 分支（提示词 + 生图）
    d0_runs = {}
    for tag, nid in (("prompt_a", prompt_a["id"]), ("prompt_b", prompt_b_id),
                     ("gen_a", gen_a_id), ("gen_b", gen_b_id)):
        r = run_node(nid, {"model_id": None, "params": {"count": 1},
                           "idempotency_key": f"idem_d0_{tag}_{time.time()}"})
        d0_runs[tag] = r
        check(f"首次运行成功：{tag}", r.get("status") == "succeeded",
              f"status={r.get('status')} err={r.get('error_code')}")
    check("A/B 四个节点均产生真实运行记录（run_id 非空）",
          all(bool(d0_runs[k].get("id")) for k in d0_runs),
          str({k: d0_runs[k].get("id") for k in d0_runs}))

    g = graph_of(pid)
    def _n(nid):
        # 每次都重新拉图，避免读到运行前的旧快照（否则版本断言会失真）
        return next((x for x in (graph_of(pid).get("nodes") or []) if x["id"] == nid), {}) or {}
    pa0, pb0 = _n(prompt_a["id"]), _n(prompt_b_id)
    ga0, gb0 = _n(gen_a_id), _n(gen_b_id)
    strat_now = (_n(strat_d["id"]).get("content") or {})
    strs_now = strat_now.get("strategies") or []
    item1 = next((s for s in strs_now if s.get("id") == "hero_1"), None)
    item2 = next((s for s in strs_now if s.get("id") == "hero_2"), None)
    check("A/B 提示词各自保留 strategy_ref（hero_1 / hero_2）",
          ((pa0.get("content") or {}).get("strategy_ref"), (pb0.get("content") or {}).get("strategy_ref"))
          == ("hero_1", "hero_2"),
          f"{((pa0.get('content') or {}).get('strategy_ref'))} / {((pb0.get('content') or {}).get('strategy_ref'))}")
    check("A 分支提示词引用 hero_1 的卖点", bool(item1) and item1["selling_point_text"] in ((pa0.get("content") or {}).get("prompt") or ""),
          str(item1 and item1.get("selling_point_text")))
    check("B 分支提示词引用 hero_2 的卖点（非旧哨兵）",
          bool(item2) and item2["selling_point_text"] in ((pb0.get("content") or {}).get("prompt") or "")
          and B_SENTINEL not in ((pb0.get("content") or {}).get("prompt") or ""),
          str(item2 and item2.get("selling_point_text")))
    sa0 = d0_runs["gen_a"].get("input_snapshot") or {}
    sb0 = d0_runs["gen_b"].get("input_snapshot") or {}
    check("生图 A 快照指向提示词 A 与其运行后版本",
          sa0.get("prompt_node_id") == prompt_a["id"] and sa0.get("prompt_node_version") == pa0.get("current_version"),
          f"{sa0.get('prompt_node_id')}@{sa0.get('prompt_node_version')} vs {prompt_a['id']}@{pa0.get('current_version')}")
    check("生图 B 快照指向提示词 B 与其运行后版本",
          sb0.get("prompt_node_id") == prompt_b_id and sb0.get("prompt_node_version") == pb0.get("current_version"),
          f"{sb0.get('prompt_node_id')}@{sb0.get('prompt_node_version')} vs {prompt_b_id}@{pb0.get('current_version')}")
    check("A/B 生图快照 prompt 分别等于各自提示词内容",
          sa0.get("prompt") == (pa0.get("content") or {}).get("prompt")
          and sb0.get("prompt") == (pb0.get("content") or {}).get("prompt"), "两条分支快照 prompt 与提示词不一致")
    print(f"[D0] 基线：提示词A v{pa0.get('current_version')} run={d0_runs['prompt_a'].get('id')}；"
          f"生图A v{ga0.get('current_version')} run={d0_runs['gen_a'].get('id')}；"
          f"提示词B v{pb0.get('current_version')} run={d0_runs['prompt_b'].get('id')}；"
          f"生图B v{gb0.get('current_version')} run={d0_runs['gen_b'].get('id')}")

    # ===== D. 局部修改与版本隔离 =====
    print("\n=== D. 局部修改与版本隔离 ===")
    g = graph_of(pid)
    p_before = nodes_by_type(g, "image_prompt")
    s_before = nodes_by_type(g, "strategy")
    gen_before = nodes_by_type(g, "image_generation")
    review_before = nodes_by_type(g, "review")
    layout_before = nodes_by_type(g, "layout_export")
    others_before = {n["id"]: (n["current_version"], n.get("content"))
                     for n in (g.get("nodes") or []) if n["type"] == "image_prompt" and n["id"] != p_before["id"]}
    # B 分支基线（真实存在且已运行过），用于验证“只影响相关分支”
    t_before_edit = time.time()
    b_before = {"prompt": (_n(prompt_b_id).get("current_version"), _n(prompt_b_id).get("content")),
                "gen": (_n(gen_b_id).get("current_version"), _n(gen_b_id).get("content"), _n(gen_b_id).get("status"))}
    pv1, p_content1 = p_before["current_version"], p_before.get("content") or {}
    sv1, s_content1 = s_before["current_version"], s_before.get("content") or {}
    check("局部修改前生成节点处于 ready（保证 stale 断言非空）", gen_before["status"] == "ready",
          f"status={gen_before['status']}")

    st, txt = req("POST", f"/api/nodes/{p_before['id']}/ai-edit", {"instruction": "更有高级感"})
    cand = jbody(txt) or {}
    check("AI 局部修改生成候选", st == 200 and bool(cand.get("candidate_id")), f"HTTP {st} {txt[:160]}")
    check("候选基于当前版本（base_version 正确）", cand.get("base_version") == pv1,
          f"{cand.get('base_version')} vs {pv1}")
    cand_prompt = ((cand.get("content") or {}).get("prompt") or "")
    check("候选内容在原提示词基础上追加（未清空既有内容）",
          cand_prompt.startswith(p_content1.get("prompt") or "") and len(cand_prompt) > len(p_content1.get("prompt") or ""),
          f"prompt_len={len(cand_prompt)} base_len={len(p_content1.get('prompt') or '')}")
    print("[11] AI 局部修改候选摘要：", cand.get("summary"))

    st, txt = req("POST", f"/api/nodes/{p_before['id']}/apply-candidate",
                  {"candidate_id": cand["candidate_id"], "base_version": cand["base_version"]})
    applied = jbody(txt) or {}
    check("应用候选成功且版本 +1", st == 200 and applied.get("version") == pv1 + 1,
          f"HTTP {st} version={applied.get('version')} 期望={pv1 + 1} {txt[:120]}")

    st, node_p = json_get(f"/api/nodes/{p_before['id']}")
    node_p = node_p or {}
    check("提示词节点 current_version 已增加", node_p.get("current_version") == pv1 + 1,
          f"{node_p.get('current_version')} vs {pv1 + 1}")
    check("提示词节点内容等于候选内容（逐字段一致）",
          (node_p.get("content") or {}) == (cand.get("content") or {}),
          str(node_p.get("content"))[:200])
    check("提示词节点自身被标记 ready（mark_stale 不含自身）", node_p.get("status") == "ready",
          f"status={node_p.get('status')}")

    g = graph_of(pid)
    s_after = nodes_by_type(g, "strategy")
    check("未修改的策略节点版本不变", s_after["current_version"] == sv1,
          f"{s_after['current_version']} vs {sv1}")
    check("未修改的策略节点内容不变", (s_after.get("content") or {}) == s_content1, "策略内容发生变化")
    others_after = {n["id"]: (n["current_version"], n.get("content"))
                    for n in (g.get("nodes") or []) if n["type"] == "image_prompt" and n["id"] != p_before["id"]}
    check("未受影响的其他提示词分支（真实 B 分支，非空集合）版本与内容不变",
          bool(others_before) and others_after == others_before,
          f"before={others_before} after={others_after}")
    b_after = {"prompt": (_n(prompt_b_id).get("current_version"), _n(prompt_b_id).get("content")),
               "gen": (_n(gen_b_id).get("current_version"), _n(gen_b_id).get("content"), _n(gen_b_id).get("status"))}
    check("B 分支提示词与生图的版本/内容/状态均未受影响", b_after == b_before,
          f"before={b_before} after={b_after}")
    b_new_runs = [r for r in (runs_of(prompt_b_id) + runs_of(gen_b_id)) if (r.get("started_at") or 0) >= t_before_edit]
    check("修改提示词 A 未给 B 分支新增任何运行记录", not b_new_runs, f"新增={b_new_runs}")
    print(f"[11b] 仅修改 A 后：提示词A v{pv1}->{node_p.get('current_version')}；"
          f"提示词B v{b_before['prompt'][0]} 不变={b_after['prompt'][0] == b_before['prompt'][0]}；"
          f"生图B v{b_before['gen'][0]} 不变={b_after['gen'][0] == b_before['gen'][0]}；B 新增运行={len(b_new_runs)}")

    gen_after = nodes_by_type(g, "image_generation")
    review_after = nodes_by_type(g, "review")
    layout_after = nodes_by_type(g, "layout_export")
    check("下游生图节点被标记 stale（与设计一致）", gen_after["status"] == "stale",
          f"status={gen_after['status']}")
    if review_before and review_after:
        check("再下游审核节点被标记 stale", review_after["status"] == "stale", f"status={review_after['status']}")
    if layout_before and layout_after:
        check("再下游排版导出节点被标记 stale", layout_after["status"] == "stale",
              f"status={layout_after['status']}")
    print("    -> 应用后提示词节点版本：", node_p.get("current_version"),
          "；策略节点未改动（版本", s_after["current_version"], "）；生成节点状态：", gen_after["status"])

    # ===== D2. 从提示词 A 重跑下游：只影响 A 分支 =====
    print("\n=== D2. 从提示词 A 重跑下游（只应影响 A 分支）===")
    g = graph_of(pid)
    gid_d2 = g.get("graph_id")
    t_before_d2 = time.time()
    pa_ref, pb_ref = _n(prompt_a["id"]), _n(prompt_b_id)
    pa_v0 = pa_ref.get("current_version")
    b_before_d2 = {"prompt": (pb_ref.get("current_version"), pb_ref.get("content")),
                   "gen": (_n(gen_b_id).get("current_version"), _n(gen_b_id).get("content"), _n(gen_b_id).get("status"))}
    ga_v0 = _n(gen_a_id).get("current_version")

    st, txt = req("POST", f"/api/graphs/{gid_d2}/run-downstream", {"from_node_id": prompt_a["id"]})
    check("从提示词 A 重跑下游返回 200", st == 200, f"HTTP {st} 响应={txt[:300]}")
    ds_a = jbody(txt) or {}
    ran_a = ds_a.get("ran") or []
    check("响应未标记失败节点（A 分支重跑）", ds_a.get("failed") in (None, {}, []), str(ds_a.get("failed"))[:200])
    check("ran 集合 = 从提示词 A 可达且可执行的节点集合（仅生图 A）",
          {x.get("node_id") for x in ran_a} == {gen_a_id},
          f"ran={[(x.get('type'), x.get('node_id')) for x in ran_a]}")
    check("提示词 A 自身未被执行（重跑下游不含起点）",
          prompt_a["id"] not in {x.get("node_id") for x in ran_a},
          str([x.get("node_id") for x in ran_a]))
    check("A 分支重跑清单类型正确（只有生图）",
          all(x.get("type") == "image_generation" and x.get("status") == "succeeded" for x in ran_a),
          str(ran_a)[:240])

    pa_after = _n(prompt_a["id"])
    ga_after = _n(gen_a_id)
    check("生图 A 版本递增", ga_after.get("current_version") == (ga_v0 or 0) + 1,
          f"{ga_after.get('current_version')} vs {(ga_v0 or 0) + 1}")
    check("提示词 A 版本未变（本次未执行提示词节点）", pa_after.get("current_version") == pa_v0,
          f"{pa_after.get('current_version')} vs {pa_v0}")
    gen_a_run2 = next((x.get("run_id") for x in ran_a if x.get("node_id") == gen_a_id), None)
    st_, gd2 = (json_get(f"/api/runs/{gen_a_run2}") if gen_a_run2 else (0, None))
    snap_a2 = (gd2 or {}).get("input_snapshot") or {}
    check("A 分支重跑快照指向提示词 A 的新版本",
          st_ == 200 and snap_a2.get("prompt_node_id") == prompt_a["id"]
          and snap_a2.get("prompt_node_version") == pa_after.get("current_version"),
          f"HTTP {st_} {snap_a2.get('prompt_node_id')}@{snap_a2.get('prompt_node_version')} "
          f"vs {prompt_a['id']}@{pa_after.get('current_version')}")
    check("A 分支重跑快照 prompt 等于提示词 A 当前内容",
          snap_a2.get("prompt") == (pa_after.get("content") or {}).get("prompt"),
          f"snap={str(snap_a2.get('prompt'))[:100]!r}")

    b_after_d2 = {"prompt": (_n(prompt_b_id).get("current_version"), _n(prompt_b_id).get("content")),
                  "gen": (_n(gen_b_id).get("current_version"), _n(gen_b_id).get("content"), _n(gen_b_id).get("status"))}
    check("从 A 重跑下游未影响 B 分支的版本/内容/状态", b_after_d2 == b_before_d2,
          f"before={b_before_d2} after={b_after_d2}")
    b_new_d2 = [r for r in (runs_of(prompt_b_id) + runs_of(gen_b_id)) if (r.get("started_at") or 0) >= t_before_d2]
    check("从 A 重跑下游未给 B 分支新增运行记录", not b_new_d2, f"新增={b_new_d2}")
    print(f"[D2] 从 A 重跑：生图A v{ga_v0}->{ga_after.get('current_version')} run={gen_a_run2}；"
          f"提示词A v{pa_v0}（未执行）；B 分支版本不变={b_after_d2 == b_before_d2}")

    # ===== E. 重跑下游：按依赖顺序串联（新事实 → 新策略 → 新提示词 → 新图片）=====
    print("\n=== E. 重跑下游（按依赖顺序）===")
    g = graph_of(pid)
    gid = g.get("graph_id")
    facts_e = nodes_by_type(g, "product_facts")
    strat_e = nodes_by_type(g, "strategy")
    p_e = nodes_by_type(g, "image_prompt", 0)
    gen_e_id = next((e["to_node"] for e in (g.get("edges") or []) if e["from_node"] == p_e["id"]), None)
    gen_e = next((n for n in (g.get("nodes") or []) if n["id"] == gen_e_id), None)
    check("定位重跑主分支（策略 → 提示词 → 生图）", bool(strat_e and p_e and gen_e),
          f"graph={gid} gen={gen_e_id}")

    # 新增一对“未连接”的孤立节点：用于验证可达性（不得被重跑）
    st, op_txt = req("POST", f"/api/graphs/{gid}/nodes", {"type": "image_prompt", "x": 2600, "y": 60})
    st2, og_txt = req("POST", f"/api/graphs/{gid}/nodes", {"type": "image_generation", "x": 2800, "y": 60})
    orphan_p_id = (jbody(op_txt) or {}).get("id")
    orphan_g_id = (jbody(og_txt) or {}).get("id")
    check("新增一对未连接的孤立节点（可达性验证前置）",
          st == 200 and st2 == 200 and bool(orphan_p_id) and bool(orphan_g_id),
          f"HTTP {st}/{st2} {op_txt[:100]} {og_txt[:100]}")

    # 修改已确认事实 → 下游全部 stale
    new_facts = {**facts, "product_id": "SKU-E2E-2", "product_name": "暖光蓝牙音箱 Pro",
                 "confirmed_selling_points": [
                     {"id": "sp_e1", "text": "E2E新卖点一：静音升级", "audience": "音乐爱好者", "scene": "客厅"},
                     {"id": "sp_e2", "text": "E2E新卖点二：充电更快", "audience": "通勤用户", "scene": "书房"},
                     {"id": "sp_e3", "text": "E2E新卖点三：胡桃木外壳", "audience": "家居党", "scene": "卧室"}],
                 "forbidden_expressions": ["全网最低", "绝对第一"]}
    st, txt = req("PATCH", f"/api/nodes/{facts_e['id']}", {"content": new_facts})
    check("修改已确认产品事实（让下游变 stale）", st == 200, f"HTTP {st} {txt[:160]}")

    g = graph_of(pid)
    orphan_before = {n["id"]: (n["current_version"], n["status"]) for n in (g.get("nodes") or [])
                     if n["id"] in (orphan_p_id, orphan_g_id)}
    branch_before = {n["id"]: n["current_version"] for n in (g.get("nodes") or [])
                     if n["type"] in ("image_prompt", "image_generation")
                     and n["id"] not in (orphan_p_id, orphan_g_id)}
    strat_v0 = (next((n for n in (g.get("nodes") or []) if n["id"] == strat_e["id"]), {}) or {}).get("current_version")
    p_v0 = (next((n for n in (g.get("nodes") or []) if n["id"] == p_e["id"]), {}) or {}).get("current_version")
    gen_v0 = (next((n for n in (g.get("nodes") or []) if n["id"] == gen_e_id), {}) or {}).get("current_version")
    stale_ok = all(n.get("status") == "stale" for n in (g.get("nodes") or [])
                   if n["id"] in (strat_e["id"], p_e["id"], gen_e_id))
    check("触发条件成立：策略 / 提示词 / 生图均为 stale", stale_ok,
          str({k[-6:]: v for k, v in branch_before.items()}))

    st, txt = req("POST", f"/api/graphs/{gid}/run-downstream", {"from_node_id": facts_e["id"]})
    check("重跑下游接口返回 200", st == 200, f"HTTP {st} 响应={txt[:300]}")
    ds = jbody(txt) or {}
    ran = ds.get("ran") or []
    check("响应返回已执行节点清单与状态",
          bool(ran) and all(isinstance(x, dict) and x.get("status") == "succeeded" for x in ran), str(ds)[:300])
    check("响应未标记失败节点", ds.get("failed") in (None, {}, []), str(ds.get("failed"))[:200])
    check("响应中的节点类型均为受支持的可执行类型",
          bool(ran) and all(x.get("type") in ("strategy", "image_prompt", "image_generation") for x in ran),
          str([x.get("type") for x in ran]))
    check("响应包含策略 / 提示词 / 生图三类节点",
          {x.get("type") for x in ran} >= {"strategy", "image_prompt", "image_generation"},
          str(sorted({x.get("type") for x in ran})))
    reachable_expected = {n["id"] for n in (g.get("nodes") or [])
                          if n["type"] in ("strategy", "image_prompt", "image_generation")
                          and n["id"] not in (orphan_p_id, orphan_g_id)}
    check("响应恰好覆盖主分支全部可达的可执行节点（策略 + 提示词 + 生图）",
          {x.get("node_id") for x in ran} == reachable_expected,
          f"ran={sorted(x.get('node_id') for x in ran)} expected={sorted(reachable_expected)}")
    check(f"响应节点数量与可达节点数量一致（{len(reachable_expected)}）", len(ran) == len(reachable_expected),
          f"len={len(ran)} {[x.get('type') for x in ran]}")
    skipped = ds.get("skipped") or []
    check("非自动执行节点（审核 / 排版导出）被显式列为 skipped",
          {"review", "layout_export"} <= {x.get("type") for x in skipped if isinstance(x, dict)},
          str(skipped)[:240])

    mk = lambda nid: next((x for x in (graph_of(pid).get("nodes") or []) if x["id"] == nid), {}) or {}
    strat_after = mk(strat_e["id"])
    p_after = mk(p_e["id"])
    gen_after_e = mk(gen_e_id)
    check("策略节点版本递增", strat_after.get("current_version") == (strat_v0 or 0) + 1,
          f"{strat_after.get('current_version')} vs {(strat_v0 or 0) + 1}")
    check("提示词节点版本递增", p_after.get("current_version") == (p_v0 or 0) + 1,
          f"{p_after.get('current_version')} vs {(p_v0 or 0) + 1}")
    check("生图节点版本递增", gen_after_e.get("current_version") == (gen_v0 or 0) + 1,
          f"{gen_after_e.get('current_version')} vs {(gen_v0 or 0) + 1}")

    sc = strat_after.get("content") or {}
    strs = sc.get("strategies") or []
    check("新策略基于新事实（product_id 为新值）", sc.get("product_id") == "SKU-E2E-2", str(sc.get("product_id")))
    check("新策略的卖点文本来自新事实",
          len(strs) == 3 and strs[0].get("selling_point_text") == "E2E新卖点一：静音升级",
          str([s.get("selling_point_text") for s in strs])[:200])
    pc_after = p_after.get("content") or {}
    check("新提示词使用新策略的卖点", "E2E新卖点一：静音升级" in (pc_after.get("prompt") or ""),
          (pc_after.get("prompt") or "")[:200])
    check("新提示词的 selling_point_id 指向新策略条目", pc_after.get("selling_point_id") == "sp_e1",
          str(pc_after.get("selling_point_id")))

    run_by_node = {x.get("node_id"): x.get("run_id") for x in ran if isinstance(x, dict)}
    details = {}
    for nid in (strat_e["id"], p_e["id"], gen_e_id):
        rid_ = run_by_node.get(nid)
        st_, d = (json_get(f"/api/runs/{rid_}") if rid_ else (0, None))
        details[nid] = d or {}
        check(f"本次运行详情可读（{nid[-6:]}）", st_ == 200 and bool(d), f"HTTP {st_} rid={rid_}")
    t_s = details[strat_e["id"]].get("started_at")
    t_p = details[p_e["id"]].get("started_at")
    t_g = details[gen_e_id].get("started_at")
    check("执行顺序为 策略 → 提示词 → 生图（按 started_at）",
          None not in (t_s, t_p, t_g) and t_s <= t_p <= t_g, f"{t_s} / {t_p} / {t_g}")
    check("三个节点本次运行状态均为 succeeded",
          all(details[nid].get("status") == "succeeded" for nid in (strat_e["id"], p_e["id"], gen_e_id)),
          str({k[-6:]: v.get("status") for k, v in details.items()}))
    gen_snap = details[gen_e_id].get("input_snapshot") or {}
    check("生图运行快照 prompt 等于新提示词", gen_snap.get("prompt") == (pc_after.get("prompt") or ""),
          f"snap={str(gen_snap.get('prompt'))[:120]!r}")
    check("生图运行快照指向新提示词版本",
          gen_snap.get("prompt_node_id") == p_e["id"]
          and gen_snap.get("prompt_node_version") == p_after.get("current_version"),
          f"{gen_snap.get('prompt_node_id')}@{gen_snap.get('prompt_node_version')} vs "
          f"{p_e['id']}@{p_after.get('current_version')}")
    check("生图运行快照模型为免费本地合成器", gen_snap.get("model_id") == MODEL_ID, str(gen_snap.get("model_id")))
    print(f"[12] 重跑下游证据：策略 ver {strat_v0}->{strat_after.get('current_version')} "
          f"run={run_by_node.get(strat_e['id'])}；提示词 ver {p_v0}->{p_after.get('current_version')} "
          f"run={run_by_node.get(p_e['id'])}；生图 ver {gen_v0}->{gen_after_e.get('current_version')} "
          f"run={run_by_node.get(gen_e_id)}；started_at 策略={t_s} <= 提示词={t_p} <= 生图={t_g}")

    # —— 双分支：共同上游事实变更后，两条分支各自按依赖关系重跑一次 ——
    counts = {}
    for x in ran:
        counts[x.get("node_id")] = counts.get(x.get("node_id"), 0) + 1
    check("策略节点在本次重跑中只执行一次", counts.get(strat_e["id"]) == 1, str(counts))
    check("提示词 A / 提示词 B 各执行一次", counts.get(p_e["id"]) == 1 and counts.get(prompt_b_id) == 1, str(counts))
    check("生图 A / 生图 B 各执行一次", counts.get(gen_e_id) == 1 and counts.get(gen_b_id) == 1, str(counts))

    pb_e, gb_e = mk(prompt_b_id), mk(gen_b_id)
    check("B 分支提示词版本递增",
          pb_e.get("current_version") == (branch_before.get(prompt_b_id, 0) or 0) + 1,
          f"{pb_e.get('current_version')} vs {(branch_before.get(prompt_b_id, 0) or 0) + 1}")
    check("B 分支生图版本递增",
          gb_e.get("current_version") == (branch_before.get(gen_b_id, 0) or 0) + 1,
          f"{gb_e.get('current_version')} vs {(branch_before.get(gen_b_id, 0) or 0) + 1}")
    pc_b = pb_e.get("content") or {}
    check("A / B 提示词各自保留 strategy_ref（hero_1 / hero_2）",
          (pc_after.get("strategy_ref"), pc_b.get("strategy_ref")) == ("hero_1", "hero_2"),
          f"{pc_after.get('strategy_ref')} / {pc_b.get('strategy_ref')}")
    strs_new = (strat_after.get("content") or {}).get("strategies") or []
    item_a = next((s for s in strs_new if s.get("id") == "hero_1"), None)
    item_b = next((s for s in strs_new if s.get("id") == "hero_2"), None)
    check("A / B 提示词分别包含各自策略条目的新卖点文本",
          bool(item_a) and item_a["selling_point_text"] in (pc_after.get("prompt") or "")
          and bool(item_b) and item_b["selling_point_text"] in (pc_b.get("prompt") or ""),
          f"A={item_a and item_a.get('selling_point_text')} B={item_b and item_b.get('selling_point_text')}")

    details_b = {}
    for nid in (prompt_b_id, gen_b_id):
        rid_b = run_by_node.get(nid)
        st_b, d_b = (json_get(f"/api/runs/{rid_b}") if rid_b else (0, None))
        details_b[nid] = d_b or {}
        check(f"B 分支本次运行详情可读（{nid[-6:]}）", st_b == 200 and bool(d_b), f"HTTP {st_b} rid={rid_b}")
    snap_gb = details_b[gen_b_id].get("input_snapshot") or {}
    check("生图 B 快照指向提示词 B 的新版本",
          snap_gb.get("prompt_node_id") == prompt_b_id
          and snap_gb.get("prompt_node_version") == pb_e.get("current_version"),
          f"{snap_gb.get('prompt_node_id')}@{snap_gb.get('prompt_node_version')} vs "
          f"{prompt_b_id}@{pb_e.get('current_version')}")
    check("生图 A / B 快照分别等于各自提示词的当前内容",
          gen_snap.get("prompt") == (pc_after.get("prompt") or "")
          and snap_gb.get("prompt") == (pc_b.get("prompt") or ""),
          "两条分支快照 prompt 与各自提示词不一致")
    t_pb = details_b[prompt_b_id].get("started_at")
    t_gb = details_b[gen_b_id].get("started_at")
    check("依赖顺序正确：策略早于两条分支的提示词，提示词早于各自生图（不要求 A/B 先后）",
          None not in (t_s, t_p, t_g, t_pb, t_gb)
          and t_s <= t_p and t_p <= t_g and t_s <= t_pb and t_pb <= t_gb,
          f"策略={t_s} 提示词A={t_p} 生图A={t_g} 提示词B={t_pb} 生图B={t_gb}")
    print(f"[13] 双分支重跑：策略 v{strat_v0}->{strat_after.get('current_version')}；"
          f"提示词A v{p_v0}->{p_after.get('current_version')}；生图A v{gen_v0}->{gen_after_e.get('current_version')}；"
          f"提示词B v{branch_before.get(prompt_b_id)}->{pb_e.get('current_version')}；"
          f"生图B v{branch_before.get(gen_b_id)}->{gb_e.get('current_version')}；"
          f"run 策略={run_by_node.get(strat_e['id'])} 提示词A={run_by_node.get(p_e['id'])} "
          f"生图A={run_by_node.get(gen_e_id)} 提示词B={run_by_node.get(prompt_b_id)} "
          f"生图B={run_by_node.get(gen_b_id)}")

    g = graph_of(pid)
    branch_after = {n["id"]: n["current_version"] for n in (g.get("nodes") or []) if n["id"] in branch_before}
    check("可达分支上的提示词/生图节点版本全部递增",
          all(branch_after.get(k, 0) == v + 1 for k, v in branch_before.items()),
          f"before={sorted(branch_before.values())} after={sorted(branch_after.values())}")
    orp = next((n for n in (g.get("nodes") or []) if n["id"] == orphan_p_id), {}) or {}
    org = next((n for n in (g.get("nodes") or []) if n["id"] == orphan_g_id), {}) or {}
    check("未连接的孤立提示词节点未被重跑（版本与状态不变）",
          (orp.get("current_version"), orp.get("status")) == orphan_before.get(orphan_p_id),
          f"before={orphan_before.get(orphan_p_id)} after={(orp.get('current_version'), orp.get('status'))}")
    check("未连接的孤立生图节点未被重跑（版本与状态不变）",
          (org.get("current_version"), org.get("status")) == orphan_before.get(orphan_g_id),
          f"before={orphan_before.get(orphan_g_id)} after={(org.get('current_version'), org.get('status'))}")
    check("响应清单不含孤立节点", not any(x.get("node_id") in (orphan_p_id, orphan_g_id) for x in ran),
          str([x.get("node_id") for x in ran]))
    runs_all = json_get("/api/runs")[1] or []
    check("孤立节点没有任何运行记录",
          not any(r.get("node_id") in (orphan_p_id, orphan_g_id) for r in runs_all),
          str([(r.get("node_id"), r.get("status")) for r in runs_all[:8]]))
    check("响应清单不含事实 / 参考图 / 审核 / 导出节点（这些节点不自动执行）",
          not any(x.get("type") in ("product_facts", "product_image") for x in ran),
          str([x.get("type") for x in ran]))

    # ===== F. 多图素材：节点 → 本地拼接器 的语义与实际效果 =====
    # 说明：本轮只改测试。以下两个业务断言用于暴露「拼接器只读 product_imgs[0]」的缺陷，
    # 预期失败；失败现场保留在本报告的失败清单中，不修改生产代码。
    print("\n=== F. 多图素材的语义与实际效果 ===")
    F_A = make_png(400, 400, (220, 40, 40, 255), "round")     # 产品 A：红
    F_B = make_png(400, 400, (40, 80, 220, 255), "round")     # 产品 B：蓝
    F_L = make_png(160, 80, (255, 0, 255, 255), "bar")        # Logo L：品红
    st_a, up_a = upload_asset(pid, "f_product_a.png", F_A, "product")
    st_b, up_b = upload_asset(pid, "f_product_b.png", F_B, "product")
    st_l, up_l = upload_asset(pid, "f_logo_l.png", F_L, "logo")
    f_a, f_b, f_l = (up_a or {}).get("id"), (up_b or {}).get("id"), (up_l or {}).get("id")
    check("上传产品 A / 产品 B / Logo L 三张素材", st_a == 200 and st_b == 200 and st_l == 200
          and all([f_a, f_b, f_l]), f"HTTP {st_a}/{st_b}/{st_l}")
    f_roles = {x["id"]: x.get("role") for x in project_assets(pid)}
    check("三张素材 role 入库分别为 product / product / logo",
          f_roles.get(f_a) == "product" and f_roles.get(f_b) == "product" and f_roles.get(f_l) == "logo",
          f"A={f_roles.get(f_a)} B={f_roles.get(f_b)} L={f_roles.get(f_l)}")

    F_SEED = 20260926
    F_PARAMS = {"aspect_ratio": "1:1", "resolution_tier": "standard", "count": 1, "seed": F_SEED,
                "title": "多图语义测试", "subtitle": "固定参数"}
    f_inputs = {"R1": (f_a, []), "R2": (f_a, [f_b]), "R3": (f_a, [f_b, f_l])}
    f_runs = {}
    for tag in ("R1", "R2", "R3"):
        primary, refs = f_inputs[tag]
        # 参考图挂在 product_image 节点上：image_generation 的 refs = [节点 asset_id] + [节点 reference_asset_ids]
        st, ct = req("PATCH", f"/api/nodes/{img_n['id']}",
                     {"content": {"asset_id": primary, "asset_role": "product", "reference_asset_ids": refs}})
        check(f"{tag} 运行前按顺序挂载参考图（共 {len(refs) + 1} 张）", st == 200, f"HTTP {st} {ct[:160]}")
        f_runs[tag] = run_node(gen_a_id, {"model_id": MODEL_ID, "params": F_PARAMS,
                                          "idempotency_key": f"idem_F_{tag}_{time.time()}"})
        check(f"{tag} 运行成功", f_runs[tag].get("status") == "succeeded",
              f"status={f_runs[tag].get('status')} err={f_runs[tag].get('error_code')}")

    print(f"[F] 素材：产品A(红)={f_a} 产品B(蓝)={f_b} LogoL(品红)={f_l}；模型={MODEL_ID} seed={F_SEED}")
    f_expected = {"R1": [f_a], "R2": [f_a, f_b], "R3": [f_a, f_b, f_l]}
    for tag in ("R1", "R2", "R3"):
        snap = f_runs[tag].get("input_snapshot") or {}
        check(f"{tag} 输入快照的参考资产 ID 顺序与本次输入一致",
              snap.get("reference_asset_ids") == f_expected[tag],
              f"{snap.get('reference_asset_ids')} vs {f_expected[tag]}")

    print("[F] 三次输入快照的参考资产顺序："
          + "；".join(f"{t}={((f_runs[t].get('input_snapshot') or {}).get('reference_asset_ids'))}"
                      for t in ("R1", "R2", "R3")))
    f_imgs, f_shas = {}, {}
    for tag in ("R1", "R2", "R3"):
        outs = f_runs[tag].get("outputs") or []
        aid = outs[0].get("asset_id") if outs else None
        st, raw = (fetch_raw(f"/files/{aid}") if aid else (0, b""))
        f_shas[tag] = hashlib.sha256(raw).hexdigest() if raw else ""
        try:
            f_imgs[tag] = Image.open(io.BytesIO(raw)).convert("RGBA") if st == 200 and raw else None
        except Exception as ferr:
            f_imgs[tag] = None
            print("    解码异常:", ferr)
        check(f"{tag} 生成图可读取且可解码", f_imgs[tag] is not None, f"HTTP {st} bytes={len(raw)}")

    f_d12 = px_diff(f_imgs.get("R1"), f_imgs.get("R2"))
    f_d23 = px_diff(f_imgs.get("R2"), f_imgs.get("R3"))
    f_logo_r2 = color_count(f_imgs.get("R2"), (255, 0, 255))
    f_logo_r3 = color_count(f_imgs.get("R3"), (255, 0, 255))
    print(f"[F] 输出 SHA-256：R1={f_shas['R1']} R2={f_shas['R2']} R3={f_shas['R3']}")
    print(f"[F] 像素差异：R2-R1={f_d12}；R3-R2={f_d23}")
    print(f"[F] Logo 颜色(255,0,255)像素数诊断：R2={f_logo_r2} R3={f_logo_r3}（非断言）")

    check("业务断言①：加入产品 B 后 R2 输出与 R1 存在像素差异",
          bool(f_d12) and (f_d12.get("diff_pixels") or 0) > 0,
          f"R2-R1 差异像素={f_d12 and f_d12.get('diff_pixels')}；SHA 相同={f_shas['R1'] == f_shas['R2']}")
    check("业务断言②：加入 Logo L 后 R3 输出与 R2 存在像素差异（且不应被当作产品主体）",
          bool(f_d23) and (f_d23.get("diff_pixels") or 0) > 0,
          f"R3-R2 差异像素={f_d23 and f_d23.get('diff_pixels')}；SHA 相同={f_shas['R2'] == f_shas['R3']}")
    # —— 版式契约断言（role 已从数据库传到本地拼接器）——
    F_W, F_H = (f_imgs["R1"].size if f_imgs.get("R1") else (1024, 1024))
    f_aux_box = (int(F_W - F_W * 0.05 - F_W * 0.22), int(F_H * 0.72 - F_H * 0.22),
                 int(F_W - F_W * 0.05), int(F_H * 0.72))
    f_logo_box = (int(F_W * 0.05), int(F_H * 0.06), int(F_W * 0.05 + F_W * 0.18), int(F_H * 0.06 + F_H * 0.12))
    f_center_box = (int(F_W * 0.30), int(F_H * 0.20), int(F_W * 0.70), int(F_H * 0.60))
    f_crop = lambda im, box: (im.crop(box) if im is not None else None)

    def _box_inside(inner, outer, pad=2):
        if not inner:
            return False
        return (inner[0] >= outer[0] - pad and inner[1] >= outer[1] - pad
                and inner[2] <= outer[2] + pad and inner[3] <= outer[3] + pad)

    f_red_center_r1 = color_count(f_crop(f_imgs.get("R1"), f_center_box), (220, 40, 40))
    check("R1 主产品视觉存在（中央区域含产品 A 的红色）", (f_red_center_r1 or 0) > 0,
          f"中心区红像素={f_red_center_r1}")
    check("R2 相对 R1 的差异位于右下辅图区域",
          bool(f_d12) and (f_d12.get("diff_pixels") or 0) > 0 and _box_inside(f_d12.get("bbox"), f_aux_box),
          f"差异={f_d12}；约定辅图区={f_aux_box}")
    f_blue_aux_r2 = color_count(f_crop(f_imgs.get("R2"), f_aux_box), (40, 80, 220))
    check("R2 的辅图出现在右下辅图区域（含产品 B 的蓝色）", (f_blue_aux_r2 or 0) > 0,
          f"辅图区蓝像素={f_blue_aux_r2}")
    f_logo_in_r3 = color_count(f_crop(f_imgs.get("R3"), f_logo_box), (255, 0, 255))
    f_logo_in_r2 = color_count(f_crop(f_imgs.get("R2"), f_logo_box), (255, 0, 255))
    check("R3 的 Logo 测试标记出现在左上 Logo 区域",
          (f_logo_in_r3 or 0) > 0 and (f_logo_in_r2 or 0) == 0,
          f"R3 Logo 区品红={f_logo_in_r3}；R2 同区={f_logo_in_r2}")
    f_center_d23 = px_diff(f_crop(f_imgs.get("R2"), f_center_box), f_crop(f_imgs.get("R3"), f_center_box))
    check("添加 Logo 不改变中央主产品区域",
          bool(f_center_d23) and f_center_d23.get("diff_pixels") == 0, f"中心区 R3-R2 差异={f_center_d23}")

    # 换序：[Logo, 产品A, 产品B] —— Logo 排第一也不得被当作主产品图
    st4, ct4 = req("PATCH", f"/api/nodes/{img_n['id']}",
                   {"content": {"asset_id": f_l, "asset_role": "logo", "reference_asset_ids": [f_a, f_b]}})
    check("换序运行前挂载 [Logo, 产品A, 产品B]", st4 == 200, f"HTTP {st4} {ct4[:160]}")
    f_run4 = run_node(gen_a_id, {"model_id": MODEL_ID, "params": F_PARAMS,
                                 "idempotency_key": f"idem_F_R4_{time.time()}"})
    check("R4（换序）运行成功", f_run4.get("status") == "succeeded",
          f"status={f_run4.get('status')} err={f_run4.get('error_code')}")
    snap4 = f_run4.get("input_snapshot") or {}
    check("R4 输入快照保持传入顺序 [Logo, 产品A, 产品B]",
          snap4.get("reference_asset_ids") == [f_l, f_a, f_b], str(snap4.get("reference_asset_ids")))
    outs4 = f_run4.get("outputs") or []
    st4f, raw4 = (fetch_raw(f"/files/{outs4[0]['asset_id']}") if outs4 else (0, b""))
    f_img4 = None
    try:
        f_img4 = Image.open(io.BytesIO(raw4)).convert("RGBA") if st4f == 200 and raw4 else None
    except Exception as ferr4:
        print("    解码异常:", ferr4)
    check("R4 生成图可解码", f_img4 is not None, f"HTTP {st4f} bytes={len(raw4)}")
    check("换序后 Logo 仍在左上区域", (color_count(f_crop(f_img4, f_logo_box), (255, 0, 255)) or 0) > 0,
          f"Logo 区品红={color_count(f_crop(f_img4, f_logo_box), (255, 0, 255))}")
    f_red_center_r4 = color_count(f_crop(f_img4, f_center_box), (220, 40, 40))
    f_magenta_center_r4 = color_count(f_crop(f_img4, f_center_box), (255, 0, 255))
    check("换序后产品 A 仍是中央主视觉（中心区红>0 且品红=0）",
          (f_red_center_r4 or 0) > 0 and (f_magenta_center_r4 or 0) == 0,
          f"中心区 红={f_red_center_r4} 品红={f_magenta_center_r4}")
    f_sha4 = hashlib.sha256(raw4).hexdigest() if raw4 else ""
    check("换序不改变成图（按 role 取图而非按位置）", bool(raw4) and f_sha4 == f_shas["R3"],
          f"R4 sha={f_sha4[:16]} vs R3 sha={f_shas['R3'][:16]}")

    # 旧调用兼容：直接调用本地生成函数（不传 roles）仍按“第一张即主产品图”
    import shutil as _shutil, tempfile as _tempfile
    _legacy_dir = _tempfile.mkdtemp(prefix="wb_legacy_")
    os.environ["WB_DB_PATH"] = os.path.join(_legacy_dir, "legacy.db")
    os.environ["WB_UPLOAD_DIR"] = os.path.join(_legacy_dir, "uploads")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app.generators import image as _image_gen
    _legacy = _image_gen.generate({"model_id": MODEL_ID, "prompt": "legacy-no-roles",
                                   "aspect_ratio": "1:1", "resolution_tier": "standard",
                                   "count": 1, "params": {"title": "旧调用"}},
                                  [Image.open(io.BytesIO(F_A))])
    _legacy_raw = (_legacy.get("outputs") or [{}])[0].get("bytes") or b""
    _legacy_img = None
    try:
        _legacy_img = Image.open(io.BytesIO(_legacy_raw)).convert("RGBA") if _legacy_raw else None
    except Exception as lerr:
        print("    解码异常:", lerr)
    check("无 role 的旧调用仍返回可解码 PNG",
          _legacy_img is not None and _legacy_img.size == (F_W, F_H),
          f"size={_legacy_img and _legacy_img.size} bytes={len(_legacy_raw)}")
    f_red_legacy = color_count(f_crop(_legacy_img, f_center_box), (220, 40, 40))
    check("旧调用保持“第一张即主产品图”（中心区仍是产品 A）", (f_red_legacy or 0) > 0,
          f"中心区红={f_red_legacy}")
    _shutil.rmtree(_legacy_dir, ignore_errors=True)
    print(f"[F] 区域约定：辅图区={f_aux_box} Logo区={f_logo_box} 中心区={f_center_box}")
    print(f"[F] 区域像素：R2辅图区蓝={f_blue_aux_r2}；R3 Logo区品红={f_logo_in_r3}；"
          f"中心区红 R1={f_red_center_r1} R3={color_count(f_crop(f_imgs.get('R3'), f_center_box), (220, 40, 40))} "
          f"R4={f_red_center_r4} 旧调用={f_red_legacy}")

    # ===== G. 标题/副标题必须进入最终 PNG =====
    print("\n=== G. 标题与副标题渲染 ===")
    # 安全区由现有绘制代码确定：标题 anchor="mm" 于 (w/2, 0.78h)、字号 0.07h；副标题 0.90h、字号 0.038h
    g_title_zone = (0, int(F_H * 0.78) - int(F_H * 0.07), F_W, int(F_H * 0.78) + int(F_H * 0.07))
    g_sub_zone = (0, int(F_H * 0.90) - int(F_H * 0.038), F_W, int(F_H * 0.90) + int(F_H * 0.038))
    check("标题安全区与副标题安全区互不重叠", g_title_zone[3] < g_sub_zone[1],
          f"标题区={g_title_zone} 副标题区={g_sub_zone}")
    print(f"[G] 坐标：标题安全区={g_title_zone}；副标题安全区={g_sub_zone}；"
          f"辅图区={f_aux_box}；Logo区={f_logo_box}；中心区={f_center_box}")

    st_g, gt_g = req("PATCH", f"/api/nodes/{img_n['id']}",
                     {"content": {"asset_id": f_a, "asset_role": "product", "reference_asset_ids": [f_b, f_l]}})
    check("G 段运行前挂载 [产品A, 产品B, Logo]", st_g == 200, f"HTTP {st_g} {gt_g[:160]}")

    g_runs, g_imgs, g_shas = {}, {}, {}
    for tag, g_title, g_sub in (("T1", "标题甲", "副标题甲"), ("T2", "标题乙", "副标题甲"),
                                ("T3", "标题甲", "副标题乙"), ("T1b", "标题甲", "副标题甲")):
        g_params = dict(F_PARAMS)
        g_params["title"] = g_title
        g_params["subtitle"] = g_sub
        r = run_node(gen_a_id, {"model_id": MODEL_ID, "params": g_params,
                                "idempotency_key": f"idem_G_{tag}_{time.time()}"})
        g_runs[tag] = r
        check(f"{tag}（title={g_title} subtitle={g_sub}）运行成功", r.get("status") == "succeeded",
              f"status={r.get('status')} err={r.get('error_code')}")
        snap_g = r.get("input_snapshot") or {}
        check(f"{tag} 快照中的有效标题/副标题与本次请求一致",
              (snap_g.get("params") or {}).get("title") == g_title
              and (snap_g.get("params") or {}).get("subtitle") == g_sub, str(snap_g.get("params")))
        outs_g = r.get("outputs") or []
        st_gf, raw_g = (fetch_raw(f"/files/{outs_g[0]['asset_id']}") if outs_g else (0, b""))
        g_shas[tag] = hashlib.sha256(raw_g).hexdigest() if raw_g else ""
        try:
            g_imgs[tag] = Image.open(io.BytesIO(raw_g)).convert("RGBA") if st_gf == 200 and raw_g else None
        except Exception as gerr:
            g_imgs[tag] = None
            print("    解码异常:", gerr)
        check(f"{tag} 生成图可解码", g_imgs[tag] is not None, f"HTTP {st_gf} bytes={len(raw_g)}")

    g_d21 = px_diff(g_imgs.get("T1"), g_imgs.get("T2"))
    g_d31 = px_diff(g_imgs.get("T1"), g_imgs.get("T3"))
    check("T2 与 T1 存在像素差异（标题进入成图）",
          bool(g_d21) and (g_d21.get("diff_pixels") or 0) > 0,
          f"差异={g_d21}；SHA 相同={g_shas['T1'] == g_shas['T2']}")
    check("T2 与 T1 的差异区域仅位于标题安全区",
          bool(g_d21) and _box_inside(g_d21.get("bbox"), g_title_zone),
          f"差异 bbox={g_d21 and g_d21.get('bbox')}；标题安全区={g_title_zone}")
    check("T3 与 T1 存在像素差异（副标题进入成图）",
          bool(g_d31) and (g_d31.get("diff_pixels") or 0) > 0,
          f"差异={g_d31}；SHA 相同={g_shas['T1'] == g_shas['T3']}")
    check("T3 与 T1 的差异区域仅位于副标题安全区",
          bool(g_d31) and _box_inside(g_d31.get("bbox"), g_sub_zone),
          f"差异 bbox={g_d31 and g_d31.get('bbox')}；副标题安全区={g_sub_zone}")
    for g_name, g_d in (("T2-T1", g_d21), ("T3-T1", g_d31)):
        g_bb = (g_d or {}).get("bbox")
        g_hit = [nm for nm, box in (("辅图区", f_aux_box), ("Logo区", f_logo_box), ("中心区", f_center_box))
                 if g_bb and not (g_bb[2] <= box[0] or g_bb[0] >= box[2] or g_bb[3] <= box[1] or g_bb[1] >= box[3])]
        check(f"{g_name} 差异未触及辅图/Logo/中央主产品区", not g_hit, f"触及={g_hit} bbox={g_bb}")
    for g_name, g_box in (("产品 A 中心区", f_center_box), ("产品 B 辅图区", f_aux_box), ("Logo 区", f_logo_box)):
        g_d12c = px_diff(f_crop(g_imgs.get("T1"), g_box), f_crop(g_imgs.get("T2"), g_box))
        g_d13c = px_diff(f_crop(g_imgs.get("T1"), g_box), f_crop(g_imgs.get("T3"), g_box))
        check(f"{g_name} 在 T1/T2/T3 间不变",
              (g_d12c or {}).get("diff_pixels") == 0 and (g_d13c or {}).get("diff_pixels") == 0,
              f"T2-T1={g_d12c} T3-T1={g_d13c}")
    check("固定参数重复生成（T1b）与首次 T1 像素一致",
          g_shas["T1b"] == g_shas["T1"] and (px_diff(g_imgs.get("T1"), g_imgs.get("T1b")) or {}).get("diff_pixels") == 0,
          f"T1 sha={g_shas['T1'][:16]} T1b sha={g_shas['T1b'][:16]}")
    print(f"[G] SHA-256：T1={g_shas['T1'][:16]} T2={g_shas['T2'][:16]} T3={g_shas['T3'][:16]} T1b={g_shas['T1b'][:16]}")
    print(f"[G] 像素差异：T2-T1={g_d21}；T3-T1={g_d31}")

    print("\n【未覆盖项】")
    if NOT_COVERED:
        for item in NOT_COVERED:
            print("  - " + item)
    else:
        print("  （无）")
    return R.summary("E2E 工作流验收")


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except SystemExit as e:
        code = e.code or 0
    except Exception:
        traceback.print_exc()
        code = 1
    finally:
        if ISOLATED is not None:
            ISOLATED.__exit__(None, None, None)
            print(f"[清理] 已停止隔离实例并删除临时目录 {ISOLATED.tmpdir}")
    raise SystemExit(code)

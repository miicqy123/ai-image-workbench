"""第 2 批安全验收测试：组织、项目与资源级授权。

覆盖：两个组织 / 三个账号 / 两个项目，各自的素材、任务、审核、通知与成本记录，
全部通过**真实会话登录**访问接口。另外包含跨项目引用、归属不明项目与 CSRF 校验。

运行（在 workbench/backend 目录下）：
    python tests/org_isolation_tests.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Client, Instance, Results, TINY_PNG, sql  # noqa: E402

R = Results()
ADMIN_PW = "PlatformAdmin123"
ORG_A_PW = "OrgAdminA123"
EDIT_A_PW = "EditorA12345"
EDIT_B_PW = "EditorB12345"


def login(c: Client, uid: str, pw: str) -> bool:
    st, _ = c.req("POST", "/api/auth/login", {"user_id": uid, "password": pw})
    return st == 200


def main() -> int:
    with Instance() as inst:
        base = inst.base
        root = Client(base)                     # 平台管理员（超管）
        org_a = Client(base)                    # A 组织管理员
        editor_a = Client(base)                 # A 组织编辑
        editor_b = Client(base)                 # B 组织编辑
        editor_b2 = Client(base)                # 另一个 B 组织编辑会话（用于 CSRF 场景）

        print("\n[准备：平台管理员初始化]")
        st, _ = root.req("POST", "/api/auth/bootstrap", {"user_id": "usr_default", "password": ADMIN_PW, "name": "平台管理员"})
        R.check("初始化平台管理员", st == 201, f"got {st}")

        st, org = root.req("POST", "/api/admin/organizations", {"id": "org_a", "name": "A 公司", "quota_total": 50000})
        R.check("创建组织 A", st == 201, f"got {st} {org}")
        st, _ = root.req("POST", "/api/admin/organizations", {"id": "org_b", "name": "B 公司", "quota_total": 50000})
        R.check("创建组织 B", st == 201, f"got {st}")

        def add_member(org_id, name, role, pw):
            st, res = root.req("POST", f"/api/admin/organizations/{org_id}/members", {"name": name, "role": role})
            uid = (res or {}).get("user_id") if isinstance(res, dict) else None
            if uid:
                root.req("POST", f"/api/admin/users/{uid}/reset-password", {"new_password": pw})
            return st, uid

        st, admin_a = add_member("org_a", "A 组织管理员", "organization_admin", ORG_A_PW)
        R.check("在组织 A 建组织管理员", st == 201 and bool(admin_a), f"got {st} {admin_a}")
        st, editor_a_id = add_member("org_a", "A 编辑", "editor", EDIT_A_PW)
        R.check("在组织 A 建编辑", st == 201 and bool(editor_a_id), f"got {st}")
        st, editor_b_id = add_member("org_b", "B 编辑", "editor", EDIT_B_PW)
        R.check("在组织 B 建编辑", st == 201 and bool(editor_b_id), f"got {st}")

        R.check("A 组织管理员登录", login(org_a, admin_a, ORG_A_PW))
        R.check("A 编辑登录", login(editor_a, editor_a_id, EDIT_A_PW))
        R.check("B 编辑登录", login(editor_b, editor_b_id, EDIT_B_PW))
        login(editor_b2, editor_b_id, EDIT_B_PW)

        print("\n[准备：两个项目 + 素材 + 任务 + 审核]")
        def make_project(c: Client, name: str):
            st, p = c.req("POST", "/api/projects", {"name": name})
            return (p or {}).get("id") if isinstance(p, dict) else None

        proj_a = make_project(editor_a, "A 项目")
        proj_b = make_project(editor_b, "B 项目")
        R.check("A 编辑创建项目 A", bool(proj_a), str(proj_a))
        R.check("B 编辑创建项目 B", bool(proj_b), str(proj_b))

        def upload(c: Client, pid: str):
            st, a = c.upload(f"/api/projects/{pid}/assets", "p.png", TINY_PNG, {"role": "product"})
            return (a or {}).get("id") if isinstance(a, dict) else None

        asset_a = upload(editor_a, proj_a)
        asset_b = upload(editor_b, proj_b)
        R.check("A 项目上传素材", bool(asset_a), f"asset_a={asset_a}")
        R.check("B 项目上传素材", bool(asset_b), f"asset_b={asset_b}")

        def make_brief(c: Client, pid: str, asset_id):
            return c.req("PUT", f"/api/projects/{pid}/brief", {
                "user_prompt": "企业获客海报", "purpose": "marketing_poster", "platform": "淘宝/天猫",
                "aspect_ratio": "1:1", "image_count": 1, "product_asset_ids": [asset_id]})

        st, _ = make_brief(editor_a, proj_a, asset_a)
        R.check("A 项目写入 Brief（引用本项目素材）", st in (200, 201), f"got {st}")
        st, _ = make_brief(editor_b, proj_b, asset_b)
        R.check("B 项目写入 Brief", st in (200, 201), f"got {st}")

        st, job_a = editor_a.req("POST", f"/api/projects/{proj_a}/generation-jobs", {})
        job_a_id = (job_a or {}).get("id") if isinstance(job_a, dict) else None
        R.check("A 项目创建生图任务", bool(job_a_id), f"got {st} {job_a}")
        st, job_b = editor_b.req("POST", f"/api/projects/{proj_b}/generation-jobs", {})
        job_b_id = (job_b or {}).get("id") if isinstance(job_b, dict) else None
        R.check("B 项目创建生图任务", bool(job_b_id), f"got {st}")

        st, rev_a = editor_a.req("POST", f"/api/projects/{proj_a}/reviews", {"target_type": "canvas_output"})
        rev_a_id = (rev_a or {}).get("id") if isinstance(rev_a, dict) else None
        R.check("A 项目提交审核", st == 201 and bool(rev_a_id), f"got {st}")
        st, rev_b = editor_b.req("POST", f"/api/projects/{proj_b}/reviews", {"target_type": "canvas_output"})
        rev_b_id = (rev_b or {}).get("id") if isinstance(rev_b, dict) else None
        R.check("B 项目提交审核", st == 201 and bool(rev_b_id), f"got {st}")

        # 运行一个节点，产出 node_run 用于 SSE / 运行列表的越权测试
        editor_b.req("POST", f"/api/projects/{proj_b}/graph/init-template", {"skeleton": "poster"})
        g = editor_b.req("GET", f"/api/projects/{proj_b}/graph")[1]
        nodes = (g or {}).get("nodes") or []
        strat = next((n for n in nodes if n["type"] == "strategy"), None)
        run_b = None
        if strat:
            st, r = editor_b.req("POST", f"/api/nodes/{strat['id']}/runs", {"idempotency_key": "b-run-1"})
            run_b = (r or {}).get("run_id") if isinstance(r, dict) else None
        R.check("B 项目运行节点（准备 node_run）", bool(run_b), f"got {run_b}")

        print("\n[准备：通知、用量成本与归属不明项目（隔离库 fixture）]")
        now = int(time.time())
        sql(inst.db_path, [
            ("INSERT INTO notifications(id,user_id,type,title,body,ref_type,ref_id,read,created_at) "
             "VALUES(?,?,?,?,?,?,?,0,?)", ("ntf_a", editor_a_id, "job", "A 的通知", "", "", "", now)),
            ("INSERT INTO notifications(id,user_id,type,title,body,ref_type,ref_id,read,created_at) "
             "VALUES(?,?,?,?,?,?,?,0,?)", ("ntf_b", editor_b_id, "job", "B 的通知", "", "", "", now)),
            ("INSERT INTO usage_records(id,tenant_id,organization_id,project_id,user_id,model_id,task_type,"
             "input_images,output_images,unit_cost,cost,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
             ("usg_a", "tnt_default", "org_a", proj_a, editor_a_id, "local-poster-compositor", "text_to_image", 0, 3, 5.0, 15.0, "succeeded", now)),
            ("INSERT INTO usage_records(id,tenant_id,organization_id,project_id,user_id,model_id,task_type,"
             "input_images,output_images,unit_cost,cost,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
             ("usg_b", "tnt_default", "org_b", proj_b, editor_b_id, "local-poster-compositor", "text_to_image", 0, 2, 5.0, 10.0, "succeeded", now)),
            ("INSERT INTO projects(id,tenant_id,owner_id,name,status,organization_id,created_at,updated_at) "
             "VALUES(?,?,?,?,?,?,?,?)", ("prj_orphan", "tnt_default", editor_a_id, "归属不明项目", "active", None, now, now)),
            ("INSERT INTO graphs(id,project_id,current_version) VALUES(?,?,1)", ("grf_orphan", "prj_orphan")),
        ])
        R.check("fixture 写入完成（2 条用量、2 条通知、1 个归属不明项目）", True)

        print("\n[1. 普通编辑不得读取后台数据]")
        for path, label in [("/api/admin/cost-summary", "成本汇总"), ("/api/admin/usage-records", "用量明细"),
                            ("/api/admin/reviews", "审核队列"), ("/api/audit", "审计日志"),
                            ("/api/admin/organizations", "组织列表"), ("/api/admin/users", "成员列表"),
                            ("/api/admin/stats", "后台总览"), ("/api/assets", "全站素材")]:
            st, _ = editor_a.req("GET", path)
            R.check(f"编辑读取{label} → 403", st == 403, f"got {st}")

        print("\n[2. 组织管理员：只能看到本组织范围]")
        st, cost = org_a.req("GET", "/api/admin/cost-summary")
        R.check("A 组织管理员读成本汇总 → 200", st == 200, f"got {st}")
        R.check("成本汇总只含 A 组织（15.0）", isinstance(cost, dict) and cost.get("total_cost") == 15.0, str(cost)[:160])
        st, a_only = org_a.req("GET", "/api/admin/usage-records")
        R.check("A 组织管理员的成本视图不含 B 组织 project",
                isinstance(a_only, list) and all(r.get("project_id") != proj_b for r in a_only), str(a_only)[:160])

        st, usage = org_a.req("GET", "/api/admin/usage-records")
        R.check("A 组织管理员读用量明细 → 200", st == 200, f"got {st}")
        R.check("用量明细只含 A 组织", isinstance(usage, list) and len(usage) == 1 and usage[0]["id"] == "usg_a",
                str(usage)[:160])

        st, revs = org_a.req("GET", "/api/admin/reviews")
        R.check("A 组织管理员读审核队列 → 200", st == 200, f"got {st}")
        R.check("审核队列只含 A 组织", isinstance(revs, list) and all(r["project_id"] == proj_a for r in revs)
                and len(revs) >= 1, str(revs)[:160])

        st, orgs = org_a.req("GET", "/api/admin/organizations")
        R.check("A 组织管理员读组织列表 → 仅自己组织", st == 200 and isinstance(orgs, list) and len(orgs) == 1
                and orgs[0]["id"] == "org_a", f"got {st} {str(orgs)[:160]}")

        st, members = org_a.req("GET", "/api/admin/users")
        ids = {m["user_id"] for m in members} if isinstance(members, list) else set()
        R.check("A 组织管理员读成员列表 → 不含 B 组织成员", st == 200 and editor_b_id not in ids, str(ids))
        R.check("A 组织管理员读成员列表 → 含 A 组织成员", editor_a_id in ids and admin_a in ids, str(ids))

        st, stats = org_a.req("GET", "/api/admin/stats")
        R.check("A 组织管理员读后台总览 → 200 且标记为组织范围", st == 200 and (stats or {}).get("scope") == "organization",
                f"got {st} {str(stats)[:160]}")

        st, assets = org_a.req("GET", "/api/assets")
        R.check("A 组织管理员读全站素材 → 仅 A 组织", st == 200 and all(a["project_id"] == proj_a for a in assets),
                str(assets)[:160])

        print("\n[3. 跨组织对象访问必须拒绝（404）]")
        cross = [
            ("GET", f"/api/projects/{proj_b}", None, "读 B 项目详情"),
            ("PATCH", f"/api/projects/{proj_b}", {"name": "被改名"}, "改 B 项目名"),
            ("DELETE", f"/api/projects/{proj_b}", None, "删 B 项目（无删除权限，先撞 403）"),
            ("GET", f"/api/projects/{proj_b}/assets", None, "读 B 项目素材列表"),
            ("GET", f"/api/projects/{proj_b}/brief", None, "读 B 项目 Brief"),
            ("GET", f"/api/projects/{proj_b}/candidates", None, "读 B 项目候选图"),
            ("GET", f"/api/generation-jobs/{job_b_id}", None, "读 B 项目任务"),
            ("GET", f"/api/projects/{proj_b}/reviews", None, "读 B 项目审核"),
            ("GET", f"/api/admin/reviews/{rev_b_id}", None, "读 B 项目审核详情"),
            ("GET", f"/api/runs/{run_b}", None, "读 B 项目运行记录"),
            ("GET", f"/api/assets/{asset_b}/download", None, "下载 B 项目素材"),
            ("GET", f"/files/{asset_b}", None, "预览 B 项目素材"),
            ("POST", f"/api/projects/{proj_b}/generation-jobs", {}, "在 B 项目建任务"),
            ("POST", f"/api/projects/{proj_b}/canvas/export", None, "导出 B 项目画布"),
        ]
        for method, path, body, label in cross:
            st, _ = editor_a.req(method, path, body)
            expect = 403 if "403" in label else 404
            R.check(f"A 编辑{label} → {expect}", st == expect, f"got {st}")

        st, _ = org_a.req("DELETE", f"/api/projects/{proj_b}")
        R.check("A 组织管理员删 B 项目 → 404", st == 404, f"got {st}")
        st, _ = org_a.req("PATCH", f"/api/projects/{proj_b}", {"name": "改名"})
        R.check("A 组织管理员改 B 项目名 → 404", st == 404, f"got {st}")

        st, _ = org_a.req("PUT", "/api/admin/organizations/org_b/quota", {"quota_total": 1})
        R.check("A 组织管理员改 B 组织额度 → 404", st == 404, f"got {st}")
        st, _ = org_a.req("POST", "/api/admin/users/" + editor_b_id + "/disable", {})
        R.check("A 组织管理员禁用 B 组织成员 → 404", st == 404, f"got {st}")

        print("\n[4. 跨项目引用必须拒绝]")
        st, _ = editor_a.req("PUT", f"/api/projects/{proj_a}/brief", {
            "user_prompt": "越权引用", "purpose": "marketing_poster", "platform": "淘宝/天猫",
            "aspect_ratio": "1:1", "image_count": 1, "product_asset_ids": [asset_b]})
        R.check("A 项目 Brief 引用 B 项目素材 → 422", st == 422, f"got {st}")

        st, _ = editor_a.req("PUT", f"/api/projects/{proj_a}/canvas", {
            "width": 768, "height": 1024,
            "document": {"version": 1, "width": 768, "height": 1024,
                         "layers": [{"id": "l1", "kind": "background_bitmap", "asset_id": asset_b, "z_index": 0}]}})
        R.check("A 项目画布引用 B 项目素材 → 422", st == 422, f"got {st}")

        st, _ = editor_a.req("POST", f"/api/projects/{proj_a}/layout/export", {"base_asset_id": asset_b})
        R.check("A 项目排版导出用 B 项目底图 → 422", st == 422, f"got {st}")

        st, _ = editor_a.req("POST", f"/api/projects/{proj_a}/reviews", {"target_type": "canvas_output", "asset_id": asset_b})
        R.check("A 项目送审引用 B 项目素材 → 422", st == 422, f"got {st}")

        st, _ = editor_a.req("POST", "/api/image-tools/run", {"node_id": "node_not_exist", "intent": "改字", "asset_id": asset_b})
        R.check("图片工具引用越权素材 → 404", st == 404, f"got {st}")

        print("\n[5. 通知只能读自己的]")
        st, ntfs = editor_a.req("GET", "/api/creator/notifications")
        items = (ntfs or {}).get("items") if isinstance(ntfs, dict) else None
        R.check("A 编辑只看到自己的通知", st == 200 and items is not None and all(n["user_id"] == editor_a_id for n in items),
                str(ntfs)[:160])
        st, _ = editor_a.req("POST", "/api/creator/notifications/ntf_b/read", {})
        R.check("A 编辑标记 B 的通知为已读 → 404", st == 404, f"got {st}")
        st, _ = editor_a.req("POST", "/api/creator/notifications/read-all", {})
        R.check("全部标记已读 → 200（只影响自己）", st == 200, f"got {st}")
        rows = sql(inst.db_path, [("SELECT id,read FROM notifications WHERE id=?", ("ntf_b",))], fetch=True)[0]
        R.check("B 的通知未被 A 改成已读", rows and rows[0]["read"] == 0, str(rows))

        print("\n[6. 归属不明项目：组织级角色不可见，平台管理员可见并指定]")
        for label, c in [("A 编辑", editor_a), ("A 组织管理员", org_a)]:
            st, _ = c.req("GET", "/api/projects/prj_orphan")
            R.check(f"{label}读归属不明项目 → 404", st == 404, f"got {st}")
        lst = editor_a.req("GET", "/api/projects")[1]
        R.check("项目列表不含归属不明项目", isinstance(lst, list) and all(p["id"] != "prj_orphan" for p in lst), str(lst)[:160])

        st, unassigned = root.req("GET", "/api/admin/projects/unassigned")
        R.check("平台管理员可见归属不明清单 → 200", st == 200 and (unassigned or {}).get("count") == 1,
                f"got {st} {str(unassigned)[:200]}")
        st, _ = root.req("PUT", "/api/admin/projects/prj_orphan/organization", {"organization_id": "org_a", "reason": "确认归属"})
        R.check("平台管理员指定归属 → 200", st == 200, f"got {st}")
        st, _ = editor_a.req("GET", "/api/projects/prj_orphan")
        R.check("指定归属后 A 编辑可访问", st == 200, f"got {st}")

        print("\n[7. 角色与平台级能力边界]")
        st, _ = editor_a.req("GET", "/api/models")
        R.check("编辑可读模型清单 → 200", st == 200, f"got {st}")
        st, _ = editor_a.req("POST", "/api/models", {"model_id": "x", "modality": "image"})
        R.check("编辑新增模型 → 403", st == 403, f"got {st}")
        st, _ = org_a.req("PUT", "/api/admin/models/local-poster-compositor/pricing", {"unit_cost": 9})
        R.check("组织管理员改模型单价 → 403（平台级）", st == 403, f"got {st}")
        st, _ = org_a.req("GET", "/api/providers")
        R.check("组织管理员读服务商配置 → 403（平台级）", st == 403, f"got {st}")
        st, _ = root.req("PUT", "/api/admin/models/local-poster-compositor/pricing", {"unit_cost": 5})
        R.check("平台管理员改模型单价 → 200", st == 200, f"got {st}")
        st, _ = org_a.req("POST", "/api/admin/organizations", {"id": "org_c", "name": "C 公司"})
        R.check("组织管理员建新组织 → 403（平台级）", st == 403, f"got {st}")
        st, _ = editor_a.req("PUT", "/api/admin/organizations/org_a/quota", {"quota_total": 1})
        R.check("编辑改本组织额度 → 403（无额度权限）", st == 403, f"got {st}")
        st, _ = org_a.req("PUT", "/api/admin/organizations/org_a/quota", {"quota_total": 60000})
        R.check("组织管理员改本组织额度 → 200", st == 200, f"got {st}")

        print("\n[8. 合法同组织、同项目操作仍然成功]")
        st, _ = editor_a.req("GET", f"/api/projects/{proj_a}")
        R.check("A 编辑读本项目 → 200", st == 200, f"got {st}")
        st, _ = editor_a.req("GET", f"/api/projects/{proj_a}/assets")
        R.check("A 编辑读本项目素材 → 200", st == 200, f"got {st}")
        st, _ = editor_a.req("GET", f"/files/{asset_a}")
        R.check("A 编辑预览本项目素材 → 200", st == 200, f"got {st}")
        st, _ = editor_a.req("GET", f"/api/generation-jobs/{job_a_id}")
        R.check("A 编辑读本项目任务 → 200", st == 200, f"got {st}")
        st, _ = editor_a.req("PUT", f"/api/projects/{proj_a}/canvas", {
            "width": 768, "height": 1024,
            "document": {"version": 1, "width": 768, "height": 1024,
                         "layers": [{"id": "l1", "kind": "background_bitmap", "asset_id": asset_a, "z_index": 0}]}})
        R.check("A 编辑保存引用本项目素材的画布 → 200", st == 200, f"got {st}")
        st, _ = org_a.req("POST", f"/api/admin/reviews/{rev_a_id}/approve", {})
        R.check("A 组织管理员通过本项目审核 → 200", st == 200, f"got {st}")
        st, _ = editor_a.req("GET", f"/api/projects/{proj_a}/review-gate")
        R.check("A 编辑查看本项目审核门禁 → 200", st == 200, f"got {st}")
        st, all_cost = root.req("GET", "/api/admin/cost-summary")
        R.check("平台管理员读全平台成本 → 含两个组织合计 25.0",
                st == 200 and (all_cost or {}).get("total_cost") == 25.0, f"got {st} {str(all_cost)[:160]}")
        st, all_usage = root.req("GET", "/api/admin/usage-records")
        R.check("平台管理员用量明细含两家组织",
                st == 200 and isinstance(all_usage, list) and {u["id"] for u in all_usage} >= {"usg_a", "usg_b"},
                str(all_usage)[:160])

        print("\n[9. CSRF：会话修改请求必须同源]")
        st, _ = editor_b2.req("POST", "/api/projects", {"name": "无 Origin"}, headers={"Origin": None})
        R.check("带会话但无 Origin/Referer 的 POST → 403", st == 403, f"got {st}")
        st, _ = editor_b2.req("POST", "/api/projects", {"name": "跨站"}, headers={"Origin": "http://evil.example"})
        R.check("跨站 Origin 的 POST → 403", st == 403, f"got {st}")
        st, _ = editor_b2.req("POST", "/api/projects", {"name": "同源"}, headers={"Origin": base})
        R.check("同源 Origin 的 POST → 200/201", st in (200, 201), f"got {st}")
        st, _ = editor_b2.req("POST", "/api/projects", {"name": "伪造Referer"}, headers={"Origin": None, "Referer": "http://evil.example/x"})
        R.check("跨站 Referer 的 POST → 403", st == 403, f"got {st}")

        print("\n[10. 列表接口不返回全站数据（抽样复核）]")
        st, a_projects = editor_a.req("GET", "/api/projects")
        ids = {p["id"] for p in a_projects} if isinstance(a_projects, list) else set()
        R.check("A 编辑项目列表不含 B 项目", proj_b not in ids, str(ids))
        st, a_runs = editor_a.req("GET", "/api/runs")
        run_ids = {r["id"] for r in a_runs} if isinstance(a_runs, list) else set()
        R.check("A 编辑运行列表不含 B 运行", run_b not in run_ids, str(run_ids))
        st, a_usage = editor_a.req("GET", "/api/creator/usage-records")
        R.check("A 编辑个人用量记录只含自己", isinstance(a_usage, list) and all(u["project_id"] == proj_a for u in a_usage),
                str(a_usage)[:160])
        st, a_sum = editor_a.req("GET", "/api/creator/usage-summary")
        R.check("A 编辑额度视图标记为个人范围", st == 200 and (a_sum or {}).get("usage_scope") == "self",
                f"got {st} {str(a_sum)[:160]}")

    return R.summary("第 2 批组织与资源级授权验收")


if __name__ == "__main__":
    raise SystemExit(main())

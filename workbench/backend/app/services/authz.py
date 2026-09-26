"""对象级授权：先判权限（capability），再判归属（scope）。

规则约定（对应第 2 批图纸）：
- 缺少操作权限             → 403
- 目标不在自己可见作用域内  → 404（不告知资源是否存在）
- 归属不明的历史项目（organization_id 为空）对组织级角色一律不可见
- 平台级角色（super_admin / platform_admin / prompt_engineer）可见全部组织，
  其跨组织能力由 rbac 的角色作用域显式声明
- 归属一律从数据库重新查询，不接受客户端在请求体里附带的 project_id/organization_id
"""
from fastapi import HTTPException

from .. import db
from . import auth, rbac


# ---------------- 作用域 ----------------

def visible_org_ids(actor) -> list | None:
    """返回 None 表示平台级（全部组织可见）。"""
    if rbac.is_platform_role(actor.role):
        return None
    orgs = list(actor.org_ids or [])
    if not orgs and actor.organization_id:
        orgs = [actor.organization_id]
    return orgs


def org_scope_clause(actor, column: str = "organization_id") -> tuple:
    """返回 (SQL 片段, 参数列表)，用于把任意列表查询限定在可见组织内。"""
    orgs = visible_org_ids(actor)
    if orgs is None:
        return "", []
    if not orgs:
        return " AND 1=0", []
    return f" AND {column} IN ({','.join(['?'] * len(orgs))})", list(orgs)


def project_scope_clause(actor, column: str = "p.organization_id") -> tuple:
    return org_scope_clause(actor, column)


def in_scope(actor, organization_id) -> bool:
    orgs = visible_org_ids(actor)
    if orgs is None:
        return True
    if not organization_id:
        return False        # 归属不明 → 组织级角色不可见
    return organization_id in orgs


def not_found() -> HTTPException:
    return HTTPException(404, "资源不存在或无权访问")


def require_capability(actor, action: str) -> None:
    """只校验能力（不涉及具体对象），用于全局配置类接口。"""
    _require_permission(actor, action)


def require_admin_area(actor) -> None:
    """后台管理区基线校验：只有具备 admin 区域的角色可进入后台类接口。"""
    areas = rbac.role_def(actor.role).get("areas") or []
    if "admin" not in areas:
        raise HTTPException(403, f"当前角色（{actor.role}）不属于后台管理区")


def require_platform(actor, action: str) -> None:
    """平台级能力：除权限外，还要求角色作用域为 platform。"""
    _require_permission(actor, action)
    if not rbac.is_platform_role(actor.role):
        raise HTTPException(403, f"{action} 属于平台级能力，当前角色（{actor.role}）不可用")


def _require_permission(actor, action: str) -> None:
    if not actor.has(action):
        raise HTTPException(403, f"当前角色（{actor.role}）没有 {action} 权限")


# ---------------- 各资源对象 ----------------

def require_organization_permission(actor, organization_id: str, action: str) -> dict:
    _require_permission(actor, action)
    org = db.query_one("SELECT * FROM organizations WHERE id=?", (organization_id,))
    if not org or not in_scope(actor, organization_id):
        raise not_found()
    return org


def require_project_permission(actor, project_id: str, action: str) -> dict:
    _require_permission(actor, action)
    p = db.query_one("SELECT * FROM projects WHERE id=?", (project_id,))
    if not p or not in_scope(actor, p.get("organization_id")):
        raise not_found()
    return p


def require_project_optional(actor, project_id: str, action: str) -> dict | None:
    """软校验：项目不存在时返回 None（用于 404 语义更细的入口）。"""
    _require_permission(actor, action)
    p = db.query_one("SELECT * FROM projects WHERE id=?", (project_id,))
    if not p or not in_scope(actor, p.get("organization_id")):
        return None
    return p


def require_asset_permission(actor, asset_id: str, action: str) -> dict:
    """资产权限继承所属项目。"""
    _require_permission(actor, action)
    a = db.query_one("SELECT * FROM assets WHERE id=?", (asset_id,))
    if not a:
        raise not_found()
    p = db.query_one("SELECT id,organization_id FROM projects WHERE id=?", (a["project_id"],))
    if not p or not in_scope(actor, p.get("organization_id")):
        raise not_found()
    return {**a, "project_organization_id": p.get("organization_id")}


def require_job_permission(actor, job_id: str, action: str) -> dict:
    _require_permission(actor, action)
    j = db.query_one("SELECT * FROM generation_jobs WHERE id=?", (job_id,))
    if not j:
        raise not_found()
    p = db.query_one("SELECT id,organization_id FROM projects WHERE id=?", (j["project_id"],))
    if not p or not in_scope(actor, p.get("organization_id")):
        raise not_found()
    return {**j, "project_organization_id": p.get("organization_id")}


def require_review_permission(actor, review_id: str, action: str) -> dict:
    _require_permission(actor, action)
    r = db.query_one("SELECT * FROM reviews WHERE id=?", (review_id,))
    if not r:
        raise not_found()
    p = db.query_one("SELECT id,organization_id FROM projects WHERE id=?", (r["project_id"],))
    if not p or not in_scope(actor, p.get("organization_id")):
        raise not_found()
    return {**r, "project_organization_id": p.get("organization_id")}


def require_notification_permission(actor, notification_id: str, action: str = "notification.read") -> dict:
    """通知按 user_id 隔离：只能读/标记自己的通知。"""
    _require_permission(actor, action)
    n = db.query_one("SELECT * FROM notifications WHERE id=?", (notification_id,))
    if not n or n.get("user_id") != actor.user_id:
        raise not_found()
    return n


def require_graph_permission(actor, graph_id: str, action: str) -> dict:
    _require_permission(actor, action)
    g = db.query_one("SELECT * FROM graphs WHERE id=?", (graph_id,))
    if not g:
        raise not_found()
    p = db.query_one("SELECT * FROM projects WHERE id=?", (g["project_id"],))
    if not p or not in_scope(actor, p.get("organization_id")):
        raise not_found()
    return {**g, "project": p}


def require_node_permission(actor, node_id: str, action: str) -> dict:
    _require_permission(actor, action)
    n = db.query_one("SELECT * FROM nodes WHERE id=?", (node_id,))
    if not n:
        raise not_found()
    g = db.query_one("SELECT * FROM graphs WHERE id=?", (n["graph_id"],))
    if not g:
        raise not_found()
    p = db.query_one("SELECT * FROM projects WHERE id=?", (g["project_id"],))
    if not p or not in_scope(actor, p.get("organization_id")):
        raise not_found()
    return {**n, "project": p}


def require_run_permission(actor, run_id: str, action: str = "job.read") -> dict:
    _require_permission(actor, action)
    r = db.query_one("SELECT * FROM node_runs WHERE id=?", (run_id,))
    if not r:
        raise not_found()
    n = db.query_one("SELECT graph_id FROM nodes WHERE id=?", (r["node_id"],))
    g = db.query_one("SELECT project_id FROM graphs WHERE id=?", (n["graph_id"],)) if n else None
    p = db.query_one("SELECT * FROM projects WHERE id=?", (g["project_id"],)) if g else None
    if not p or not in_scope(actor, p.get("organization_id")):
        raise not_found()
    return {**r, "project": p}


def require_candidate_permission(actor, candidate_id: str, action: str) -> dict:
    _require_permission(actor, action)
    c = db.query_one("SELECT * FROM generation_candidates WHERE id=?", (candidate_id,))
    if not c:
        raise not_found()
    p = db.query_one("SELECT * FROM projects WHERE id=?", (c["project_id"],))
    if not p or not in_scope(actor, p.get("organization_id")):
        raise not_found()
    return {**c, "project": p}


def require_user_permission(actor, user_id: str, action: str = "member.manage") -> dict:
    """成员管理：组织级角色只能管理自己组织内的用户。"""
    _require_permission(actor, action)
    u = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not u:
        raise not_found()
    if rbac.is_platform_role(actor.role):
        return u
    orgs = set(actor.org_ids or [])
    member_orgs = {m["organization_id"] for m in
                   db.query("SELECT organization_id FROM memberships WHERE user_id=?", (user_id,))}
    member_orgs.add(u.get("organization_id"))
    if not (orgs & {o for o in member_orgs if o}):
        raise not_found()
    return u


def require_membership_permission(actor, membership_id: str, action: str = "member.manage") -> dict:
    _require_permission(actor, action)
    m = db.query_one("SELECT * FROM memberships WHERE id=?", (membership_id,))
    if not m or not in_scope(actor, m.get("organization_id")):
        raise not_found()
    return m


# ---------------- 跨项目引用校验 ----------------

def assert_assets_in_project(project_id: str, asset_ids: list, field: str = "asset") -> None:
    """校验素材属于指定项目，防止把 B 项目的图片用进 A 项目的任务。

    同属一个项目即通过；跨项目一律拒绝（不做"同组织即可共享"的放宽）。
    """
    for aid in asset_ids or []:
        if not aid:
            continue
        a = db.query_one("SELECT id,project_id FROM assets WHERE id=?", (aid,))
        if not a:
            raise HTTPException(404, f"素材不存在：{aid}")
        if a["project_id"] != project_id:
            raise HTTPException(422, f"素材 {aid} 不属于本项目，禁止跨项目引用（{field}）")

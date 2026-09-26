"""RBAC：角色目录、权限目录与权限校验。

角色口径以「图片生成平台前后台开发计划 2」为准（super_admin / platform_admin /
organization_admin / brand_admin / reviewer / editor / viewer），并保留历史别名
（creator / ops / org_admin / admin / prompt_engineer）以免旧登录态失效。

说明：本层是**软鉴权**。单机演示默认按 super_admin 放行；接入真实登录后，由
X-WB-Role 头或会话解析出角色，再由 require_permission 强制校验。
"""
from fastapi import Header, HTTPException

PERMISSIONS = [
    {"code": "project.create", "name": "创建项目", "area": "creator"},
    {"code": "project.export", "name": "导出成品", "area": "creator"},
    {"code": "asset.upload", "name": "上传素材", "area": "creator"},
    {"code": "generation.run", "name": "发起生图", "area": "creator"},
    {"code": "review.submit", "name": "提交审核", "area": "creator"},
    {"code": "review.decide", "name": "审核裁决（通过/退回/拒绝）", "area": "admin"},
    {"code": "prompt.manage", "name": "Prompt 库管理", "area": "admin"},
    {"code": "template.manage", "name": "模板管理", "area": "admin"},
    {"code": "model.manage", "name": "模型与服务商管理", "area": "admin"},
    {"code": "brand.manage", "name": "品牌资产管理", "area": "admin"},
    {"code": "usage.read", "name": "查看用量与成本", "area": "admin"},
    {"code": "org.quota.manage", "name": "组织额度管理", "area": "admin"},
    {"code": "member.manage", "name": "成员与角色管理", "area": "admin"},
    {"code": "audit.read", "name": "查看审计日志", "area": "admin"},
]

_ROLE_DEFS = [
    {"id": "super_admin", "name": "超级管理员", "areas": ["creator", "admin"], "aliases": [],
     "permissions": ["*"]},
    {"id": "platform_admin", "name": "平台管理员", "areas": ["admin"], "aliases": ["ops", "admin"],
     "permissions": ["usage.read", "org.quota.manage", "member.manage", "audit.read",
                     "model.manage", "template.manage", "prompt.manage", "brand.manage", "review.decide"]},
    {"id": "organization_admin", "name": "组织管理员", "areas": ["admin"], "aliases": ["org_admin"],
     "permissions": ["usage.read", "org.quota.manage", "member.manage", "audit.read", "brand.manage"]},
    {"id": "brand_admin", "name": "品牌管理员", "areas": ["creator", "admin"], "aliases": [],
     "permissions": ["project.create", "project.export", "asset.upload", "generation.run",
                     "review.submit", "brand.manage", "template.manage", "usage.read"]},
    {"id": "reviewer", "name": "审核者", "areas": ["admin"], "aliases": [],
     "permissions": ["review.decide", "review.submit", "usage.read", "audit.read"]},
    {"id": "prompt_engineer", "name": "Prompt 工程师", "areas": ["admin"], "aliases": [],
     "permissions": ["prompt.manage", "template.manage", "usage.read"]},
    {"id": "editor", "name": "编辑 / 创作者", "areas": ["creator"], "aliases": ["creator"],
     "permissions": ["project.create", "project.export", "asset.upload", "generation.run", "review.submit"]},
    {"id": "viewer", "name": "浏览者（只读）", "areas": ["creator"], "aliases": [],
     "permissions": []},
]

DEFAULT_ROLE = "super_admin"
ALL_CODES = [p["code"] for p in PERMISSIONS]


def roles() -> list:
    return [{**r, "permission_list": (ALL_CODES if r["permissions"] == ["*"] else r["permissions"])}
            for r in _ROLE_DEFS]


def resolve_role(role: str | None) -> str:
    """把历史别名归一化到规范角色名。"""
    if not role:
        return DEFAULT_ROLE
    r = (role or "").strip()
    for d in _ROLE_DEFS:
        if r == d["id"] or r in d["aliases"]:
            return d["id"]
    return DEFAULT_ROLE


def role_def(role: str | None) -> dict:
    rid = resolve_role(role)
    return next((r for r in _ROLE_DEFS if r["id"] == rid), _ROLE_DEFS[0])


def permissions_for(role: str | None) -> list:
    d = role_def(role)
    return ALL_CODES if d["permissions"] == ["*"] else list(d["permissions"])


def has_permission(role: str | None, perm: str) -> bool:
    perms = permissions_for(role)
    return "*" in perms or perm in perms


def require_permission(perm: str):
    """FastAPI 依赖：校验当前请求角色是否具备指定权限。"""
    def dep(x_wb_role: str | None = Header(None, alias="X-WB-Role")):
        if not has_permission(x_wb_role, perm):
            raise HTTPException(403, f"当前角色（{resolve_role(x_wb_role)}）没有 {perm} 权限")
        return resolve_role(x_wb_role)
    return dep

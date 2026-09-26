"""RBAC：角色目录、权限目录、作用域与纯判定函数。

角色口径以「图片生成平台前后台开发计划 2」为准，并保留历史别名
（creator / ops / org_admin / admin）以免旧登录态失效。

本模块只做**纯目录 + 纯判定**，不读请求、不碰数据库：
- 身份解析与权限依赖在 services/auth.py（身份只来自服务端会话）；
- 对象归属判定在 services/authz.py（按数据库真实归属逐个对象校验）。

作用域（scope）说明：
- platform     ：平台级角色，可见全部组织（跨组织能力在此显式列出）
- organization ：组织级角色，只能在自己所属组织范围内操作
"""

PERMISSIONS = [
    {"code": "project.create", "name": "创建项目", "area": "creator"},
    {"code": "project.read", "name": "查看项目", "area": "creator"},
    {"code": "project.write", "name": "编辑项目", "area": "creator"},
    {"code": "project.delete", "name": "删除项目", "area": "admin"},
    {"code": "project.export", "name": "导出成品", "area": "creator"},
    {"code": "asset.upload", "name": "上传素材", "area": "creator"},
    {"code": "asset.read", "name": "查看素材", "area": "creator"},
    {"code": "asset.delete", "name": "删除素材", "area": "admin"},
    {"code": "generation.run", "name": "发起生图", "area": "creator"},
    {"code": "job.read", "name": "查看生成任务", "area": "creator"},
    {"code": "review.submit", "name": "提交审核", "area": "creator"},
    {"code": "review.read", "name": "查看审核（项目内）", "area": "creator"},
    {"code": "review.queue", "name": "查看审核队列（后台审核中心）", "area": "admin"},
    {"code": "review.decide", "name": "审核裁决（通过/退回/拒绝）", "area": "admin"},
    {"code": "notification.read", "name": "查看自己的通知", "area": "creator"},
    {"code": "usage.read", "name": "查看用量与成本", "area": "admin"},
    {"code": "audit.read", "name": "查看审计日志", "area": "admin"},
    {"code": "org.read", "name": "查看组织信息", "area": "admin"},
    {"code": "org.quota.manage", "name": "组织额度管理", "area": "admin"},
    {"code": "member.manage", "name": "成员与角色管理", "area": "admin"},
    {"code": "brand.manage", "name": "品牌资产管理", "area": "admin"},
    {"code": "prompt.manage", "name": "Prompt 库管理", "area": "admin"},
    {"code": "template.manage", "name": "模板管理", "area": "admin"},
    {"code": "model.read", "name": "查看模型清单", "area": "creator"},
    {"code": "model.manage", "name": "模型与服务商管理", "area": "admin"},
    {"code": "provider.manage", "name": "服务商配置（含凭据）", "area": "admin"},
]

ALL_CODES = [p["code"] for p in PERMISSIONS]

CREATOR_READ = ["project.read", "asset.read", "job.read", "review.read", "notification.read", "model.read"]
# 后台管理区角色（组织级）：能看本组织范围内的审核队列、组织信息、审计（审计另配）
ORG_ADMIN_READ = CREATOR_READ + ["review.queue", "org.read"]

_ROLE_DEFS = [
    {
        "id": "super_admin", "name": "超级管理员", "scope": "platform",
        "areas": ["creator", "admin"], "aliases": [],
        "permissions": ["*"],
    },
    {
        "id": "platform_admin", "name": "平台管理员", "scope": "platform",
        "areas": ["admin"], "aliases": ["ops", "admin"],
        "permissions": [p["code"] for p in PERMISSIONS if p["code"] != "brand.manage"],
    },
    {
        "id": "prompt_engineer", "name": "Prompt 工程师", "scope": "platform",
        "areas": ["admin"], "aliases": [],
        "permissions": CREATOR_READ + ["prompt.manage", "template.manage", "usage.read", "review.queue", "org.read", "audit.read"],
    },
    {
        "id": "organization_admin", "name": "组织管理员", "scope": "organization",
        "areas": ["admin"], "aliases": ["org_admin"],
        "permissions": ORG_ADMIN_READ + [
            "project.create", "project.write", "project.delete", "project.export",
            "asset.upload", "asset.delete", "generation.run", "review.submit", "review.decide",
            "usage.read", "audit.read", "org.quota.manage", "member.manage",
            "brand.manage", "template.manage",
        ],
    },
    {
        "id": "brand_admin", "name": "品牌管理员", "scope": "organization",
        "areas": ["creator", "admin"], "aliases": [],
        "permissions": ORG_ADMIN_READ + [
            "project.create", "project.write", "project.export",
            "asset.upload", "asset.delete", "generation.run", "review.submit",
            "usage.read", "brand.manage", "template.manage",
        ],
    },
    {
        "id": "reviewer", "name": "审核者", "scope": "organization",
        "areas": ["admin"], "aliases": [],
        "permissions": ORG_ADMIN_READ + ["review.submit", "review.decide", "usage.read", "audit.read"],
    },
    {
        "id": "editor", "name": "编辑 / 创作者", "scope": "organization",
        "areas": ["creator"], "aliases": ["creator"],
        "permissions": CREATOR_READ + [
            "project.create", "project.write", "project.export",
            "asset.upload", "asset.delete", "generation.run", "review.submit",
        ],
    },
    {
        "id": "viewer", "name": "浏览者（只读）", "scope": "organization",
        "areas": ["creator"], "aliases": [],
        "permissions": CREATOR_READ,
    },
]

DEFAULT_ROLE = "viewer"
PLATFORM_SCOPE = "platform"
ORG_SCOPE = "organization"


def roles() -> list:
    """角色目录（含作用域与展开后的权限清单），供后台展示与前端对齐。"""
    out = []
    for r in _ROLE_DEFS:
        perms = ALL_CODES if r["permissions"] == ["*"] else list(r["permissions"])
        out.append({**r, "permission_list": perms})
    return out


def resolve_role(role: str | None) -> str:
    """把历史别名归一化到规范角色名；未知角色一律降级为 viewer（默认拒绝）。"""
    if not role:
        return DEFAULT_ROLE
    r = (role or "").strip()
    for d in _ROLE_DEFS:
        if r == d["id"] or r in d["aliases"]:
            return d["id"]
    return DEFAULT_ROLE


def role_def(role: str | None) -> dict:
    rid = resolve_role(role)
    return next((r for r in _ROLE_DEFS if r["id"] == rid), _ROLE_DEFS[-1])


def role_scope(role: str | None) -> str:
    return role_def(role)["scope"]


def is_platform_role(role: str | None) -> bool:
    """平台级角色拥有显式列出的跨组织能力。"""
    return role_scope(role) == PLATFORM_SCOPE


def permissions_for(role: str | None) -> list:
    d = role_def(role)
    return ALL_CODES if d["permissions"] == ["*"] else list(d["permissions"])


def has_permission(role: str | None, perm: str) -> bool:
    d = role_def(role)
    return d["permissions"] == ["*"] or perm in d["permissions"]

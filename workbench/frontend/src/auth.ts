export type Role =
  | 'super_admin' | 'platform_admin' | 'organization_admin' | 'brand_admin'
  | 'reviewer' | 'prompt_engineer' | 'editor' | 'viewer'

export type Area = 'creator' | 'admin'

export interface RoleDef { id: Role; name: string; areas: Area[] }

/** 与后端 services/rbac.py 的角色目录保持一致；角色由服务端下发，前端只负责展示。 */
export const ROLES: RoleDef[] = [
  { id: 'super_admin', name: '超级管理员', areas: ['creator', 'admin'] },
  { id: 'platform_admin', name: '平台管理员', areas: ['admin'] },
  { id: 'organization_admin', name: '组织管理员', areas: ['admin'] },
  { id: 'brand_admin', name: '品牌管理员', areas: ['creator', 'admin'] },
  { id: 'reviewer', name: '审核者', areas: ['admin'] },
  { id: 'prompt_engineer', name: 'Prompt 工程师', areas: ['admin'] },
  { id: 'editor', name: '编辑 / 创作者', areas: ['creator'] },
  { id: 'viewer', name: '浏览者（只读）', areas: ['creator'] },
]

/** 历史别名 → 规范角色（后端同样归一化，这里只为了前端展示不丢区域）。 */
const ALIASES: Record<string, Role> = {
  creator: 'editor',
  ops: 'platform_admin',
  admin: 'platform_admin',
  org_admin: 'organization_admin',
}

export function canonicalRole(role: string | null | undefined): Role | null {
  if (!role) return null
  if (ROLES.some((r) => r.id === role)) return role as Role
  return ALIASES[role] || null
}

export function canArea(role: string | null, area: Area): boolean {
  const r = ROLES.find((x) => x.id === canonicalRole(role))
  return !!r && r.areas.includes(area)
}

export function roleName(role: string | null): string {
  return ROLES.find((x) => x.id === canonicalRole(role))?.name || '未登录'
}

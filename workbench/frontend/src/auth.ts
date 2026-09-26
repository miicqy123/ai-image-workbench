export type Role = 'creator' | 'viewer' | 'brand_admin' | 'reviewer' | 'prompt_engineer' | 'ops' | 'org_admin' | 'super_admin'
export type Area = 'creator' | 'admin'

export interface RoleDef { id: Role; name: string; areas: Area[] }

export const ROLES: RoleDef[] = [
  { id: 'creator', name: '编辑 / 创作者', areas: ['creator'] },
  { id: 'viewer', name: '浏览者（只读）', areas: ['creator'] },
  { id: 'brand_admin', name: '品牌管理员', areas: ['creator', 'admin'] },
  { id: 'reviewer', name: '审核者', areas: ['admin'] },
  { id: 'prompt_engineer', name: 'Prompt 工程师', areas: ['admin'] },
  { id: 'ops', name: '平台运营', areas: ['admin'] },
  { id: 'org_admin', name: '组织管理员', areas: ['admin'] },
  { id: 'super_admin', name: '超级管理员', areas: ['creator', 'admin'] },
]

export function canArea(role: Role | null, area: Area): boolean {
  if (!role) return false
  const r = ROLES.find((x) => x.id === role)
  return !!r && r.areas.includes(area)
}

export function roleName(role: Role | null): string {
  return ROLES.find((x) => x.id === role)?.name || '未登录'
}

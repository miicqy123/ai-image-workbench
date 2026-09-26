import React, { useState } from 'react'
import { Role, ROLES } from './auth'

export default function LoginPage({ onLogin }: { onLogin: (role: Role) => void }) {
  const [role, setRole] = useState<Role>('creator')
  return (
    <div className="login-wrap">
      <div className="login-card">
        <h1>AI 生图工作台</h1>
        <p className="muted">选择一个角色进入（本地开发模式，暂未接真实鉴权）</p>
        <div className="field">
          <label>角色</label>
          <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {ROLES.map((r) => <option key={r.id} value={r.id}>{r.name}（{r.areas.map((a) => a === 'creator' ? '前台' : '后台').join(' + ')}）</option>)}
          </select>
        </div>
        <button className="btn primary" style={{ width: '100%' }} onClick={() => onLogin(role)}>进入工作台</button>
      </div>
    </div>
  )
}

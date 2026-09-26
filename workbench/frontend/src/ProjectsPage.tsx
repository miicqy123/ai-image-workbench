import React, { useEffect, useState } from 'react'
import { api } from './api'

const L: Record<string, string> = { draft: '草稿', generating: '生成中', completed: '已完成', archived: '已归档' }
const FILTERS = [['all', '全部'], ['draft', '草稿'], ['generating', '生成中'], ['completed', '已完成'], ['archived', '已归档']] as const

export default function ProjectsPage({ onOpen }: { onOpen: () => void }) {
  const [d, setD] = useState<any>(null)
  const [q, setQ] = useState('')
  const [f, setF] = useState('all')
  const load = () => api.creatorDashboard().then(setD).catch(() => setD(null))
  useEffect(() => { load() }, [])
  const items = (d?.projects || []).filter((p: any) => (f === 'all' ? true : p.derived_status === f) && (!q || p.name.includes(q)))
  return (
    <div className="page">
      <header className="page-head"><h1>我的项目</h1></header>
      <div className="proj-toolbar">
        <input placeholder="搜索项目名称" value={q} onChange={(e) => setQ(e.target.value)} />
        {FILTERS.map(([k, label]) => <button key={k} className={`tag-chip ${f === k ? 'on' : ''}`} onClick={() => setF(k)}>{label}</button>)}
      </div>
      <table className="admin-table">
        <thead><tr><th>名称</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody>
          {items.map((p: any) => (
            <tr key={p.id}>
              <td>{p.name}</td>
              <td><span className={`run-tag run-${p.derived_status}`}>{L[p.derived_status] || p.derived_status}</span></td>
              <td>{new Date(p.created_at * 1000).toLocaleString()}</td>
              <td className="proj-actions">
                <button className="btn" onClick={onOpen}>打开</button>
                <button className="btn" onClick={async () => { await api.duplicateProject(p.id); load() }}>复制</button>
                {p.derived_status === 'archived'
                  ? <button className="btn" onClick={async () => { await api.restoreProject(p.id); load() }}>恢复</button>
                  : <button className="btn" onClick={async () => { await api.archiveProject(p.id); load() }}>归档</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!items.length && <div className="muted" style={{ padding: 12 }}>没有匹配的项目。</div>}
    </div>
  )
}

import React, { useEffect, useState } from 'react'
import { api } from './api'
import ModelManager from './ModelManager'
import { fileUrl } from './api'

interface Stats { projects: number; assets: number; models: number; providers: number; runs: number; audits: number }

const TABS = [
  { key: 'projects', label: '项目' },
  { key: 'runs', label: '运行记录' },
  { key: 'audit', label: '审计日志' },
  { key: 'assets', label: '素材' },
  { key: 'models', label: '模型 / 服务商' },
] as const

export default function Admin({ onBack }: { onBack: () => void }) {
  const [stats, setStats] = useState<Stats | null>(null)
  const [projects, setProjects] = useState<any[]>([])
  const [runs, setRuns] = useState<any[]>([])
  const [audits, setAudits] = useState<any[]>([])
  const [models, setModels] = useState<any[]>([])
  const [providers, setProviders] = useState<any[]>([])
  const [assets, setAssets] = useState<any[]>([])
  const [tab, setTab] = useState<'projects' | 'runs' | 'audit' | 'models' | 'assets'>('projects')
  const [showModels, setShowModels] = useState(false)

  const refresh = async () => {
    setStats(await api.adminStats())
    setProjects(await api.listProjects())
    setRuns(await api.listRuns())
    setAudits(await api.listAudit())
    setModels(await api.listModels())
    setProviders(await api.listProviders())
    setAssets(await api.listAllAssets())
  }
  useEffect(() => { refresh() }, [])

  const statCards = stats ? [
    { label: '项目', n: stats.projects }, { label: '素材', n: stats.assets }, { label: '模型', n: stats.models },
    { label: '服务商', n: stats.providers }, { label: '运行', n: stats.runs }, { label: '审计', n: stats.audits },
  ] : []

  return (
    <div className="admin">
      <header className="admin-head">
        <div><h1>后台管理</h1><p className="muted">项目 · 素材 · 模型 · 运行 · 审计</p></div>
        <button className="btn ghost" onClick={onBack}>← 返回</button>
      </header>

      <div className="admin-stats">
        {statCards.map((s) => (
          <div className="stat-card" key={s.label}><div className="stat-num">{s.n}</div><div className="stat-label">{s.label}</div></div>
        ))}
      </div>

      <div className="admin-tabs">
        {TABS.map((t) => <button key={t.key} className={`tag-chip ${tab === t.key ? 'on' : ''}`} onClick={() => setTab(t.key)}>{t.label}</button>)}
      </div>

      <div className="admin-body">
        {tab === 'projects' && (
          <table className="admin-table">
            <thead><tr><th>名称</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody>
              {projects.map((p) => (
                <tr key={p.id}>
                  <td>{p.name}</td><td>{p.status}</td>
                  <td>{new Date(p.created_at * 1000).toLocaleString()}</td>
                  <td><button className="btn danger" onClick={async () => { if (confirm(`删除项目「${p.name}」？`)) { await api.deleteProject(p.id); refresh() } }}>删除</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {tab === 'runs' && (
          <table className="admin-table">
            <thead><tr><th>运行 ID</th><th>节点</th><th>模型</th><th>状态</th><th>开始时间</th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.id}</td><td className="mono">{r.node_id}</td><td>{r.model_id}</td>
                  <td><span className={`run-tag run-${r.status}`}>{r.status}</span></td>
                  <td>{r.started_at ? new Date(r.started_at * 1000).toLocaleString() : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {tab === 'audit' && (
          <table className="admin-table">
            <thead><tr><th>时间</th><th>动作</th><th>实体</th><th>版本</th></tr></thead>
            <tbody>
              {audits.map((a) => (
                <tr key={a.id}>
                  <td>{new Date(a.time * 1000).toLocaleString()}</td><td>{a.action}</td><td className="mono">{a.entity_id}</td>
                  <td>{a.before_version} → {a.after_version}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {tab === 'assets' && (
          <div className="asset-overview">
            <div className="admin-models-head"><h2>素材（{assets.length}）</h2></div>
            <div className="asset-grid">
              {assets.map((a) => (
                <div className="asset-tile" key={a.id}>
                  <img src={fileUrl(a.id)} alt="" />
                  <div className="asset-tile-meta">{a.role} · {a.width}×{a.height}</div>
                  <button className="btn danger" onClick={async () => { if (confirm('删除该素材？')) { await api.deleteAsset(a.id); refresh() } }}>删除</button>
                </div>
              ))}
            </div>
          </div>
        )}

        {tab === 'models' && (
          <div className="admin-models">
            <div className="admin-models-head">
              <h2>模型（{models.length}）</h2>
              <button className="btn primary" onClick={() => setShowModels(true)}>模型 / 服务商管理</button>
            </div>
            <table className="admin-table">
              <thead><tr><th>模型 ID</th><th>类型</th><th>适配器</th><th>服务商</th><th>状态</th></tr></thead>
              <tbody>
                {models.map((m) => (
                  <tr key={m.model_id}>
                    <td>{m.model_id}</td><td>{m.modality}</td><td>{m.adapter || '内置'}</td>
                    <td>{providers.find((p) => p.id === m.provider_id)?.name || m.provider}</td>
                    <td><span className={`run-tag ${m.enabled ? 'run-succeeded' : 'run-failed'}`}>{m.enabled ? '已上线' : '已下线'}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <h2 style={{ marginTop: 16 }}>服务商（{providers.length}）</h2>
            <table className="admin-table">
              <thead><tr><th>名称</th><th>Base URL</th><th>API Key</th></tr></thead>
              <tbody>
                {providers.map((p) => (
                  <tr key={p.id}>
                    <td>{p.name}</td><td className="mono">{p.base_url || '（未配置）'}</td>
                    <td>{p.api_key ? '已填写' : '未填写'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {showModels && <ModelManager models={models} onClose={() => setShowModels(false)} onChanged={refresh} showToast={() => {}} />}
    </div>
  )
}

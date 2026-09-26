import React, { useEffect, useState } from 'react'
import { api } from './api'
import ModelManager from './ModelManager'
import { fileUrl } from './api'

interface Stats { projects: number; assets: number; models: number; providers: number; runs: number; audits: number; usage_calls?: number; total_cost?: number }

const TABS = [
  { key: 'projects', label: '项目' },
  { key: 'runs', label: '运行记录' },
  { key: 'audit', label: '审计日志' },
  { key: 'assets', label: '素材' },
  { key: 'templates', label: '模板中心' },
  { key: 'prompts', label: 'Prompt 管理' },
  { key: 'models', label: '模型 / 服务商' },
  { key: 'usage', label: '用量与成本' },
  { key: 'quota', label: '额度与组织' },
] as const

export default function Admin({ onBack }: { onBack: () => void }) {
  const [stats, setStats] = useState<Stats | null>(null)
  const [projects, setProjects] = useState<any[]>([])
  const [runs, setRuns] = useState<any[]>([])
  const [audits, setAudits] = useState<any[]>([])
  const [models, setModels] = useState<any[]>([])
  const [providers, setProviders] = useState<any[]>([])
  const [assets, setAssets] = useState<any[]>([])
  const [templates, setTemplates] = useState<any[]>([])
  const [prompts, setPrompts] = useState<any[]>([])
  const [usage, setUsage] = useState<any[]>([])
  const [cost, setCost] = useState<any>(null)
  const [orgs, setOrgs] = useState<any[]>([])
  const [ledger, setLedger] = useState<any[]>([])
  const [tab, setTab] = useState<'projects' | 'runs' | 'audit' | 'models' | 'assets' | 'templates' | 'prompts' | 'usage' | 'quota'>('projects')
  const [showModels, setShowModels] = useState(false)

  const refresh = async () => {
    setStats(await api.adminStats())
    setProjects(await api.listProjects())
    setRuns(await api.listRuns())
    setAudits(await api.listAudit())
    setModels(await api.listModels())
    setProviders(await api.listProviders())
    setAssets(await api.listAllAssets())
    setTemplates(await api.adminTemplates())
    setPrompts(await api.listPromptTemplates())
    setUsage(await api.adminUsageRecords('?limit=200'))
    setCost(await api.adminCostSummary(30))
    setOrgs(await api.adminOrganizations())
    setLedger(await api.adminCreditLedger())
  }
  useEffect(() => { refresh() }, [])

  const statCards = stats ? [
    { label: '项目', n: stats.projects }, { label: '素材', n: stats.assets }, { label: '模型', n: stats.models },
    { label: '服务商', n: stats.providers }, { label: '运行', n: stats.runs }, { label: '审计', n: stats.audits },
    { label: '计费调用', n: stats.usage_calls || 0 }, { label: '累计成本', n: stats.total_cost || 0 },
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

        {tab === 'templates' && (
          <div>
            <div className="admin-models-head"><h2>模板中心（{templates.length}）</h2>
              <button className="btn primary" onClick={async () => { const name = prompt('模板名称'); if (!name) return; const l2 = prompt('二级分类（如 营销｜品牌海报）') || ''; const sk = prompt('工作流骨架 poster/long/detail/cover/illustration', 'poster') || 'poster'; await api.createTemplate({ name, level2: l2, skeleton: sk }); refresh() }}>+ 新增模板</button>
            </div>
            <table className="admin-table">
              <thead><tr><th>名称</th><th>一级</th><th>二级</th><th>骨架</th><th>比例</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>{templates.map((t) => (
                <tr key={t.id}><td>{t.name}</td><td>{t.level1}</td><td>{t.level2}</td><td>{t.skeleton}</td><td>{t.aspect}</td>
                  <td>{t.enabled ? '启用' : '停用'}</td>
                  <td><button className="btn danger" onClick={async () => { if (confirm('删除该模板？')) { await api.deleteTemplate(t.id); refresh() } }}>删除</button></td></tr>
              ))}</tbody>
            </table>
          </div>
        )}

        {tab === 'prompts' && (
          <div>
            <div className="admin-models-head"><h2>Prompt 管理（{prompts.length}）</h2>
              <button className="btn primary" onClick={async () => { const name = prompt('Prompt 名称'); if (!name) return; const cat = prompt('分类') || ''; const text = prompt('Prompt 模板正文') || ''; await api.createPromptTemplate({ name, category: cat, template_text: text }); refresh() }}>+ 新增 Prompt</button>
            </div>
            <table className="admin-table">
              <thead><tr><th>名称</th><th>分类</th><th>模板正文</th><th>操作</th></tr></thead>
              <tbody>{prompts.map((p) => (
                <tr key={p.id}><td>{p.name}</td><td>{p.category}</td><td className="mono">{String(p.template_text).slice(0, 60)}</td>
                  <td><button className="btn danger" onClick={async () => { if (confirm('删除该 Prompt？')) { await api.deletePromptTemplate(p.id); refresh() } }}>删除</button></td></tr>
              ))}</tbody>
            </table>
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
                    <td>{p.has_api_key ? '已填写' : '未填写'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {tab === 'usage' && (
          <div>
            <div className="admin-models-head">
              <h2>用量与成本（近 30 天）</h2>
              <button className="btn" onClick={async () => { setUsage(await api.adminUsageRecords('?limit=200')); setCost(await api.adminCostSummary(30)) }}>刷新</button>
            </div>
            {cost && (
              <div className="stat-row">
                <div className="stat-card"><div className="stat-num">{cost.calls}</div><div className="stat-label">调用次数</div></div>
                <div className="stat-card"><div className="stat-num">{cost.succeeded}</div><div className="stat-label">成功</div></div>
                <div className="stat-card"><div className="stat-num">{cost.failed}</div><div className="stat-label">失败</div></div>
                <div className="stat-card"><div className="stat-num">{cost.images}</div><div className="stat-label">产出图片</div></div>
                <div className="stat-card"><div className="stat-num">{cost.total_cost}</div><div className="stat-label">累计成本（点）</div></div>
                <div className="stat-card"><div className="stat-num">{cost.avg_cost}</div><div className="stat-label">平均单次成本</div></div>
              </div>
            )}
            <h2 style={{ marginTop: 16 }}>按模型</h2>
            <table className="admin-table">
              <thead><tr><th>模型</th><th>调用</th><th>失败</th><th>产出图片</th><th>成本</th><th>单价</th><th>操作</th></tr></thead>
              <tbody>
                {(cost?.by_model || []).map((m: any) => {
                  const info = models.find((x: any) => x.model_id === m.model_id)
                  return (
                    <tr key={m.model_id}>
                      <td>{m.model_id}</td><td>{m.calls}</td><td>{m.failed}</td><td>{m.images}</td>
                      <td>{m.cost}</td><td>{info?.unit_cost ?? 0}</td>
                      <td><button className="btn" onClick={async () => {
                        const v = prompt(`「${m.model_id}」计费单价（每个产出单位）`, String(info?.unit_cost ?? 0))
                        if (v === null) return
                        const n = Number(v)
                        if (Number.isNaN(n) || n < 0) { alert('请输入不小于 0 的数字'); return }
                        await api.setModelPricing(m.model_id, n); refresh()
                      }}>设置单价</button></td>
                    </tr>
                  )
                })}
                {!(cost?.by_model || []).length && <tr><td colSpan={7} className="muted">近 30 天还没有计费调用。</td></tr>}
              </tbody>
            </table>
            <h2 style={{ marginTop: 16 }}>最近调用记录（{usage.length}）</h2>
            <table className="admin-table">
              <thead><tr><th>时间</th><th>项目</th><th>模型</th><th>任务</th><th>输出图</th><th>耗时</th><th>成本</th><th>状态</th></tr></thead>
              <tbody>
                {usage.map((u) => (
                  <tr key={u.id}>
                    <td>{new Date(u.created_at * 1000).toLocaleString()}</td>
                    <td className="mono">{u.project_id}</td><td>{u.model_id}</td><td>{u.task_type}</td>
                    <td>{u.output_images}</td><td>{u.duration_ms != null ? `${u.duration_ms} ms` : ''}</td>
                    <td>{u.cost}</td>
                    <td><span className={`run-tag run-${u.status}`}>{u.status}</span></td>
                  </tr>
                ))}
                {!usage.length && <tr><td colSpan={8} className="muted">还没有用量记录。</td></tr>}
              </tbody>
            </table>
          </div>
        )}

        {tab === 'quota' && (
          <div>
            <div className="admin-models-head"><h2>组织额度（{orgs.length}）</h2></div>
            <table className="admin-table">
              <thead><tr><th>组织</th><th>套餐</th><th>余额</th><th>总额度</th><th>单日限额</th><th>操作</th></tr></thead>
              <tbody>
                {orgs.map((o) => (
                  <tr key={o.id}>
                    <td>{o.name}<div className="muted mono">{o.id}</div></td>
                    <td>{o.plan}</td><td>{o.credit_balance}</td><td>{o.quota_total}</td>
                    <td>{o.daily_limit ? o.daily_limit : '不限额'}</td>
                    <td>
                      <button className="btn" onClick={async () => {
                        const plan = prompt('套餐名称', o.plan || 'standard')
                        if (plan === null) return
                        const total = prompt('总额度', String(o.quota_total))
                        if (total === null) return
                        const daily = prompt('单日限额（0 表示不限额）', String(o.daily_limit || 0))
                        if (daily === null) return
                        await api.updateOrgQuota(o.id, { plan, quota_total: Number(total), daily_limit: Number(daily) })
                        refresh()
                      }}>编辑额度</button>
                      <button className="btn" onClick={async () => {
                        const v = prompt(`给「${o.name}」补发额度`, '1000')
                        if (v === null) return
                        await api.grantCredits({ organization_id: o.id, amount: Number(v), reason: '后台人工补发' })
                        refresh()
                      }}>补发</button>
                      <button className="btn" onClick={async () => {
                        const v = prompt(`从「${o.name}」扣除额度`, '100')
                        if (v === null) return
                        await api.deductCredits({ organization_id: o.id, amount: Number(v), reason: '后台人工扣除' })
                        refresh()
                      }}>扣除</button>
                      <button className="btn" onClick={async () => {
                        const v = prompt(`给「${o.name}」返还额度`, '100')
                        if (v === null) return
                        await api.refundCredits({ organization_id: o.id, amount: Number(v), reason: '失败任务返还' })
                        refresh()
                      }}>退款</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <h2 style={{ marginTop: 16 }}>额度流水（{ledger.length}）</h2>
            <table className="admin-table">
              <thead><tr><th>时间</th><th>组织</th><th>变动</th><th>变动后余额</th><th>原因</th><th>操作人</th></tr></thead>
              <tbody>
                {ledger.map((l) => (
                  <tr key={l.id}>
                    <td>{new Date(l.created_at * 1000).toLocaleString()}</td>
                    <td className="mono">{l.organization_id}</td>
                    <td>{l.change > 0 ? `+${l.change}` : l.change}</td>
                    <td>{l.balance_after}</td><td>{l.reason}</td><td>{l.operator_id}</td>
                  </tr>
                ))}
                {!ledger.length && <tr><td colSpan={6} className="muted">还没有额度流水。</td></tr>}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {showModels && <ModelManager models={models} onClose={() => setShowModels(false)} onChanged={refresh} showToast={() => {}} />}
    </div>
  )
}

import React, { useEffect, useState } from 'react'
import { api } from './api'

const QUICK = ['商品广告图', '小红书封面', '视频封面', '电商主图', '品牌海报', '活动海报', '信息图', '自定义创作']
const L: Record<string, string> = { draft: '草稿', generating: '生成中', completed: '已完成', archived: '已归档', active: '进行中' }

export default function HomePage({ onQuickCreate, onProjects }: { onQuickCreate: () => void; onProjects: () => void }) {
  const [d, setD] = useState<any>(null)
  const [u, setU] = useState<any>(null)
  const [ntf, setNtf] = useState<any>(null)
  useEffect(() => { api.creatorDashboard().then(setD).catch(() => setD(null)) }, [])
  useEffect(() => { api.creatorUsageSummary().then(setU).catch(() => setU(null)) }, [])
  useEffect(() => { api.notifications().then(setNtf).catch(() => setNtf(null)) }, [])
  return (
    <div className="page">
      <header className="page-head"><h1>我的工作台</h1><button className="btn primary" onClick={onQuickCreate}>+ 新建创作</button></header>
      <div className="stat-row">
        <div className="stat-card"><div className="stat-num">{d?.count || 0}</div><div className="stat-label">项目总数</div></div>
        {['draft', 'generating', 'completed', 'archived'].map((s) => (
          <div className="stat-card" key={s}><div className="stat-num">{d?.by_status?.[s] || 0}</div><div className="stat-label">{L[s]}</div></div>
        ))}
      </div>
      <h2 className="page-sub">额度与用量</h2>
      <div className="stat-row">
        <div className="stat-card"><div className="stat-num">{u?.balance ?? '—'}</div><div className="stat-label">剩余额度</div></div>
        <div className="stat-card"><div className="stat-num">{u?.quota_total ?? '—'}</div><div className="stat-label">总额度</div></div>
        <div className="stat-card"><div className="stat-num">{u?.used_this_month ?? '—'}</div><div className="stat-label">本月消耗</div></div>
        <div className="stat-card"><div className="stat-num">{u?.calls_this_month ?? 0}</div><div className="stat-label">本月调用</div></div>
        <div className="stat-card"><div className="stat-num">{u?.images_this_month ?? 0}</div><div className="stat-label">本月出图</div></div>
      </div>
      <h2 className="page-sub">快速创建</h2>
      <div className="quick-grid">{QUICK.map((q) => <button key={q} className="quick-card" onClick={onQuickCreate}>{q}</button>)}</div>
      <h2 className="page-sub">
        通知{ntf?.unread ? `（${ntf.unread} 条未读）` : ''}
        {ntf?.unread ? <button className="btn" onClick={async () => { await api.readAllNotifications(); setNtf(await api.notifications()) }}>全部标记已读</button> : null}
      </h2>
      <div className="proj-list">
        {(ntf?.items || []).slice(0, 5).map((n: any) => (
          <div className="proj-row" key={n.id}>
            <span className={`run-tag run-${n.type === 'review' ? 'running' : 'succeeded'}`}>{n.type === 'review' ? '审核' : '任务'}</span>
            <span className="proj-row-name">{n.title}</span>
            <span className="muted">{n.body}</span>
            {!n.read && <button className="btn" onClick={async () => { await api.readNotification(n.id); setNtf(await api.notifications()) }}>标记已读</button>}
          </div>
        ))}
        {!(ntf?.items || []).length && <div className="muted">暂无通知。</div>}
      </div>
      <h2 className="page-sub">最近项目 <button className="btn" onClick={onProjects}>全部项目</button></h2>
      <div className="proj-list">
        {(d?.recent || []).map((p: any) => (
          <div className="proj-row" key={p.id}><span className="proj-row-name">{p.name}</span><span className={`run-tag run-${p.derived_status}`}>{L[p.derived_status]}</span><button className="btn" onClick={onQuickCreate}>打开</button></div>
        ))}
        {!d?.recent?.length && <div className="muted">还没有项目，点「新建创作」开始。</div>}
      </div>
    </div>
  )
}

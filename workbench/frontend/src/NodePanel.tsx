import React, { useState } from 'react'
import { api, fileUrl } from './api'
import { Asset, ModelInfo, WBNode } from './types'

interface Props {
  node: WBNode
  pid: string
  assets: Asset[]
  models: ModelInfo[]
  defaultTextModel?: string
  defaultImageModel?: string
  onRun: (nid: string, model_id?: string, params?: any) => void
  onSaveFacts: (content: any) => void
  onPatch: (content: any) => void
  onRefresh: () => void
  showToast: (m: string) => void
}

function StringList({ label, items, onChange, placeholder }: { label: string; items: string[]; onChange: (v: string[]) => void; placeholder: string }) {
  const [val, setVal] = useState('')
  return (
    <div className="field">
      <label>{label}（每行一条）</label>
      <textarea value={items.join('\n')} onChange={(e) => onChange(e.target.value.split('\n').map((s) => s.trim()).filter(Boolean))} />
      <input style={{ marginTop: 4 }} placeholder={placeholder} value={val}
        onChange={(e) => setVal(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && val.trim()) { onChange([...items, val.trim()]); setVal('') } }} />
    </div>
  )
}

export default function NodePanel({ node, pid, assets, models, defaultTextModel, defaultImageModel, onRun, onSaveFacts, onPatch, onRefresh, showToast }: Props) {
  const c = node.content || {}
  const [facts, setFacts] = useState(c)
  const [briefDraft, setBriefDraft] = useState(c.task_brief || { goal: '', channel: '', audience: '' })
  const [layoutTitle, setLayoutTitle] = useState(c.title || '')
  const [layoutSub, setLayoutSub] = useState(c.subtitle || '')
  const [baseAsset, setBaseAsset] = useState('')
  const [genParams, setGenParams] = useState({ model_id: '', aspect_ratio: '1:1', resolution_tier: 'standard', count: 1, title: '', subtitle: '' })

  if (node.type === 'product_facts') {
    return (
      <div>
        <div className="section-title">产品事实卡（已确认输入，AI 不二次淘汰）</div>
        <div className="field"><label>产品名称</label><input value={facts.product_name || ''} onChange={(e) => setFacts({ ...facts, product_name: e.target.value })} /></div>
        <div className="field"><label>产品 ID</label><input value={facts.product_id || ''} onChange={(e) => setFacts({ ...facts, product_id: e.target.value })} /></div>
        <div className="field"><label>活动版本</label><input value={facts.activity_version || ''} onChange={(e) => setFacts({ ...facts, activity_version: e.target.value })} /></div>
        <div className="field"><label>已确认卖点（每条可含 卖点/人群/场景，用「|」分隔）</label>
          <textarea value={(facts.confirmed_selling_points || []).map((s: any) => `${s.id}|${s.text}|${s.audience || ''}|${s.scene || ''}`).join('\n')}
            onChange={(e) => setFacts({ ...facts, confirmed_selling_points: e.target.value.split('\n').filter(Boolean).map((l, i) => { const p = l.split('|'); return { id: p[0] || `sp_${i + 1}`, text: p[1] || '', audience: p[2] || '', scene: p[3] || '' } }) })} />
        </div>
        <StringList label="禁改外观（锁定项）" items={facts.locked_appearance || []} placeholder="如：外壳颜色、旋钮位置" onChange={(v) => setFacts({ ...facts, locked_appearance: v })} />
        <StringList label="适用场景" items={facts.applicable_scenes || []} placeholder="如：客厅边柜" onChange={(v) => setFacts({ ...facts, applicable_scenes: v })} />
        <StringList label="禁用表述" items={facts.forbidden_expressions || []} placeholder="如：全网最低" onChange={(v) => setFacts({ ...facts, forbidden_expressions: v })} />
        <StringList label="可用政策" items={facts.policies || []} placeholder="如：以旧换新补贴" onChange={(v) => setFacts({ ...facts, policies: v })} />
        <button className="btn primary" onClick={() => onSaveFacts(facts)}>保存事实卡</button>
        <div className="hint">提示：AI 仅能从图中抽取「识别推测」并单独标记，不会改动以上已确认事实。</div>
      </div>
    )
  }

  if (node.type === 'product_image') {
    const refs: string[] = c.reference_asset_ids || (c.asset_id ? [c.asset_id] : [])
    const toggle = (id: string) => {
      const next = refs.includes(id) ? refs.filter((x) => x !== id) : [...refs, id]
      onPatch({ ...c, asset_id: next[0] || null, reference_asset_ids: next, asset_role: 'product' })
    }
    return (
      <div>
        <div className="section-title">产品素材节点（可多选）</div>
        <div className="field"><label>挂载产品参考图 / Logo（可多张）</label>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {assets.filter((a) => a.role === 'product' || a.role === 'generated').map((a) => (
              <label key={a.id} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
                <input type="checkbox" checked={refs.includes(a.id)} onChange={() => toggle(a.id)} />
                <img src={fileUrl(a.id)} style={{ width: 36, height: 36, objectFit: 'cover', borderRadius: 4 }} alt="" />
                {a.id.slice(-6)}
              </label>
            ))}
          </div>
        </div>
        {refs.length > 0 && <div className="hint">已选 {refs.length} 张参考图，将作为生图的「产品主体 / 视觉参考」来源。</div>}
      </div>
    )
  }

  if (node.type === 'strategy') {
    const strs = c.strategies || []
    const brief = briefDraft
    const baseline = c.visual_baseline || {}
    return (
      <div>
        <div className="section-title">视觉策略（3 套差异化）</div>
        <details style={{ marginBottom: 8 }}>
          <summary style={{ cursor: 'pointer', fontSize: 12 }}>任务 Brief（参与策略生成）</summary>
          <div className="field" style={{ marginTop: 6 }}><label>目标</label><input value={brief.goal} onChange={(e) => setBriefDraft({ ...brief, goal: e.target.value })} /></div>
          <div className="field"><label>渠道</label><input value={brief.channel} onChange={(e) => setBriefDraft({ ...brief, channel: e.target.value })} /></div>
          <div className="field"><label>人群</label><input value={brief.audience} onChange={(e) => setBriefDraft({ ...brief, audience: e.target.value })} /></div>
          <button className="btn" onClick={() => onPatch({ ...c, task_brief: brief })}>保存 Brief</button>
        </details>
        {strs.length === 0 && <div className="muted">尚未生成。请先填写事实卡并保存，再点击生成。</div>}
        {c.requirement_summary && <div className="card-soft"><b>需求摘要：</b>{c.requirement_summary}</div>}
        {baseline.palette && (
          <div className="card-soft">
            <div><b>统一视觉基线：</b></div>
            <div className="line">配色：{(baseline.palette || []).join('、')}</div>
            <div className="line">材质光影：{baseline.material_light}</div>
            <div className="line">成像方式：{baseline.imaging_mode}</div>
            <div className="line">目标比例：{(c.target_aspect || []).join(' / ')}</div>
          </div>
        )}
        {strs.map((s: any) => (
          <div key={s.id} style={{ border: '1px solid #eee', borderRadius: 8, padding: 8, marginBottom: 8 }}>
            <div className="pill">{s.role}</div>
            <div className="line"><b>卖点：</b>{s.selling_point_text || s.selling_point_id}</div>
            <div className="line"><b>人群：</b>{s.audience}</div>
            <div className="line"><b>场景：</b>{s.scene}（备选：{(s.scene_options || []).slice(1).join('、') || '—'}）</div>
            <div className="line"><b>构图：</b>{s.composition}</div>
            <div className="line"><b>产品位置：</b>{s.product_placement}</div>
            <div className="line"><b>卖点证明：</b>{s.proposition_proof}</div>
            <div className="line"><b>文字模块：</b>{s.text_modules?.headline} / {s.text_modules?.selling_point} / {s.text_modules?.policy}</div>
          </div>
        ))}
        <button className="btn primary" onClick={() => onRun(node.id, defaultTextModel || 'rule-based-planner')}>生成 / 重生成策略</button>
      </div>
    )
  }

  if (node.type === 'image_prompt') {
    return (
      <div>
        <div className="section-title">单图提示词（strategy_ref: {c.strategy_ref}）</div>
        <div className="field"><label>正向提示词</label><textarea value={c.prompt || ''} readOnly /></div>
        <div className="field"><label>负向提示词</label><textarea value={c.negative_prompt || ''} readOnly /></div>
        <button className="btn primary" onClick={() => onRun(node.id, defaultTextModel || 'rule-based-planner')}>生成 / 扩写提示词</button>
        <div className="hint">提示词只扩写当前策略，若需改卖点/场景类型请回到策略节点。</div>
      </div>
    )
  }

  if (node.type === 'image_generation') {
    const imageModels = models.filter((m) => m.modality === 'image' && m.enabled === 1)
    const outputs: string[] = c.outputs || []
    return (
      <div>
        <div className="section-title">生图节点</div>
        <div className="field"><label>图片模型</label>
          <select value={genParams.model_id || c.model_override || defaultImageModel || imageModels[0]?.model_id || c.model_id || ''}
            onChange={(e) => { setGenParams({ ...genParams, model_id: e.target.value }); onPatch({ ...c, model_override: e.target.value }) }}>
            {imageModels.map((m) => <option key={m.model_id} value={m.model_id}>{m.model_id}（{m.cost_policy}）</option>)}
          </select>
        </div>
        <div className="row">
          <div className="field"><label>画幅</label>
            <select value={genParams.aspect_ratio} onChange={(e) => setGenParams({ ...genParams, aspect_ratio: e.target.value })}>
              {['1:1', '4:3', '3:4', '16:9'].map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
          </div>
          <div className="field"><label>清晰度</label>
            <select value={genParams.resolution_tier} onChange={(e) => setGenParams({ ...genParams, resolution_tier: e.target.value })}>
              <option value="standard">standard</option><option value="hd">hd</option>
            </select>
          </div>
          <div className="field"><label>张数</label>
            <select value={genParams.count} onChange={(e) => setGenParams({ ...genParams, count: Number(e.target.value) })}>
              {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </div>
        </div>
        <div className="row">
          <div className="field"><label>标题文字（可编辑层）</label><input value={genParams.title} placeholder="如：夏日焕新价" onChange={(e) => setGenParams({ ...genParams, title: e.target.value })} /></div>
          <div className="field"><label>副标题（价格/政策）</label><input value={genParams.subtitle} placeholder="如：以旧换新补贴" onChange={(e) => setGenParams({ ...genParams, subtitle: e.target.value })} /></div>
        </div>
        <button className="btn primary" onClick={() => onRun(node.id, genParams.model_id || defaultImageModel || c.model_id || imageModels[0]?.model_id, genParams)}>生成候选图</button>
        {c.last_usage && <div className="hint">上次用量：{JSON.stringify(c.last_usage)}</div>}

        <div className="section-title" style={{ marginTop: 12 }}>结果画廊（{outputs.length}）</div>
        <div className="gallery">
          {outputs.map((id) => (
            <div key={id} style={{ position: 'relative' }}>
              <img src={fileUrl(id)} alt="" />
              <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
                <button className="btn" style={{ fontSize: 11, padding: '2px 6px' }} onClick={() => { setBaseAsset(id); showToast('已选为排版底图') }}>排版</button>
                <button className="btn" style={{ fontSize: 11, padding: '2px 6px' }} onClick={() => window.open(fileUrl(id))}>原图</button>
              </div>
            </div>
          ))}
        </div>
        {baseAsset && (
          <div style={{ marginTop: 8, borderTop: '1px solid #eee', paddingTop: 8 }}>
            <div className="line">已选底图：{baseAsset}</div>
            <button className="btn primary" onClick={async () => { const r = await api.exportLayout(pid, { base_asset_id: baseAsset, title: layoutTitle, subtitle: layoutSub }); showToast('已导出最终 PNG'); window.open(fileUrl(r.asset_id)) }}>导出最终 PNG（改字不重生图）</button>
          </div>
        )}
      </div>
    )
  }

  if (node.type === 'review') {
    return (
      <div>
        <div className="section-title">审核节点</div>
        <div className="field"><label>审核结论</label><textarea value={c.conclusion || ''} onChange={(e) => onPatch({ ...c, conclusion: e.target.value })} /></div>
        <div className="field"><label>问题标签（逗号分隔）</label><input value={(c.issue_tags || []).join(',')} onChange={(e) => onPatch({ ...c, issue_tags: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })} /></div>
        <div className="hint">审核是辅助提醒（如「旋钮位置疑似变化」），最终由人确认。</div>
      </div>
    )
  }

  if (node.type === 'layout_export') {
    const gens = assets.filter((a) => a.role === 'generated')
    return (
      <div>
        <div className="section-title">分层排版导出</div>
        <div className="field"><label>选择底图（生成结果）</label>
          <select value={baseAsset} onChange={(e) => setBaseAsset(e.target.value)}>
            <option value="">— 选择 —</option>
            {gens.map((a) => <option key={a.id} value={a.id}>{a.id} ({a.width}×{a.height})</option>)}
          </select>
        </div>
        {baseAsset && <img src={fileUrl(baseAsset)} style={{ width: '100%', borderRadius: 8 }} alt="" />}
        <div className="field"><label>标题文字</label><input value={layoutTitle} onChange={(e) => setLayoutTitle(e.target.value)} /></div>
        <div className="field"><label>副标题（价格/政策）</label><input value={layoutSub} onChange={(e) => setLayoutSub(e.target.value)} /></div>
        <button className="btn primary" disabled={!baseAsset} onClick={async () => { const r = await api.exportLayout(pid, { base_asset_id: baseAsset, title: layoutTitle, subtitle: layoutSub }); showToast('已导出'); window.open(fileUrl(r.asset_id)) }}>导出可下载 PNG</button>
        <div className="hint">文字层在底图之上叠加，修改文案无需重新生图。</div>
      </div>
    )
  }

  return <div className="muted">节点类型：{node.type}</div>
}

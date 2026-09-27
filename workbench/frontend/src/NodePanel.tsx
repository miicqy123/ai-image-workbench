import React, { useEffect, useState } from 'react'
import { api, fileUrl } from './api'
import { Asset, GraphData, ModelInfo, WBNode } from './types'

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
  graph?: GraphData | null
  onPatchNode?: (nid: string, content: any) => Promise<void>
}

// 素材角色标签：与后端 assets.role 取值一致
const ROLE_LABEL: Record<string, string> = { product: '产品图', logo: 'Logo', reference: '参考图', generated: '生成结果', export: '导出产物' }

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

export default function NodePanel({ node, pid, assets, models, defaultTextModel, defaultImageModel, onRun, onSaveFacts, onPatch, onRefresh, showToast, graph, onPatchNode }: Props) {
  const c = node.content || {}
  const [facts, setFacts] = useState(c)
  const [briefDraft, setBriefDraft] = useState(c.task_brief || { goal: '', channel: '', audience: '' })
  const [layoutTitle, setLayoutTitle] = useState(c.title || '')
  const [layoutSub, setLayoutSub] = useState(c.subtitle || '')
  const [baseAsset, setBaseAsset] = useState('')
  const [genParams, setGenParams] = useState({ model_id: '', aspect_ratio: '1:1', resolution_tier: 'standard', count: 1, title: '', subtitle: '' })
  const [tpls, setTpls] = useState<any[]>([])
  useEffect(() => { api.listPromptTemplatesPublic().then(setTpls).catch(() => setTpls([])) }, [])
  // —— 阶段D：候选图/底图/文案/审核 的真实状态来源（一律来自服务端图数据）——
  const allNodes = (graph?.nodes || [])
  const genOutputs: string[] = Array.from(new Set(allNodes.filter((n) => n.type === 'image_generation')
    .flatMap((n) => ((n.content?.outputs as string[]) || []))))
  const layoutNode = allNodes.find((n) => n.type === 'layout_export') || null
  const layoutContent: any = (layoutNode?.content as any) || {}
  const savedBase: string = layoutContent.base_asset_id || ''
  const savedTitle: string = layoutContent.title || ''
  const savedSub: string = layoutContent.subtitle || ''
  const [poleBusy, setPoleBusy] = useState(false)
  const [gate, setGate] = useState<any>(null)
  const [revs, setRevs] = useState<any[]>([])
  useEffect(() => { api.reviewGate(pid).then(setGate).catch(() => setGate(null)) }, [pid, graph?.version])
  useEffect(() => { api.listProjectReviews(pid).then(setRevs).catch(() => setRevs([])) }, [pid, graph?.version])
  const saveIssues = async (patchObj: any) => {
    if (!layoutNode) { showToast('当前工作流没有「排版与 PNG 导出」节点'); return }
    try {
      if (onPatchNode) await onPatchNode(layoutNode.id, { ...layoutContent, ...patchObj })
      else await api.patchNode(layoutNode.id, { content: { ...layoutContent, ...patchObj } })
      showToast('已保存（服务端）')
    } catch (e: any) { showToast('保存失败：' + (e?.message || e)) }
  }
  const doExportPng = async (title: string, subtitle: string) => {
    if (poleBusy) return
    if (!savedBase) { showToast('请先在候选图中选择一张作为排版底图（并保存）'); return }
    setPoleBusy(true)
    try {
      const r = await api.exportLayout(pid, { base_asset_id: savedBase, title, subtitle })
      showToast(`已导出 PNG（底图 ${savedBase}，用已保存文案）`)
      window.open(fileUrl(r.asset_id))
    } catch (e: any) { showToast('导出失败：' + (e?.message || e)) } finally { setPoleBusy(false) }
  }
  // 参考图上限直接取模型能力声明，不在前端另写一套能力描述
  const imgInputLimit = (() => {
    const m = models.find((x) => x.modality === 'image' && x.enabled === 1)
    try { return Number(JSON.parse(m?.capabilities_json || '{}').image_input_limit || 0) } catch { return 0 }
  })()

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
    // 与后端一致：asset_id = 第 1 张，reference_asset_ids = 其余参考
    const refs: string[] = Array.from(new Set([...(c.asset_id ? [c.asset_id] : []), ...((c.reference_asset_ids as string[]) || [])].filter(Boolean)))
    // 绑定只把资产 ID 写进本节点内容；不改写资产自身 role，也不动其他节点与项目
    const toggle = (a: Asset) => {
      const next = refs.includes(a.id) ? refs.filter((x) => x !== a.id) : [...refs, a.id]
      const primary = assets.find((x) => x.id === next[0])
      onPatch({ ...c, asset_id: next[0] || null, reference_asset_ids: next.slice(1), asset_role: primary ? primary.role : (c.asset_role || 'product') })
    }
    const candidates = assets.filter((a) => a.role === 'product' || a.role === 'logo' || a.role === 'reference' || a.role === 'generated')
    const overLimit = imgInputLimit > 0 && refs.length > imgInputLimit
    return (
      <div>
        <div className="section-title">产品素材节点（可多选）</div>
        <div className="hint">
          产品图：第 1 张作中央主产品、第 2 张作右下辅图；Logo：叠加到左上角、不当作产品主体；参考图：仅作提示参考，不叠加到本地成图。
        </div>
        <div className="field"><label>挂载素材（按上传时选定的角色区分）</label>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {candidates.map((a) => (
              <label key={a.id} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12 }} title={`${ROLE_LABEL[a.role] || a.role} · ${a.width}×${a.height}`}>
                <input type="checkbox" checked={refs.includes(a.id)} onChange={() => toggle(a)} />
                <img src={fileUrl(a.id)} style={{ width: 36, height: 36, objectFit: 'cover', borderRadius: 4 }} alt="" />
                <span>{ROLE_LABEL[a.role] || a.role} {a.id.slice(-6)}</span>
              </label>
            ))}
            {!candidates.length && <span className="muted">当前项目还没有可挂载素材，请先在左侧「素材库」按角色上传。</span>}
          </div>
        </div>
        {refs.length > 0 && (
          <div className="hint">
            已绑定 {refs.length} 张：{refs.map((id) => { const a = assets.find((x) => x.id === id); return (a ? (ROLE_LABEL[a.role] || a.role) : '已不存在的素材') + ' ' + id.slice(-6) }).join('，')}
          </div>
        )}
        {overLimit && <div className="hint" style={{ color: '#d4380d' }}>已超过当前图片模型的参考图上限（{imgInputLimit} 张），运行时会被拒绝。</div>}
        <div className="hint">绑定/解绑只改本节点记录，不会修改素材角色，也不会影响其他项目。</div>
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
        <div className="field"><label>Prompt 模板（后台提示词库）</label>
          <select value={c.prompt_template_id || ''} onChange={(e) => onPatch({ ...c, prompt_template_id: e.target.value })}>
            <option value="">不使用模板（纯模型扩写）</option>
            {tpls.map((t) => <option key={t.id} value={t.id}>{t.name}（{t.category}）</option>)}
          </select>
        </div>
        {c.prompt_template_name ? <div className="hint">上次编译使用的模板：{c.prompt_template_name}</div> : null}
        <div className="field"><label>正向提示词</label><textarea value={c.prompt || ''} readOnly /></div>
        <div className="field"><label>负向提示词</label><textarea value={c.negative_prompt || ''} readOnly /></div>
        <button className="btn primary" onClick={() => onRun(node.id, defaultTextModel || 'rule-based-planner')}>生成 / 扩写提示词</button>
        <div className="hint">提示词只扩写当前策略，若需改卖点/场景类型请回到策略节点。</div>
        <div className="hint">选择 Prompt 模板后，生成的提示词会按模板重新编译（platform / aspect_ratio / style_keywords 等变量取自项目 Brief）。</div>
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
          <div className="field"><label>标题文字</label><input value={genParams.title} placeholder="如：夏日焕新价" onChange={(e) => setGenParams({ ...genParams, title: e.target.value })} /></div>
          <div className="field"><label>副标题（价格/政策）</label><input value={genParams.subtitle} placeholder="如：以旧换新补贴" onChange={(e) => setGenParams({ ...genParams, subtitle: e.target.value })} /></div>
        </div>
        <button className="btn primary" disabled={poleBusy} onClick={() => onRun(node.id, genParams.model_id || defaultImageModel || c.model_id || imageModels[0]?.model_id, genParams)}>生成候选图</button>
        {c.last_usage && <div className="hint">上次用量：{JSON.stringify(c.last_usage)}</div>}

        <div className="section-title" style={{ marginTop: 12 }}>候选图（{outputs.length}）· 本节点真实生成结果</div>
        {outputs.length === 0 && <div className="muted">暂无候选图：请先点「生成候选图」，或检查模型/服务商配置是否可用。</div>}
        <div className="gallery">
          {outputs.map((id) => (
            <div key={id} style={{ position: 'relative' }}>
              <img src={fileUrl(id)} alt="" />
              <div style={{ display: 'flex', gap: 4, marginTop: 4, flexWrap: 'wrap' }}>
                <button className="btn" style={{ fontSize: 11, padding: '2px 6px' }} disabled={!layoutNode || poleBusy}
                  onClick={() => saveIssues({ base_asset_id: id })}>{savedBase === id ? '已保存为底图' : '设为排版底图'}</button>
                <button className="btn" style={{ fontSize: 11, padding: '2px 6px' }} onClick={() => window.open(fileUrl(id))}>原图预览</button>
              </div>
            </div>
          ))}
        </div>
        <div className="hint">
          排版底图（服务端已保存）：{savedBase || '（未选择）'} · 切换节点/项目后以此为准；导出在「排版与 PNG 导出」节点执行。
        </div>
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
    const candidates: string[] = Array.from(new Set([...genOutputs, ...gens.map((a) => a.id)]))
    const dirty = layoutTitle !== savedTitle || layoutSub !== savedSub
    const gateText = !gate ? '审核状态：读取中/不可用' : gate.ok
      ? '审核门禁：通过（可导出）'
      : gate.reason === 'pending_review' ? '审核门禁：有未完成的人工审核，导出被拦截'
        : gate.reason === 'review_required' ? `审核门禁：模板「${gate.template || ''}」要求导出前人工审核通过`
          : gate.reason === 'stale_approval' ? '审核门禁：成品在审核通过后又被修改，需重新提交审核'
            : `审核门禁：未通过（${gate.reason || '未知原因'}）`
    return (
      <div>
        <div className="section-title">排版与 PNG 导出（交付闭环）</div>
        <div className="field"><label>排版底图（来自真实生成结果 {candidates.length} 张）</label>
          {candidates.length === 0
            ? <div className="muted">无候选图：请先在生图节点生成，或确认该节点已有 outputs。</div>
            : <div className="gallery">
                {candidates.map((id) => (
                  <div key={id} style={{ position: 'relative' }}>
                    <img src={fileUrl(id)} alt="" />
                    <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
                      <button className="btn" style={{ fontSize: 11, padding: '2px 6px' }} disabled={poleBusy}
                        onClick={() => onPatch({ ...c, base_asset_id: id })}>{c.base_asset_id === id ? '已是底图' : '设为底图'}</button>
                      <button className="btn" style={{ fontSize: 11, padding: '2px 6px' }} onClick={() => window.open(fileUrl(id))}>原图预览</button>
                    </div>
                  </div>
                ))}
              </div>}
        </div>
        <div className="hint">当前底图（服务端已保存）：{c.base_asset_id || '（未选择）'}</div>
        <div className="field"><label>标题文字 {dirty ? '（有未保存修改）' : '（已保存）'}</label><input value={layoutTitle} onChange={(e) => setLayoutTitle(e.target.value)} /></div>
        <div className="field"><label>副标题 {dirty ? '（有未保存修改）' : '（已保存）'}</label><input value={layoutSub} onChange={(e) => setLayoutSub(e.target.value)} /></div>
        <div className="row">
          <button className="btn" disabled={poleBusy} onClick={() => onPatch({ ...c, title: layoutTitle, subtitle: layoutSub })}>保存文案</button>
          <button className="btn primary" disabled={poleBusy || !c.base_asset_id} onClick={() => doExportPng(savedTitle, savedSub)}>
            {poleBusy ? '导出中…' : '导出最终 PNG（用已保存底图与文案）'}
          </button>
          <button className="btn" disabled={poleBusy} onClick={() => { try { window.open(api.exportPackage(pid)) } catch (e: any) { showToast('ZIP 导出失败：' + (e?.message || e)) } }}>导出素材包 ZIP</button>
        </div>
        {dirty && <div className="hint">存在未保存文案：导出将使用「已保存」的文案（{savedTitle || '（空）'} / {savedSub || '（空）'}），请先保存再导出。</div>}
        <div className="hint">PNG 导出 = 底图 + 文字层重绘（改字不重生图）。ZIP 素材包按现有接口内容为准（项目 JSON + 已生成/导出图片文件），不含分层可编辑文件。</div>
        <div className="card-soft" style={{ marginTop: 8 }}>
          <div><b>审核（人工）</b></div>
          <div className="line">{gateText}</div>
          {revs.length === 0 ? <div className="line">本项目暂无人工审核记录。</div>
            : revs.map((r) => <div className="line" key={r.id}>{r.target_label || '成品'} · {r.status} · 风险 {r.risk_level}{r.reason ? ` · ${r.reason}` : ''}</div>)}
          <div className="line muted">审核来自后台人工流程；本页不做 AI 自动审核。</div>
        </div>
      </div>
    )
  }

  return <div className="muted">节点类型：{node.type}</div>
}

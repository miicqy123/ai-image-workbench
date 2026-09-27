import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ReactFlow, Background, BackgroundVariant, Controls, MiniMap, Handle, Position,
  SelectionMode, useReactFlow, MarkerType, useNodesState, useEdgesState,
} from '@xyflow/react'
import { api, fileUrl, ApiError } from './api'
import { Asset, GraphData, ModelInfo, WBNode, WBEdge } from './types'
import NodePanel from './NodePanel'
import RunReport, { DownstreamResult, DSRanEntry } from './RunReport'
import ModelManager from './ModelManager'

const NODE_COLORS: Record<string, string> = {
  product_facts: '#7c5cff', product_image: '#13c2c2', strategy: '#4c7bf3',
  image_prompt: '#eb2f96', image_generation: '#fa8c16', review: '#52c41a', layout_export: '#722ed1',
}
const NODE_LABELS: Record<string, string> = {
  product_facts: '产品事实卡', product_image: '产品素材', strategy: '视觉策略',
  image_prompt: '单图提示词', image_generation: '生图节点', review: '审核', layout_export: '排版与 PNG 导出',
}
const OLD_LAYOUT_NAME = '分层排版导出'
const NEW_LAYOUT_NAME = '排版与 PNG 导出'
// 统一节点显示名：layout_export 的历史默认名/无名字都显示新名；自定义名原样保留（不改 API 返回值、不写库）
function nodeDisplayName(n?: { type?: string; name?: string } | null): string {
  if (!n) return ''
  if (n.type === 'layout_export' && (!n.name || n.name === OLD_LAYOUT_NAME)) return NEW_LAYOUT_NAME
  return n.name || NODE_LABELS[n.type || ''] || n.type || ''
}

const BG_VARIANT: Record<string, BackgroundVariant> = {
  dots: BackgroundVariant.Dots, lines: BackgroundVariant.Lines, cross: BackgroundVariant.Cross,
}

// 素材角色：与后端 assets.role 取值一致；说明与本地拼接器实际版式一致（generators/image.py _pick_by_role）
const ASSET_ROLES = [
  { role: 'product', label: '产品图', hint: '产品图：第 1 张作中央主产品、第 2 张作右下辅图，参与本地成图' },
  { role: 'logo', label: 'Logo', hint: 'Logo：叠加到本地成图左上角，不当作产品主体' },
  { role: 'reference', label: '参考图', hint: '参考图：仅作为提示参考传入模型，不叠加到本地成图' },
]
const ASSET_ROLE_LABEL: Record<string, string> = { product: '产品图', logo: 'Logo', reference: '参考图', generated: '生成结果', export: '导出产物' }
// 产品素材节点绑定约定（与后端 main.py image_generation 分支一致）：
// asset_id = 第 1 张，reference_asset_ids = 其余参考；生图实际入参顺序为 [asset_id] + reference_asset_ids
function readBoundRefs(c: any): string[] {
  const raw = [...(c?.asset_id ? [c.asset_id] : []), ...((c?.reference_asset_ids as string[]) || [])]
  return Array.from(new Set(raw.filter((x) => !!x)))
}
function boundContent(c: any, next: string[], assets: Asset[]) {
  const primary = assets.find((x) => x.id === next[0])
  return { ...c, asset_id: next[0] || null, reference_asset_ids: next.slice(1), asset_role: primary ? primary.role : (c.asset_role || 'product') }
}

// 连接端口推断（前端仅做友好推断，后端做强类型校验）
const PORT_MAP: Record<string, { from: string; to: string }> = {
  'product_facts>strategy': { from: 'facts', to: 'facts' },
  'product_image>image_prompt': { from: 'visual_reference', to: 'visual_reference' },
  'strategy>image_prompt': { from: 'strategy', to: 'strategy' },
  'image_prompt>image_generation': { from: 'prompt', to: 'prompt' },
  'image_generation>review': { from: 'generated_asset', to: 'generated_asset' },
  'review>layout_export': { from: 'review', to: 'review' },
}

interface RFNodeData { node: WBNode; intents?: any[]; tool?: (nodeId: string, intent: string, assetId: string, title: string, subtitle: string) => Promise<void>; toast?: (m: string) => void; refresh?: () => void; rename?: (nid: string, name: string) => void; runDownstream?: (nid: string) => void }

const IMG_NODE_TYPES = ['image_generation', 'product_image', 'layout_export']

function WBNodeComp({ data, selected }: { data: RFNodeData; selected?: boolean }) {
  const n = data.node
  const color = NODE_COLORS[n.type] || '#97a0ad'
  const outputs: string[] = n.content?.outputs || []
  const stratCount = n.content?.strategies?.length || 0
  const intents: any[] = data.intents || []
  const [hov, setHov] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)
  const [et, setEt] = useState('')
  const [es, setEs] = useState('')
  const [running, setRunning] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [nameDraft, setNameDraft] = useState('')
  const isImgNode = IMG_NODE_TYPES.includes(n.type)
  const baseAsset = outputs[0] || n.content?.asset_id

  const summary = (() => {
    switch (n.type) {
      case 'product_facts': return `名称：${n.content?.product_name || '（未填）'}`
      case 'product_image': return n.content?.asset_id ? '已挂载产品图' : '未挂载素材'
      case 'strategy': return stratCount ? `已生成 ${stratCount} 套策略` : '待生成策略'
      case 'image_prompt': return n.content?.prompt ? n.content.prompt.slice(0, 40) + '…' : '待生成提示词'
      case 'image_generation': return outputs.length ? `已生成 ${outputs.length} 张` : '待生图'
      case 'review': return n.content?.conclusion ? '已审核' : '待审核'
      case 'layout_export': return n.content?.title ? `标题：${n.content.title}` : '待导出'
      default: return ''
    }
  })()

  const runTool = async (intent: string) => {
    if (intent === '替换') { data.toast?.('替换底图请在右侧面板选择素材'); return }
    if (!baseAsset) { data.toast?.('请先生成或选择一张底图'); return }
    if ((intent === '改字' || intent === '图文分层') && !editOpen) { setEditOpen(true); return }
    setRunning(true)
    try {
      await data.tool?.(n.id, intent, baseAsset, et, es)
      data.refresh?.()
      data.toast?.(`已执行：${intent}`)
    } catch (e: any) { data.toast?.('失败：' + e.message) }
    finally { setRunning(false); setEditOpen(false) }
  }

  const topIntents = ['替换', '改字', '抠图', '消除', '变清晰', '扩图']
  const moreIntents = ['相似图', '风格迁移', '溶图', '变体', '转矢量', '图文分层']
  const intentMap = Object.fromEntries(intents.map((i) => [i.intent, i]))
  const enabled = (name: string) => (intentMap[name]?.available ?? false)

  const commitRename = () => {
    const v = nameDraft.trim()
    if (v && v !== nodeDisplayName(n)) data.rename?.(n.id, v)
    setRenaming(false)
  }

  return (
    <div className={`wb-node ${selected ? 'selected' : ''}`} onMouseEnter={() => setHov(true)} onMouseLeave={() => setHov(false)}>
      <div className="nh" style={{ background: color }}>
        {renaming ? (
          <input className="node-title-input" autoFocus value={nameDraft} onChange={(e) => setNameDraft(e.target.value)}
            onBlur={commitRename} onKeyDown={(e) => { if (e.key === 'Enter') commitRename(); if (e.key === 'Escape') setRenaming(false) }}
            onPointerDown={(e) => e.stopPropagation()} />
        ) : (
          <span className="node-title" title="双击重命名"
            onDoubleClick={(e) => { e.stopPropagation(); setNameDraft(nodeDisplayName(n)); setRenaming(true) }}>
            {nodeDisplayName(n)}
          </span>
        )}
        <span className={`tag status-${n.status}`}>{n.status}</span>
      </div>
      <div className="nb">
        <div className="line">{summary}</div>
        {outputs.length > 0 && (
          <div className="thumbs">
            {outputs.slice(0, 4).map((id: string) => (
              <img key={id} src={fileUrl(id)} alt="" />
            ))}
          </div>
        )}
      </div>
      {hov && (
        <div className="node-toolbar">
          <button className="nt" title="运行下游：由服务端按依赖顺序执行本节点之后的下游节点，默认不重跑本节点"
            onClick={() => data.runDownstream?.(n.id)}>运行下游</button>
          {isImgNode && topIntents.map((t) => (
            <button key={t} className={`nt ${enabled(t) ? '' : 'disabled'}`} disabled={!enabled(t) || running}
              title={enabled(t) ? t : `${t}（未接入）`} onClick={() => runTool(t)}>{t}</button>
          ))}
          {isImgNode && (<div className="nt-more">
            <button className="nt" disabled={running} onClick={() => setMoreOpen((v) => !v)}>更多 ▾</button>
            <div className={`nt-menu ${moreOpen ? 'open' : ''}`}>
              {moreIntents.map((t) => (
                <button key={t} className={`nt ${enabled(t) ? '' : 'disabled'}`} disabled={!enabled(t) || running}
                  title={enabled(t) ? t : `${t}（未接入）`} onClick={() => runTool(t)}>{t}</button>
              ))}
            </div>
          </div>)}
        </div>
      )}
      {editOpen && (
        <div className="nt-edit">
          <input placeholder="标题文字" value={et} onChange={(e) => setEt(e.target.value)} />
          <input placeholder="副标题/政策" value={es} onChange={(e) => setEs(e.target.value)} />
          <button className="nt primary" disabled={running} onClick={() => runTool('改字')}>应用改字</button>
        </div>
      )}
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
    </div>
  )
}
const SKELETON_LABEL: Record<string, string> = { poster: '海报', long: '长图', detail: '详情页', cover: '封面', illustration: '插图' }
const NODE_PALETTE = [
  { type: 'product_facts', label: '产品事实' },
  { type: 'product_image', label: '产品图' },
  { type: 'strategy', label: '策略' },
  { type: 'image_prompt', label: '提示词' },
  { type: 'image_generation', label: '生图' },
  { type: 'review', label: '审核' },
  { type: 'layout_export', label: '导出' },
]
const nodeTypes = { wb: WBNodeComp }

function toRF(graph: GraphData, intents: any[], tool: any, toast: any, refresh: any, rename: any, runDownstream: any) {
  const nodes = graph.nodes.map((n) => ({
    id: n.id, type: 'wb', position: n.position, data: { node: n, intents, tool, toast, refresh, rename, runDownstream },
  }))
  const edges = graph.edges.map((e: WBEdge) => ({
    id: e.id, source: e.from_node, target: e.to_node, label: e.semantic,
    markerEnd: { type: MarkerType.ArrowClosed }, style: { stroke: '#b0b6bf' },
  }))
  return { nodes, edges }
}

// 可自动执行的节点类型（与后端 RUN_DOWNSTREAM_TYPES 一致）
const AUTO_RUN_TYPES = ['strategy', 'image_prompt', 'image_generation']

/**
 * 一键运行的「起点」选择：前端只决定从哪里开始，真正的执行顺序与失败处理全部由服务端拓扑执行决定。
 * 服务端 run-downstream 不执行起点自身，因此覆盖集 = 起点下游 ∪（起点自身，若其可自动执行）；
 * 只有当它恰好覆盖图中全部可自动执行节点（不重复、不遗漏）时才允许一键运行。
 */
function planWholeGraphRun(graph: GraphData): { start: string | null; runSelfFirst: boolean; reason: string } {
  const auto = new Set(graph.nodes.filter((n) => AUTO_RUN_TYPES.includes(n.type)).map((n) => n.id))
  if (!auto.size) return { start: null, runSelfFirst: false, reason: '当前图里没有可自动执行的节点（策略 / 提示词 / 生图）。' }
  const children: Record<string, string[]> = {}
  graph.edges.forEach((e) => { children[e.from_node] = (children[e.from_node] || []).concat(e.to_node) })
  const downstream = (id: string) => {
    const seen = new Set<string>()
    const frontier = [id]
    while (frontier.length) {
      const cur = frontier.pop() as string
      for (const t of children[cur] || []) if (!seen.has(t)) { seen.add(t); frontier.push(t) }
    }
    return seen
  }
  for (const wantSelf of [false, true]) {
    for (const n of graph.nodes) {
      const selfAuto = auto.has(n.id)
      if (selfAuto !== wantSelf) continue
      const covered = new Set<string>()
      downstream(n.id).forEach((x) => { if (auto.has(x)) covered.add(x) })
      if (selfAuto) covered.add(n.id)
      if (covered.size === auto.size) return { start: n.id, runSelfFirst: selfAuto, reason: '' }
    }
  }
  return { start: null, runSelfFirst: false,
    reason: '该图存在多个彼此独立的可自动执行分支，现有 run-downstream 只能从一个起点按拓扑重跑其下游，无法在不重复执行的前提下覆盖全图，因此本轮不执行。请先在各节点用「运行下游」逐步执行；如需真正的整图一键运行，需要后端新增整图接口（最小契约见本轮回报）。' }
}

/** 服务端节点版本字段为 current_version，前端类型用 version；统一取值避免 undefined */
function nodeVersion(n: any): number { return (n?.version ?? n?.current_version ?? 0) as number }

/** 归一化请求异常：区分“服务端明确拒绝（HTTP）”与“网络/结果未知” */
function errInfo(e: any): { rejected: boolean; status?: number; message: string; hint?: string; manual?: boolean } {
  if (e && typeof e.status === 'number') {
    const d = e.detail
    const msg = (d && typeof d === 'object' && (d.message || d.detail)) || (typeof d === 'string' ? d : '') || e.message || '服务端拒绝本次请求'
    return { rejected: true, status: e.status, message: String(msg),
             hint: d && typeof d === 'object' ? d.hint : undefined,
             manual: !!(d && typeof d === 'object' && d.manual_check_required) }
  }
  return { rejected: false, message: (e && e.message) || String(e) }
}

/** 用服务端返回的 status 生成提示语，不把 HTTP 200 等同于全部完成 */
function dsSummary(r: any): string {
  const n = (a?: any[]) => (a || []).length
  if (r && r.status === 'ok') return `下游执行结束：已执行 ${n(r.ran)}，跳过 ${n(r.skipped)}${n(r.not_executed) ? `，未执行 ${n(r.not_executed)}` : ''}`
  if (r && r.status === 'running') return `仍在运行：已执行 ${n(r.ran)}，仍在运行/待执行 ${n(r.pending)}（结果未确认，不是失败）`
  if (r && r.status === 'blocked') return '已阻止本次运行：该节点存在未结束的运行，需人工排查（详见运行结果面板）'
  if (r && r.status === 'failed') return `下游执行失败：已执行 ${n(r.ran)}，失败 ${r.failed ? 1 : 0}，未执行 ${n(r.not_executed)}（详见运行结果面板）`
  if (r && r.status === 'rejected') return `服务端拒绝本次运行（HTTP ${r.local_status || ''}），非网络问题`
  return '下游执行结果未确认：不能视为成功，请查看运行结果面板'
}

function Icon({ d }: { d: string }) {
  return <svg width={19} height={19} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round"><path d={d} /></svg>
}

function LeftNav({ onBack, onAddNode, onRun, onModels }: { onBack?: () => void; onAddNode: (t: string) => void; onRun: () => void; onModels: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="left-nav">
      <button className="ln-btn" title="返回汇总" onClick={onBack}><Icon d="M15 18l-6-6 6-6" /></button>
      <button className="ln-btn" title="素材库" onClick={() => { const el = document.querySelector('.sidebar'); el?.scrollIntoView({ behavior: 'smooth' }) }}><Icon d="M3 5h18v14H3zM8 9l3 3 3-3" /></button>
      <div className="ln-wrap">
        <button className={`ln-btn ${open ? 'on' : ''}`} title="新增节点" onClick={() => setOpen((v) => !v)}><Icon d="M12 5v14M5 12h14" /></button>
        {open && (
          <div className="ln-menu">
            {NODE_PALETTE.map((n) => <button key={n.type} onClick={() => { onAddNode(n.type); setOpen(false) }}>+ {n.label}</button>)}
          </div>
        )}
      </div>
      <button className="ln-btn" title="一键运行（服务端按依赖顺序，失败即停）" onClick={onRun}><Icon d="M6 4l14 8-14 8z" /></button>
      <button className="ln-btn" title="模型管理" onClick={onModels}><Icon d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z" /></button>
      <div className="ln-sep" />
      <button className="ln-btn disabled" title="文字（待接入）"><Icon d="M4 6V4h16v2M12 4v16m-3 0h6" /></button>
      <button className="ln-btn disabled" title="图形（待接入）"><Icon d="M4 6h16M4 6v12M4 18h16M20 6v12" /></button>
      <button className="ln-btn disabled" title="便签（待接入）"><Icon d="M5 3h14a2 2 0 0 1 2 2v8l-6 6H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM13 19v-6h6" /></button>
    </div>
  )
}

export default function App({ templateId, templateName, skeleton, onBack }: { templateId?: string; templateName?: string; skeleton?: string; onBack?: () => void } = {}) {
  const [projects, setProjects] = useState<any[]>([])
  const [pid, setPid] = useState<string>('')
  const [graph, setGraph] = useState<GraphData | null>(null)
  const [assets, setAssets] = useState<Asset[]>([])
  const [models, setModels] = useState<ModelInfo[]>([])
  const [selectedId, setSelectedId] = useState<string>('')
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<any>([])
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<any>([])
  const [toast, setToast] = useState('')
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  // 运行状态统一入口：busyRef 供回调闭包内即时判断，避免重复触发
  const setRunning = (v: boolean) => { busyRef.current = v; setBusy(v) }
  const [creating, setCreating] = useState(false)
  const [ds, setDs] = useState<DownstreamResult | null>(null)
  // 阶段C补修第二批：对服务端返回的 pending_run_ids 做限时只读轮询（不重新提交生成，不取消服务端任务）
  const [pollInfo, setPollInfo] = useState<{ ids: string[]; statuses: Record<string, string>; running: number; done: number; unknown: number; ended: boolean; timedOut?: boolean } | null>(null)
  const pollRef = useRef<{ stop: boolean; timer: any }>({ stop: true, timer: null })
  const runDownstreamRef = useRef<(nid: string) => void>(() => {})
  const [candidate, setCandidate] = useState<any>(null)
  const [aiInstr, setAiInstr] = useState('')
  const [showModelMgr, setShowModelMgr] = useState(false)
  const [gridType, setGridType] = useState<'dots' | 'lines' | 'cross'>('dots')
  const [snap, setSnap] = useState(false)
  const [zoom, setZoom] = useState(1)
  const [rfInstance, setRfInstance] = useState<any>(null)
  const [defText, setDefText] = useState('')
  const [defImage, setDefImage] = useState('')
  const [intents, setIntents] = useState<any[]>([])
  const [importedText, setImportedText] = useState('')

  const selected = useMemo(() => graph?.nodes.find((n) => n.id === selectedId) || null, [graph, selectedId])

  const showToast = (m: string) => { setToast(m); setTimeout(() => setToast(''), 2600) }

  const refreshProjects = useCallback(async () => {
    const ps = await api.listProjects()
    setProjects(ps)
    if (!pid && ps.length) setPid(ps[0].id)
  }, [pid])

  // 显式传入项目 ID：新建/切换项目时用新 ID 直接加载，不依赖 setPid 之后的旧闭包
  const refreshGraph = useCallback(async (targetPid?: string) => {
    const gid = targetPid || pid
    if (!gid) return
    const raw = await api.getGraph(gid)
    const g: GraphData = { ...raw, nodes: raw.nodes.map((n: any) => ({ ...n, version: nodeVersion(n) })) }
    setGraph(g)
    const { nodes, edges } = toRF(g, intents, runImageTool, showToast, () => { refreshGraph(gid); refreshAssets(gid) }, renameNode, runDownstreamRef.current)
    setRfNodes(nodes)
    setRfEdges(edges)
  }, [pid, setRfNodes, setRfEdges, intents])

  const refreshAssets = useCallback(async (targetPid?: string) => {
    const gid = targetPid || pid
    if (!gid) return
    setAssets(await api.listAssets(gid))
  }, [pid])

  const refreshDefaults = useCallback(async () => {
    if (!pid) return
    try {
      const d = await api.getDefaults(pid)
      setDefText(d.default_text_model || '')
      setDefImage(d.default_image_model || '')
    } catch { /* 忽略 */ }
  }, [pid])

  useEffect(() => { refreshProjects() }, [])
  useEffect(() => { if (pid) { refreshGraph(); refreshAssets(); api.listModels().then(setModels); refreshDefaults() } }, [pid])
  useEffect(() => { api.imageToolIntents().then(setIntents).catch(() => setIntents([])) }, [])
  useEffect(() => {
    if (pid && rfInstance) {
      const t = setTimeout(() => rfInstance.fitView({ padding: 0.18 }), 60)
      return () => clearTimeout(t)
    }
  }, [pid, rfInstance])

  // 选模板 → 创建项目（写入 template_id）→ 按该模板 skeleton 初始化图 → 显式按新项目 ID 加载图与素材
  const newProject = async () => {
    if (creating) return
    const name = prompt('项目名称（如：XX产品 618 主图）', templateName ? `${templateName} · 新项目` : '示例产品 · 营销主图')
    if (!name || !name.trim()) return
    setCreating(true)
    let p: any = null
    try {
      p = await api.createProject(name.trim(), templateId)
    } catch (e: any) {
      // 创建失败：不改动本地项目列表，不谎报成功，可直接重试
      showToast('创建项目失败：' + (e?.message || e) + '（未创建项目，可重试）')
      setCreating(false)
      return
    }
    setProjects((ps) => [p, ...ps])
    setSelectedId('')
    setGraph(null); setAssets([])
    setPid(p.id)
    try {
      await api.initTemplate(p.id, skeleton || 'poster')
      await refreshGraph(p.id)
      await refreshAssets(p.id)
      showToast(templateName ? `已创建项目并按「${templateName}」初始化工作流` : '已创建项目并初始化工作流模板')
    } catch (e: any) {
      // 项目已创建但图未初始化：如实提示（不显示“成功”），并加载该项目当前状态供用户重试
      try { await refreshGraph(p.id); await refreshAssets(p.id) } catch { /* 忽略：保持报错信息 */ }
      showToast('项目已创建，但工作流初始化失败：' + (e?.message || e) + '（可在该项目点「重置模板」重试）')
    } finally {
      setCreating(false)
    }
  }

  // 重置模板会删除当前图全部节点、连线及节点内容，必须先二次确认；取消则不发任何写请求
  const initTpl = async () => {
    if (!pid || creating) return
    if (busyRef.current) { showToast('运行中禁止重置模板：请等待本次运行结束后再操作'); return }
    const ok = window.confirm('重置模板将删除当前项目的全部工作流节点、连线及这些节点上的内容（节点名称、事实卡、提示词、生成记录等），且不可恢复。\n\n项目本身、已上传素材与已生成的图片会保留。\n\n确定要重置为模板工作流吗？')
    if (!ok) return
    setCreating(true)
    try {
      await api.initTemplate(pid, skeleton || 'poster')
      await refreshGraph(pid)
      await refreshAssets(pid)
      showToast('已重置为模板工作流')
    } catch (e: any) {
      showToast('重置失败：' + (e?.message || e))
    } finally { setCreating(false) }
  }

  const addNode = async (type: string) => {
    if (!pid || !graph) return
    await api.addNode(graph.graph_id, { type, x: 260 + Math.random() * 260, y: 140 + Math.random() * 260 })
    await refreshGraph(); showToast('已添加节点')
  }

  const deleteSelected = async () => {
    if (!pid || !selectedId) return
    if (busyRef.current) { showToast('运行中禁止删除节点：请等待本次运行结束后再操作'); return }
    await api.deleteNode(selectedId)
    setSelectedId(''); await refreshGraph(); showToast('已删除节点')
  }

  const onConnect = useCallback(async (c: any) => {
    if (!graph || !pid) return
    const s = graph.nodes.find((n) => n.id === c.source)
    const t = graph.nodes.find((n) => n.id === c.target)
    const key = `${s?.type}>${t?.type}`
    const pm = PORT_MAP[key]
    if (!pm) { showToast('该连接类型不兼容'); return }
    await api.patchGraph(pid, { expected_graph_version: graph.version, add_edges: [{ from_node: c.source, from_port: pm.from, to_node: c.target, to_port: pm.to, semantic: NODE_LABELS[s!.type] + '→' + NODE_LABELS[t!.type] }] })
    await refreshGraph()
    showToast('已添加连线（强类型校验通过）')
  }, [graph, pid])

  const onNodeDragStop = useCallback(async (_: any, node: any) => {
    if (!pid || !graph) return
    await api.patchGraph(pid, { expected_graph_version: graph.version, moves: [{ node_id: node.id, x: Math.round(node.position.x), y: Math.round(node.position.y) }] })
  }, [pid, graph])

  const runNode = async (nid: string, model_id?: string, params: any = {}) => {
    if (busyRef.current) { showToast('正在运行中，请等待本次运行结束'); return }
    setRunning(true)
    try {
      const r = await api.runNode(nid, { model_id, params, idempotency_key: `idem_${nid}_${Date.now()}` })
      if (r?.reused) showToast('该节点已有同一输入/参数的在跑运行，已复用既有运行')
      const es = new EventSource(`/api/runs/${r.run_id}/events`)
      let settled = false
      const timer = setTimeout(() => {
        // 客户端等待超时：只报“结果未确认”，绝不报失败，也不重复触发
        if (settled) return
        settled = true; es.close(); setRunning(false); refreshGraph()
        showToast(`等待超时：结果未确认（run_id=${r.run_id}，服务端可能仍在执行）`)
      }, 240000)
      const finish = (msg?: string) => {
        if (settled) return
        settled = true; clearTimeout(timer); es.close(); setRunning(false); refreshGraph(); refreshAssets()
        if (msg) showToast(msg)
      }
      es.onmessage = (ev) => {
        const d = JSON.parse(ev.data)
        if (d.event === 'succeeded') finish()
        else if (d.event === 'failed') finish('运行失败：' + (d.error_code || '') + '（状态以服务端为准）')
      }
      es.onerror = () => finish(`运行事件流中断：结果未确认（run_id=${r.run_id}，服务端可能仍在执行，请刷新后按节点状态查看）`)
    } catch (e: any) {
      setRunning(false)
      const info = errInfo(e)
      showToast(info.rejected
        ? `服务端拒绝（HTTP ${info.status}）：${info.message}${info.manual ? ' 需人工排查' : ''}`
        : '结果未确认：' + info.message)
    }
  }

  // —— 阶段 C：工作流真实执行（状态一律以服务端返回为准）——
  const runNodeAndWait = async (nid: string) => {
    const r = await api.runNode(nid, { idempotency_key: `idem_${nid}_${Date.now()}` })
    const deadline = Date.now() + 180000
    while (Date.now() < deadline) {
      await new Promise((res) => setTimeout(res, 800))
      const row = await api.getRun(r.run_id)
      if (row.status === 'succeeded' || row.status === 'failed' || row.status === 'canceled') {
        return { run_id: r.run_id, status: row.status, error: row.error_code }
      }
    }
    return { run_id: r.run_id, status: 'running', error: '等待超时：结果未确认（服务端可能仍在执行，可查询该 run_id）' }
  }

  const stopPendingPoll = () => {
    pollRef.current.stop = true
    if (pollRef.current.timer) { clearTimeout(pollRef.current.timer); pollRef.current.timer = null }
  }

  /** 限时轮询 pending_run_ids：只读状态；轮询失败/停止都不会取消服务端任务，也不会自动重新提交生成 */
  const pollPendingRuns = (runIds?: string[]) => {
    stopPendingPoll()
    const ids = Array.from(new Set((runIds || []).filter(Boolean)))
    if (!ids.length) return
    const state = { stop: false, timer: null as any }
    pollRef.current = state
    setPollInfo({ ids, statuses: {}, running: ids.length, done: 0, unknown: 0, ended: false })
    const deadline = Date.now() + 60000
    const isTerminal = (s: string) => s === 'succeeded' || s === 'failed' || s === 'canceled'
    const tick = async () => {
      if (state.stop) return
      const statuses: Record<string, string> = {}
      let unknown = 0
      for (const rid of ids) {
        try {
          const row = await api.getRun(rid)
          statuses[rid] = (row && row.status) || 'unknown'
        } catch {
          statuses[rid] = 'query-unknown'   // 查询失败/无权限：只标未知，不等于任务失败
          unknown++
        }
      }
      if (state.stop) return
      const running = ids.filter((r) => !isTerminal(statuses[r]) && statuses[r] !== 'query-unknown').length
      const done = ids.filter((r) => isTerminal(statuses[r])).length
      setPollInfo({ ids, statuses, running, done, unknown, ended: running === 0 })
      if (running === 0) {
        try { await refreshGraph(); await refreshAssets() } catch { /* 忽略 */ }
        showToast('当前节点运行已结束；后续节点尚未自动执行，可由用户确认后继续')
        return
      }
      if (Date.now() > deadline) {
        setPollInfo({ ids, statuses, running, done, unknown, ended: true, timedOut: true })
        showToast('轮询已结束：结果未确认（服务端任务仍在继续，不会被取消）')
        return
      }
      state.timer = setTimeout(tick, 2000)
    }
    tick()
  }

  // 组件卸载只停止前端查询，不影响服务端任务
  useEffect(() => () => stopPendingPoll(), [])

  // 从某节点运行下游：后端执行的是「该节点的下游」，默认不重跑该节点
  const runDownstreamFrom = async (fromNodeId: string) => {
    if (!graph || !pid) { showToast('请先打开一个项目'); return }
    if (busyRef.current) { showToast('正在运行中，请等待本次运行结束'); return }
    stopPendingPoll()
    setRunning(true)
    try {
      const r = await api.runDownstream(graph.graph_id, fromNodeId)
      setDs({ ...r, finished_at: Date.now() })
      await refreshGraph(); await refreshAssets()
      if (r && r.status === 'running' && (r.pending_run_ids || []).length) pollPendingRuns(r.pending_run_ids)
      showToast(dsSummary(r))
    } catch (e: any) {
      const info = errInfo(e)
      setDs({ graph_id: graph.graph_id, from_node_id: fromNodeId,
              status: info.rejected ? 'rejected' : 'unknown',
              local_kind: info.rejected ? 'rejected' : 'network',
              local_status: info.status,
              ran: [],
              local_message: info.rejected
                ? `服务端拒绝本次运行（HTTP ${info.status}）：${info.message}${info.manual ? ' 需人工排查。' : ''}${info.hint ? ' ' + info.hint : ''}`
                : '未能从服务端确认本次运行结果（网络中断或请求超时）。服务端可能仍在执行，请刷新页面按节点状态确认，不要视为成功。',
              finished_at: Date.now() })
      showToast(info.rejected
        ? `服务端拒绝（HTTP ${info.status}）：${info.message.slice(0, 60)}${info.manual ? '（需人工排查）' : ''}`
        : '运行结果未确认：' + info.message + '（可能仍在执行，请勿重复触发）')
      try { await refreshGraph() } catch { /* 忽略 */ }
    } finally { setRunning(false) }
  }

  // 一键运行：仍由服务端按依赖顺序执行，前端只选起点；无法安全覆盖全图时不执行任何节点
  const runWhole = async () => {
    if (!graph || !pid) return
    if (busyRef.current) { showToast('正在运行中，请等待本次运行结束'); return }
    const plan = planWholeGraphRun(graph)
    if (!plan.start) {
      setDs({ graph_id: graph.graph_id, status: 'unsupported', local_kind: 'unsupported', local_message: plan.reason, finished_at: Date.now() })
      showToast('未执行：现有接口无法安全覆盖全图，详见运行结果面板')
      return
    }
    stopPendingPoll()
    setRunning(true)
    let prefix: DSRanEntry[] = []
    try {
      const startNode = graph.nodes.find((n) => n.id === plan.start)
      if (plan.runSelfFirst && startNode) {
        const r1 = await runNodeAndWait(startNode.id)
        const g2: any = await api.getGraph(pid)
        const after = nodeVersion((g2.nodes.find((n: any) => n.id === startNode.id) || {}))
        const entry: DSRanEntry = { node_id: startNode.id, type: startNode.type, run_id: r1.run_id, status: r1.status,
                                     version_before: nodeVersion(startNode), version_after: after,
                                     error: r1.status === 'succeeded' ? undefined : r1.error }
        if (r1.status === 'running') {
          setDs({ graph_id: graph.graph_id, from_node_id: startNode.id, status: 'running', ran: [], skipped: [], failed: null,
                  pending: [{ ...entry, reason: '等待超时，起点节点仍在运行' }],
                  pending_run_ids: r1.run_id ? [r1.run_id] : [],
                  not_executed: graph.nodes.filter((n) => n.id !== startNode.id && AUTO_RUN_TYPES.includes(n.type)).map((n) => ({ node_id: n.id, type: n.type })),
                  local_message: '起始节点等待超时但仍在运行（结果未确认，不是失败），因此未继续执行下游、也未重试。请刷新后按节点状态确认，或稍后用该 run_id 只读查询。',
                  finished_at: Date.now() })
          await refreshGraph(); await refreshAssets()
          if (r1.run_id) pollPendingRuns([r1.run_id])
          showToast('一键运行结果未确认：起始节点仍在运行，未继续下游')
          return
        }
        if (r1.status !== 'succeeded') {
          setDs({ graph_id: graph.graph_id, from_node_id: startNode.id, status: 'failed', ran: [], skipped: [], failed: entry,
                  not_executed: graph.nodes.filter((n) => n.id !== startNode.id && AUTO_RUN_TYPES.includes(n.type)).map((n) => ({ node_id: n.id, type: n.type })),
                  finished_at: Date.now() })
          await refreshGraph(); await refreshAssets()
          showToast('一键运行失败：起始节点未成功，后续节点未执行')
          return
        }
        prefix = [entry]
      }
      const r = await api.runDownstream(graph.graph_id, plan.start)
      const merged = { ...r, ran: [...prefix, ...(r.ran || [])], finished_at: Date.now() }
      setDs(merged)
      await refreshGraph(); await refreshAssets()
      if (merged.status === 'running' && (merged.pending_run_ids || []).length) pollPendingRuns(merged.pending_run_ids)
      showToast(dsSummary(merged))
    } catch (e: any) {
      const info = errInfo(e)
      // 第二步失败/未确认时保留第一步已完成的记录，不用整体替换把它抹掉
      setDs({ graph_id: graph.graph_id, from_node_id: plan.start,
              status: info.rejected ? 'rejected' : 'unknown',
              local_kind: info.rejected ? 'rejected' : 'network',
              local_status: info.status,
              ran: prefix,
              local_message: (prefix.length ? `第一步已完成 ${prefix.length} 个节点（见下方「已执行」）；第二步未能确认：` : '未能从服务端确认本次运行结果：')
                + (info.rejected
                    ? `服务端拒绝（HTTP ${info.status}）：${info.message}${info.manual ? ' 需人工排查。' : ''}${info.hint ? ' ' + info.hint : ''}`
                    : '网络中断或请求超时，服务端可能仍在执行，请刷新后按节点状态确认，不要视为成功。'),
              finished_at: Date.now() })
      showToast(prefix.length
        ? `第二步未确认：第一步已执行 ${prefix.length} 个节点（结果未确认，请勿重复触发）`
        : (info.rejected ? `服务端拒绝（HTTP ${info.status}）：${info.message.slice(0, 50)}` : '一键运行结果未确认：' + info.message))
      try { await refreshGraph() } catch { /* 忽略 */ }
    } finally { setRunning(false) }
  }

  // 节点工具条通过 ref 调用，始终指向最新实现（避免旧闭包）
  useEffect(() => { runDownstreamRef.current = (nid: string) => { runDownstreamFrom(nid) } })


  // —— 素材与产品素材节点的绑定（只写节点内容，不改素材自身 role）——
  const imgNode = useMemo(() => (graph?.nodes.find((n) => n.type === 'product_image') || null), [graph])
  const boundAssetIds = useMemo(() => readBoundRefs(imgNode?.content), [imgNode])

  const bindAsset = async (a: Asset) => {
    if (!imgNode) { showToast('当前工作流没有「产品素材」节点，请先初始化模板或手动添加'); return }
    const c: any = imgNode.content || {}
    const refs = readBoundRefs(c)
    const next = refs.includes(a.id) ? refs.filter((x) => x !== a.id) : [...refs, a.id]
    try {
      await api.patchNode(imgNode.id, { content: boundContent(c, next, assets) })
      await refreshGraph()
      showToast(next.includes(a.id) ? '已绑定到产品素材节点' : '已从产品素材节点解绑')
    } catch (e: any) { showToast('绑定失败：' + (e?.message || e)) }
  }

  // —— 事实卡保存 ——
  const saveFacts = async (content: any) => {
    if (!selected) return
    await api.patchNode(selected.id, { content })
    await refreshGraph(); showToast('事实卡已保存')
  }

  const renameNode = async (nid: string, name: string) => { await api.renameNode(nid, name); await refreshGraph() }

  const runImageTool = async (nodeId: string, intent: string, assetId: string, title: string, subtitle: string) => {
    const r = await api.runImageTool(nodeId, intent, { asset_id: assetId, title, subtitle })
    if (r.status === 'noop') return
    if (r.outputs && r.outputs.length === 0) throw new Error('未产生输出（该意图可能未接入模型）')
  }

  const doAiEdit = async () => {
    if (!selected || !aiInstr.trim()) return
    const r = await api.aiEdit(selected.id, aiInstr)
    setCandidate(r); showToast('已生成候选修改，请预览后应用')
  }
  const applyCand = async () => {
    if (!selected || !candidate) return
    await api.applyCandidate(selected.id, candidate.candidate_id, candidate.base_version)
    setCandidate(null); setAiInstr(''); await refreshGraph(); showToast('已应用候选（生成新版本）')
  }

  return (
    <div className="wb-root">
      <div className="topbar">
        <h1>AI 多节点产品营销生图工作台{templateName ? ` · ${templateName}` : ''}</h1>
        <button className="btn primary" onClick={newProject} disabled={creating}>{creating ? '创建中…' : '+ 新建项目'}</button>
        <button className="btn" disabled={!pid || creating || busy} onClick={initTpl}
          title={busy ? '运行中不可重置模板：请等待本次运行结束' : '会删除当前图全部节点、连线及节点内容（需二次确认）'}>{busy ? '运行中不可重置' : '重置模板'}</button>
        <button className="btn primary" disabled={!pid || busy} onClick={runWhole}
          title="服务端按工作流依赖顺序运行可执行节点（失败即停）；前端只选择起点，无依赖关系的分支不会被重跑">{busy ? '运行中…' : '一键运行（服务端拓扑）'}</button>
        <div className="spacer" />
        <button className="btn ghost" disabled={!pid} onClick={() => { if (pid) window.open(api.exportPackage(pid)) }}>导出素材包</button>
        <button className="btn ghost" disabled={!pid} onClick={() => { if (pid) api.exportProject(pid).then((d) => downloadJson(d, `${pid}.json`)) }}>导出项目JSON</button>
        <button className="btn ghost" disabled={!pid} onClick={async () => { if (!pid) return; const p = await api.agentPlan(pid, '为当前产品生成三套促销视觉'); showToast('Agent 计划已生成（草案，需批准后执行）'); console.log(p) }}>Agent 计划</button>
        <button className="btn" onClick={() => setShowModelMgr(true)}>模型管理</button>
      </div>

      <div className="body">
        <LeftNav onBack={onBack} onAddNode={addNode} onRun={runWhole} onModels={() => setShowModelMgr(true)} />
        <div className="sidebar">
          {templateName && (
            <div className="tpl-banner">
              <div className="tpl-banner-name">{templateName}</div>
              <div className="tpl-banner-sub">{skeleton ? (SKELETON_LABEL[skeleton] || skeleton) : '通用'}工作流</div>
            </div>
          )}
          <h3>项目</h3>
          {projects.map((p) => (
            <div key={p.id} className={`proj-item ${p.id === pid ? 'active' : ''}`} onClick={() => setPid(p.id)}
              onDoubleClick={async () => { const n = prompt('重命名项目', p.name); if (n && n.trim()) { await api.renameProject(p.id, n.trim()); await refreshProjects(); showToast('已重命名') } }}>
              <span className="proj-name">{p.name}</span>
              <button className="proj-del" title="删除项目" onClick={(e) => { e.stopPropagation(); if (confirm(`删除项目「${p.name}」？该操作不可恢复。`)) { api.deleteProject(p.id).then(() => { if (p.id === pid) { setPid(''); setGraph(null) }; refreshProjects() }) } }}>×</button>
            </div>
          ))}
          {!projects.length && <div className="meta" style={{ padding: '8px 14px' }}>点击「新建项目」开始</div>}

          <h3>素材库</h3>
          {pid && (
            <>
              <div style={{ padding: '0 14px 4px' }}>
                {ASSET_ROLES.map((r) => (
                  <label key={r.role} className="btn" title={r.hint} style={{ margin: '0 6px 6px 0', display: 'inline-block', fontSize: 12 }}>
                    + {r.label}
                    <input type="file" accept="image/*" multiple style={{ display: 'none' }}
                      onChange={async (e) => {
                        const files = Array.from(e.target.files || []); e.target.value = ''
                        if (!files.length) return
                        try { await api.uploadAssetsBatch(pid, files, r.role); await refreshAssets(); showToast(`已上传 ${files.length} 张（${r.label}）`) }
                        catch (err: any) { showToast('上传失败：' + (err?.message || err)) }
                      }} />
                  </label>
                ))}
              </div>
              <div className="meta" style={{ padding: '0 14px 8px' }}>产品图与 Logo 参与生成；参考图仅作提示参考，不叠加到本地成图。</div>
              <label className="btn" style={{ margin: '0 14px 8px', display: 'inline-block' }}>
                导入文件转文字
                <input type="file" accept=".txt,.md,.json,.csv,.docx" style={{ display: 'none' }}
                  onChange={async (e) => { const f = e.target.files?.[0]; if (f) { try { const r = await api.importTextFile(pid, f); setImportedText(r.text); showToast('已导入文字') } catch (err: any) { showToast('失败：' + err.message) } } }} />
              </label>
            </>
          )}
          {assets.map((a) => {
            const bindable = a.role === 'product' || a.role === 'logo' || a.role === 'reference'
            const bound = boundAssetIds.includes(a.id)
            return (
              <div className="asset-card" key={a.id}>
                <img src={fileUrl(a.id)} alt="" />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="meta">{ASSET_ROLE_LABEL[a.role] || a.role} · {a.width}×{a.height}</div>
                  <div className="meta">{a.role === 'reference' ? '参考（不叠加成图）' : bindable ? '参与生成' : '生成 / 导出产物'} · {bound ? '已绑定素材节点' : '未绑定'}</div>
                  {bindable && (
                    <button className="btn" style={{ fontSize: 11, padding: '2px 6px', marginTop: 4 }}
                      disabled={!imgNode} title={imgNode ? '绑定到产品素材节点（只改节点记录，不改素材角色）' : '当前工作流没有「产品素材」节点'}
                      onClick={() => bindAsset(a)}>{bound ? '解除绑定' : '绑定到素材节点'}</button>
                  )}
                </div>
              </div>
            )
          })}

          <h3>工作流节点</h3>
          <div className="node-palette">
            {NODE_PALETTE.map((n) => (
              <button key={n.type} className="btn" disabled={!pid} onClick={() => addNode(n.type)}>+ {n.label}</button>
            ))}
          </div>
          <button className="btn danger" disabled={!selectedId} onClick={deleteSelected}>删除选中节点</button>
        </div>

        <div className="canvas-wrap">
          {graph ? (
            <ReactFlow nodes={rfNodes} edges={rfEdges} nodeTypes={nodeTypes}
              onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
              onConnect={onConnect} onNodeDragStop={onNodeDragStop}
              onNodeClick={(_, n) => setSelectedId(n.id)}
              onInit={setRfInstance}
              onMove={(_, vp) => setZoom(vp.zoom)}
              colorMode="light"
              minZoom={0.2} maxZoom={2.5}
              fitView fitViewOptions={{ padding: 0.18 }}
              selectionOnDrag panOnDrag={[1, 2]} selectionMode={SelectionMode.Partial}
              multiSelectionKeyCode={['Meta', 'Control', 'Shift']}
              deleteKeyCode={busy ? null : ['Delete', 'Backspace']}
              onNodesDelete={(del) => {
                if (busyRef.current) {
                  // 运行中禁止删除在途节点：不发删除请求，重新拉取图以恢复界面
                  showToast('运行中禁止删除节点：请等待本次运行结束后再操作')
                  refreshGraph()
                  return
                }
                del.forEach((n) => api.deleteNode(n.id)); if (del.some((n) => n.id === selectedId)) setSelectedId(''); refreshGraph()
              }}
              snapToGrid={snap} snapGrid={[16, 16]}
              onlyRenderVisibleElements
              defaultEdgeOptions={{ type: 'smoothstep', markerEnd: { type: MarkerType.ArrowClosed }, style: { stroke: '#b0b6bf' } }}>
              <Background variant={BG_VARIANT[gridType]} gap={gridType === 'dots' ? 18 : 22} size={1.4} color="#d8dce3" />
              <Controls />
              <MiniMap pannable zoomable nodeColor={(n: any) => NODE_COLORS[n.data?.node?.type] || '#97a0ad'}
                nodeStrokeWidth={2} maskColor="rgba(245,247,250,0.7)" style={{ background: '#fff', border: '1px solid #e6e9ee' }} />
              <CanvasToolbar gridType={gridType} setGridType={setGridType} snap={snap} setSnap={setSnap} zoom={zoom} />
            </ReactFlow>
          ) : (
            <div style={{ padding: 40, color: '#8a909a' }}>请先新建项目并初始化模板。</div>
          )}
        </div>

        <div className="right">
          {ds && <RunReport result={ds} graph={graph} poll={pollInfo} onClose={() => { setDs(null); stopPendingPoll(); setPollInfo(null) }} />}
          {selected ? (
            <>
              <input key={selected.id} className="node-name-input" defaultValue={nodeDisplayName(selected)}
                onBlur={async (e) => { const v = e.target.value.trim(); if (v && v !== nodeDisplayName(selected)) { await api.renameNode(selected.id, v); await refreshGraph(); showToast('已重命名节点') } }} />
              <div className="run-bar">
                <button className="btn primary" disabled={busy} onClick={() => runDownstreamFrom(selected.id)}
                  title="后端执行的是本节点的下游（run-downstream），按 DAG 拓扑顺序、失败即停；默认不重跑本节点">运行下游（不重跑本节点）</button>
                <div className="muted">服务端只执行本节点之后的下游节点；结果逐项展示在运行结果面板。</div>
              </div>
              <NodePanel key={selected.id} node={selected} pid={pid} assets={assets} models={models} defaultTextModel={defText} defaultImageModel={defImage} onRun={runNode}
                onSaveFacts={saveFacts} onPatch={async (c) => { await api.patchNode(selected.id, { content: c }); await refreshGraph() }}
                graph={graph}
                onPatchNode={async (nid, content) => { await api.patchNode(nid, { content }); await refreshGraph() }}
                onRefresh={refreshGraph} showToast={showToast} />
            </>
          ) : (
            <div className="muted">点击画布节点查看 / 编辑详情。</div>
          )}
        </div>
      </div>

      <div className="bottom-bar">
        <span className="muted">全局模型：</span>
        <select className="model-sel" value={defText} onChange={async (e) => { setDefText(e.target.value); await api.setDefaults(pid, { default_text_model: e.target.value }) }}>
          <option value="">— 文本默认 —</option>
          {models.filter((m) => m.modality === 'text' && m.enabled === 1).map((m) => <option key={m.model_id} value={m.model_id}>{m.model_id}</option>)}
        </select>
        <select className="model-sel" value={defImage} onChange={async (e) => { setDefImage(e.target.value); await api.setDefaults(pid, { default_image_model: e.target.value }) }}>
          <option value="">— 图片默认 —</option>
          {models.filter((m) => m.modality === 'image' && m.enabled === 1).map((m) => <option key={m.model_id} value={m.model_id}>{m.model_id}（{m.cost_policy}）</option>)}
        </select>
        <span className="sep" />
        <span className="muted">本次 AI 指令目标：</span>
        <strong style={{ minWidth: 120 }}>{selected ? NODE_LABELS[selected.type] : '（未选中节点）'}</strong>
        <input style={{ flex: 1 }} placeholder="输入只修改当前节点的指令，如：更有高级感 / 改为客厅场景 / 文案更年轻" value={aiInstr} onChange={(e) => setAiInstr(e.target.value)} />
        <button className="btn primary" disabled={!selected || busy} onClick={doAiEdit}>AI 修改（生成候选）</button>
        {candidate && (
          <button className="btn" onClick={applyCand}>应用候选</button>
        )}
      </div>

      {candidate && (
        <div className="candidate-box" style={{ position: 'fixed', right: 380, bottom: 70, width: 320, zIndex: 999 }}>
          <div className="section-title">AI 候选修改预览</div>
          <div className="hint">{candidate.summary}</div>
          <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 160, overflow: 'auto', fontSize: 11 }}>{JSON.stringify(candidate.content, null, 2)}</pre>
          <button className="btn primary" onClick={applyCand}>应用此候选</button>
          <button className="btn" onClick={() => setCandidate(null)}>丢弃</button>
        </div>
      )}

      {importedText && (
        <div className="candidate-box" style={{ position: 'fixed', right: 720, bottom: 70, width: 340, zIndex: 999 }}>
          <div className="section-title">导入的文字</div>
          <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 220, overflow: 'auto', fontSize: 12 }}>{importedText}</pre>
          <button className="btn" onClick={() => { navigator.clipboard?.writeText(importedText); showToast('已复制') }}>复制</button>
          <button className="btn primary" onClick={async () => {
            const fn = graph?.nodes.find((n) => n.type === 'product_facts')
            if (!fn) { showToast('未找到事实卡'); return }
            const cur = fn.content || {}
            const notes = (cur.recognition_notes || []).concat(importedText)
            await api.patchNode(fn.id, { content: { ...cur, recognition_notes: notes } })
            await refreshGraph(); setImportedText(''); showToast('已追加到事实卡备注')
          }}>追加到事实卡备注</button>
          <button className="btn" onClick={() => setImportedText('')}>关闭</button>
        </div>
      )}

      {busy && <div className="toast"><span className="spinner" /> 运行中…</div>}
      {toast && <div className="toast">{toast}</div>}

      {showModelMgr && (
        <ModelManager models={models} onClose={() => setShowModelMgr(false)}
          onChanged={() => { api.listModels().then(setModels); refreshGraph() }}
          showToast={showToast} />
      )}
    </div>
  )
}

function CanvasToolbar({ gridType, setGridType, snap, setSnap, zoom }: any) {
  const { fitView, zoomIn, zoomOut } = useReactFlow()
  return (
    <div className="canvas-tools">
      <button className="ct" title="适应视图（显示全部节点）" onClick={() => fitView({ padding: 0.18 })}>⤢</button>
      <button className="ct" title="放大" onClick={() => zoomIn({ duration: 150 })}>+</button>
      <button className="ct" title="缩小" onClick={() => zoomOut({ duration: 150 })}>−</button>
      <span className="ct-zoom">{Math.round(zoom * 100)}%</span>
      <div className="ct-sep" />
      <select className="ct-select" value={gridType} onChange={(e) => setGridType(e.target.value)} title="网格样式">
        <option value="dots">点阵</option>
        <option value="lines">直线</option>
        <option value="cross">十字</option>
      </select>
      <button className={`ct ${snap ? 'on' : ''}`} title="切换网格吸附（拖动节点对齐到 16px 网格）" onClick={() => setSnap(!snap)}>⊞</button>
    </div>
  )
}

function downloadJson(obj: any, name: string) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a'); a.href = url; a.download = name; a.click()
  URL.revokeObjectURL(url)
}

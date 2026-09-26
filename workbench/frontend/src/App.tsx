import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ReactFlow, Background, BackgroundVariant, Controls, MiniMap, Handle, Position,
  SelectionMode, useReactFlow, MarkerType, useNodesState, useEdgesState,
} from '@xyflow/react'
import { api, fileUrl } from './api'
import { Asset, GraphData, ModelInfo, WBNode, WBEdge } from './types'
import NodePanel from './NodePanel'
import ModelManager from './ModelManager'

const NODE_COLORS: Record<string, string> = {
  product_facts: '#7c5cff', product_image: '#13c2c2', strategy: '#4c7bf3',
  image_prompt: '#eb2f96', image_generation: '#fa8c16', review: '#52c41a', layout_export: '#722ed1',
}
const NODE_LABELS: Record<string, string> = {
  product_facts: '产品事实卡', product_image: '产品素材', strategy: '视觉策略',
  image_prompt: '单图提示词', image_generation: '生图节点', review: '审核', layout_export: '分层排版导出',
}

const BG_VARIANT: Record<string, BackgroundVariant> = {
  dots: BackgroundVariant.Dots, lines: BackgroundVariant.Lines, cross: BackgroundVariant.Cross,
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

interface RFNodeData { node: WBNode; intents?: any[]; tool?: (nodeId: string, intent: string, assetId: string, title: string, subtitle: string) => Promise<void>; toast?: (m: string) => void; refresh?: () => void; rename?: (nid: string, name: string) => void }

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
    if (v && v !== (n.name || NODE_LABELS[n.type])) data.rename?.(n.id, v)
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
            onDoubleClick={(e) => { e.stopPropagation(); setNameDraft(n.name || NODE_LABELS[n.type] || n.type); setRenaming(true) }}>
            {n.name || NODE_LABELS[n.type] || n.type}
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
      {isImgNode && hov && (
        <div className="node-toolbar">
          {topIntents.map((t) => (
            <button key={t} className={`nt ${enabled(t) ? '' : 'disabled'}`} disabled={!enabled(t) || running}
              title={enabled(t) ? t : `${t}（未接入）`} onClick={() => runTool(t)}>{t}</button>
          ))}
          <div className="nt-more">
            <button className="nt" disabled={running} onClick={() => setMoreOpen((v) => !v)}>更多 ▾</button>
            <div className={`nt-menu ${moreOpen ? 'open' : ''}`}>
              {moreIntents.map((t) => (
                <button key={t} className={`nt ${enabled(t) ? '' : 'disabled'}`} disabled={!enabled(t) || running}
                  title={enabled(t) ? t : `${t}（未接入）`} onClick={() => runTool(t)}>{t}</button>
              ))}
            </div>
          </div>
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

function toRF(graph: GraphData, intents: any[], tool: any, toast: any, refresh: any, rename: any) {
  const nodes = graph.nodes.map((n) => ({
    id: n.id, type: 'wb', position: n.position, data: { node: n, intents, tool, toast, refresh, rename },
  }))
  const edges = graph.edges.map((e: WBEdge) => ({
    id: e.id, source: e.from_node, target: e.to_node, label: e.semantic,
    markerEnd: { type: MarkerType.ArrowClosed }, style: { stroke: '#b0b6bf' },
  }))
  return { nodes, edges }
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
      <button className="ln-btn" title="一键运行" onClick={onRun}><Icon d="M6 4l14 8-14 8z" /></button>
      <button className="ln-btn" title="模型管理" onClick={onModels}><Icon d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z" /></button>
      <div className="ln-sep" />
      <button className="ln-btn disabled" title="文字（待接入）"><Icon d="M4 6V4h16v2M12 4v16m-3 0h6" /></button>
      <button className="ln-btn disabled" title="图形（待接入）"><Icon d="M4 6h16M4 6v12M4 18h16M20 6v12" /></button>
      <button className="ln-btn disabled" title="便签（待接入）"><Icon d="M5 3h14a2 2 0 0 1 2 2v8l-6 6H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM13 19v-6h6" /></button>
    </div>
  )
}

export default function App({ templateName, skeleton, onBack }: { templateName?: string; skeleton?: string; onBack?: () => void } = {}) {
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

  const refreshGraph = useCallback(async () => {
    if (!pid) return
    const g = await api.getGraph(pid)
    setGraph(g)
    const { nodes, edges } = toRF(g, intents, runImageTool, showToast, () => { refreshGraph(); refreshAssets() }, renameNode)
    setRfNodes(nodes)
    setRfEdges(edges)
  }, [pid, setRfNodes, setRfEdges, intents])

  const refreshAssets = useCallback(async () => {
    if (!pid) return
    setAssets(await api.listAssets(pid))
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

  const newProject = async () => {
    const name = prompt('项目名称（如：XX产品 618 主图）', templateName || '示例产品 · 营销主图')
    if (!name) return
    const p = await api.createProject(name)
    setProjects((ps) => [p, ...ps]); setPid(p.id)
    await api.initTemplate(p.id, skeleton || 'poster'); await refreshGraph(); await refreshAssets()
    showToast('已创建项目并初始化三图模板')
  }

  const initTpl = async () => { if (!pid) return; await api.initTemplate(pid, skeleton || 'poster'); await refreshGraph(); showToast('已重置为模板工作流') }

  const addNode = async (type: string) => {
    if (!pid || !graph) return
    await api.addNode(graph.graph_id, { type, x: 260 + Math.random() * 260, y: 140 + Math.random() * 260 })
    await refreshGraph(); showToast('已添加节点')
  }

  const deleteSelected = async () => {
    if (!pid || !selectedId) return
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
    setBusy(true)
    try {
      const r = await api.runNode(nid, { model_id, params, idempotency_key: `idem_${nid}_${Date.now()}` })
      const es = new EventSource(`/api/runs/${r.run_id}/events`)
      es.onmessage = (ev) => {
        const d = JSON.parse(ev.data)
        if (d.event === 'succeeded' || d.event === 'failed') {
          es.close(); setBusy(false); refreshGraph(); refreshAssets()
          if (d.event === 'failed') showToast('运行失败：' + (d.error_code || ''))
        }
      }
      es.onerror = () => { es.close(); setBusy(false); refreshGraph() }
    } catch (e: any) { setBusy(false); showToast('错误：' + e.message) }
  }

  const runOne = (nid: string, model_id?: string, params: any = {}) => new Promise<void>((resolve) => {
    api.runNode(nid, { model_id, params, idempotency_key: `idem_${nid}_${Date.now()}` }).then((r) => {
      const es = new EventSource(`/api/runs/${r.run_id}/events`)
      const done = () => { es.close(); resolve() }
      es.onmessage = (ev) => { try { const d = JSON.parse(ev.data); if (d.event === 'succeeded' || d.event === 'failed') done() } catch { done() } }
      es.onerror = () => done()
    }).catch(() => resolve())
  })

  const runAll = async () => {
    if (!graph || !pid) return
    setBusy(true)
    try {
      const byType = (t: string) => graph.nodes.filter((n) => n.type === t).map((n) => n.id)
      const seq = [...byType('strategy'), ...byType('image_prompt'), ...byType('image_generation')]
      for (const nid of seq) await runOne(nid)
      await refreshGraph(); await refreshAssets()
      showToast('工作流逐环节运行完成')
    } finally { setBusy(false) }
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
        <button className="btn primary" onClick={newProject}>+ 新建项目</button>
        <button className="btn" disabled={!pid} onClick={initTpl}>重置模板</button>
        <button className="btn primary" disabled={!pid || busy} onClick={runAll}>一键运行</button>
        <div className="spacer" />
        <button className="btn ghost" disabled={!pid} onClick={() => { if (pid) window.open(api.exportPackage(pid)) }}>导出素材包</button>
        <button className="btn ghost" disabled={!pid} onClick={() => { if (pid) api.exportProject(pid).then((d) => downloadJson(d, `${pid}.json`)) }}>导出项目JSON</button>
        <button className="btn ghost" disabled={!pid} onClick={async () => { if (!pid) return; const p = await api.agentPlan(pid, '为当前产品生成三套促销视觉'); showToast('Agent 计划已生成（草案，需批准后执行）'); console.log(p) }}>Agent 计划</button>
        <button className="btn" onClick={() => setShowModelMgr(true)}>模型管理</button>
      </div>

      <div className="body">
        <LeftNav onBack={onBack} onAddNode={addNode} onRun={runAll} onModels={() => setShowModelMgr(true)} />
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
              <label className="btn" style={{ margin: '0 14px 8px', display: 'inline-block' }}>
                上传素材（可多选）
                <input type="file" accept="image/*" multiple style={{ display: 'none' }}
                  onChange={async (e) => { const files = Array.from(e.target.files || []); if (files.length) { await api.uploadAssetsBatch(pid, files, 'product'); await refreshAssets(); showToast(`已上传 ${files.length} 张`) } }} />
              </label>
              <label className="btn" style={{ margin: '0 14px 8px', display: 'inline-block' }}>
                导入文件转文字
                <input type="file" accept=".txt,.md,.json,.csv,.docx" style={{ display: 'none' }}
                  onChange={async (e) => { const f = e.target.files?.[0]; if (f) { try { const r = await api.importTextFile(pid, f); setImportedText(r.text); showToast('已导入文字') } catch (err: any) { showToast('失败：' + err.message) } } }} />
              </label>
            </>
          )}
          {assets.map((a) => (
            <div className="asset-card" key={a.id}>
              <img src={fileUrl(a.id)} alt="" />
              <div className="meta">{a.role} · {a.width}×{a.height}</div>
            </div>
          ))}

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
              deleteKeyCode={['Delete', 'Backspace']}
              onNodesDelete={(del) => { del.forEach((n) => api.deleteNode(n.id)); if (del.some((n) => n.id === selectedId)) setSelectedId(''); refreshGraph() }}
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
          {selected ? (
            <>
              <input key={selected.id} className="node-name-input" defaultValue={selected.name || NODE_LABELS[selected.type] || selected.type}
                onBlur={async (e) => { const v = e.target.value.trim(); if (v && v !== (selected.name || NODE_LABELS[selected.type])) { await api.renameNode(selected.id, v); await refreshGraph(); showToast('已重命名节点') } }} />
              <NodePanel key={selected.id} node={selected} pid={pid} assets={assets} models={models} defaultTextModel={defText} defaultImageModel={defImage} onRun={runNode}
                onSaveFacts={saveFacts} onPatch={async (c) => { await api.patchNode(selected.id, { content: c }); await refreshGraph() }}
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

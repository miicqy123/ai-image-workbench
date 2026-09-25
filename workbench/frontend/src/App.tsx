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

interface RFNodeData { node: WBNode; intents?: any[]; tool?: (nodeId: string, intent: string, assetId: string, title: string, subtitle: string) => Promise<void>; toast?: (m: string) => void; refresh?: () => void }

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

  return (
    <div className={`wb-node ${selected ? 'selected' : ''}`} onMouseEnter={() => setHov(true)} onMouseLeave={() => setHov(false)}>
      <div className="nh" style={{ background: color }}>
        <span>{NODE_LABELS[n.type] || n.type}</span>
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
const nodeTypes = { wb: WBNodeComp }

function toRF(graph: GraphData, intents: any[], tool: any, toast: any, refresh: any) {
  const nodes = graph.nodes.map((n) => ({
    id: n.id, type: 'wb', position: n.position, data: { node: n, intents, tool, toast, refresh },
  }))
  const edges = graph.edges.map((e: WBEdge) => ({
    id: e.id, source: e.from_node, target: e.to_node, label: e.semantic,
    markerEnd: { type: MarkerType.ArrowClosed }, style: { stroke: '#b0b6bf' },
  }))
  return { nodes, edges }
}

export default function App() {
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
    const { nodes, edges } = toRF(g, intents, runImageTool, showToast, () => { refreshGraph(); refreshAssets() })
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
    const name = prompt('项目名称（如：XX产品 618 主图）', '示例产品 · 营销主图')
    if (!name) return
    const p = await api.createProject(name)
    setProjects((ps) => [p, ...ps]); setPid(p.id)
    await api.initTemplate(p.id); await refreshGraph(); await refreshAssets()
    showToast('已创建项目并初始化三图模板')
  }

  const initTpl = async () => { if (!pid) return; await api.initTemplate(pid); await refreshGraph(); showToast('已重置为三图模板') }

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

  // —— 事实卡保存 ——
  const saveFacts = async (content: any) => {
    if (!selected) return
    await api.patchNode(selected.id, { content })
    await refreshGraph(); showToast('事实卡已保存')
  }

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
        <h1>AI 多节点产品营销生图工作台</h1>
        <button className="btn primary" onClick={newProject}>+ 新建项目</button>
        <button className="btn" disabled={!pid} onClick={initTpl}>重置模板</button>
        <div className="spacer" />
        <button className="btn ghost" disabled={!pid} onClick={() => { if (pid) window.open(api.exportPackage(pid)) }}>导出素材包</button>
        <button className="btn ghost" disabled={!pid} onClick={() => { if (pid) api.exportProject(pid).then((d) => downloadJson(d, `${pid}.json`)) }}>导出项目JSON</button>
        <button className="btn ghost" disabled={!pid} onClick={async () => { if (!pid) return; const p = await api.agentPlan(pid, '为当前产品生成三套促销视觉'); showToast('Agent 计划已生成（草案，需批准后执行）'); console.log(p) }}>Agent 计划</button>
        <button className="btn" onClick={() => setShowModelMgr(true)}>模型管理</button>
      </div>

      <div className="body">
        <div className="sidebar">
          <h3>项目</h3>
          {projects.map((p) => (
            <div key={p.id} className={`proj-item ${p.id === pid ? 'active' : ''}`} onClick={() => setPid(p.id)}>
              {p.name}
            </div>
          ))}
          {!projects.length && <div className="meta" style={{ padding: '8px 14px' }}>点击「新建项目」开始</div>}

          <h3>素材库</h3>
          {pid && (
            <label className="btn" style={{ margin: '0 14px 8px', display: 'inline-block' }}>
              上传素材
              <input type="file" accept="image/*" style={{ display: 'none' }}
                onChange={async (e) => { const f = e.target.files?.[0]; if (f) { await api.uploadAsset(pid, f, 'product'); await refreshAssets(); showToast('已上传') } }} />
            </label>
          )}
          {assets.map((a) => (
            <div className="asset-card" key={a.id}>
              <img src={fileUrl(a.id)} alt="" />
              <div className="meta">{a.role} · {a.width}×{a.height}</div>
            </div>
          ))}
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
              deleteKeyCode={null}
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
            <NodePanel key={selected.id} node={selected} pid={pid} assets={assets} models={models} defaultTextModel={defText} defaultImageModel={defImage} onRun={runNode}
              onSaveFacts={saveFacts} onPatch={async (c) => { await api.patchNode(selected.id, { content: c }); await refreshGraph() }}
              onRefresh={refreshGraph} showToast={showToast} />
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

import React, { useRef, useState } from 'react'
import Toolbar, { STICKY_COLORS } from './Toolbar'
import AiPanel from './AiPanel'
import { api, fileUrl } from './api'
import { AspectRatio, CanvasElement, ShapeKind, Template } from './types'

const WORKFLOW_STAGES = ['上传产品图', '事实与卖点', '视觉策略', '提示词', '生图', '审核', '导出']

const RATIO: Record<AspectRatio, number> = {
  free: 4 / 3, '16:9': 16 / 9, '4:3': 4 / 3, '1:1': 1, '3:4': 3 / 4, '9:16': 9 / 16, a4: 210 / 297,
}

function boardSize(aspect: AspectRatio) {
  const r = RATIO[aspect]
  if (r >= 1) return { w: 760, h: Math.round(760 / r) }
  return { w: Math.round(760 * r), h: 760 }
}

let uid = 0
const nid = () => 'el_' + (++uid) + '_' + Math.random().toString(36).slice(2, 6)

interface Drag {
  mode: 'pan' | 'element'
  id?: string
  sx: number
  sy: number
  ox: number
  oy: number
}

export default function DesignCanvas({ template, onBack }: { template: Template; onBack: () => void }) {
  const [aspect, setAspect] = useState<AspectRatio>(template.aspect)
  const [elements, setElements] = useState<CanvasElement[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [active, setActive] = useState('')
  const [pendingShape, setPendingShape] = useState<ShapeKind | null>(null)
  const [pendingSticky, setPendingSticky] = useState<string | null>(null)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [showExport, setShowExport] = useState(false)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [uploadedFiles, setUploadedFiles] = useState<File[]>([])
  const [generatedAssets, setGeneratedAssets] = useState<string[]>([])
  const [projectId, setProjectId] = useState('')
  const [candidates, setCandidates] = useState<any[]>([])
  const [workflow, setWorkflow] = useState<{ stages: { id: string; label: string; status: string }[]; current: number } | null>(null)
  const boardRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<Drag | null>(null)
  const size = boardSize(aspect)

  const add = (el: CanvasElement) => {
    setElements((es) => [...es, el])
    setSelected(el.id)
  }

  const posFromEvent = (e: React.PointerEvent) => {
    const r = boardRef.current!.getBoundingClientRect()
    return { x: e.clientX - r.left, y: e.clientY - r.top }
  }

  function onBoardDown(e: React.PointerEvent) {
    if (active === 'pan') {
      dragRef.current = { mode: 'pan', sx: e.clientX, sy: e.clientY, ox: pan.x, oy: pan.y }
      return
    }
    const p = posFromEvent(e)
    if (active === 'text') {
      add({ id: nid(), type: 'text', x: p.x, y: p.y, w: 180, h: 44, text: '点击编辑文字' })
      setActive('')
    } else if (pendingShape) {
      add({ id: nid(), type: 'shape', shape: pendingShape, x: p.x - 45, y: p.y - 45, w: 90, h: 90, color: '#9CA3AF' })
      setPendingShape(null); setActive('')
    } else if (pendingSticky) {
      add({ id: nid(), type: 'sticky', color: pendingSticky, x: p.x - 65, y: p.y - 50, w: 130, h: 100, text: '' })
      setPendingSticky(null); setActive('')
    } else {
      setSelected(null)
    }
  }

  function onElementDown(e: React.PointerEvent, el: CanvasElement) {
    e.stopPropagation()
    setSelected(el.id)
    dragRef.current = { mode: 'element', id: el.id, sx: e.clientX, sy: e.clientY, ox: el.x, oy: el.y }
  }

  function onMove(e: React.PointerEvent) {
    const d = dragRef.current
    if (!d) return
    if (d.mode === 'pan') {
      setPan({ x: d.ox + (e.clientX - d.sx), y: d.oy + (e.clientY - d.sy) })
    } else if (d.mode === 'element' && d.id) {
      setElements((es) => es.map((el) => (el.id === d.id ? { ...el, x: d.ox + (e.clientX - d.sx), y: d.oy + (e.clientY - d.sy) } : el)))
    }
  }
  function onUp() { dragRef.current = null }

  function onKey(e: React.KeyboardEvent) {
    const t = e.target as HTMLElement
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
    if (e.key === 'Delete' || e.key === 'Backspace') {
      if (selected) { setElements((es) => es.filter((el) => el.id !== selected)); setSelected(null) }
    } else if (e.shiftKey && (e.key === 'T' || e.key === 't')) onNewNode('text')
    else if (e.shiftKey && (e.key === 'I' || e.key === 'i')) onNewNode('image')
    else if (e.shiftKey && (e.key === 'V' || e.key === 'v')) onNewNode('video')
    else if (e.key === 'h' || e.key === 'H') setActive('pan')
    else if (e.key === 't' || e.key === 'T') setActive('text')
    else if (e.key === 'n' || e.key === 'N') { setPendingSticky(STICKY_COLORS[0]); setActive('') }
  }

  function onUpload(files: FileList | null) {
    if (!files) return
    const imgs = Array.from(files).filter((f) => f.type.startsWith('image/'))
    if (imgs.length !== files!.length) alert('已跳过不支持的图片格式')
    setUploadedFiles((prev) => [...prev, ...imgs])
    imgs.forEach((f) => {
      const url = URL.createObjectURL(f)
      add({ id: nid(), type: 'image', imageUrl: url, x: size.w / 2 - 120, y: size.h / 2 - 90, w: 240, h: 180 })
    })
  }

  function onNewNode(kind: 'text' | 'image' | 'video') {
    const label = kind === 'text' ? '文字生成' : kind === 'image' ? '图片生成' : '视频生成'
    add({ id: nid(), type: 'node', nodeKind: kind, text: label, x: size.w / 2 - 90, y: size.h / 2 - 55, w: 180, h: 110 })
  }

  const notify = (m: string) => { setNotice(m); setTimeout(() => setNotice(''), 2600) }

  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

  const download = (url: string, name: string) => { const a = document.createElement('a'); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove() }

  const runAndWait = async (nid: string, params: any = {}, model_id?: string) => {
    const r = await api.runNode(nid, { params, model_id: model_id || undefined })
    for (let i = 0; i < 180; i++) {
      await sleep(500)
      const run = await api.getRun(r.run_id)
      if (run.status === 'succeeded') return
      if (run.status === 'failed') throw new Error(run.error_code || '节点运行失败')
    }
    throw new Error('运行超时')
  }

  const toLayers = () => elements.map((el) => {
    if (el.type === 'image') {
      const m = /\/files\/([^/?]+)/.exec(el.imageUrl || '')
      return { type: 'image', asset_id: m ? m[1] : null, x: el.x, y: el.y, width: el.w, height: el.h }
    }
    if (el.type === 'sticky') return { type: 'sticky', text: el.text || '', color: el.color || '#FEF3C7', x: el.x, y: el.y, width: el.w, height: el.h }
    if (el.type === 'shape') return { type: 'rect', color: el.color || '#9CA3AF', x: el.x, y: el.y, width: el.w, height: el.h }
    if (el.type === 'text') return { type: 'text', text: el.text || '', font_size: 24, fill: '#0F172A', x: el.x, y: el.y, width: el.w, height: el.h }
    return null
  }).filter(Boolean)

  const exportCanvasPng = async () => {
    if (!projectId) { notify('请先点「生成」创建项目'); return }
    try {
      await api.putCanvas(projectId, { width: size.w, height: size.h, document: { version: 1, width: size.w, height: size.h, layers: toLayers() } })
      const r = await api.exportCanvas(projectId)
      window.open(fileUrl(r.asset_id))
      notify('已导出画布 PNG（含全部图层）')
    } catch (e: any) { notify('导出失败：' + (e?.message || e)) }
  }

  const onGenerate = async (opts: { count: number; platform: string; ratio: string; prompt: string; model_id?: string }) => {
    setBusy(true)
    setWorkflow({ stages: WORKFLOW_STAGES.map((s, i) => ({ id: 's' + i, label: s, status: i === 0 ? 'active' : 'pending' })), current: 0 })
    const mark = (idx: number) => setWorkflow((w) => w ? { ...w, current: idx, stages: w.stages.map((s, i) => ({ ...s, status: i < idx ? 'done' : i === idx ? 'active' : 'pending' })) } : w)
    try {
      const p = await api.createProject(template.name + (opts.platform ? ' · ' + opts.platform : ''))
      setProjectId(p.id)
      await api.initTemplate(p.id, template.skeleton || 'poster')
      let uploaded: string[] = []
      for (const f of uploadedFiles) {
        const a = await api.uploadAsset(p.id, f, 'product')
        uploaded.push(a.id)
      }
      await api.upsertGenerationBrief(p.id, {
        user_prompt: opts.prompt, purpose: 'marketing_poster', platform: opts.platform,
        aspect_ratio: opts.ratio, image_count: opts.count, selected_model_id: opts.model_id || null, selected_prompt_template_id: (opts as any).template_id || null,
        product_asset_ids: uploaded, reference_asset_ids: [],
      })
      mark(3)
      const job = await api.createJob(p.id, { model_id: opts.model_id || undefined, task_type: uploaded.length ? 'product_composition' : 'text_to_image' })
      mark(4)
      let st = 'queued'
      for (let i = 0; i < 240; i++) {
        await sleep(500)
        const j = await api.getJob(job.id)
        st = j.status
        if (st === 'succeeded' || st === 'failed' || st === 'canceled') break
      }
      if (st === 'failed') throw new Error('生成任务失败（请检查模型/服务商配置，或确认 worker 已启动）')
      if (st !== 'succeeded') throw new Error('生成任务超时（请确认生图 worker 正在运行）')
      mark(6)
      const cands = await api.listCandidates(p.id)
      setCandidates(cands)
      notify(cands.length ? `已生成 ${cands.length} 张候选图` : '未产出候选图')
    } catch (e: any) {
      notify('运行失败：' + (e?.message || e))
    } finally {
      setBusy(false)
    }
  }

  const runNext = () => {
    if (!workflow || workflow.current >= workflow.stages.length - 1) return
    const idx = workflow.current
    const next = idx + 1
    const updated = workflow.stages.map((s, i) => {
      if (i < next) return { ...s, status: 'done' }
      if (i === next) return { ...s, status: s.label === '生图' ? 'blocked' : 'active' }
      return s
    })
    setWorkflow({ stages: updated, current: next })
    if (workflow.stages[next].label === '生图') {
      notify('生图环节待接入：请在「模型/服务商管理」配置真实模型')
    }
  }

  const updateText = (id: string, text: string) => setElements((es) => es.map((el) => (el.id === id ? { ...el, text } : el)))

  return (
    <div className="design-root" onKeyDown={onKey} tabIndex={0}>
      <header className="design-top">
        <button className="btn ghost" onClick={onBack}>← 返回汇总</button>
        <div className="design-title">{template.name}</div>
        <span className="design-aspect">{aspect === 'free' ? '自由绘制' : aspect}</span>
        <div style={{ flex: 1 }} />
        <button className="btn primary" onClick={() => setShowExport(true)}>导出</button>
      </header>

      {workflow && (
        <div className="wf-bar">
          {workflow.stages.map((s, i) => (
            <React.Fragment key={s.id}>
              {i > 0 && <span className="wf-arrow">→</span>}
              <div className={`wf-stage ${s.status}`} title={s.status === 'blocked' ? '待接入真实模型' : s.label}>{s.label}</div>
            </React.Fragment>
          ))}
          <button className="btn primary" onClick={runNext} disabled={workflow.current >= workflow.stages.length - 1}>运行下一步</button>
        </div>
      )}
      {candidates.length > 0 && (
        <div className="cand-bar">
          <span className="cand-bar-title">候选图 {candidates.length}</span>
          {candidates.map((c) => (
            <div key={c.id} className={`cand-item ${c.is_selected ? 'on' : ''}`}>
              <img src={fileUrl(c.asset_id)} alt="" />
              <div className="cand-actions">
                <button className="btn" onClick={async () => { await api.selectCandidate(c.id); setCandidates(await api.listCandidates(projectId)) }}>{c.is_selected ? '已选主图' : '设为主图'}</button>
                <button className="btn primary" onClick={() => add({ id: nid(), type: 'image', imageUrl: fileUrl(c.asset_id), x: size.w / 2 - 120, y: size.h / 2 - 120, w: 240, h: 240 })}>进入画布</button>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="design-body">
        <Toolbar
          active={active}
          aspect={aspect}
          onAspect={setAspect}
          onActivate={setActive}
          onPlaceShape={(s) => { setPendingShape(s); setActive('') }}
          onPlaceSticky={(c) => { setPendingSticky(c); setActive('') }}
          onUpload={onUpload}
          onNewNode={onNewNode}
        />
        <div className="design-stage">
          <div
            className="board"
            ref={boardRef}
            style={{ width: size.w, height: size.h, transform: `translate(${pan.x}px, ${pan.y}px)` }}
            onPointerDown={onBoardDown}
            onPointerMove={onMove}
            onPointerUp={onUp}
          >
            {elements.map((el) => (
              <ElementView key={el.id} el={el} selected={selected === el.id} onDown={(e) => onElementDown(e, el)} onText={(t) => updateText(el.id, t)} />
            ))}
            {active === 'text' && <div className="board-hint">点击画布放置文字</div>}
            {pendingShape && <div className="board-hint">点击画布放置图形</div>}
            {pendingSticky && <div className="board-hint">点击画布放置便签</div>}
          </div>
        </div>
        <AiPanel onGenerate={onGenerate} onUpload={onUpload} />
      </div>

      {notice && <div className="toast">{notice}</div>}

      {showExport && (
        <div className="modal-mask" onClick={() => setShowExport(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head"><h2>导出图片</h2></div>
            <div className="mm-form">
              {generatedAssets.length === 0 ? (
                <div className="muted" style={{ padding: '8px 0' }}>暂无生成图片，请先在右侧 AI 助手点「生成」。</div>
              ) : (
                <>
                  <div className="export-grid">
                    {generatedAssets.map((aid, i) => (
                      <div key={aid} className="export-item">
                        <img src={fileUrl(aid)} alt="" />
                        <button className="btn" onClick={() => download(fileUrl(aid), `${template.name}_${i + 1}.png`)}>下载 PNG</button>
                      </div>
                    ))}
                  </div>
                  <div className="mm-form-actions">
                    <button className="btn primary" onClick={() => generatedAssets.forEach((aid, i) => download(fileUrl(aid), `${template.name}_${i + 1}.png`))}>下载全部</button>
                    <button className="btn primary" onClick={exportCanvasPng}>导出画布 PNG（含图层）</button>
                    <button className="btn" onClick={() => setShowExport(false)}>关闭</button>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function ElementView({ el, selected, onDown, onText }: { el: CanvasElement; selected: boolean; onDown: (e: React.PointerEvent) => void; onText: (t: string) => void }) {
  const style: React.CSSProperties = { left: el.x, top: el.y, width: el.w, height: el.h }
  if (el.type === 'image' && el.imageUrl) {
    return <img className={`cv-el ${selected ? 'sel' : ''}`} style={{ ...style, objectFit: 'cover' }} src={el.imageUrl} alt="" onPointerDown={onDown} />
  }
  if (el.type === 'shape') {
    return (
      <div className={`cv-el ${selected ? 'sel' : ''}`} style={style} onPointerDown={onDown}>
        <ShapeSvg kind={el.shape || 'rect'} color={el.color || '#9CA3AF'} />
      </div>
    )
  }
  if (el.type === 'node') {
    return (
      <div className={`cv-el cv-node ${selected ? 'sel' : ''}`} style={style} onPointerDown={onDown}>
        <div className="cv-node-k">{el.nodeKind === 'text' ? 'T' : el.nodeKind === 'image' ? '图' : '视'}</div>
        <div>{el.text}</div>
        <div className="cv-node-tag">{el.nodeKind === 'text' ? '接入中' : '待接入接口'}</div>
      </div>
    )
  }
  // text & sticky
  return (
    <div className={`cv-el cv-text ${selected ? 'sel' : ''}`} style={{ ...style, background: el.type === 'sticky' ? el.color : 'transparent' }} onPointerDown={onDown}>
      <textarea value={el.text || ''} placeholder={el.type === 'sticky' ? '便签' : '文字'} onChange={(e) => onText(e.target.value)} />
    </div>
  )
}

function ShapeSvg({ kind, color }: { kind: ShapeKind; color: string }) {
  if (kind === 'rect') return <div style={{ width: '100%', height: '100%', background: color }} />
  if (kind === 'circle') return <div style={{ width: '100%', height: '100%', borderRadius: '50%', background: color }} />
  return (
    <svg width="100%" height="100%" viewBox="0 0 100 100" preserveAspectRatio="none">
      {kind === 'triangle' && <polygon points="50,2 98,98 2,98" fill={color} />}
      {kind === 'arrow' && <path d="M2 98 L98 2" stroke={color} strokeWidth={5} />}
      {kind === 'turn-arrow' && <path d="M2 2 L60 2 L60 60" fill="none" stroke={color} strokeWidth={5} />}
      {kind === 'line' && <path d="M2 50 L98 50" stroke={color} strokeWidth={5} />}
    </svg>
  )
}

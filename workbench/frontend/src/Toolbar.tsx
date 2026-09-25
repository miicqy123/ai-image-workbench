import React, { useEffect, useRef, useState } from 'react'
import { AspectRatio, ShapeKind } from './types'

export const STICKY_COLORS = ['#FEF3C7', '#D1FAE5', '#DBEAFE', '#EDE9FE', '#FCD34D', '#34D399', '#60A5FA', '#C084FC']

export const SHAPES: { kind: ShapeKind; label: string }[] = [
  { kind: 'rect', label: '矩形' },
  { kind: 'circle', label: '圆形' },
  { kind: 'triangle', label: '三角形' },
  { kind: 'arrow', label: '箭头' },
  { kind: 'turn-arrow', label: '折线箭头' },
  { kind: 'line', label: '直线' },
]

export const ASPECTS: { id: AspectRatio; label: string }[] = [
  { id: 'free', label: '自由绘制' },
  { id: '16:9', label: '16:9' },
  { id: '4:3', label: '4:3' },
  { id: '1:1', label: '1:1' },
  { id: '9:16', label: 'iPhone' },
  { id: 'a4', label: 'A4' },
]

interface Props {
  active: string
  aspect: AspectRatio
  onAspect: (a: AspectRatio) => void
  onActivate: (tool: string) => void
  onPlaceShape: (s: ShapeKind) => void
  onPlaceSticky: (color: string) => void
  onUpload: (files: FileList | null) => void
  onNewNode: (kind: 'text' | 'image' | 'video') => void
}

function Icon({ d, w = 18, h = 18 }: { d: string; w?: number; h?: number }) {
  return (
    <svg width={w} height={h} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round">
      <path d={d} />
    </svg>
  )
}

const ICONS: Record<string, string> = {
  plus: 'M12 5v14M5 12h14',
  hand: 'M7 11.5V5a1.5 1.5 0 0 1 3 0v4m0-4.5a1.5 1.5 0 0 1 3 0v4m0-3.5a1.5 1.5 0 0 1 3 0V14a5 5 0 0 1-5 5h-2.5a5 5 0 0 1-4.5-3l-1.5-4a1.5 1.5 0 0 1 2.8-1.2L7 13',
  crop: 'M6 3v13a2 2 0 0 0 2 2h13M3 6h4m0 0v4M20 14h-4m0 0v4',
  text: 'M4 6V4h16v2M12 4v16m-3 0h6',
  shape: 'M4 6h16M4 6v12M4 18h16M20 6v12',
  sticky: 'M5 3h14a2 2 0 0 1 2 2v8l-6 6H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM13 19v-6h6',
  pin: 'M12 21s7-5.5 7-11a7 7 0 0 0-14 0c0 5.5 7 11 7 11zM12 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
  star: 'M12 3l2.4 4.9 5.4.8-3.9 3.8.9 5.4-4.8-2.5-4.8 2.5.9-5.4L4.2 8.7l5.4-.8z',
  avatar: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0',
}

type Panel = 'plus' | 'crop' | 'shape' | 'sticky'

export default function Toolbar(props: Props) {
  const [panel, setPanel] = useState<Panel | null>(null)
  const ref = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setPanel(null)
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setPanel(null)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey) }
  }, [])

  const close = () => setPanel(null)
  const toggle = (p: Panel) => setPanel(panel === p ? null : p)

  return (
    <div className="tb-wrap" ref={ref}>
      <input ref={fileRef} type="file" accept="image/*" multiple style={{ display: 'none' }}
        onChange={(e) => { props.onUpload(e.target.files); e.target.value = ''; close() }} />

      <div className="tb">
        <button className={`tb-btn plus ${panel === 'plus' ? 'on' : ''}`} data-tip="新增" aria-label="新增" onClick={() => toggle('plus')}><Icon d={ICONS.plus} /></button>
        <button className={`tb-btn ${props.active === 'pan' ? 'on' : ''}`} data-tip="抓手 · H" aria-label="抓手" onClick={() => { props.onActivate('pan'); close() }}><Icon d={ICONS.hand} /></button>
        <button className={`tb-btn ${panel === 'crop' ? 'on' : ''}`} data-tip="画布比例" aria-label="画布比例" onClick={() => toggle('crop')}><Icon d={ICONS.crop} /></button>
        <button className={`tb-btn ${props.active === 'text' ? 'on' : ''}`} data-tip="文字 · T" aria-label="文字" onClick={() => { props.onActivate('text'); close() }}><Icon d={ICONS.text} /></button>
        <button className={`tb-btn ${panel === 'shape' ? 'on' : ''}`} data-tip="图形" aria-label="图形" onClick={() => toggle('shape')}><Icon d={ICONS.shape} /></button>
        <button className={`tb-btn ${panel === 'sticky' ? 'on' : ''}`} data-tip="便签 · N" aria-label="便签" onClick={() => toggle('sticky')}><Icon d={ICONS.sticky} /></button>
        <div className="tb-sep" />
        <button className="tb-btn disabled" data-tip="定位图钉（待确认）"><Icon d={ICONS.pin} /></button>
        <button className="tb-btn disabled" data-tip="魔法棒 / 星光（待确认）"><Icon d={ICONS.star} /></button>
        <button className="tb-btn disabled" data-tip="用户头像（待确认）"><Icon d={ICONS.avatar} /></button>
        <div className="tb-sep" />
        <button className="tb-btn disabled" data-tip="底部工具（待确认）"><Icon d={ICONS.shape} /></button>
        <button className="tb-btn disabled" data-tip="底部工具（待确认）"><Icon d={ICONS.star} /></button>
      </div>

      {panel === 'plus' && (
        <div className="tb-panel">
          <button className="tb-item" onClick={() => fileRef.current?.click()}>上传</button>
          <div className="tb-panel-title">新增节点</div>
          <button className="tb-item" onClick={() => { props.onNewNode('text'); close() }}>文字生成 <kbd>⇧T</kbd></button>
          <button className="tb-item" onClick={() => { props.onNewNode('image'); close() }}>图片生成 <kbd>⇧I</kbd></button>
          <button className="tb-item" onClick={() => { props.onNewNode('video'); close() }}>视频生成 <kbd>⇧V</kbd></button>
        </div>
      )}

      {panel === 'crop' && (
        <div className="tb-panel">
          <div className="tb-panel-title">画布比例</div>
          {ASPECTS.map((a) => (
            <button key={a.id} className={`tb-item ${props.aspect === a.id ? 'sel' : ''}`} onClick={() => { props.onAspect(a.id); close() }}>{a.label}</button>
          ))}
        </div>
      )}

      {panel === 'shape' && (
        <div className="tb-panel tb-grid3">
          {SHAPES.map((s) => (
            <button key={s.kind} className="tb-cell" title={s.label} onClick={() => { props.onPlaceShape(s.kind); close() }}>
              <ShapeGlyph kind={s.kind} />
            </button>
          ))}
        </div>
      )}

      {panel === 'sticky' && (
        <div className="tb-panel tb-grid4">
          {STICKY_COLORS.map((c) => (
            <button key={c} className="tb-swatch" style={{ background: c }} onClick={() => { props.onPlaceSticky(c); close() }} />
          ))}
        </div>
      )}
    </div>
  )
}

function ShapeGlyph({ kind }: { kind: ShapeKind }) {
  const stroke = 'currentColor'
  if (kind === 'rect') return <div className="glyph" style={{ border: '2px solid currentColor', borderRadius: 3 }} />
  if (kind === 'circle') return <div className="glyph" style={{ border: '2px solid currentColor', borderRadius: '50%' }} />
  return (
    <svg width={22} height={22} viewBox="0 0 22 22" fill="none" stroke={stroke} strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
      {kind === 'triangle' && <polygon points="11,2 20,19 2,19" />}
      {kind === 'arrow' && <path d="M2 20 L20 2 M20 2 h-7 M20 2 v7" />}
      {kind === 'turn-arrow' && <path d="M2 2 h12 v12 M14 10 l4 4 M14 14 l4 -4" />}
      {kind === 'line' && <path d="M2 11 h18" />}
    </svg>
  )
}

import React, { useEffect, useState } from 'react'
import { api } from './api'

const QUICK_TASKS = [
  { id: 'main', label: '生成主图套图', prompt: '点击上传产品图，生成5张主图，适配淘宝/天猫平台，比例为1:1，自动生成商品名称、卖点。' },
  { id: 'aplus', label: '生成A+/详情页', prompt: '上传产品图，生成A+详情页，含卖点、参数与场景图。' },
  { id: 'scene', label: '生成商品场景图', prompt: '上传产品图，生成多场景商品图，适配家居/户外等真实场景。' },
  { id: 'retouch', label: '商品精修处理', prompt: '上传产品图，进行智能精修，高清画质输出。' },
]

const PLATFORMS = ['淘宝/天猫', '京东', '拼多多', '抖音', '小红书', '通用']
// 只提供后端本地合成模型能力范围内的画幅（1:1 / 4:3 / 3:4 / 16:9）；
// 后端不支持的 9:16 不再作为可提交选项，也不在此声明任何新能力
const RATIOS = ['1:1', '3:4', '4:3', '16:9']

interface Props {
  onGenerate: (opts: { count: number; platform: string; ratio: string; prompt: string; model_id?: string; template_id?: string }) => void
  onUpload: (files: FileList | null) => void
}

export default function AiPanel({ onGenerate, onUpload }: Props) {
  const [count, setCount] = useState(5)
  const [platform, setPlatform] = useState('淘宝/天猫')
  const [ratio, setRatio] = useState('1:1')
  const [prompt, setPrompt] = useState(QUICK_TASKS[0].prompt)
  const [feat, setFeat] = useState(0)
  const [models, setModels] = useState<any[]>([])
  const [modelId, setModelId] = useState('')
  const [tpls, setTpls] = useState<any[]>([])
  const [tplId, setTplId] = useState('')

  useEffect(() => { api.listModels().then((ms) => setModels(ms.filter((m: any) => m.modality === 'image' && m.enabled === 1))) }, [])
  // 张数上限取所选模型的能力声明（max_count），避免提交后端不接受的张数
  const effectiveModel = models.find((m) => m.model_id === modelId) || models[0]
  const maxCount = (() => {
    try { return Number(JSON.parse(effectiveModel?.capabilities_json || '{}').max_count || 0) || 20 } catch { return 20 }
  })()
  useEffect(() => { if (maxCount > 0 && count > maxCount) setCount(maxCount) }, [maxCount, count])
  useEffect(() => { api.listPromptTemplatesPublic().then(setTpls).catch(() => setTpls([])) }, [])

  const cycle = () => {
    const next = (feat + 1) % QUICK_TASKS.length
    setFeat(next)
    setPrompt(QUICK_TASKS[next].prompt)
  }

  return (
    <aside className="ai-panel">
      <div className="ai-greet">
        <div className="ai-greet-title">Hi，你好</div>
        <div className="ai-greet-sub">今天有什么好想法</div>
      </div>

      <div className="ai-tasks">
        {QUICK_TASKS.map((t) => (
          <button key={t.id} className="ai-task" onClick={() => setPrompt(t.prompt)}>{t.label}</button>
        ))}
      </div>
      <button className="btn ghost ai-shuffle" onClick={cycle}>换一换</button>

      <div className="ai-composer">
        <div className="ai-slots">
          <button className="slot" onClick={() => document.getElementById('ai-file')?.click()}>上传产品图</button>
          <span className="ai-text">生成</span>
          <input className="ai-num" type="number" min={1} max={maxCount} value={count} onChange={(e) => setCount(Number(e.target.value))} />
          <span className="ai-text">张</span>
          <select className="ai-sel" value={platform} onChange={(e) => setPlatform(e.target.value)}>{PLATFORMS.map((p) => <option key={p}>{p}</option>)}</select>
          <span className="ai-text">· 比例</span>
          <select className="ai-sel" value={ratio} onChange={(e) => setRatio(e.target.value)}>{RATIOS.map((r) => <option key={r}>{r}</option>)}</select>
          <span className="ai-text">· 模型</span>
          <select className="ai-sel" value={modelId} onChange={(e) => setModelId(e.target.value)}>
            <option value="">默认（本地合成）</option>
            {models.map((m) => <option key={m.model_id} value={m.model_id}>{m.model_id}</option>)}
          </select>
          <select className="ai-sel" value={tplId} onChange={(e) => setTplId(e.target.value)} title="Prompt 模板">
            <option value="">Prompt 模板：无</option>
            {tpls.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </div>
        <textarea className="ai-input" placeholder="描述你的灵感，支持 @ 上传图片、选择技能以及 Agent" value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        <input id="ai-file" type="file" accept="image/*" multiple style={{ display: 'none' }} onChange={(e) => { onUpload(e.target.files); e.target.value = '' }} />
      </div>

      <div className="ai-actions">
        <button className="btn" onClick={() => document.getElementById('ai-file')?.click()}>选择文件</button>
        <button className="btn">Agent</button>
        <button className="btn primary" onClick={() => onGenerate({ count, platform, ratio, prompt, model_id: modelId || undefined, template_id: tplId || undefined })}>生成</button>
      </div>
      <div className="ai-disclaimer">画幅按后端本地合成模型能力：1:1 / 3:4 / 4:3 / 16:9；内容由 AI 生成</div>
    </aside>
  )
}

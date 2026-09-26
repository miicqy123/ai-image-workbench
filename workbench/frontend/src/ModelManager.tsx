import React, { useEffect, useState } from 'react'
import { api } from './api'
import { ModelInfo, Provider } from './types'

interface Props {
  models: ModelInfo[]
  onClose: () => void
  onChanged: () => void
  showToast: (m: string) => void
}

type ModelForm = Partial<ModelInfo> & { isNew?: boolean }
type ProvForm = Partial<Provider> & { isNew?: boolean }

const EMPTY_MODEL: ModelForm = {
  isNew: true, model_id: '', provider: 'local', provider_id: '', adapter: '', modality: 'image',
  enabled: 1, cost_policy: 'free_local', workflow_version: 'v1',
  capabilities_json: '{\n  "image_input_limit": 1,\n  "aspect_ratios": ["1:1"],\n  "max_count": 1,\n  "editing_modes": [],\n  "tasks": ["image_generation"]\n}',
  parameter_schema: '{}',
}
const EMPTY_PROV: ProvForm = { isNew: true, id: '', name: '', base_url: '', api_key: '', enabled: 1 }

const ADAPTERS: { value: string; label: string }[] = [
  { value: 'local', label: '本地/内置' },
  { value: 'chat', label: '文本对话（OpenAI 兼容）' },
  { value: 'image_openai', label: '生图（OpenAI 兼容 images）' },
  { value: 'image_dashscope', label: '生图（通义万相 DashScope）' },
]

function tryParse(v: string): string {
  try { JSON.parse(v); return '' } catch (e) { return 'JSON 格式错误：' + (e as Error).message }
}

export default function ModelManager({ models, onClose, onChanged, showToast }: Props) {
  const [providers, setProviders] = useState<Provider[]>([])
  const [tab, setTab] = useState<'models' | 'providers'>('models')
  const [form, setForm] = useState<ModelForm | null>(null)
  const [provForm, setProvForm] = useState<ProvForm | null>(null)
  const [saving, setSaving] = useState(false)

  const refreshProviders = () => api.listProviders().then(setProviders).catch(() => setProviders([]))
  useEffect(() => { refreshProviders() }, [])

  const toggleEnabled = async (m: ModelInfo) => {
    await api.updateModel(m.model_id, { enabled: m.enabled ? 0 : 1 })
    showToast(`模型 ${m.model_id} 已${m.enabled ? '下线' : '上线'}`); onChanged()
  }
  const removeModel = async (m: ModelInfo) => {
    if (!confirm(`确认删除模型「${m.model_id}」？`)) return
    await api.deleteModel(m.model_id); showToast('已删除模型 ' + m.model_id); onChanged()
  }
  const openEdit = (m: ModelInfo) => setForm({ ...m, isNew: false })

  const saveModel = async () => {
    if (!form) return
    const capErr = tryParse(form.capabilities_json || '{}')
    const psErr = tryParse(form.parameter_schema || '{}')
    if (capErr || psErr) { showToast(capErr || psErr); return }
    if (form.isNew && !(form.model_id || '').trim()) { showToast('model_id 必填'); return }
    setSaving(true)
    try {
      const provName = providers.find((p) => p.id === form.provider_id)?.name || form.provider || 'local'
      const payload = {
        model_id: form.model_id, provider: provName, modality: form.modality,
        capabilities_json: form.capabilities_json, parameter_schema: form.parameter_schema,
        enabled: form.enabled, cost_policy: form.cost_policy, workflow_version: form.workflow_version,
        provider_id: form.provider_id || null, adapter: form.adapter || null,
      }
      if (form.isNew) { await api.createModel(payload); showToast('已新增模型 ' + form.model_id) }
      else { await api.updateModel(form.model_id!, payload); showToast('已保存模型 ' + form.model_id) }
      setForm(null); onChanged()
    } catch (e: any) { showToast('错误：' + e.message) } finally { setSaving(false) }
  }

  const saveProvider = async () => {
    if (!provForm) return
    if (!(provForm.name || '').trim()) { showToast('服务商名称必填'); return }
    setSaving(true)
    try {
      const body: any = { name: provForm.name, base_url: provForm.base_url || '', enabled: provForm.enabled }
      if ((provForm.api_key || '').trim()) body.api_key = provForm.api_key
      if (provForm.isNew) await api.createProvider(body)
      else await api.updateProvider(provForm.id!, body)
      showToast('已保存服务商'); setProvForm(null); refreshProviders()
    } catch (e: any) { showToast('错误：' + e.message) } finally { setSaving(false) }
  }
  const removeProvider = async (p: Provider) => {
    if (!confirm(`确认删除服务商「${p.name}」？`)) return
    await api.deleteProvider(p.id); refreshProviders(); showToast('已删除服务商 ' + p.name)
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal mm-lg" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>模型 / 服务商管理</h2>
          <div style={{ display: 'flex', gap: 6 }}>
            <button className={`btn ${tab === 'models' ? 'primary' : ''}`} onClick={() => { setTab('models'); setForm(null) }}>模型</button>
            <button className={`btn ${tab === 'providers' ? 'primary' : ''}`} onClick={() => { setTab('providers'); setProvForm(null) }}>服务商</button>
            <button className="btn ghost" onClick={onClose}>关闭</button>
          </div>
        </div>

        {tab === 'models' && !form && (
          <div className="mm-list">
            {models.map((m) => {
              const prov = providers.find((pp) => pp.id === m.provider_id)
              return (
                <div className="mm-card" key={m.model_id}>
                  <div className="mm-main">
                    <div className="mm-title">{m.model_id}</div>
                    <div className="meta">{prov ? prov.name : m.provider} · {m.modality} · {m.cost_policy} · {m.adapter || '内置'}</div>
                  </div>
                  <div className="mm-actions">
                    <label className="sw"><input type="checkbox" checked={!!m.enabled} onChange={() => toggleEnabled(m)} /><span>{m.enabled ? '已上线' : '已下线'}</span></label>
                    <button className="btn" onClick={() => openEdit(m)}>编辑</button>
                    <button className="btn danger" onClick={() => removeModel(m)}>删除</button>
                  </div>
                </div>
              )
            })}
            {!models.length && <div className="muted">暂无模型</div>}
            <div style={{ marginTop: 8 }}><button className="btn primary" onClick={() => setForm({ ...EMPTY_MODEL })}>+ 新增模型</button></div>
          </div>
        )}

        {tab === 'providers' && !provForm && (
          <div className="mm-list">
            {providers.map((p) => (
              <div className="mm-card" key={p.id}>
                <div className="mm-main">
                  <div className="mm-title">{p.name}</div>
                  <div className="meta">{p.base_url || '（未配置 Base URL）'} · Key: {p.has_api_key ? '已配置' : '未配置'}</div>
                </div>
                <div className="mm-actions">
                  <button className="btn" onClick={() => setProvForm({ ...p, isNew: false })}>编辑</button>
                  <button className="btn danger" onClick={() => removeProvider(p)}>删除</button>
                </div>
              </div>
            ))}
            {!providers.length && <div className="muted">暂无服务商</div>}
            <div style={{ marginTop: 8 }}><button className="btn primary" onClick={() => setProvForm({ ...EMPTY_PROV })}>+ 新增服务商</button></div>
          </div>
        )}

        {tab === 'models' && form && (
          <div className="mm-form">
            <div className="row"><label>模型名 / model_id{form.isNew ? '' : '（不可改）'}</label>
              <input disabled={!form.isNew} value={form.model_id || ''} onChange={(e) => setForm({ ...form, model_id: e.target.value })} /></div>
            <div className="row"><label>服务商</label>
              <select value={form.provider_id || ''} onChange={(e) => setForm({ ...form, provider_id: e.target.value })}>
                <option value="">— 本地 / 内置 —</option>
                {providers.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select></div>
            <div className="row"><label>类型 modality</label>
              <select value={form.modality || 'image'} onChange={(e) => setForm({ ...form, modality: e.target.value })}>
                <option value="image">image（生图）</option><option value="text">text（文本）</option>
              </select></div>
            <div className="row"><label>适配器 adapter</label>
              <select value={form.adapter || ''} onChange={(e) => setForm({ ...form, adapter: e.target.value })}>
                <option value="">自动</option>
                {ADAPTERS.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
              </select></div>
            <div className="row"><label>成本策略 cost_policy</label>
              <input value={form.cost_policy || ''} onChange={(e) => setForm({ ...form, cost_policy: e.target.value })} /></div>
            <div className="row"><label>上线</label>
              <label className="sw"><input type="checkbox" checked={!!form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked ? 1 : 0 })} /><span>{form.enabled ? '上线' : '下线'}</span></label></div>
            <div className="row col"><label>capabilities_json（能力声明，JSON）</label>
              <textarea rows={7} value={form.capabilities_json || ''} onChange={(e) => setForm({ ...form, capabilities_json: e.target.value })} /></div>
            <div className="row col"><label>parameter_schema（参数规格，JSON）</label>
              <textarea rows={4} value={form.parameter_schema || ''} onChange={(e) => setForm({ ...form, parameter_schema: e.target.value })} /></div>
            <div className="mm-form-actions">
              <button className="btn primary" disabled={saving} onClick={saveModel}>{saving ? '保存中…' : '保存'}</button>
              <button className="btn" onClick={() => setForm(null)}>取消</button>
            </div>
          </div>
        )}

        {tab === 'providers' && provForm && (
          <div className="mm-form">
            <div className="row"><label>服务商名称</label><input value={provForm.name || ''} onChange={(e) => setProvForm({ ...provForm, name: e.target.value })} /></div>
            <div className="row"><label>Base URL</label><input placeholder="https://.../v1" value={provForm.base_url || ''} onChange={(e) => setProvForm({ ...provForm, base_url: e.target.value })} /></div>
            <div className="row"><label>API Key</label><input type="password" placeholder="sk-..." value={provForm.api_key || ''} onChange={(e) => setProvForm({ ...provForm, api_key: e.target.value })} /></div>
            <div className="row"><label>启用</label>
              <label className="sw"><input type="checkbox" checked={provForm.enabled !== 0} onChange={(e) => setProvForm({ ...provForm, enabled: e.target.checked ? 1 : 0 })} /><span>{provForm.enabled !== 0 ? '启用' : '停用'}</span></label></div>
            <div className="mm-form-actions">
              <button className="btn primary" disabled={saving} onClick={saveProvider}>{saving ? '保存中…' : '保存'}</button>
              <button className="btn" onClick={() => setProvForm(null)}>取消</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

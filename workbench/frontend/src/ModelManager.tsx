import React, { useState } from 'react'
import { api } from './api'
import { ModelInfo } from './types'

interface Props {
  models: ModelInfo[]
  onClose: () => void
  onChanged: () => void
  showToast: (m: string) => void
}

type FormState = Partial<ModelInfo> & { isNew?: boolean }

const EMPTY: FormState = {
  isNew: true, model_id: '', provider: 'local', modality: 'image',
  enabled: 1, cost_policy: 'free_local', workflow_version: 'v1',
  capabilities_json: '{\n  "image_input_limit": 1,\n  "aspect_ratios": ["1:1"],\n  "resolutions": [{"tier": "standard", "min": 768}],\n  "max_count": 1,\n  "editing_modes": [],\n  "tasks": ["image_generation"]\n}',
  parameter_schema: '{}',
}

function tryParse(v: string): string {
  try { JSON.parse(v); return '' } catch (e) { return 'JSON 格式错误：' + (e as Error).message }
}

export default function ModelManager({ models, onClose, onChanged, showToast }: Props) {
  const [form, setForm] = useState<FormState | null>(null)
  const [saving, setSaving] = useState(false)

  const toggleEnabled = async (m: ModelInfo) => {
    await api.updateModel(m.model_id, { enabled: m.enabled ? 0 : 1 })
    showToast(`模型 ${m.model_id} 已${m.enabled ? '下线' : '上线'}`)
    onChanged()
  }

  const remove = async (m: ModelInfo) => {
    if (!confirm(`确认删除模型「${m.model_id}」？该操作不可恢复。`)) return
    await api.deleteModel(m.model_id)
    showToast('已删除模型 ' + m.model_id)
    onChanged()
  }

  const openEdit = (m: ModelInfo) => setForm({ ...m, isNew: false })

  const save = async () => {
    if (!form) return
    const capErr = tryParse(form.capabilities_json || '{}')
    const psErr = tryParse(form.parameter_schema || '{}')
    if (capErr || psErr) { showToast(capErr || psErr); return }
    if (form.isNew && !(form.model_id || '').trim()) { showToast('model_id 必填'); return }
    setSaving(true)
    try {
      if (form.isNew) {
        await api.createModel({
          model_id: form.model_id, provider: form.provider, modality: form.modality,
          capabilities_json: form.capabilities_json, parameter_schema: form.parameter_schema,
          enabled: form.enabled, cost_policy: form.cost_policy, workflow_version: form.workflow_version,
        })
        showToast('已新增模型 ' + form.model_id)
      } else {
        await api.updateModel(form.model_id!, {
          provider: form.provider, modality: form.modality, enabled: form.enabled,
          cost_policy: form.cost_policy, workflow_version: form.workflow_version,
          capabilities_json: form.capabilities_json, parameter_schema: form.parameter_schema,
        })
        showToast('已保存模型 ' + form.model_id)
      }
      setForm(null); onChanged()
    } catch (e: any) { showToast('错误：' + e.message) }
    finally { setSaving(false) }
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal mm-lg" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>模型管理</h2>
          <div>
            <button className="btn primary" onClick={() => setForm({ ...EMPTY })}>+ 新增模型</button>
            <button className="btn ghost" onClick={onClose}>关闭</button>
          </div>
        </div>

        {!form && (
          <div className="mm-list">
            {models.map((m) => (
              <div className="mm-card" key={m.model_id}>
                <div className="mm-main">
                  <div className="mm-title">{m.model_id}</div>
                  <div className="meta">{m.provider} · {m.modality} · {m.cost_policy} · v{m.workflow_version}</div>
                </div>
                <div className="mm-actions">
                  <label className="sw">
                    <input type="checkbox" checked={!!m.enabled} onChange={() => toggleEnabled(m)} />
                    <span>{m.enabled ? '已上线' : '已下线'}</span>
                  </label>
                  <button className="btn" onClick={() => openEdit(m)}>编辑</button>
                  <button className="btn danger" onClick={() => remove(m)}>删除</button>
                </div>
              </div>
            ))}
            {!models.length && <div className="muted">暂无模型</div>}
          </div>
        )}

        {form && (
          <div className="mm-form">
            <div className="row"><label>model_id{form.isNew ? '' : '（不可改）'}</label>
              <input disabled={!form.isNew} value={form.model_id || ''} onChange={(e) => setForm({ ...form, model_id: e.target.value })} /></div>
            <div className="row"><label>provider</label>
              <input value={form.provider || ''} onChange={(e) => setForm({ ...form, provider: e.target.value })} /></div>
            <div className="row"><label>modality</label>
              <select value={form.modality || 'image'} onChange={(e) => setForm({ ...form, modality: e.target.value })}>
                <option value="image">image</option><option value="text">text</option>
              </select></div>
            <div className="row"><label>enabled</label>
              <label className="sw"><input type="checkbox" checked={!!form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked ? 1 : 0 })} /><span>{form.enabled ? '上线' : '下线'}</span></label></div>
            <div className="row"><label>cost_policy</label>
              <input value={form.cost_policy || ''} onChange={(e) => setForm({ ...form, cost_policy: e.target.value })} /></div>
            <div className="row"><label>workflow_version</label>
              <input value={form.workflow_version || ''} onChange={(e) => setForm({ ...form, workflow_version: e.target.value })} /></div>
            <div className="row col"><label>capabilities_json（能力声明，JSON）</label>
              <textarea rows={8} value={form.capabilities_json || ''} onChange={(e) => setForm({ ...form, capabilities_json: e.target.value })} /></div>
            <div className="row col"><label>parameter_schema（参数规格，JSON）</label>
              <textarea rows={5} value={form.parameter_schema || ''} onChange={(e) => setForm({ ...form, parameter_schema: e.target.value })} /></div>
            <div className="mm-form-actions">
              <button className="btn primary" disabled={saving} onClick={save}>{saving ? '保存中…' : '保存'}</button>
              <button className="btn" onClick={() => setForm(null)}>取消</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

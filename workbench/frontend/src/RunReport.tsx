import React from 'react'
import { GraphData } from './types'

/** 后端 run-downstream 的返回体（字段与 main.py run_downstream 一致） */
export interface DSRanEntry {
  node_id: string
  type?: string
  run_id?: string
  model_id?: string
  status?: string
  version_before?: number
  version_after?: number
  error?: string
  status_code?: number
  /** 该节点的运行复用了既有 run（服务端返回） */
  reused?: boolean
  /** 需要人工排查（例如同节点已有未结束的运行） */
  manual_check_required?: boolean
}

export interface DownstreamResult {
  graph_id?: string
  from_node_id?: string
  order?: string[]
  ran?: DSRanEntry[]
  skipped?: { node_id: string; type?: string; reason?: string }[]
  failed?: DSRanEntry | null
  not_executed?: { node_id: string; type?: string }[]
  /** 等待超时但仍在运行的节点（服务端返回）与可直接查询的 run_id */
  pending?: (DSRanEntry & { reason?: string })[]
  pending_run_ids?: string[]
  status?: string
  /** 前端本地判定（不是服务端返回）：网络未确认 / 服务端拒绝 / 该图无法用现有接口安全覆盖 */
  local_kind?: 'network' | 'unsupported' | 'rejected'
  local_status?: number
  local_message?: string
  finished_at?: number
}

const TYPE_LABEL: Record<string, string> = {
  product_facts: '产品事实卡', product_image: '产品素材', strategy: '视觉策略',
  image_prompt: '单图提示词', image_generation: '生图节点', review: '审核', layout_export: '排版与 PNG 导出',
}
function displayName(n?: { type?: string; name?: string } | null): string {
  if (!n) return ''
  if (n.type === 'layout_export' && (!n.name || n.name === '分层排版导出')) return '排版与 PNG 导出'
  return n.name || TYPE_LABEL[n.type || ''] || n.type || ''
}

const STATUS_TEXT: Record<string, string> = {
  ok: '服务端 status=ok：本次下游执行结束',
  failed: '服务端 status=failed：中途失败，失败节点之后的节点未执行',
  running: '服务端 status=running：等待超时但该节点仍在运行（结果未确认，不是失败）',
  blocked: '服务端 status=blocked：该节点已有未结束的运行，本次未新建运行，需人工排查',
  rejected: '服务端拒绝本次运行（HTTP 拒绝，不是网络问题）',
  unsupported: '未执行：当前接口无法在不重复执行的前提下覆盖全图（需要后端整图接口）',
  unknown: '结果未确认：网络中断或等待超时，不能视为成功',
}

/** 运行结果面板：逐项展示服务端返回的 ran / skipped / failed / not_executed */
export interface PollInfo {
  ids: string[]
  statuses: Record<string, string>
  running: number
  done: number
  unknown: number
  ended: boolean
  timedOut?: boolean
}

const POLL_STATUS_TEXT: Record<string, string> = {
  queued: '排队中', running: '运行中', succeeded: '成功', failed: '失败', canceled: '已取消',
  'query-unknown': '查询未知（不等于失败）', unknown: '未知状态',
}

export default function RunReport({ result, graph, poll, onClose }: { result: DownstreamResult; graph: GraphData | null; poll?: PollInfo | null; onClose: () => void }) {
  const name = (id?: string) => {
    if (!id) return ''
    const n = graph?.nodes.find((x) => x.id === id)
    return `${displayName(n) || '节点'} ${id.slice(-6)}`
  }
  const ran = result.ran || []
  const skipped = result.skipped || []
  const notExec = result.not_executed || []
  const pending = result.pending || []
  const status = result.status || 'unknown'
  return (
    <div className="run-report">
      <div className="rr-head">
        <b>运行结果（以服务端返回为准）</b>
        <button className="btn ghost" style={{ padding: '0 6px' }} title="只关闭展示，不影响服务端状态" onClick={onClose}>×</button>
      </div>
      <div className={`rr-status rr-${status}`}>{STATUS_TEXT[status] || `服务端 status=${status}`}</div>
      {result.local_message && <div className="rr-line">{result.local_message}</div>}
      {result.from_node_id && <div className="rr-line">起点：{name(result.from_node_id)}（run-downstream 不重跑起点自身）</div>}
      <div className="rr-line">已执行 {ran.length} · 仍在运行/待执行 {pending.length} · 跳过 {skipped.length} · 失败 {result.failed ? 1 : 0} · 未执行 {notExec.length}</div>
      {result.local_status ? <div className="rr-line">HTTP 状态：{result.local_status}{result.local_kind === 'rejected' ? '（服务端拒绝，非网络问题）' : ''}</div> : null}
      {(result.order || []).length > 0 && (
        <div className="rr-line">服务端拓扑执行顺序：{(result.order || []).map((id) => { const n = graph?.nodes.find((x) => x.id === id); return displayName(n) || id.slice(-6) }).join(' → ')}</div>
      )}
      {ran.length > 0 && (
        <div className="rr-block">
          <div className="rr-title">已执行</div>
          {ran.map((e) => (
            <div className="rr-item" key={(e.run_id || '') + e.node_id}>
              {name(e.node_id)} · {e.status || '未知状态'} · v{e.version_before ?? '?'} → v{e.version_after ?? '?'}{e.run_id ? ` · ${e.run_id}` : ''}{e.reused ? ' · 复用了既有运行' : ''}
            </div>
          ))}
        </div>
      )}
      {skipped.length > 0 && (
        <div className="rr-block">
          <div className="rr-title">跳过（未自动执行）</div>
          {skipped.map((s) => <div className="rr-item" key={s.node_id}>{name(s.node_id)} · {s.reason || '未说明原因'}</div>)}
        </div>
      )}
      {result.failed && (
        <div className="rr-block rr-fail">
          <div className="rr-title">失败</div>
          <div className="rr-item">{name(result.failed.node_id)} · {result.failed.type || ''} · {result.failed.status || ''}{result.failed.status_code ? ` · HTTP ${result.failed.status_code}` : ''}</div>
          <div className="rr-item rr-err">{result.failed.error || '未返回错误信息'}</div>
          {result.failed.manual_check_required ? (
            <div className="rr-item rr-err">需人工排查：该节点存在未结束的运行。只读定位：GET /api/runs → GET /api/runs/&lt;run_id&gt;；本批不会按时长自动清理或自动重跑。</div>
          ) : null}
        </div>
      )}
      {pending.length > 0 && (
        <div className="rr-block">
          <div className="rr-title">仍在运行 / 待执行</div>
          {pending.map((s) => (
            <div className="rr-item" key={s.node_id + (s.run_id || '')}>
              {name(s.node_id)} · {s.status || 'running'}{s.run_id ? ` · ${s.run_id}` : ''}{s.reason ? ` · ${s.reason}` : ''}
            </div>
          ))}
          <div className="rr-item rr-err">可将上述 run_id 用于只读查询（GET /api/runs/&lt;run_id&gt;）；结果未确认前不要重复触发。</div>
        </div>
      )}
      {notExec.length > 0 && (
        <div className="rr-block">
          <div className="rr-title">未执行</div>
          {notExec.map((s) => <div className="rr-item" key={s.node_id}>{name(s.node_id)}</div>)}
        </div>
      )}
      {poll && (
        <div className="rr-block">
          <div className="rr-title">当前节点运行状态（限时只读轮询）</div>
          {poll.running > 0 && !poll.ended && (
            <div className="rr-item">当前节点运行中；后续节点尚未开始。</div>
          )}
          {poll.ended && (
            <div className="rr-item">
              轮询结束：后续节点尚未自动执行，可由用户确认后继续。{poll.timedOut ? '（轮询超时，结果未确认；服务端任务仍在继续，不会被取消）' : ''}
            </div>
          )}
          {poll.ids.map((rid) => (
            <div className="rr-item" key={rid}>{name(rid)} · {POLL_STATUS_TEXT[poll.statuses[rid] || 'unknown'] || poll.statuses[rid]} · {rid}</div>
          ))}
          <div className="rr-item rr-err">轮询只读状态：不会重新提交生成、也不会取消服务端任务；页面刷新不保留进度展示。</div>
        </div>
      )}
      <div className="rr-foot">刷新页面不保留本次结果；节点状态始终来自服务端。</div>
    </div>
  )
}

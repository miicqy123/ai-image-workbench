import { Asset, GraphData, ModelInfo, Provider, WBNode } from './types'

const BASE = ''

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const t = await res.text()
    throw new Error(`${res.status}: ${t}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  listProjects: () => fetch(`${BASE}/api/projects`).then(j<any[]>),
  createProject: (name: string) =>
    fetch(`${BASE}/api/projects`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }).then(j<any>),
  renameProject: (id: string, name: string) =>
    fetch(`${BASE}/api/projects/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }).then(j<any>),
  deleteProject: (id: string) => fetch(`${BASE}/api/projects/${id}`, { method: 'DELETE' }).then(j<any>),

  initTemplate: (pid: string, skeleton?: string) =>
    fetch(`${BASE}/api/projects/${pid}/graph/init-template`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ skeleton: skeleton || 'poster' }) }).then(j<any>),

  getGraph: (pid: string) => fetch(`${BASE}/api/projects/${pid}/graph`).then(j<GraphData>),
  patchGraph: (pid: string, body: any) =>
    fetch(`${BASE}/api/projects/${pid}/graph`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),

  getNode: (nid: string) => fetch(`${BASE}/api/nodes/${nid}`).then(j<WBNode>),
  patchNode: (nid: string, body: any) => fetch(`${BASE}/api/nodes/${nid}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),
  renameNode: (nid: string, name: string) =>
    fetch(`${BASE}/api/nodes/${nid}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }).then(j<any>),
  aiEdit: (nid: string, instruction: string) =>
    fetch(`${BASE}/api/nodes/${nid}/ai-edit`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ instruction }) }).then(j<any>),
  applyCandidate: (nid: string, candidate_id: string, base_version: number) =>
    fetch(`${BASE}/api/nodes/${nid}/apply-candidate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ candidate_id, base_version }) }).then(j<any>),

  runNode: (nid: string, body: any) => fetch(`${BASE}/api/nodes/${nid}/runs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),
  getRun: (rid: string) => fetch(`${BASE}/api/runs/${rid}`).then(j<any>),
  runDownstream: (gid: string, from_node_id: string) =>
    fetch(`${BASE}/api/graphs/${gid}/run-downstream`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ from_node_id }) }).then(j<any>),

  listModels: () => fetch(`${BASE}/api/models`).then(j<ModelInfo[]>),
  getModel: (id: string) => fetch(`${BASE}/api/models/${id}`).then(j<ModelInfo>),
  createModel: (body: any) =>
    fetch(`${BASE}/api/models`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<ModelInfo>),
  updateModel: (id: string, body: any) =>
    fetch(`${BASE}/api/models/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<ModelInfo>),
  deleteModel: (id: string) => fetch(`${BASE}/api/models/${id}`, { method: 'DELETE' }).then(j<any>),

  addNode: (gid: string, body: any) =>
    fetch(`${BASE}/api/graphs/${gid}/nodes`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),
  deleteNode: (nid: string) => fetch(`${BASE}/api/nodes/${nid}`, { method: 'DELETE' }).then(j<any>),

  listProviders: () => fetch(`${BASE}/api/providers`).then(j<Provider[]>),
  adminStats: () => fetch(`${BASE}/api/admin/stats`).then(j<any>),
  listRuns: () => fetch(`${BASE}/api/runs`).then(j<any[]>),
  listAudit: () => fetch(`${BASE}/api/audit`).then(j<any[]>),
  listAllAssets: () => fetch(`${BASE}/api/assets`).then(j<any[]>),
  deleteAsset: (aid: string) => fetch(`${BASE}/api/assets/${aid}`, { method: 'DELETE' }).then(j<any>),
  createProvider: (body: any) =>
    fetch(`${BASE}/api/providers`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<Provider>),
  updateProvider: (id: string, body: any) =>
    fetch(`${BASE}/api/providers/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<Provider>),
  deleteProvider: (id: string) => fetch(`${BASE}/api/providers/${id}`, { method: 'DELETE' }).then(j<any>),

  uploadAsset: (pid: string, file: File, role: string) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('role', role)
    return fetch(`${BASE}/api/projects/${pid}/assets`, { method: 'POST', body: fd }).then(j<any>)
  },
  uploadAssetsBatch: (pid: string, files: File[], role: string) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    fd.append('role', role)
    return fetch(`${BASE}/api/projects/${pid}/assets/batch`, { method: 'POST', body: fd }).then(j<any>)
  },
  importTextFile: (pid: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return fetch(`${BASE}/api/projects/${pid}/files/import`, { method: 'POST', body: fd }).then(j<any>)
  },
  listAssets: (pid: string) => fetch(`${BASE}/api/projects/${pid}/assets`).then(j<Asset[]>),

  exportLayout: (pid: string, body: any) => fetch(`${BASE}/api/projects/${pid}/layout/export`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),
  exportProject: (pid: string) => fetch(`${BASE}/api/projects/${pid}/export`).then(j<any>),
  exportPackage: (pid: string) => `${BASE}/api/projects/${pid}/export-package`,

  getDefaults: (pid: string) => fetch(`${BASE}/api/projects/${pid}/defaults`).then(j<any>),
  listPromptTemplatesPublic: () => fetch(`${BASE}/api/prompt-templates`).then(j<any[]>),
  creatorUsageSummary: () => fetch(`${BASE}/api/creator/usage-summary`).then(j<any>),
  creatorUsageRecords: () => fetch(`${BASE}/api/creator/usage-records`).then(j<any[]>),
  adminUsageRecords: (q = '') => fetch(`${BASE}/api/admin/usage-records${q}`).then(j<any[]>),
  adminCostSummary: (days = 30) => fetch(`${BASE}/api/admin/cost-summary?days=${days}`).then(j<any>),
  adminOrganizations: () => fetch(`${BASE}/api/admin/organizations`).then(j<any[]>),
  adminOrgQuota: (oid: string) => fetch(`${BASE}/api/admin/organizations/${oid}/quota`).then(j<any>),
  updateOrgQuota: (oid: string, body: any) => fetch(`${BASE}/api/admin/organizations/${oid}/quota`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),
  adminCreditLedger: () => fetch(`${BASE}/api/admin/credit-ledger`).then(j<any[]>),
  grantCredits: (body: any) => fetch(`${BASE}/api/admin/credits/grant`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),
  deductCredits: (body: any) => fetch(`${BASE}/api/admin/credits/deduct`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),
  refundCredits: (body: any) => fetch(`${BASE}/api/admin/credits/refund`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),
  setModelPricing: (modelId: string, unit_cost: number) => fetch(`${BASE}/api/admin/models/${modelId}/pricing`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ unit_cost }) }).then(j<any>),
  listTemplates: () => fetch(`${BASE}/api/templates`).then(j<any[]>),
  adminTemplates: () => fetch(`${BASE}/api/admin/templates`).then(j<any[]>),
  createTemplate: (body: any) => fetch(`${BASE}/api/admin/templates`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),
  deleteTemplate: (id: string) => fetch(`${BASE}/api/admin/templates/${id}`, { method: 'DELETE' }).then(j<any>),
  listPromptTemplates: () => fetch(`${BASE}/api/admin/prompt-templates`).then(j<any[]>),
  createPromptTemplate: (body: any) => fetch(`${BASE}/api/admin/prompt-templates`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),
  deletePromptTemplate: (id: string) => fetch(`${BASE}/api/admin/prompt-templates/${id}`, { method: 'DELETE' }).then(j<any>),

  creatorDashboard: () => fetch(`${BASE}/api/creator/dashboard`).then(j<any>),
  archiveProject: (pid: string) => fetch(`${BASE}/api/projects/${pid}/archive`, { method: 'POST' }).then(j<any>),
  restoreProject: (pid: string) => fetch(`${BASE}/api/projects/${pid}/restore`, { method: 'POST' }).then(j<any>),
  duplicateProject: (pid: string) => fetch(`${BASE}/api/projects/${pid}/duplicate`, { method: 'POST' }).then(j<any>),

  createJob: (pid: string, body: any) =>
    fetch(`${BASE}/api/projects/${pid}/generation-jobs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),
  getJob: (jid: string) => fetch(`${BASE}/api/generation-jobs/${jid}`).then(j<any>),
  listCandidates: (pid: string) => fetch(`${BASE}/api/projects/${pid}/candidates`).then(j<any[]>),
  selectCandidate: (cid: string) => fetch(`${BASE}/api/candidates/${cid}/select`, { method: 'POST' }).then(j<any>),

  getCanvas: (pid: string) => fetch(`${BASE}/api/projects/${pid}/canvas`).then(j<any>),
  putCanvas: (pid: string, body: any) =>
    fetch(`${BASE}/api/projects/${pid}/canvas`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),
  exportCanvas: (pid: string) =>
    fetch(`${BASE}/api/projects/${pid}/canvas/export`, { method: 'POST' }).then(j<any>),

  getGenerationBrief: (pid: string) => fetch(`${BASE}/api/projects/${pid}/brief`).then(j<any>),
  upsertGenerationBrief: (pid: string, body: any) =>
    fetch(`${BASE}/api/projects/${pid}/brief`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),

  setDefaults: (pid: string, body: any) =>
    fetch(`${BASE}/api/projects/${pid}/defaults`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<any>),

  agentPlan: (pid: string, instruction: string) =>
    fetch(`${BASE}/api/projects/${pid}/agent/plan`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ instruction }) }).then(j<any>),

  imageToolIntents: () => fetch(`${BASE}/api/image-tools/intents`).then(j<any[]>),
  runImageTool: (nodeId: string, intent: string, body: any) =>
    fetch(`${BASE}/api/image-tools/run`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ node_id: nodeId, intent, ...body }) }).then(j<any>),

  fileUrl: (assetId: string) => `${BASE}/api/assets/${assetId}/download`,
}

export function fileUrl(assetId: string) {
  return `${BASE}/api/assets/${assetId}/download`
}

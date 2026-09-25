export interface WBNode {
  id: string
  type: string
  position: { x: number; y: number }
  version: number
  status: string
  content: any
}

export interface WBEdge {
  id: string
  from_node: string
  from_port: string
  to_node: string
  to_port: string
  semantic: string
}

export interface GraphData {
  graph_id: string
  version: number
  nodes: WBNode[]
  edges: WBEdge[]
}

export interface Asset {
  id: string
  role: string
  kind: string
  mime: string
  width: number
  height: number
  object_key: string
}

export interface ModelInfo {
  model_id: string
  provider: string
  modality: string
  capabilities_json: string
  parameter_schema: string
  enabled: number
  cost_policy: string
  workflow_version: string
}

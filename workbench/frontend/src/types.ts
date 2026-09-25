export interface WBNode {
  id: string
  type: string
  name?: string
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
  provider_id?: string
  adapter?: string
}

export interface Provider {
  id: string
  name: string
  base_url: string
  api_key: string
  enabled: number
}

export type AspectRatio = 'free' | '16:9' | '4:3' | '1:1' | '3:4' | '9:16' | 'a4'

export interface Template {
  id: string
  name: string
  subtitle: string
  aspect: AspectRatio
  accent: string
  level1: string
  level2: string
  skeleton: string
}

export type ShapeKind = 'rect' | 'circle' | 'triangle' | 'arrow' | 'turn-arrow' | 'line'

export type CanvasElementType = 'text' | 'shape' | 'sticky' | 'image' | 'node'

export interface CanvasElement {
  id: string
  type: CanvasElementType
  x: number
  y: number
  w: number
  h: number
  text?: string
  shape?: ShapeKind
  color?: string
  imageUrl?: string
  nodeKind?: 'text' | 'image' | 'video'
}

export type TagDimension = '一级类别' | '二级类别' | '生产环节' | '状态'

export interface Tag {
  id: string
  name: string
  dimension: TagDimension
}

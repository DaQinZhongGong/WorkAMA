export type LayerType = 'rectangle' | 'circle' | 'text' | 'image' | 'group'

export interface LayerStyle {
  fill?: string
  stroke?: string
  strokeWidth?: number
  opacity?: number
  fontSize?: number
  fontFamily?: string
  color?: string
  borderRadius?: number
}

export interface Layer {
  id: string
  name: string
  type: LayerType
  x: number
  y: number
  width: number
  height: number
  rotation?: number
  visible: boolean
  locked: boolean
  zIndex: number
  parentId?: string
  style: LayerStyle
  content?: string
  src?: string
}

export interface CanvasState {
  layers: Layer[]
  selectedIds: string[]
  zoom: number
  canvasWidth: number
  canvasHeight: number
}

export interface CommandSnapshot {
  type: string
  targetId?: string
  before?: unknown
  after?: unknown
  timestamp: number
}

export type AlignAction =
  | 'alignLeft'
  | 'alignCenterH'
  | 'alignRight'
  | 'alignTop'
  | 'alignCenterV'
  | 'alignBottom'
  | 'distributeH'
  | 'distributeV'

export interface ExportOptions {
  format: 'png' | 'jpeg' | 'svg'
  scale: 1 | 2 | 3
}

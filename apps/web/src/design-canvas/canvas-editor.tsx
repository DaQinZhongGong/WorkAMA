import { useRef, useEffect, useState, useCallback } from 'react'
import {
  Undo2,
  Redo2,
  ZoomIn,
  ZoomOut,
  Maximize,
  MousePointer2,
  Square,
  Circle as CircleIcon,
  Type,
  Image as ImageIcon,
  Trash2,
} from 'lucide-react'
import type { Layer, AlignAction } from './types'
import { UndoManager, type Command } from './undo-manager'
import { LayerPanel } from './layer-panel'
import { AlignToolbar } from './align-toolbar'
import { ExportPanel } from './export-panel'
import {
  sortLayersByZIndex,
  alignLayers,
  generateId,
  getDefaultLayerName,
} from './utils'

interface CanvasEditorProps {
  projectName: string
  canvasWidth: number
  canvasHeight: number
  initialLayers?: Layer[]
}

function createLayer(type: Layer['type'], x: number, y: number): Layer {
  const base: Layer = {
    id: generateId(),
    name: getDefaultLayerName(type),
    type,
    x,
    y,
    width: type === 'text' ? 120 : 100,
    height: type === 'text' ? 40 : 100,
    visible: true,
    locked: false,
    zIndex: 0,
    style: {
      fill: type === 'text' ? 'transparent' : '#dbe7f3',
      stroke: '#1d3557',
      strokeWidth: 1,
      opacity: 1,
      fontSize: 14,
      color: '#1d3557',
    },
  }
  if (type === 'text') base.content = 'Text'
  if (type === 'circle') {
    base.width = 100
    base.height = 100
  }
  return base
}

export function CanvasEditor({ projectName, canvasWidth, canvasHeight, initialLayers = [] }: CanvasEditorProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const svgRef = useRef<SVGSVGElement | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const undoManagerRef = useRef(new UndoManager())

  const [layers, setLayers] = useState<Layer[]>(initialLayers.length ? initialLayers : [
    createLayer('rectangle', 50, 50),
    createLayer('circle', 200, 80),
    createLayer('text', 120, 200),
  ])
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [zoom, setZoom] = useState(1)
  const [tool, setTool] = useState<'select' | 'rectangle' | 'circle' | 'text'>('select')
  const [canUndo, setCanUndo] = useState(false)
  const [canRedo, setCanRedo] = useState(false)

  const dragState = useRef<{
    active: boolean
    id: string | null
    startX: number
    startY: number
    initialX: number
    initialY: number
  }>({ active: false, id: null, startX: 0, startY: 0, initialX: 0, initialY: 0 })

  useEffect(() => {
    const unsub = undoManagerRef.current.subscribe((u, r) => {
      setCanUndo(u)
      setCanRedo(r)
    })
    return () => { unsub() }
  }, [])

  const assignZIndices = useCallback((ls: Layer[]): Layer[] => {
    return sortLayersByZIndex(ls).map((l, i) => ({ ...l, zIndex: i }))
  }, [])

  const pushCommand = useCallback((type: string, before: Layer[], after: Layer[], targetId?: string) => {
    const um = undoManagerRef.current
    const cmd: Command = {
      execute: () => setLayers(assignZIndices(after)),
      undo: () => setLayers(assignZIndices(before)),
      snapshot: () => ({ type, targetId, before, after, timestamp: Date.now() }),
    }
    um.execute(cmd)
  }, [assignZIndices])

  const drawCanvas = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    canvas.width = canvasWidth
    canvas.height = canvasHeight
    ctx.clearRect(0, 0, canvas.width, canvas.height)

    // Grid
    ctx.strokeStyle = '#e5edef'
    ctx.lineWidth = 1
    const gridSize = 20
    for (let x = 0; x <= canvas.width; x += gridSize) {
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, canvas.height); ctx.stroke()
    }
    for (let y = 0; y <= canvas.height; y += gridSize) {
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(canvas.width, y); ctx.stroke()
    }

    const visibleLayers = sortLayersByZIndex(layers).filter((l) => l.visible)
    for (const layer of visibleLayers) {
      ctx.save()
      ctx.globalAlpha = layer.style.opacity ?? 1
      const isSelected = selectedIds.includes(layer.id)

      if (layer.type === 'rectangle') {
        ctx.fillStyle = layer.style.fill ?? '#dbe7f3'
        ctx.strokeStyle = layer.style.stroke ?? '#1d3557'
        ctx.lineWidth = layer.style.strokeWidth ?? 1
        ctx.fillRect(layer.x, layer.y, layer.width, layer.height)
        ctx.strokeRect(layer.x, layer.y, layer.width, layer.height)
      } else if (layer.type === 'circle') {
        ctx.fillStyle = layer.style.fill ?? '#b9d6c2'
        ctx.strokeStyle = layer.style.stroke ?? '#1d3557'
        ctx.lineWidth = layer.style.strokeWidth ?? 1
        ctx.beginPath()
        ctx.ellipse(
          layer.x + layer.width / 2,
          layer.y + layer.height / 2,
          layer.width / 2,
          layer.height / 2,
          0,
          0,
          Math.PI * 2
        )
        ctx.fill()
        ctx.stroke()
      } else if (layer.type === 'text') {
        ctx.font = `${layer.style.fontSize ?? 14}px ${layer.style.fontFamily ?? 'sans-serif'}`
        ctx.fillStyle = layer.style.color ?? '#1d3557'
        ctx.textBaseline = 'top'
        ctx.fillText(layer.content ?? 'Text', layer.x + 4, layer.y + 4, layer.width - 8)
        ctx.strokeStyle = '#d0d0d0'
        ctx.lineWidth = 1
        ctx.strokeRect(layer.x, layer.y, layer.width, layer.height)
      } else if (layer.type === 'image') {
        ctx.fillStyle = '#f0f0f0'
        ctx.fillRect(layer.x, layer.y, layer.width, layer.height)
        ctx.strokeStyle = layer.style.stroke ?? '#1d3557'
        ctx.strokeRect(layer.x, layer.y, layer.width, layer.height)
        ctx.fillStyle = '#999'
        ctx.font = '12px sans-serif'
        ctx.textAlign = 'center'
        ctx.textBaseline = 'middle'
        ctx.fillText('Image', layer.x + layer.width / 2, layer.y + layer.height / 2)
      }

      if (isSelected) {
        ctx.strokeStyle = '#2e63c9'
        ctx.lineWidth = 2
        ctx.setLineDash([4, 4])
        ctx.strokeRect(layer.x - 2, layer.y - 2, layer.width + 4, layer.height + 4)
        ctx.setLineDash([])
      }

      ctx.restore()
    }
  }, [layers, selectedIds, canvasWidth, canvasHeight])

  useEffect(() => {
    drawCanvas()
  }, [drawCanvas])

  const getCanvasPoint = useCallback((e: React.MouseEvent) => {
    const canvas = canvasRef.current
    if (!canvas) return { x: 0, y: 0 }
    const rect = canvas.getBoundingClientRect()
    return {
      x: (e.clientX - rect.left) / zoom,
      y: (e.clientY - rect.top) / zoom,
    }
  }, [zoom])

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (tool !== 'select') return
    const pt = getCanvasPoint(e)
    const clicked = sortLayersByZIndex(layers)
      .reverse()
      .find((l) => l.visible && !l.locked && pt.x >= l.x && pt.x <= l.x + l.width && pt.y >= l.y && pt.y <= l.y + l.height)

    if (clicked) {
      if (!selectedIds.includes(clicked.id)) {
        setSelectedIds(e.ctrlKey || e.metaKey ? [...selectedIds, clicked.id] : [clicked.id])
      }
      dragState.current = {
        active: true,
        id: clicked.id,
        startX: pt.x,
        startY: pt.y,
        initialX: clicked.x,
        initialY: clicked.y,
      }
    } else {
      setSelectedIds([])
    }
  }, [tool, layers, selectedIds, getCanvasPoint])

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!dragState.current.active || !dragState.current.id) return
    const pt = getCanvasPoint(e)
    const dx = pt.x - dragState.current.startX
    const dy = pt.y - dragState.current.startY
    setLayers((prev) =>
      prev.map((l) =>
        l.id === dragState.current.id
          ? { ...l, x: dragState.current.initialX + dx, y: dragState.current.initialY + dy }
          : l
      )
    )
  }, [getCanvasPoint])

  const handleMouseUp = useCallback(() => {
    if (dragState.current.active && dragState.current.id) {
      const id = dragState.current.id
      const after = layers.find((l) => l.id === id)
      if (after) {
        const beforeLayers = layers.map((l) => (l.id === id ? { ...l, x: dragState.current.initialX, y: dragState.current.initialY } : l))
        pushCommand('move', beforeLayers, layers, id)
      }
    }
    dragState.current = { active: false, id: null, startX: 0, startY: 0, initialX: 0, initialY: 0 }
  }, [layers, pushCommand])

  const handleCanvasClick = useCallback((e: React.MouseEvent) => {
    if (tool === 'select') return
    const pt = getCanvasPoint(e)
    const newLayer = createLayer(tool, pt.x - 50, pt.y - 50)
    const next = assignZIndices([...layers, { ...newLayer, zIndex: layers.length }])
    pushCommand('add', layers, next, newLayer.id)
    setSelectedIds([newLayer.id])
    setTool('select')
  }, [tool, layers, getCanvasPoint, assignZIndices, pushCommand])

  const handleSelectLayer = useCallback((id: string, multi?: boolean) => {
    setSelectedIds((prev) => {
      if (multi) return prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
      return [id]
    })
  }, [])

  const handleToggleVisible = useCallback((id: string) => {
    setLayers((prev) => {
      const next = prev.map((l) => (l.id === id ? { ...l, visible: !l.visible } : l))
      pushCommand('toggleVisible', prev, next, id)
      return next
    })
  }, [pushCommand])

  const handleToggleLock = useCallback((id: string) => {
    setLayers((prev) => {
      const next = prev.map((l) => (l.id === id ? { ...l, locked: !l.locked } : l))
      pushCommand('toggleLock', prev, next, id)
      return next
    })
  }, [pushCommand])

  const handleRename = useCallback((id: string, name: string) => {
    setLayers((prev) => {
      const next = prev.map((l) => (l.id === id ? { ...l, name } : l))
      pushCommand('rename', prev, next, id)
      return next
    })
  }, [pushCommand])

  const handleDelete = useCallback((id: string) => {
    setLayers((prev) => {
      const next = prev.filter((l) => l.id !== id)
      pushCommand('delete', prev, next, id)
      return next
    })
    setSelectedIds((prev) => prev.filter((x) => x !== id))
  }, [pushCommand])

  const handleReorder = useCallback((dragId: string, targetId: string) => {
    setLayers((prev) => {
      const dragIdx = prev.findIndex((l) => l.id === dragId)
      const targetIdx = prev.findIndex((l) => l.id === targetId)
      if (dragIdx === -1 || targetIdx === -1) return prev
      const next = [...prev]
      const [removed] = next.splice(dragIdx, 1)
      next.splice(targetIdx, 0, removed)
      const assigned = assignZIndices(next)
      pushCommand('reorder', prev, assigned, dragId)
      return assigned
    })
  }, [assignZIndices, pushCommand])

  const handleAlign = useCallback((action: AlignAction) => {
    setLayers((prev) => {
      const next = alignLayers(prev, selectedIds, action)
      pushCommand(action, prev, next)
      return next
    })
  }, [selectedIds, pushCommand])

  const handleUndo = useCallback(() => undoManagerRef.current.undo(), [])
  const handleRedo = useCallback(() => undoManagerRef.current.redo(), [])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    const isMod = e.ctrlKey || e.metaKey
    if (isMod && e.key.toLowerCase() === 'z') {
      e.preventDefault()
      if (e.shiftKey) undoManagerRef.current.redo()
      else undoManagerRef.current.undo()
    }
    if (e.key === 'Delete' || e.key === 'Backspace') {
      if (selectedIds.length > 0) {
        setLayers((prev) => {
          const next = prev.filter((l) => !selectedIds.includes(l.id))
          pushCommand('delete', prev, next, selectedIds.join(','))
          return next
        })
        setSelectedIds([])
      }
    }
  }, [selectedIds, pushCommand])

  const handleZoomIn = useCallback(() => setZoom((z) => Math.min(z + 0.25, 3)), [])
  const handleZoomOut = useCallback(() => setZoom((z) => Math.max(z - 0.25, 0.25)), [])
  const handleZoomFit = useCallback(() => setZoom(1), [])

  const selectedLayerName = layers.find((l) => selectedIds[0] === l.id)?.name

  return (
    <div className="canvas-editor" onKeyDown={handleKeyDown} tabIndex={0} ref={containerRef} data-testid="canvas-editor">
      <div className="canvas-toolbar-row">
        <div className="canvas-tools">
          <button className={`tool-btn ${tool === 'select' ? 'active' : ''}`} onClick={() => setTool('select')} title="Select" data-testid="tool-select">
            <MousePointer2 size={15} />
          </button>
          <button className={`tool-btn ${tool === 'rectangle' ? 'active' : ''}`} onClick={() => setTool('rectangle')} title="Rectangle" data-testid="tool-rectangle">
            <Square size={15} />
          </button>
          <button className={`tool-btn ${tool === 'circle' ? 'active' : ''}`} onClick={() => setTool('circle')} title="Circle" data-testid="tool-circle">
            <CircleIcon size={15} />
          </button>
          <button className={`tool-btn ${tool === 'text' ? 'active' : ''}`} onClick={() => setTool('text')} title="Text" data-testid="tool-text">
            <Type size={15} />
          </button>
          <button className="tool-btn" onClick={() => { if (selectedIds.length) { selectedIds.forEach(handleDelete); setSelectedIds([]) } }} title="Delete" data-testid="tool-delete">
            <Trash2 size={15} />
          </button>
        </div>
        <AlignToolbar selectedCount={selectedIds.length} onAlign={handleAlign} />
        <div className="canvas-zoom">
          <button onClick={handleZoomOut} title="Zoom out"><ZoomOut size={14} /></button>
          <span>{Math.round(zoom * 100)}%</span>
          <button onClick={handleZoomIn} title="Zoom in"><ZoomIn size={14} /></button>
          <button onClick={handleZoomFit} title="Fit"><Maximize size={14} /></button>
        </div>
        <div className="canvas-undo">
          <button disabled={!canUndo} onClick={handleUndo} data-testid="undo-button"><Undo2 size={14} /></button>
          <button disabled={!canRedo} onClick={handleRedo} data-testid="redo-button"><Redo2 size={14} /></button>
        </div>
      </div>
      <div className="canvas-workspace">
        <LayerPanel
          layers={layers}
          selectedIds={selectedIds}
          onSelect={handleSelectLayer}
          onToggleVisible={handleToggleVisible}
          onToggleLock={handleToggleLock}
          onRename={handleRename}
          onDelete={handleDelete}
          onReorder={handleReorder}
        />
        <div className="canvas-stage" data-testid="canvas-stage">
          <div
            className="canvas-viewport"
            style={{
              width: canvasWidth * zoom,
              height: canvasHeight * zoom,
            }}
          >
            <canvas
              ref={canvasRef}
              width={canvasWidth}
              height={canvasHeight}
              style={{
                width: canvasWidth * zoom,
                height: canvasHeight * zoom,
                cursor: tool === 'select' ? 'default' : 'crosshair',
              }}
              onMouseDown={tool === 'select' ? handleMouseDown : handleCanvasClick}
              onMouseMove={handleMouseMove}
              onMouseUp={handleMouseUp}
              onMouseLeave={handleMouseUp}
              data-testid="design-canvas"
            />
            <svg
              ref={svgRef}
              width={canvasWidth}
              height={canvasHeight}
              viewBox={`0 0 ${canvasWidth} ${canvasHeight}`}
              style={{ position: 'absolute', left: '-9999px', top: '-9999px' }}
            >
              <rect width={canvasWidth} height={canvasHeight} fill="#f4f7fb" />
              {sortLayersByZIndex(layers)
                .filter((l) => l.visible)
                .map((l) => {
                  if (l.type === 'rectangle') {
                    return (
                      <rect
                        key={l.id}
                        x={l.x}
                        y={l.y}
                        width={l.width}
                        height={l.height}
                        fill={l.style.fill}
                        stroke={l.style.stroke}
                        strokeWidth={l.style.strokeWidth}
                        opacity={l.style.opacity}
                      />
                    )
                  }
                  if (l.type === 'circle') {
                    return (
                      <ellipse
                        key={l.id}
                        cx={l.x + l.width / 2}
                        cy={l.y + l.height / 2}
                        rx={l.width / 2}
                        ry={l.height / 2}
                        fill={l.style.fill}
                        stroke={l.style.stroke}
                        strokeWidth={l.style.strokeWidth}
                        opacity={l.style.opacity}
                      />
                    )
                  }
                  if (l.type === 'text') {
                    return (
                      <text
                        key={l.id}
                        x={l.x + 4}
                        y={l.y + (l.style.fontSize ?? 14)}
                        fill={l.style.color}
                        fontSize={l.style.fontSize}
                        fontFamily={l.style.fontFamily}
                        opacity={l.style.opacity}
                      >
                        {l.content}
                      </text>
                    )
                  }
                  if (l.type === 'image') {
                    return (
                      <rect
                        key={l.id}
                        x={l.x}
                        y={l.y}
                        width={l.width}
                        height={l.height}
                        fill="#f0f0f0"
                        stroke={l.style.stroke}
                        strokeWidth={l.style.strokeWidth}
                      />
                    )
                  }
                  return null
                })}
            </svg>
          </div>
        </div>
        <div className="canvas-sidebar-right">
          <ExportPanel
            projectName={projectName}
            selectedLayerName={selectedLayerName}
            canvasRef={canvasRef}
            svgRef={svgRef}
            layers={layers}
          />
        </div>
      </div>
    </div>
  )
}

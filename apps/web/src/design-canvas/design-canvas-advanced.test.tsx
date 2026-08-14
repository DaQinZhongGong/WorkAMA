import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import type { Layer } from './types'
import { LayerPanel } from './layer-panel'
import { AlignToolbar } from './align-toolbar'
import { ExportPanel } from './export-panel'
import { UndoManager } from './undo-manager'
import { alignLayers, generateId } from './utils'
import { CanvasEditor } from './canvas-editor'

afterEach(() => cleanup())

// Mock canvas API for jsdom
beforeEach(() => {
  const canvasProto = HTMLCanvasElement.prototype
  // Always override because jsdom has getContext but it throws "Not implemented"
  Object.defineProperty(canvasProto, 'getContext', {
    value: vi.fn((contextId: string) => {
      if (contextId === '2d') {
        return {
          clearRect: vi.fn(),
          fillRect: vi.fn(),
          strokeRect: vi.fn(),
          beginPath: vi.fn(),
          moveTo: vi.fn(),
          lineTo: vi.fn(),
          ellipse: vi.fn(),
          fill: vi.fn(),
          stroke: vi.fn(),
          save: vi.fn(),
          restore: vi.fn(),
          scale: vi.fn(),
          drawImage: vi.fn(),
          setLineDash: vi.fn(),
          fillText: vi.fn(),
          measureText: vi.fn(() => ({ width: 40 })),
          font: '',
          globalAlpha: 1,
          strokeStyle: '',
          fillStyle: '',
          lineWidth: 1,
          textBaseline: '',
          textAlign: '',
        } as unknown as CanvasRenderingContext2D
      }
      return null
    }),
    writable: true,
    configurable: true,
  })
  Object.defineProperty(canvasProto, 'toBlob', {
    value: vi.fn((callback: ((blob: Blob | null) => void)) => {
      callback(new Blob(['png'], { type: 'image/png' }))
    }),
    writable: true,
    configurable: true,
  })
})

function makeLayer(overrides: Partial<Layer> & { type: Layer['type'] }): Layer {
  return {
    ...overrides,
    id: generateId(),
    name: overrides.name ?? getDefaultLayerName(overrides.type),
    type: overrides.type,
    x: overrides.x ?? 0,
    y: overrides.y ?? 0,
    width: overrides.width ?? 100,
    height: overrides.height ?? 100,
    visible: overrides.visible ?? true,
    locked: overrides.locked ?? false,
    zIndex: overrides.zIndex ?? 0,
    style: overrides.style ?? { fill: '#dbe7f3', stroke: '#1d3557', strokeWidth: 1, opacity: 1 },
  }
}

function getDefaultLayerName(type: string): string {
  const names: Record<string, string> = {
    rectangle: 'Rectangle',
    circle: 'Circle',
    text: 'Text',
    image: 'Image',
    group: 'Group',
  }
  return names[type] ?? 'Layer'
}

describe('LayerPanel', () => {
  const layers: Layer[] = [
    makeLayer({ type: 'rectangle', name: 'Rect 1', zIndex: 0 }),
    makeLayer({ type: 'circle', name: 'Circle 1', zIndex: 1 }),
    makeLayer({ type: 'text', name: 'Text 1', zIndex: 2, visible: false, locked: true }),
  ]

  it('renders all layers sorted by z-index', () => {
    render(
      <LayerPanel
        layers={layers}
        selectedIds={[]}
        onSelect={() => {}}
        onToggleVisible={() => {}}
        onToggleLock={() => {}}
        onRename={() => {}}
        onDelete={() => {}}
        onReorder={() => {}}
      />,
    )
    expect(screen.getByTestId('layer-panel')).toBeInTheDocument()
    expect(screen.getByText('Rect 1')).toBeInTheDocument()
    expect(screen.getByText('Circle 1')).toBeInTheDocument()
    expect(screen.getByText('Text 1')).toBeInTheDocument()
  })

  it('selects a layer on click', () => {
    const onSelect = vi.fn()
    render(
      <LayerPanel
        layers={layers}
        selectedIds={[]}
        onSelect={onSelect}
        onToggleVisible={() => {}}
        onToggleLock={() => {}}
        onRename={() => {}}
        onDelete={() => {}}
        onReorder={() => {}}
      />,
    )
    fireEvent.click(screen.getByText('Rect 1'))
    expect(onSelect).toHaveBeenCalledTimes(1)
  })

  it('toggles visibility when eye icon is clicked', () => {
    const onToggleVisible = vi.fn()
    render(
      <LayerPanel
        layers={layers}
        selectedIds={[]}
        onSelect={() => {}}
        onToggleVisible={onToggleVisible}
        onToggleLock={() => {}}
        onRename={() => {}}
        onDelete={() => {}}
        onReorder={() => {}}
      />,
    )
    const visibleBtn = screen.getByTestId(`layer-visible-${layers[0].id}`)
    fireEvent.click(visibleBtn)
    expect(onToggleVisible).toHaveBeenCalledWith(layers[0].id)
  })

  it('toggles lock when lock icon is clicked', () => {
    const onToggleLock = vi.fn()
    render(
      <LayerPanel
        layers={layers}
        selectedIds={[]}
        onSelect={() => {}}
        onToggleVisible={() => {}}
        onToggleLock={onToggleLock}
        onRename={() => {}}
        onDelete={() => {}}
        onReorder={() => {}}
      />,
    )
    const lockBtn = screen.getByTestId(`layer-lock-${layers[1].id}`)
    fireEvent.click(lockBtn)
    expect(onToggleLock).toHaveBeenCalledWith(layers[1].id)
  })

  it('renames a layer on double click and blur', () => {
    const onRename = vi.fn()
    render(
      <LayerPanel
        layers={layers}
        selectedIds={[]}
        onSelect={() => {}}
        onToggleVisible={() => {}}
        onToggleLock={() => {}}
        onRename={onRename}
        onDelete={() => {}}
        onReorder={() => {}}
      />,
    )
    fireEvent.doubleClick(screen.getByText('Rect 1'))
    const input = screen.getByTestId(`layer-rename-input-${layers[0].id}`)
    fireEvent.change(input, { target: { value: 'New Name' } })
    fireEvent.blur(input)
    expect(onRename).toHaveBeenCalledWith(layers[0].id, 'New Name')
  })

  it('deletes a layer when trash icon is clicked', () => {
    const onDelete = vi.fn()
    render(
      <LayerPanel
        layers={layers}
        selectedIds={[]}
        onSelect={() => {}}
        onToggleVisible={() => {}}
        onToggleLock={() => {}}
        onRename={() => {}}
        onDelete={onDelete}
        onReorder={() => {}}
      />,
    )
    const deleteBtn = screen.getByTestId(`layer-delete-${layers[0].id}`)
    fireEvent.click(deleteBtn)
    expect(onDelete).toHaveBeenCalledWith(layers[0].id)
  })
})

describe('AlignToolbar', () => {
  it('disables all buttons when selectedCount < 2', () => {
    render(<AlignToolbar selectedCount={1} onAlign={() => {}} />)
    const buttons = screen.getAllByRole('button')
    expect(buttons.length).toBe(8)
    buttons.forEach((btn) => expect(btn).toBeDisabled())
  })

  it('enables all buttons when selectedCount >= 2', () => {
    render(<AlignToolbar selectedCount={2} onAlign={() => {}} />)
    const buttons = screen.getAllByRole('button')
    buttons.forEach((btn) => expect(btn).not.toBeDisabled())
  })

  it('fires align action on click', () => {
    const onAlign = vi.fn()
    render(<AlignToolbar selectedCount={2} onAlign={onAlign} />)
    fireEvent.click(screen.getByTestId('align-alignLeft'))
    expect(onAlign).toHaveBeenCalledWith('alignLeft')
  })
})

describe('alignLayers', () => {
  it('left aligns selected layers to the minimum left bound', () => {
    const layers: Layer[] = [
      makeLayer({ type: 'rectangle', x: 10, y: 0, width: 50, height: 50 }),
      makeLayer({ type: 'rectangle', x: 100, y: 0, width: 50, height: 50 }),
      makeLayer({ type: 'rectangle', x: 200, y: 0, width: 50, height: 50 }),
    ]
    const selectedIds = [layers[0].id, layers[1].id, layers[2].id]
    const result = alignLayers(layers, selectedIds, 'alignLeft')
    expect(result[0].x).toBe(10)
    expect(result[1].x).toBe(10)
    expect(result[2].x).toBe(10)
  })

  it('does nothing when fewer than 2 layers selected', () => {
    const layers: Layer[] = [
      makeLayer({ type: 'rectangle', x: 10, y: 0, width: 50, height: 50 }),
    ]
    const result = alignLayers(layers, [layers[0].id], 'alignLeft')
    expect(result[0].x).toBe(10)
  })
})

describe('ExportPanel', () => {
  it('triggers blob download on export click for PNG', async () => {
    const canvasRef = { current: document.createElement('canvas') }
    const svgRef = { current: document.createElementNS('http://www.w3.org/2000/svg', 'svg') }
    const URLCreateObjectURL = vi.fn(() => 'blob:mock-url')
    const URLRevokeObjectURL = vi.fn()
    vi.stubGlobal('URL', {
      createObjectURL: URLCreateObjectURL,
      revokeObjectURL: URLRevokeObjectURL,
    })

    render(
      <ExportPanel
        projectName="TestProject"
        canvasRef={canvasRef as unknown as React.RefObject<HTMLCanvasElement | null>}
        svgRef={svgRef as unknown as React.RefObject<SVGSVGElement | null>}
        layers={[makeLayer({ type: 'rectangle' })]}
      />,
    )

    fireEvent.click(screen.getByTestId('export-button'))
    await waitFor(() => expect(URLCreateObjectURL).toHaveBeenCalled())

    vi.unstubAllGlobals()
  })
})

describe('UndoManager', () => {
  it('supports execute, undo, redo sequence', () => {
    const um = new UndoManager()
    let state = 0
    const cmd = {
      execute: () => { state = 1 },
      undo: () => { state = 0 },
      snapshot: () => ({ type: 'test', timestamp: Date.now() }),
    }
    um.execute(cmd)
    expect(state).toBe(1)
    expect(um.canUndo()).toBe(true)
    expect(um.canRedo()).toBe(false)

    um.undo()
    expect(state).toBe(0)
    expect(um.canUndo()).toBe(false)
    expect(um.canRedo()).toBe(true)

    um.redo()
    expect(state).toBe(1)
    expect(um.canUndo()).toBe(true)
    expect(um.canRedo()).toBe(false)
  })

  it('drops future history on new execute after undo', () => {
    const um = new UndoManager()
    let state = ''
    const cmdA = {
      execute: () => { state = 'A' },
      undo: () => { state = '' },
      snapshot: () => ({ type: 'A', timestamp: Date.now() }),
    }
    const cmdB = {
      execute: () => { state = 'B' },
      undo: () => { state = 'A' },
      snapshot: () => ({ type: 'B', timestamp: Date.now() }),
    }
    um.execute(cmdA)
    um.execute(cmdB)
    um.undo()
    expect(state).toBe('A')

    const cmdC = {
      execute: () => { state = 'C' },
      undo: () => { state = 'A' },
      snapshot: () => ({ type: 'C', timestamp: Date.now() }),
    }
    um.execute(cmdC)
    expect(state).toBe('C')
    expect(um.canRedo()).toBe(false)
  })
})

describe('CanvasEditor', () => {
  it('renders canvas editor with initial layers', () => {
    render(
      <CanvasEditor
        projectName="Demo"
        canvasWidth={800}
        canvasHeight={600}
      />,
    )
    expect(screen.getByTestId('canvas-editor')).toBeInTheDocument()
    expect(screen.getByTestId('layer-panel')).toBeInTheDocument()
    expect(screen.getByTestId('design-canvas')).toBeInTheDocument()
    expect(screen.getByTestId('align-toolbar')).toBeInTheDocument()
    expect(screen.getByTestId('export-panel')).toBeInTheDocument()
  })

  it('adds a layer via tool click and canvas click, then undo/redo', () => {
    render(
      <CanvasEditor
        projectName="Demo"
        canvasWidth={800}
        canvasHeight={600}
      />,
    )
    const initialCount = screen.getAllByTestId(/^layer-item-/).length

    // Select rectangle tool
    fireEvent.click(screen.getByTestId('tool-rectangle'))
    // Click on canvas to add layer
    fireEvent.mouseDown(screen.getByTestId('design-canvas'))

    // Undo should remove the added layer
    const undoBtn = screen.getByTestId('undo-button')
    const redoBtn = screen.getByTestId('redo-button')

    // After adding, undo should be enabled
    expect(undoBtn).not.toBeDisabled()

    fireEvent.click(undoBtn)
    expect(screen.getAllByTestId(/^layer-item-/).length).toBe(initialCount)

    // Redo should restore
    expect(redoBtn).not.toBeDisabled()
    fireEvent.click(redoBtn)
    expect(screen.getAllByTestId(/^layer-item-/).length).toBe(initialCount + 1)
  })

  it('keyboard shortcuts trigger undo/redo', () => {
    render(
      <CanvasEditor
        projectName="Demo"
        canvasWidth={800}
        canvasHeight={600}
      />,
    )
    const editor = screen.getByTestId('canvas-editor')

    // Add a layer first
    fireEvent.click(screen.getByTestId('tool-rectangle'))
    fireEvent.mouseDown(screen.getByTestId('design-canvas'))

    const initialCount = screen.getAllByTestId(/^layer-item-/).length

    // Ctrl+Z undo
    fireEvent.keyDown(editor, { key: 'z', ctrlKey: true })
    expect(screen.getAllByTestId(/^layer-item-/).length).toBe(initialCount - 1)

    // Ctrl+Shift+Z redo
    fireEvent.keyDown(editor, { key: 'Z', ctrlKey: true, shiftKey: true })
    expect(screen.getAllByTestId(/^layer-item-/).length).toBe(initialCount)
  })

  it('Delete key removes selected layers', () => {
    render(
      <CanvasEditor
        projectName="Demo"
        canvasWidth={800}
        canvasHeight={600}
      />,
    )
    const items = screen.getAllByTestId(/^layer-item-/)
    const initialCount = items.length

    // Click first layer to select it
    fireEvent.click(within(items[0]).getByText(/Rectangle|Circle|Text/))

    const editor = screen.getByTestId('canvas-editor')
    fireEvent.keyDown(editor, { key: 'Delete' })

    expect(screen.getAllByTestId(/^layer-item-/).length).toBe(initialCount - 1)
  })
})

import { useState, useCallback, useRef } from 'react'
import {
  Eye,
  EyeOff,
  Lock,
  Unlock,
  Type,
  Square,
  Circle as CircleIcon,
  Image as ImageIcon,
  FolderOpen,
  Trash2,
  GripVertical,
  ChevronRight,
  ChevronDown,
} from 'lucide-react'
import type { Layer } from './types'
import { sortLayersByZIndex, getDefaultLayerName } from './utils'

interface LayerPanelProps {
  layers: Layer[]
  selectedIds: string[]
  onSelect: (id: string, multi?: boolean) => void
  onToggleVisible: (id: string) => void
  onToggleLock: (id: string) => void
  onRename: (id: string, name: string) => void
  onDelete: (id: string) => void
  onReorder: (dragId: string, targetId: string) => void
}

const typeIcons: Record<string, React.ReactNode> = {
  rectangle: <Square size={13} />,
  circle: <CircleIcon size={13} />,
  text: <Type size={13} />,
  image: <ImageIcon size={13} />,
  group: <FolderOpen size={13} />,
}

export function LayerPanel({
  layers,
  selectedIds,
  onSelect,
  onToggleVisible,
  onToggleLock,
  onRename,
  onDelete,
  onReorder,
}: LayerPanelProps) {
  const sorted = sortLayersByZIndex(layers)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set())
  const dragRef = useRef<string | null>(null)

  const startRename = useCallback((layer: Layer) => {
    setEditingId(layer.id)
    setEditName(layer.name || getDefaultLayerName(layer.type))
  }, [])

  const commitRename = useCallback(() => {
    if (editingId) {
      onRename(editingId, editName.trim() || getDefaultLayerName(layers.find((l) => l.id === editingId)?.type ?? 'layer'))
    }
    setEditingId(null)
  }, [editingId, editName, layers, onRename])

  const toggleGroup = useCallback((id: string) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const handleDragStart = useCallback((id: string) => {
    dragRef.current = id
  }, [])

  const handleDrop = useCallback(
    (targetId: string) => {
      const dragId = dragRef.current
      if (dragId && dragId !== targetId) {
        onReorder(dragId, targetId)
      }
      dragRef.current = null
    },
    [onReorder]
  )

  const renderLayerItem = (layer: Layer, depth = 0) => {
    const isSelected = selectedIds.includes(layer.id)
    const isGroup = layer.type === 'group'
    const isExpanded = expandedGroups.has(layer.id)
    const children = layers.filter((l) => l.parentId === layer.id)

    return (
      <div key={layer.id}>
        <div
          className={`layer-row ${isSelected ? 'selected' : ''}`}
          style={{ paddingLeft: `${10 + depth * 16}px` }}
          draggable
          onDragStart={() => handleDragStart(layer.id)}
          onDragOver={(e) => e.preventDefault()}
          onDrop={() => handleDrop(layer.id)}
          data-testid={`layer-item-${layer.id}`}
        >
          <span className="layer-drag-handle">
            <GripVertical size={12} />
          </span>
          {isGroup && (
            <button
              className="layer-expand"
              onClick={() => toggleGroup(layer.id)}
              data-testid={`layer-expand-${layer.id}`}
            >
              {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            </button>
          )}
          <span className="layer-icon">{typeIcons[layer.type]}</span>
          {editingId === layer.id ? (
            <input
              className="layer-name-input"
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              onBlur={commitRename}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitRename()
                if (e.key === 'Escape') setEditingId(null)
              }}
              autoFocus
              data-testid={`layer-rename-input-${layer.id}`}
            />
          ) : (
            <span
              className="layer-name"
              onClick={(e) => onSelect(layer.id, e.ctrlKey || e.metaKey)}
              onDoubleClick={() => startRename(layer)}
              data-testid={`layer-name-${layer.id}`}
            >
              {layer.name || getDefaultLayerName(layer.type)}
            </span>
          )}
          <span className="layer-actions">
            <button
              className="layer-action-btn"
              onClick={() => onToggleVisible(layer.id)}
              title={layer.visible ? 'Hide' : 'Show'}
              data-testid={`layer-visible-${layer.id}`}
            >
              {layer.visible ? <Eye size={12} /> : <EyeOff size={12} />}
            </button>
            <button
              className="layer-action-btn"
              onClick={() => onToggleLock(layer.id)}
              title={layer.locked ? 'Unlock' : 'Lock'}
              data-testid={`layer-lock-${layer.id}`}
            >
              {layer.locked ? <Lock size={12} /> : <Unlock size={12} />}
            </button>
            <button
              className="layer-action-btn"
              onClick={() => onDelete(layer.id)}
              title="Delete"
              data-testid={`layer-delete-${layer.id}`}
            >
              <Trash2 size={12} />
            </button>
          </span>
        </div>
        {isGroup && isExpanded && children.map((child) => renderLayerItem(child, depth + 1))}
      </div>
    )
  }

  return (
    <div className="layer-panel" data-testid="layer-panel">
      <div className="layer-panel-header">
        <strong>Layers</strong>
        <span>{layers.length}</span>
      </div>
      <div className="layer-list">
        {sorted
          .filter((l) => !l.parentId)
          .map((layer) => renderLayerItem(layer))}
        {layers.length === 0 && (
          <div className="layer-empty">No layers</div>
        )}
      </div>
    </div>
  )
}

import type { Layer, AlignAction } from './types'

export function sortLayersByZIndex(layers: Layer[]): Layer[] {
  return [...layers].sort((a, b) => a.zIndex - b.zIndex)
}

export function getLayerBounds(layer: Layer) {
  return {
    left: layer.x,
    top: layer.y,
    right: layer.x + layer.width,
    bottom: layer.y + layer.height,
    centerX: layer.x + layer.width / 2,
    centerY: layer.y + layer.height / 2,
  }
}

export function alignLayers(layers: Layer[], selectedIds: string[], action: AlignAction): Layer[] {
  const selected = layers.filter((l) => selectedIds.includes(l.id) && l.visible)
  if (selected.length < 2) return layers

  const bounds = selected.map(getLayerBounds)
  const minLeft = Math.min(...bounds.map((b) => b.left))
  const maxRight = Math.max(...bounds.map((b) => b.right))
  const minTop = Math.min(...bounds.map((b) => b.top))
  const maxBottom = Math.max(...bounds.map((b) => b.bottom))
  const avgCenterX = bounds.reduce((sum, b) => sum + b.centerX, 0) / bounds.length
  const avgCenterY = bounds.reduce((sum, b) => sum + b.centerY, 0) / bounds.length

  const updates = new Map<string, Partial<Layer>>()

  switch (action) {
    case 'alignLeft':
      selected.forEach((l) => updates.set(l.id, { x: minLeft }))
      break
    case 'alignCenterH':
      selected.forEach((l) => updates.set(l.id, { x: avgCenterX - l.width / 2 }))
      break
    case 'alignRight':
      selected.forEach((l) => updates.set(l.id, { x: maxRight - l.width }))
      break
    case 'alignTop':
      selected.forEach((l) => updates.set(l.id, { y: minTop }))
      break
    case 'alignCenterV':
      selected.forEach((l) => updates.set(l.id, { y: avgCenterY - l.height / 2 }))
      break
    case 'alignBottom':
      selected.forEach((l) => updates.set(l.id, { y: maxBottom - l.height }))
      break
    case 'distributeH': {
      const sorted = [...selected].sort((a, b) => a.x - b.x)
      const totalWidth = maxRight - minLeft
      const totalItemsWidth = sorted.reduce((sum, l) => sum + l.width, 0)
      const gap = (totalWidth - totalItemsWidth) / (sorted.length - 1)
      let currentX = minLeft
      sorted.forEach((l) => {
        updates.set(l.id, { x: currentX })
        currentX += l.width + gap
      })
      break
    }
    case 'distributeV': {
      const sorted = [...selected].sort((a, b) => a.y - b.y)
      const totalHeight = maxBottom - minTop
      const totalItemsHeight = sorted.reduce((sum, l) => sum + l.height, 0)
      const gap = (totalHeight - totalItemsHeight) / (sorted.length - 1)
      let currentY = minTop
      sorted.forEach((l) => {
        updates.set(l.id, { y: currentY })
        currentY += l.height + gap
      })
      break
    }
  }

  return layers.map((l) => {
    const update = updates.get(l.id)
    return update ? { ...l, ...update } : l
  })
}

export function generateId(prefix = 'layer'): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 9)}_${Date.now().toString(36).slice(-4)}`
}

export function getDefaultLayerName(type: string): string {
  const names: Record<string, string> = {
    rectangle: 'Rectangle',
    circle: 'Circle',
    text: 'Text',
    image: 'Image',
    group: 'Group',
  }
  return names[type] ?? 'Layer'
}

export function exportCanvasToBlob(
  canvasEl: HTMLCanvasElement | null,
  format: 'png' | 'jpeg',
  scale: number
): Promise<Blob> {
  return new Promise((resolve, reject) => {
    if (!canvasEl) return reject(new Error('Canvas not found'))
    const scaled = document.createElement('canvas')
    scaled.width = canvasEl.width * scale
    scaled.height = canvasEl.height * scale
    const ctx = scaled.getContext('2d')
    if (!ctx) return reject(new Error('Context not available'))
    ctx.scale(scale, scale)
    ctx.drawImage(canvasEl, 0, 0)
    scaled.toBlob(
      (blob) => {
        if (blob) resolve(blob)
        else reject(new Error('Blob creation failed'))
      },
      format === 'png' ? 'image/png' : 'image/jpeg',
      0.92
    )
  })
}

export function serializeSvgFromElement(svgEl: SVGSVGElement | null): string {
  if (!svgEl) return ''
  const clone = svgEl.cloneNode(true) as SVGSVGElement
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  const serializer = new XMLSerializer()
  return serializer.serializeToString(clone)
}

export function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

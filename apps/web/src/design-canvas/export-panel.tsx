import { useState } from 'react'
import { Download } from 'lucide-react'
import type { Layer } from './types'
import { exportCanvasToBlob, serializeSvgFromElement, downloadBlob } from './utils'

interface ExportPanelProps {
  projectName: string
  selectedLayerName?: string
  canvasRef: React.RefObject<HTMLCanvasElement | null>
  svgRef: React.RefObject<SVGSVGElement | null>
  layers: Layer[]
}

export function ExportPanel({ projectName, selectedLayerName, canvasRef, svgRef, layers }: ExportPanelProps) {
  const [format, setFormat] = useState<'png' | 'jpeg' | 'svg'>('png')
  const [scale, setScale] = useState<1 | 2 | 3>(1)
  const [exporting, setExporting] = useState(false)

  const handleExport = async () => {
    setExporting(true)
    try {
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
      const nameBase = [projectName, selectedLayerName || 'canvas'].filter(Boolean).join('_')
      const filename = `${nameBase}_${timestamp}.${format}`

      if (format === 'svg') {
        const svgString = serializeSvgFromElement(svgRef.current)
        const blob = new Blob([svgString], { type: 'image/svg+xml;charset=utf-8' })
        downloadBlob(blob, filename)
      } else {
        const blob = await exportCanvasToBlob(canvasRef.current, format, scale)
        downloadBlob(blob, filename)
      }
    } finally {
      setExporting(false)
    }
  }

  const hasVisible = layers.some((l) => l.visible)

  return (
    <div className="export-panel" data-testid="export-panel">
      <strong>Export</strong>
      <div className="export-field">
        <label>Format</label>
        <select value={format} onChange={(e) => setFormat(e.target.value as 'png' | 'jpeg' | 'svg')} data-testid="export-format">
          <option value="png">PNG</option>
          <option value="jpeg">JPEG</option>
          <option value="svg">SVG</option>
        </select>
      </div>
      {format !== 'svg' && (
        <div className="export-field">
          <label>Scale</label>
          <select value={scale} onChange={(e) => setScale(Number(e.target.value) as 1 | 2 | 3)} data-testid="export-scale">
            <option value={1}>1x</option>
            <option value={2}>2x</option>
            <option value={3}>3x</option>
          </select>
        </div>
      )}
      <button
        className="button button-primary export-btn"
        disabled={exporting || !hasVisible}
        onClick={handleExport}
        data-testid="export-button"
      >
        <Download size={14} />
        {exporting ? 'Exporting…' : 'Export'}
      </button>
    </div>
  )
}

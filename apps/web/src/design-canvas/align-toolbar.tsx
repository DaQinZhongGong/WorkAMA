import {
  AlignLeft,
  AlignCenter,
  AlignRight,
  AlignVerticalJustifyStart,
  AlignVerticalJustifyCenter,
  AlignVerticalJustifyEnd,
  ArrowLeftRight,
  ArrowUpDown,
} from 'lucide-react'
import type { AlignAction } from './types'

interface AlignToolbarProps {
  selectedCount: number
  onAlign: (action: AlignAction) => void
}

const buttons: { action: AlignAction; icon: React.ReactNode; title: string }[] = [
  { action: 'alignLeft', icon: <AlignLeft size={14} />, title: 'Align Left' },
  { action: 'alignCenterH', icon: <AlignCenter size={14} />, title: 'Align Center Horizontally' },
  { action: 'alignRight', icon: <AlignRight size={14} />, title: 'Align Right' },
  { action: 'distributeH', icon: <ArrowLeftRight size={14} />, title: 'Distribute Horizontally' },
  { action: 'alignTop', icon: <AlignVerticalJustifyStart size={14} />, title: 'Align Top' },
  { action: 'alignCenterV', icon: <AlignVerticalJustifyCenter size={14} />, title: 'Align Center Vertically' },
  { action: 'alignBottom', icon: <AlignVerticalJustifyEnd size={14} />, title: 'Align Bottom' },
  { action: 'distributeV', icon: <ArrowUpDown size={14} />, title: 'Distribute Vertically' },
]

export function AlignToolbar({ selectedCount, onAlign }: AlignToolbarProps) {
  const disabled = selectedCount < 2
  return (
    <div className="align-toolbar" data-testid="align-toolbar">
      {buttons.map((btn) => (
        <button
          key={btn.action}
          className="align-btn"
          disabled={disabled}
          title={btn.title}
          onClick={() => onAlign(btn.action)}
          data-testid={`align-${btn.action}`}
        >
          {btn.icon}
        </button>
      ))}
    </div>
  )
}

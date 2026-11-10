import { statusLabel, statusTone } from './lib'

export default function StatusChip({ status }: { status: string }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${statusTone(status)}`}>
      {statusLabel(status)}
    </span>
  )
}

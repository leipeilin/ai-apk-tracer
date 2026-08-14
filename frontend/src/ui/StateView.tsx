import { WarningCircle } from '@phosphor-icons/react'
import type { ReactNode } from 'react'
import { Button } from './Button'

export function SkeletonRows({ count = 4 }: { count?: number }) {
  return (
    <div className="skeleton-list" aria-label="正在加载" aria-busy="true">
      {Array.from({ length: count }).map((_, index) => (
        <div className="skeleton-row" key={index}>
          <span className="skeleton-block skeleton-icon" />
          <span className="skeleton-lines">
            <span className="skeleton-block w-3/5" />
            <span className="skeleton-block w-2/5" />
          </span>
        </div>
      ))}
    </div>
  )
}

export function EmptyState({ icon, title, description, action }: { icon: ReactNode; title: string; description: string; action?: ReactNode }) {
  return (
    <div className="state-view">
      <div className="state-icon">{icon}</div>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  )
}

export function ErrorState({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div className="state-view state-error" role="alert">
      <div className="state-icon"><WarningCircle size={25} /></div>
      <h3>数据暂时不可用</h3>
      <p>{error.message}</p>
      {onRetry && <Button onClick={onRetry}>重新加载</Button>}
    </div>
  )
}

import { X } from '@phosphor-icons/react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { useEffect, type ReactNode } from 'react'
import { Button } from './Button'

/** 提供可通过遮罩、关闭按钮和 Escape 退出的无障碍侧边详情容器。 */
export function Drawer({ open, onClose, title, eyebrow, children }: { open: boolean; onClose: () => void; title: string; eyebrow?: string; children: ReactNode }) {
  const reduceMotion = useReducedMotion()
  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKeyDown)
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = ''
    }
  }, [open, onClose])

  return (
    <AnimatePresence>
      {open && (
        <div className="drawer-root" role="dialog" aria-modal="true" aria-label={title}>
          <motion.button
            className="drawer-backdrop"
            aria-label="关闭详情"
            onClick={onClose}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          />
          <motion.aside
            className="drawer-panel"
            initial={reduceMotion ? { opacity: 0 } : { x: '100%' }}
            animate={reduceMotion ? { opacity: 1 } : { x: 0 }}
            exit={reduceMotion ? { opacity: 0 } : { x: '100%' }}
            transition={{ type: 'spring', stiffness: 260, damping: 30 }}
          >
            <header className="drawer-header">
              <div>
                {eyebrow && <span className="eyebrow">{eyebrow}</span>}
                <h2>{title}</h2>
              </div>
              <Button variant="ghost" className="icon-button" onClick={onClose} aria-label="关闭"><X size={19} /></Button>
            </header>
            <div className="drawer-content">{children}</div>
          </motion.aside>
        </div>
      )}
    </AnimatePresence>
  )
}

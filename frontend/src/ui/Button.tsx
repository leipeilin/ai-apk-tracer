import { CircleNotch } from '@phosphor-icons/react'
import type { ButtonHTMLAttributes, ReactNode } from 'react'

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  loading?: boolean
  icon?: ReactNode
}

export function Button({ variant = 'secondary', loading, icon, children, className = '', disabled, ...props }: ButtonProps) {
  return (
    <button
      className={`button button-${variant} ${className}`}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? <CircleNotch className="animate-spin" size={17} aria-hidden /> : icon}
      {children}
    </button>
  )
}

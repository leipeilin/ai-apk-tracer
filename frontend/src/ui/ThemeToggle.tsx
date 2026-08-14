import { Desktop, Moon, Sun } from '@phosphor-icons/react'
import { useEffect, useState } from 'react'

type Theme = 'light' | 'dark' | 'system'
const options: Array<{ value: Theme; label: string; icon: typeof Sun }> = [
  { value: 'light', label: '浅色', icon: Sun },
  { value: 'dark', label: '深色', icon: Moon },
  { value: 'system', label: '跟随系统', icon: Desktop },
]

function applyTheme(theme: Theme) {
  const dark = theme === 'dark' || (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light'
  const meta = document.querySelector('meta[name="theme-color"]')
  meta?.setAttribute('content', dark ? '#191815' : '#f3f1ec')
}

/** 在浅色、深色和跟随系统之间切换，并持久化用户偏好。 */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => (localStorage.getItem('apk-tracer-theme') as Theme) || 'system')

  useEffect(() => {
    applyTheme(theme)
    localStorage.setItem('apk-tracer-theme', theme)
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const update = () => theme === 'system' && applyTheme(theme)
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [theme])

  return (
    <div className="theme-switch" aria-label="外观主题">
      {options.map(({ value, label, icon: Icon }) => (
        <button
          key={value}
          className={theme === value ? 'active' : ''}
          onClick={() => setTheme(value)}
          aria-label={label}
          aria-pressed={theme === value}
          title={label}
        >
          <Icon size={15} weight={theme === value ? 'fill' : 'regular'} />
        </button>
      ))}
    </div>
  )
}
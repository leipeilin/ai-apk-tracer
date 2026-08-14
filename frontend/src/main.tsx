import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { HashRouter } from 'react-router-dom'
import { App } from './app/App'
import './styles.css'

const savedTheme = localStorage.getItem('apk-tracer-theme') || 'system'
const dark = savedTheme === 'dark' || (savedTheme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
document.documentElement.dataset.theme = dark ? 'dark' : 'light'
document.documentElement.style.colorScheme = dark ? 'dark' : 'light'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <HashRouter>
      <App />
    </HashRouter>
  </StrictMode>,
)
